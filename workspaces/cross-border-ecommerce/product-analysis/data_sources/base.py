"""数据源抽象基类"""
from abc import ABC, abstractmethod
from typing import Optional


class DataSource(ABC):
    """数据源统一接口"""

    def __init__(self, platform: str = "shopee_tw"):
        self.platform = platform

    @abstractmethod
    def search_products(self, keyword: str, limit: int = 50) -> list[dict]:
        """搜索商品"""
        ...

    @abstractmethod
    def get_product_detail(self, product_id: str) -> dict:
        """获取商品详情"""
        ...

    @abstractmethod
    def get_keyword_volume(self, keyword: str) -> int:
        """获取关键词搜索量"""
        ...

    @abstractmethod
    def get_category_trend(self, category_id: str, days: int = 30) -> dict:
        """获取品类趋势"""
        ...

    def health_check(self) -> bool:
        """检查数据源是否可用"""
        return True
