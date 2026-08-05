"""多平台订单同步器 — 对接 Shopee/Lazada/Shopify 订单API"""
import hashlib
import hmac
import json
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

import httpx

from config import config
from models import Order, OrderStatus, Platform, init_db
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


engine = create_engine(config.db.url, echo=False)


class BasePlatformSync(ABC):
    """平台同步基类"""

    def __init__(self, platform: Platform):
        self.platform = platform
        self.client = httpx.Client(timeout=30)

    @abstractmethod
    def fetch_orders(self, start_time: datetime, end_time: Optional[datetime] = None) -> list[dict]:
        """从平台拉取订单列表"""
        ...

    @abstractmethod
    def fetch_order_detail(self, platform_order_id: str) -> dict:
        """拉取订单详情"""
        ...

    @abstractmethod
    def parse_order(self, raw: dict) -> dict:
        """将平台原始数据转为统一格式"""
        ...

    def sync(self, start_time: datetime, end_time: Optional[datetime] = None) -> int:
        """执行同步，返回同步订单数"""
        raw_orders = self.fetch_orders(start_time, end_time)
        count = 0
        with Session(engine) as session:
            for raw in raw_orders:
                detail = self.fetch_order_detail(raw.get("order_id", ""))
                parsed = self.parse_order(detail)
                count += self._upsert_order(session, parsed)
            session.commit()
        return count

    def _upsert_order(self, session: Session, data: dict) -> int:
        """插入或更新订单"""
        existing = session.query(Order).filter_by(
            platform_order_id=data["platform_order_id"]
        ).first()
        if existing:
            for k, v in data.items():
                if k != "items":
                    setattr(existing, k, v)
            return 0
        else:
            order = Order(**{k: v for k, v in data.items() if k != "items"})
            session.add(order)
            return 1


class ShopeeSync(BasePlatformSync):
    """Shopee 订单同步"""

    def __init__(self):
        super().__init__(Platform.SHOPEE)
        self.base_url = config.platforms.shopee["base_url"]
        self.partner_id = config.platforms.shopee["partner_id"]
        self.partner_key = config.platforms.shopee["partner_key"]
        self.shop_id = config.platforms.shopee["shop_id"]

    def _sign(self, path: str, timestamp: int) -> str:
        """Shopee API 签名"""
        base = f"{self.partner_id}{path}{timestamp}"
        return hmac.new(
            self.partner_key.encode(), base.encode(), hashlib.sha256
        ).hexdigest()

    def _request(self, path: str, params: dict) -> dict:
        timestamp = int(time.time())
        params["partner_id"] = self.partner_id
        params["shopid"] = self.shop_id
        params["timestamp"] = timestamp
        params["sign"] = self._sign(path, timestamp)
        resp = self.client.post(f"{self.base_url}{path}", json=params)
        resp.raise_for_status()
        return resp.json()

    def fetch_orders(self, start_time: datetime, end_time: Optional[datetime] = None) -> list[dict]:
        path = "/order/get_order_list"
        params = {
            "time_range_field": "create_time",
            "time_from": int(start_time.timestamp()),
            "time_to": int((end_time or datetime.utcnow()).timestamp()),
            "page_size": 100,
        }
        result = self._request(path, params)
        return result.get("response", {}).get("order_list", [])

    def fetch_order_detail(self, platform_order_id: str) -> dict:
        path = "/order/get_order_detail"
        result = self._request(path, {"order_sn_list": platform_order_id})
        orders = result.get("response", {}).get("order_list", [])
        return orders[0] if orders else {}

    def parse_order(self, raw: dict) -> dict:
        status_map = {
            "UNPAID": OrderStatus.UNPAID,
            "READY_TO_SHIP": OrderStatus.PAID,
            "PROCESSED": OrderStatus.PROCESSING,
            "SHIPPED": OrderStatus.SHIPPED,
            "COMPLETED": OrderStatus.COMPLETED,
            "CANCELLED": OrderStatus.CANCELLED,
        }
        return {
            "platform_order_id": raw.get("order_sn", ""),
            "platform": Platform.SHOPEE,
            "shop_name": raw.get("shop_name", ""),
            "status": status_map.get(raw.get("order_status", ""), OrderStatus.UNPAID),
            "buyer_name": raw.get("recipient_address", {}).get("name", ""),
            "buyer_phone": raw.get("recipient_address", {}).get("phone", ""),
            "buyer_address": json.dumps(raw.get("recipient_address", {})),
            "currency": raw.get("currency", "TWD"),
            "total_amount": float(raw.get("total_amount", 0)),
            "shipping_fee": float(raw.get("shipping_carrier_price", 0)),
            "actual_amount": float(raw.get("actual_amount", 0)),
        }


