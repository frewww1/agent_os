"""AI 客服对话引擎

混合架构：
1. 规则引擎先匹配 FAQ 知识库（快速、确定性）
2. 命中失败则降级到 LLM 生成（灵活、覆盖长尾）
3. 所有对话经过多语言翻译层
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from shared.models import (
    Customer,
    KnowledgeEntry,
    Language,
    Message,
    Order,
    Platform,
    Ticket,
    TicketCategory,
    TicketPriority,
    TicketStatus,
)
from shared.event_bus import Event, EventBus, EventType

logger = logging.getLogger(__name__)


class KnowledgeBase:
    """FAQ 知识库 —— 基于向量相似度匹配"""

    def __init__(self):
        self._entries: list[KnowledgeEntry] = []
        self._embedder = None   # lazy init: sentence-transformers

    def add(self, entry: KnowledgeEntry) -> None:
        self._entries.append(entry)

    def remove(self, entry_id: str) -> None:
        self._entries = [e for e in self._entries if e.id != entry_id]

    async def search(self, query: str, language: Language, top_k: int = 3) -> list[tuple[KnowledgeEntry, float]]:
        """语义搜索，返回 (条目, 相似度) 列表"""
        if not self._entries:
            return []

        # 简易实现：关键词匹配
        # 生产环境应替换为 embedding + FAISS
        results = []
        query_lower = query.lower()
        for entry in self._entries:
            # 检查语言适配
            q_text = entry.question_translations.get(language.value, entry.question)
            a_text = entry.answer_translations.get(language.value, entry.answer)
            combined = (q_text + " " + a_text).lower()

            # 简单关键词重叠度
            query_words = set(query_lower.split())
            entry_words = set(combined.split())
            if not query_words:
                continue
            overlap = len(query_words & entry_words) / len(query_words)
            if overlap > 0.3:
                results.append((entry, overlap))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    async def search_semantic(self, query: str, threshold: float = 0.75) -> list[tuple[KnowledgeEntry, float]]:
        """基于 embedding 的语义搜索（生产环境启用）"""
        if self._embedder is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._embedder = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
            except ImportError:
                logger.warning("sentence-transformers 未安装，回退到关键词匹配")
                return []

        # TODO: 构建 FAISS 索引 + embedding 搜索
        return []


class AIAgent:
    """LLM 对话 Agent —— 当知识库无法匹配时调用"""

    def __init__(self, api_key: str = "", model: str = "gpt-4o-mini", base_url: str = ""):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

    async def generate_reply(
        self,
        customer_message: str,
        language: Language,
        context: dict | None = None,
    ) -> str:
        """生成客服回复"""
        import openai

        client = openai.AsyncOpenAI(
            api_key=self.api_key or "sk-placeholder",
            base_url=self.base_url or None,
        )

        system_prompt = self._build_system_prompt(language, context)
        try:
            response = await client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": customer_message},
                ],
                temperature=0.3,
                max_tokens=512,
            )
            return response.choices[0].message.content or ""
        except Exception:
            logger.exception("LLM 调用失败")
            return self._fallback_reply(language)

    def _build_system_prompt(self, language: Language, context: dict | None) -> str:
        lang_instruction = {
            Language.ZH_TW: "请用繁体中文回复，语气亲切专业。",
            Language.ZH_CN: "请用简体中文回复，语气亲切专业。",
            Language.EN: "Reply in English, friendly and professional tone.",
            Language.ID: "Balas dalam Bahasa Indonesia, nada ramah dan profesional.",
            Language.TH: "ตอบเป็นภาษาไทย ด้วยน้ำเสียงเป็นมิตรและเป็นมืออาชีพ",
            Language.VI: "Trả lời bằng tiếng Việt, giọng điệu thân thiện và chuyên nghiệp.",
        }
        base = f"""你是一个跨境电商客服助手。{lang_instruction.get(language, '')}

规则：
1. 回复简洁，不超过 150 字
2. 不确定时请客户等待人工客服
3. 不要承诺无法兑现的事情（如确切退款时间）
4. 涉及投诉/法律问题时，请客户联系人工客服"""
        if context:
            base += f"\n\n当前上下文：{context}"
        return base

    def _fallback_reply(self, language: Language) -> str:
        fallbacks = {
            Language.ZH_TW: "很抱歉，我目前無法處理您的問題，已為您轉接人工客服，請稍候。",
            Language.EN: "Sorry, I'm unable to process your request right now. I've forwarded it to our support team. Please wait a moment.",
            Language.ID: "Maaf, saya tidak dapat memproses permintaan Anda saat ini. Telah diteruskan ke tim dukungan kami. Mohon tunggu sebentar.",
        }
        return fallbacks.get(language, fallbacks[Language.ZH_TW])


class ChatEngine:
    """客服对话引擎 —— 协调知识库 + LLM + 翻译"""

    def __init__(
        self,
        knowledge_base: KnowledgeBase | None = None,
        ai_agent: AIAgent | None = None,
        event_bus: EventBus | None = None,
    ):
        self.kb = knowledge_base or KnowledgeBase()
        self.ai = ai_agent or AIAgent()
        self.event_bus = event_bus

    async def process_message(
        self,
        ticket: Ticket,
        customer: Customer,
        content: str,
    ) -> Message:
        """处理一条客户消息，返回 AI 回复"""
        # 1. 先查知识库
        matches = await self.kb.search(content, ticket.language, top_k=3)
        if matches:
            best_entry, score = matches[0]
            if score > 0.6:
                reply_text = best_entry.answer_translations.get(
                    ticket.language.value, best_entry.answer
                )
                logger.info(f"FAQ 命中: {best_entry.question[:50]} (score={score:.2f})")
                return self._build_reply(ticket, reply_text, is_faq=True)

        # 2. 知识库未命中，调 LLM
        context = {
            "customer_name": customer.username,
            "customer_tier": customer.tier.value,
            "order_id": ticket.order_id,
            "ticket_category": ticket.category.value,
        }
        reply_text = await self.ai.generate_reply(content, ticket.language, context)
        return self._build_reply(ticket, reply_text, is_faq=False)

    def _build_reply(self, ticket: Ticket, text: str, is_faq: bool = False) -> Message:
        return Message(
            ticket_id=ticket.id,
            sender="ai",
            content=text,
            language=ticket.language,
            created_at=datetime.now(),
        )

    async def escalate_to_human(self, ticket: Ticket, reason: str) -> None:
        """升级到人工客服"""
        ticket.assigned_to = "human"
        ticket.priority = TicketPriority.HIGH
        ticket.status = TicketStatus.OPEN
        logger.warning(f"工单 {ticket.id} 升级人工: {reason}")

        if self.event_bus:
            await self.event_bus.publish(Event(
                type=EventType.CUSTOMER_ESCALATED,
                source="customer-support",
                payload={"ticket_id": ticket.id, "reason": reason},
            ))
