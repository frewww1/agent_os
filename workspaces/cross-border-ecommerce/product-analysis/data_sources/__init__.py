"""数据源适配层

统一接口，支持多种数据源：
- Shopee API
- 第三方数据平台（知虾、电霸等）
- 爬虫数据
"""
from .base import DataSource


__all__ = ["DataSource"]