class LazadaSync(BasePlatformSync):
    """Lazada 订单同步"""

    def __init__(self):
        super().__init__(Platform.LAZADA)
        self.base_url = config.platforms.lazada["base_url"]
        self.app_key = config.platforms.lazada["app_key"]
        self.app_secret = config.platforms.lazada["app_secret"]

    def _sign(self, params: dict) -> str:
        """Lazada API 签名"""
        sorted_params = sorted(params.items())
        sign_str = self.app_secret + "".join(f"{k}{v}" for k, v in sorted_params)
        return hashlib.sha256(sign_str.encode()).hexdigest().upper()

    def _request(self, path: str, params: dict) -> dict:
        params["app_key"] = self.app_key
        params["sign_method"] = "sha256"
        params["timestamp"] = str(int(time.time() * 1000))
        params["sign"] = self._sign(params)
        resp = self.client.get(f"{self.base_url}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    def fetch_orders(self, start_time: datetime, end_time: Optional[datetime] = None) -> list[dict]:
        path = "/orders/get"
        params = {
            "created_after": start_time.isoformat(),
            "created_before": (end_time or datetime.utcnow()).isoformat(),
            "limit": 100,
        }
        result = self._request(path, params)
        return result.get("data", {}).get("orders", [])

    def fetch_order_detail(self, platform_order_id: str) -> dict:
        path = "/order/get"
        result = self._request(path, {"order_id": platform_order_id})
        return result.get("data", {})

    def parse_order(self, raw: dict) -> dict:
        status_map = {
            "unpaid": OrderStatus.UNPAID,
            "pending": OrderStatus.PAID,
            "ready_to_ship": OrderStatus.PROCESSING,
            "shipped": OrderStatus.SHIPPED,
            "delivered": OrderStatus.DELIVERED,
            "canceled": OrderStatus.CANCELLED,
        }
        addr = raw.get("address_shipping", {})
        return {
            "platform_order_id": str(raw.get("order_id", "")),
            "platform": Platform.LAZADA,
            "shop_name": raw.get("seller_nick", ""),
            "status": status_map.get(raw.get("statuses", [""])[-1] if raw.get("statuses") else "", OrderStatus.UNPAID),
            "buyer_name": f"{addr.get('first_name', '')} {addr.get('last_name', '')}",
            "buyer_phone": addr.get("phone", ""),
            "buyer_address": json.dumps(addr),
            "currency": raw.get("currency", "PHP"),
            "total_amount": float(raw.get("price", 0)),
            "shipping_fee": float(raw.get("shipping_fee", 0)),
            "actual_amount": float(raw.get("price", 0)),
        }


class MockSync(BasePlatformSync):
    """Mock同步器 — 用于开发和测试"""

    def __init__(self):
        super().__init__(Platform.MANUAL)

    def fetch_orders(self, start_time: datetime, end_time: Optional[datetime] = None) -> list[dict]:
        return [
            {"order_id": "MOCK-20260728-001"},
            {"order_id": "MOCK-20260728-002"},
        ]

    def fetch_order_detail(self, platform_order_id: str) -> dict:
        return {
            "order_sn": platform_order_id,
            "order_status": "READY_TO_SHIP",
            "shop_name": "MockShop",
            "currency": "TWD",
            "total_amount": 500.0,
            "shipping_carrier_price": 60.0,
            "actual_amount": 450.0,
            "recipient_address": {
                "name": "Test Buyer",
                "phone": "0912345678",
                "address": "Test Address, Taipei",
            },
        }

    def parse_order(self, raw: dict) -> dict:
        return ShopeeSync().parse_order(raw)


def get_syncer(platform: Platform) -> BasePlatformSync:
    """工厂函数：根据平台返回对应的同步器"""
    syncers = {
        Platform.SHOPEE: ShopeeSync,
        Platform.LAZADA: LazadaSync,
        Platform.MANUAL: MockSync,
    }
    return syncers.get(platform, MockSync)()
