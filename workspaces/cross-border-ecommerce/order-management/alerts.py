"""异常监控告警模块 — 检测退货/差评/纠纷/超时/低利润"""
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from config import config
from models import Order, OrderStatus, Alert, AlertType, engine


class AlertMonitor:
    """异常监控器"""

    def __init__(self):
        self.alert_cfg = config.alert

    def check_all(self) -> list[Alert]:
        """执行全部检查，返回新生成的告警列表"""
        alerts = []
        alerts.extend(self.check_overdue_ship())
        alerts.extend(self.check_low_profit())
        alerts.extend(self.check_returns())
        return alerts

    def check_overdue_ship(self) -> list[Alert]:
        """检查超时未发货"""
        threshold = datetime.utcnow() - timedelta(hours=self.alert_cfg.overdue_hours)
        with Session(engine) as session:
            overdue_orders = session.query(Order).filter(
                Order.status == OrderStatus.PAID,
                Order.paid_at <= threshold,
            ).all()

            new_alerts = []
            for order in overdue_orders:
                # 避免重复告警
                existing = session.query(Alert).filter_by(
                    order_id=order.id,
                    alert_type=AlertType.OVERDUE_SHIP,
                    is_resolved=False,
                ).first()
                if existing:
                    continue

                alert = Alert(
                    order_id=order.id,
                    alert_type=AlertType.OVERDUE_SHIP,
                    severity="warning",
                    message=f"订单 {order.platform_order_id} 已超过{self.alert_cfg.overdue_hours}小时未发货",
                    detail=f"付款时间: {order.paid_at}, 金额: {order.actual_amount} {order.currency}",
                )
                session.add(alert)
                new_alerts.append(alert)
            session.commit()
            return new_alerts

    def check_low_profit(self) -> list[Alert]:
        """检查低利润订单"""
        with Session(engine) as session:
            low_profit_orders = session.query(Order).filter(
                Order.profit_margin < self.alert_cfg.profit_margin_min,
                Order.status.in_([OrderStatus.COMPLETED, OrderStatus.DELIVERED]),
                Order.profit_margin >= 0,  # 排除未计算的
            ).all()

            new_alerts = []
            for order in low_profit_orders:
                existing = session.query(Alert).filter_by(
                    order_id=order.id,
                    alert_type=AlertType.LOW_PROFIT,
                    is_resolved=False,
                ).first()
                if existing:
                    continue

                alert = Alert(
                    order_id=order.id,
                    alert_type=AlertType.LOW_PROFIT,
                    severity="warning",
                    message=f"订单 {order.platform_order_id} 利润率 {order.profit_margin:.2%} 低于阈值",
                    detail=f"收入: {order.actual_amount}, 成本: {order.cost_of_goods}, 利润: {order.profit}",
                )
                session.add(alert)
                new_alerts.append(alert)
            session.commit()
            return new_alerts

    def check_returns(self) -> list[Alert]:
        """检查退货订单"""
        with Session(engine) as session:
            returned_orders = session.query(Order).filter(
                Order.status == OrderStatus.RETURNED,
            ).all()

            new_alerts = []
            for order in returned_orders:
                existing = session.query(Alert).filter_by(
                    order_id=order.id,
                    alert_type=AlertType.RETURN,
                    is_resolved=False,
                ).first()
                if existing:
                    continue

                alert = Alert(
                    order_id=order.id,
                    alert_type=AlertType.RETURN,
                    severity="critical",
                    message=f"订单 {order.platform_order_id} 已退货",
                    detail=f"金额: {order.actual_amount} {order.currency}, 买家: {order.buyer_name}",
                )
                session.add(alert)
                new_alerts.append(alert)
            session.commit()
            return new_alerts

    def check_negative_reviews(self, order_id: int, rating: float, comment: str = "") -> Optional[Alert]:
        """收到差评时调用（对接客服系统后使用）"""
        if rating > self.alert_cfg.negative_review_threshold:
            return None

        with Session(engine) as session:
            existing = session.query(Alert).filter_by(
                order_id=order_id,
                alert_type=AlertType.NEGATIVE_REVIEW,
                is_resolved=False,
            ).first()
            if existing:
                return existing

            alert = Alert(
                order_id=order_id,
                alert_type=AlertType.NEGATIVE_REVIEW,
                severity="warning" if rating >= 2 else "critical",
                message=f"订单收到 {rating} 星差评",
                detail=comment or "",
            )
            session.add(alert)
            session.commit()
            return alert

    def resolve_alert(self, alert_id: int, note: str = "") -> Optional[Alert]:
        """解决告警"""
        with Session(engine) as session:
            alert = session.get(Alert, alert_id)
            if alert:
                alert.is_resolved = True
                alert.resolved_at = datetime.utcnow()
                alert.resolved_note = note
                session.commit()
            return alert

    def get_active_alerts(self, severity: Optional[str] = None) -> list[Alert]:
        """获取未解决的告警"""
        with Session(engine) as session:
            query = session.query(Alert).filter_by(is_resolved=False)
            if severity:
                query = query.filter_by(severity=severity)
            return query.order_by(Alert.created_at.desc()).all()


monitor = AlertMonitor()
