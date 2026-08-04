"""利润计算引擎 — 订单利润核算、财务报表生成"""
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import create_engine, func, and_
from sqlalchemy.orm import Session

from config import config
from models import Order, OrderStatus, Platform, ProfitReport, engine


class ProfitCalculator:
    """利润计算器"""

    def calculate_order_profit(self, order: Order) -> tuple[float, float]:
        """
        计算单笔订单利润。
        公式: 利润 = 实付金额 - 商品成本 - 平台佣金 - 物流成本 - 其他成本
        返回 (利润, 利润率)
        """
        revenue = order.actual_amount
        costs = order.cost_of_goods + order.platform_fee + order.logistics_cost + order.other_cost
        profit = revenue - costs
        margin = profit / revenue if revenue > 0 else 0.0
        return round(profit, 2), round(margin, 4)

    def update_order_profit(self, order_id: int) -> Optional[Order]:
        """更新单笔订单利润"""
        with Session(engine) as session:
            order = session.get(Order, order_id)
            if not order:
                return None
            profit, margin = self.calculate_order_profit(order)
            order.profit = profit
            order.profit_margin = margin
            session.commit()
            return order

    def batch_update_profits(self, order_ids: Optional[list[int]] = None) -> int:
        """批量更新利润"""
        with Session(engine) as session:
            query = session.query(Order)
            if order_ids:
                query = query.filter(Order.id.in_(order_ids))
            orders = query.all()
            for order in orders:
                profit, margin = self.calculate_order_profit(order)
                order.profit = profit
                order.profit_margin = margin
            session.commit()
            return len(orders)

    def generate_profit_report(
        self,
        period: str = "daily",
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        platform: Optional[str] = None,
    ) -> ProfitReport:
        """生成利润报表"""
        if end_date is None:
            end_date = datetime.utcnow()
        if start_date is None:
            if period == "daily":
                start_date = end_date - timedelta(days=1)
            elif period == "weekly":
                start_date = end_date - timedelta(days=7)
            else:
                start_date = end_date - timedelta(days=30)

        with Session(engine) as session:
            query = session.query(Order).filter(
                and_(
                    Order.created_at >= start_date,
                    Order.created_at <= end_date,
                    Order.status.in_([OrderStatus.COMPLETED, OrderStatus.DELIVERED]),
                )
            )
            if platform and platform != "all":
                query = query.filter(Order.platform == platform)

            orders = query.all()

            total_revenue = sum(o.actual_amount for o in orders)
            total_cost = sum(
                o.cost_of_goods + o.platform_fee + o.logistics_cost + o.other_cost
                for o in orders
            )
            total_profit = sum(o.profit for o in orders)
            avg_margin = sum(o.profit_margin for o in orders) / len(orders) if orders else 0.0
            return_count = sum(1 for o in orders if o.status == OrderStatus.RETURNED)
            cancel_count = sum(1 for o in orders if o.status == OrderStatus.CANCELLED)

            report = ProfitReport(
                period=period,
                period_start=start_date,
                period_end=end_date,
                platform=platform or "all",
                total_orders=len(orders),
                total_revenue=round(total_revenue, 2),
                total_cost=round(total_cost, 2),
                total_profit=round(total_profit, 2),
                avg_profit_margin=round(avg_margin, 4),
                return_count=return_count,
                cancellation_count=cancel_count,
            )
            session.add(report)
            session.commit()
            return report

    def get_summary(self, days: int = 7) -> dict:
        """获取最近N天的经营摘要"""
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=days)

        with Session(engine) as session:
            orders = session.query(Order).filter(
                Order.created_at >= start_date
            ).all()

            total = len(orders)
            completed = sum(1 for o in orders if o.status == OrderStatus.COMPLETED)
            revenue = sum(o.actual_amount for o in orders if o.status in [OrderStatus.COMPLETED, OrderStatus.DELIVERED])
            profit = sum(o.profit for o in orders if o.status in [OrderStatus.COMPLETED, OrderStatus.DELIVERED])
            returns = sum(1 for o in orders if o.status == OrderStatus.RETURNED)

            # 按平台分组
            by_platform = {}
            for o in orders:
                p = o.platform.value if hasattr(o.platform, 'value') else str(o.platform)
                if p not in by_platform:
                    by_platform[p] = {"count": 0, "revenue": 0.0}
                by_platform[p]["count"] += 1
                by_platform[p]["revenue"] += o.actual_amount

            return {
                "period_days": days,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "total_orders": total,
                "completed_orders": completed,
                "total_revenue": round(revenue, 2),
                "total_profit": round(profit, 2),
                "return_count": returns,
                "by_platform": by_platform,
            }


calculator = ProfitCalculator()
