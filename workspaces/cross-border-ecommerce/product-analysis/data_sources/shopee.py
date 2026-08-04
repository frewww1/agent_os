"""Shopee 数据源适配器

对接 Shopee API / 爬虫获取数据。
"""
from .base import DataSource


class ShopeeDataSource(DataSource):
    """Shopee 数据源"""

    BASE_URL = "https://shopee.tw"

    def __init__(self, platform: str = "shopee_tw"):
        super().__init__(platform)

    def search_products(self, keyword: str, limit: int = 50) -> list[dict]:
        """搜索商品"""
        # TODO: 对接 Shopee 搜索 API 或爬虫
        return []

    def get_product_detail(self, product_id: str) -> dict:
        """获取商品详情"""
        # TODO: 对接商品详情 API
        return {}

    def get_keyword_volume(self, keyword: str) -> int:
        """获取关键词搜索量"""
        # TODO: 通过第三方平台获取搜索量数据
        return 0

    def get_category_trend(self, category_id: str, days: int = 30) -> dict:
        """获取品类趋势"""
        # TODO: 获取品类级别数据
        return {}

    def get_shop_info(self, shop_id: str) -> dict:
        """获取店铺信息"""
        # TODO: 获取店铺数据
        return {}
