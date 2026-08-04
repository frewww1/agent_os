"""订单管理模块配置"""
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DatabaseConfig:
    url: str = os.getenv("ORDER_DB_URL", "sqlite:///./orders.db")
    echo: bool = False


@dataclass
class PlatformConfig:
    """各平台API配置"""
    shopee: dict = field(default_factory=lambda: {
        "base_url": "https://partner.shopeemobile.com/api/v2",
        "partner_id": int(os.getenv("SHOPEE_PARTNER_ID", "0")),
        "partner_key": os.getenv("SHOPEE_PARTNER_KEY", ""),
        "shop_id": int(os.getenv("SHOPEE_SHOP_ID", "0")),
    })
    lazada: dict = field(default_factory=lambda: {
        "base_url": "https://api.lazada.com/rest",
        "app_key": os.getenv("LAZADA_APP_KEY", ""),
        "app_secret": os.getenv("LAZADA_APP_SECRET", ""),
    })
    shopify: dict = field(default_factory=lambda: {
        "base_url": "",
        "api_key": os.getenv("SHOPIFY_API_KEY", ""),
        "api_secret": os.getenv("SHOPIFY_API_SECRET", ""),
    })


@dataclass
class LogisticsConfig:
    """物流API配置"""
    provider: str = os.getenv("LOGISTICS_PROVIDER", "mock")  # mock / track17 / afterShip
    api_key: str = os.getenv("LOGISTICS_API_KEY", "")
    base_url: str = os.getenv("LOGISTICS_BASE_URL", "")


@dataclass
class AlertConfig:
    """异常告警配置"""
    negative_review_threshold: float = 3.0       # 差评星级阈值
    return_rate_threshold: float = 0.05           # 退货率告警阈值
    overdue_hours: int = 48                        # 超时未发货告警（小时）
    profit_margin_min: float = 0.05                # 最低利润率阈值
    check_interval_minutes: int = 30               # 检查间隔（分钟）


@dataclass
class Config:
    db: DatabaseConfig = field(default_factory=DatabaseConfig)
    platforms: PlatformConfig = field(default_factory=PlatformConfig)
    logistics: LogisticsConfig = field(default_factory=LogisticsConfig)
    alert: AlertConfig = field(default_factory=AlertConfig)


config = Config()
