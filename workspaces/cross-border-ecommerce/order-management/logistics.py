"""物流跟踪模块 — 对接物流API，查询和更新物流状态"""
import json
from datetime import datetime
from typing import Optional

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from config import config
from models import LogisticsRecord, Order, engine


class LogisticsTracker:
    """物流跟踪器"""

    def __init__(self):
        self.provider = config.logistics.provider
        self.api_key = config.logistics.api_key
        self.base_url = config.logistics.base_url
        self.client = httpx.Client(timeout=15)

    def query_tracking(self, tracking_number: str, carrier: str = "auto") -> dict:
        """查询物流单号最新状态"""
        if self.provider == "mock":
            return self._mock_query(tracking_number)

        resp = self.client.post(
            f"{self.base_url}/trackings/realtime",
            json={"tracking_number": tracking_number, "carrier_code": carrier},
            headers={"Tracking-Api-Key": self.api_key},
        )
        resp.raise_for_status()
        return resp.json()

    def _mock_query(self, tracking_number: str) -> dict:
        """Mock物流查询"""
        return {
            "tracking_number": tracking_number,
            "status": "in_transit",
            "status_detail": "包裹运输中",
            "checkpoints": [
                {"time": "2026-07-27T10:00:00", "location": "深圳集散中心", "message": "已揽件"},
                {"time": "2026-07-27T18:00:00", "location": "深圳转运中心", "message": "已发出"},
                {"time": "2026-07-28T08:00:00", "location": "台北转运中心", "message": "到达目的地"},
            ],
            "estimated_delivery": "2026-07-30",
        }

    def update_order_logistics(self, order_id: int) -> Optional[LogisticsRecord]:
        """更新指定订单的所有物流记录"""
        with Session(engine) as session:
            records = session.query(LogisticsRecord).filter_by(order_id=order_id).all()
            for record in records:
                result = self.query_tracking(record.tracking_number, record.carrier)
                record.status = result.get("status", record.status)
                record.status_detail = result.get("status_detail", "")
                record.checkpoints = json.dumps(result.get("checkpoints", []), ensure_ascii=False)
                record.updated_at = datetime.utcnow()
                if result.get("estimated_delivery"):
                    record.estimated_delivery = datetime.fromisoformat(result["estimated_delivery"])
            session.commit()
            return records[0] if records else None

    def add_tracking(
        self,
        order_id: int,
        tracking_number: str,
        carrier: str = "auto",
    ) -> LogisticsRecord:
        """为订单添加物流记录"""
        with Session(engine) as session:
            record = LogisticsRecord(
                order_id=order_id,
                tracking_number=tracking_number,
                carrier=carrier,
            )
            session.add(record)
            session.commit()
            session.refresh(record)

            # 立即查询一次
            self.update_order_logistics(order_id)
            return record

    def batch_update_all_active(self) -> int:
        """批量更新所有在途订单的物流状态"""
        with Session(engine) as session:
            active_orders = session.query(Order).filter(
                Order.status.in_([OrderStatus.SHIPPED])
            ).all()
            count = 0
            for order in active_orders:
                self.update_order_logistics(order.id)
                count += 1
            return count


tracker = LogisticsTracker()
