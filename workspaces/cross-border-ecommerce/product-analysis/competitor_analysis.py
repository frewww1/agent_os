"""竞品分析模块

支持：
- 竞品价格监控（历史价格变化）
- 销量估算（基于评价数/库存变化）
- 竞品店铺对比
- 市场集中度分析
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional
import pandas as pd

from utils import safe_float, safe_int, date_range


@dataclass
class ProductSnapshot:
    """商品快照"""
    product_id: str
    title: str
    price: float
    original_price: float = 0.0
    sales_estimate: int = 0  # 估算月销量
    rating: float = 0.0
    review_count: int = 0
    shop_name: str = ""
    platform: str = "shopee_tw"
    category: str = ""
    snapshot_time: str = field(default_factory=lambda: datetime.now().isoformat())


class CompetitorMonitor:
    """竞品监控器"""

    def __init__(self, platform: str = "shopee_tw"):
        self.platform = platform
        self._history: dict[str, list[ProductSnapshot]] = {}

    def track_product(self, product_id: str) -> ProductSnapshot:
        """获取单品当前数据并记录历史"""
        snapshot = self._fetch_product(product_id)
        if product_id not in self._history:
            self._history[product_id] = []
        self._history[product_id].append(snapshot)
        return snapshot

    def price_history(self, product_id: str) -> pd.DataFrame:
        """获取商品历史价格"""
        if product_id not in self._history:
            return pd.DataFrame()
        rows = [asdict(s) for s in self._history[product_id]]
        df = pd.DataFrame(rows)
        df["snapshot_time"] = pd.to_datetime(df["snapshot_time"])
        return df.sort_values("snapshot_time")

    def price_change(self, product_id: str) -> dict:
        """分析价格变化"""
        df = self.price_history(product_id)
        if df.empty or len(df) < 2:
            return {"changed": False, "reason": "insufficient data"}

        first = df.iloc[0]
        last = df.iloc[-1]
        change_pct = (last["price"] - first["price"]) / first["price"] * 100
        return {
            "product_id": product_id,
            "first_price": first["price"],
            "last_price": last["price"],
            "change_pct": round(change_pct, 2),
            "min_price": df["price"].min(),
            "max_price": df["price"].max(),
            "snapshots": len(df),
        }

    def search_competitors(self, keyword: str, limit: int = 20) -> list[ProductSnapshot]:
        """按关键词搜索竞品"""
        # TODO: 对接搜索 API
        return []

    def market_overview(self, keyword: str) -> dict:
        """市场概况（价格分布、卖家集中度等）"""
        competitors = self.search_competitors(keyword, limit=50)
        if not competitors:
            return {"keyword": keyword, "total_products": 0}

        prices = [p.price for p in competitors]
        sales = [p.sales_estimate for p in competitors]
        shops = {}
        for p in competitors:
            shops[p.shop_name] = shops.get(p.shop_name, 0) + 1

        return {
            "keyword": keyword,
            "total_products": len(competitors),
            "avg_price": round(sum(prices) / len(prices), 2),
            "min_price": min(prices),
            "max_price": max(prices),
            "median_price": round(sorted(prices)[len(prices) // 2], 2),
            "total_estimated_sales": sum(sales),
            "unique_shops": len(shops),
            "top_shop_share": round(max(shops.values()) / len(competitors) * 100, 1) if shops else 0,
        }

    def _fetch_product(self, product_id: str) -> ProductSnapshot:
        """获取商品数据（占位，需对接实际数据源）"""
        return ProductSnapshot(
            product_id=product_id,
            title="placeholder",
            price=0.0,
        )
