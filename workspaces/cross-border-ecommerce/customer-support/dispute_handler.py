"""差评 / 纠纷自动处理流程

处理链路：
1. 监控差评事件 → 自动分析原因
2. 根据规则分级处理（自动回复 / 升级人工）
3. 退款/退货审批流程
4. 处理后跟踪（评价修改、客户挽回）
"""

from __future__ import annotations

import logging
from datetime import datetime
from enum import Enum
from typing import Optional

from shared.models import (
    Customer,
    CustomerTier,
    Language,
    Order,
    Platform,
    Review,
    Sentiment,
    Ticket,
    TicketCategory,
    TicketPriority,
    TicketStatus,
)
from shared.event_bus import Event, EventBus, EventType

logger = logging.getLogger(__name__)


class ResolutionAction(str, Enum):
    """纠纷处理动作"""
    AUTO_REPLY = "auto_reply"           # 自动回复安抚
    OFFER_COUPON = "offer_coupon"       # 发放优惠券
    PARTIAL_REFUND = "partial_refund"   # 部分退款
    FULL_REFUND = "full_refund"         # 全额退款
    RESEND = "resend"                   # 补发
    ESCALATE = "escalate"              # 升级人工
    IGNORE = "ignore"                   # 不处理（恶意评价等）


class ResolutionRule:
    """纠纷处理规则"""

    def __init__(
        self,
        name: str,
        condition: dict,
        action: ResolutionAction,
        priority: int = 0,
    ):
        self.name = name
        self.condition = condition
        self.action = action
        self.priority = priority

    def matches(self, review: Review, customer: Customer, order: Order | None) -> bool:
        """检查是否匹配规则"""
        c = self.condition

        if "min_rating" in c and review.rating > c["min_rating"]:
            return False
        if "max_rating" in c and review.rating < c["max_rating"]:
            return False
        if "keywords" in c:
            if not any(kw.lower() in review.content.lower() for kw in c["keywords"]):
                return False
        if "customer_tier" in c and customer.tier.value not in c["customer_tier"]:
            return False
        if "min_order_amount" in c and order and order.total_amount < c["min_order_amount"]:
            return False
        if "sentiment" in c and review.sentiment.value not in c["sentiment"]:
            return False
        return True


class DisputeHandler:
    """纠纷自动处理器"""

    # 默认规则集
    DEFAULT_RULES = [
        ResolutionRule(
            name="严重差评+VIP客户",
            condition={"max_rating": 2, "customer_tier": ["vip"], "sentiment": ["negative"]},
            action=ResolutionAction.FULL_REFUND,
            priority=10,
        ),
        ResolutionRule(
            name="严重差评+普通客户",
            condition={"max_rating": 2, "sentiment": ["negative"]},
            action=ResolutionAction.PARTIAL_REFUND,
            priority=8,
        ),
        ResolutionRule(
            name="中等差评",
            condition={"max_rating": 3, "min_rating": 2, "sentiment": ["negative"]},
            action=ResolutionAction.AUTO_REPLY,
            priority=5,
        ),
        ResolutionRule(
            name="轻微不满",
            condition={"max_rating": 3, "min_rating": 3, "sentiment": ["negative"]},
            action=ResolutionAction.OFFER_COUPON,
            priority=3,
        ),
        ResolutionRule(
            name="关键词-破损",
            condition={"keywords": ["破损", "损坏", "坏了", "broken", "damaged", "rusak"]},
            action=ResolutionAction.RESEND,
            priority=7,
        ),
        ResolutionRule(
            name="关键词-诈骗投诉",
            condition={"keywords": ["诈骗", "假货", "scam", "fake", "penipuan"]},
            action=ResolutionAction.ESCALATE,
            priority=9,
        ),
    ]

    def __init__(self, rules: list[ResolutionRule] | None = None, event_bus: EventBus | None = None):
        self.rules = rules or self.DEFAULT_RULES
        self.event_bus = event_bus

    async def handle_negative_review(
        self,
        review: Review,
        customer: Customer,
        order: Order | None = None,
    ) -> ResolutionAction:
        """处理一条差评，返回采取的动作"""
        # 按优先级排序规则
        sorted_rules = sorted(self.rules, key=lambda r: r.priority, reverse=True)

        for rule in sorted_rules:
            if rule.matches(review, customer, order):
                logger.info(
                    f"差评处理: review={review.id} "
                    f"rating={review.rating} "
                    f"rule={rule.name} "
                    f"action={rule.action.value}"
                )
                await self._execute_action(rule.action, review, customer, order)
                return rule.action

        # 默认：自动回复
        logger.info(f"差评处理: review={review.id} 无规则匹配，默认自动回复")
        return ResolutionAction.AUTO_REPLY

    async def _execute_action(
        self,
        action: ResolutionAction,
        review: Review,
        customer: Customer,
        order: Order | None = None,
    ) -> None:
        """执行处理动作"""
        if action == ResolutionAction.ESCALATE and self.event_bus:
            await self.event_bus.publish(Event(
                type=EventType.CUSTOMER_ESCALATED,
                source="customer-support",
                payload={
                    "review_id": review.id,
                    "customer_id": customer.id,
                    "rating": review.rating,
                    "content": review.content,
                    "action": "manual_review_required",
                },
            ))

        if action in (ResolutionAction.FULL_REFUND, ResolutionAction.PARTIAL_REFUND) and self.event_bus:
            await self.event_bus.publish(Event(
                type=EventType.REFUND_REQUESTED,
                source="customer-support",
                payload={
                    "review_id": review.id,
                    "order_id": review.order_id,
                    "refund_type": action.value,
                    "reason": f"差评自动处理: rating={review.rating}",
                },
            ))

    def generate_reply_template(
        self,
        review: Review,
        action: ResolutionAction,
        language: Language = Language.ZH_TW,
    ) -> str:
        """生成差评回复模板"""
        templates = {
            Language.ZH_TW: {
                ResolutionAction.AUTO_REPLY: (
                    "親愛的顧客您好，感謝您的反饋。我們非常重視您的購物體驗，"
                    "已將您的意見轉達給相關部門，我們會持續改進。如有任何問題，歡迎隨時聯繫我們！"
                ),
                ResolutionAction.OFFER_COUPON: (
                    "親愛的顧客您好，很抱歉這次的購物體驗未能達到您的期待。"
                    "我們已為您準備了一張優惠券，希望下次能帶給您更好的體驗！"
                ),
                ResolutionAction.PARTIAL_REFUND: (
                    "親愛的顧客您好，很抱歉造成您的困擾。我們已為您申請部分退款，"
                    "預計 3-5 個工作日內到帳。如有其他問題，請隨時聯繫我們。"
                ),
                ResolutionAction.FULL_REFUND: (
                    "親愛的顧客您好，非常抱歉這次的購物體驗讓您失望。"
                    "我們已為您申請全額退款，預計 3-5 個工作日內到帳。感謝您的理解！"
                ),
                ResolutionAction.RESEND: (
                    "親愛的顧客您好，很抱歉您收到的商品有問題。"
                    "我們將為您補發一件新的商品，請耐心等候。如有問題歡迎聯繫我們！"
                ),
            },
            Language.EN: {
                ResolutionAction.AUTO_REPLY: (
                    "Dear customer, thank you for your feedback. We take your experience seriously "
                    "and have forwarded your comments to our team. Please feel free to contact us anytime!"
                ),
                ResolutionAction.OFFER_COUPON: (
                    "Dear customer, we're sorry this purchase didn't meet your expectations. "
                    "We've prepared a coupon for your next order. Hope to serve you better next time!"
                ),
                ResolutionAction.FULL_REFUND: (
                    "Dear customer, we sincerely apologize for this experience. "
                    "A full refund has been processed and should arrive within 3-5 business days. Thank you for your understanding!"
                ),
                ResolutionAction.RESEND: (
                    "Dear customer, we're sorry about the issue with your item. "
                    "A replacement has been arranged. Please allow some time for delivery. Thank you!"
                ),
            },
        }
        lang_templates = templates.get(language, templates[Language.ZH_TW])
        return lang_templates.get(action, lang_templates[ResolutionAction.AUTO_REPLY])
