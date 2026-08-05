"""关键词趋势分析

支持：
- 热搜关键词排行
- 关键词趋势变化（上升/下降）
- 长尾关键词挖掘
- 跨站点关键词对比
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional
import pandas as pd

from utils import date_range


@dataclass
class KeywordItem:
    """单个关键词数据"""
    keyword: str
    search_volume: int = 0
    trend: str = "stable"  # rising, stable, falling
    competition_level: str = "medium"  # low, medium, high
    related_keywords: list[str] = field(default_factory=list)
    category: str = ""
    platform: str = "shopee_tw"
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())


class KeywordAnalyzer:
    """关键词趋势分析器"""

    def __init__(self, platform: str = "shopee_tw"):
        self.platform = platform
        self._cache: dict[str, KeywordItem] = {}

    def analyze(self, keyword: str, days: int = 30) -> KeywordItem:
        """分析单个关键词的趋势"""
        start, end = date_range(days)
        # TODO: 对接实际数据源（Shopee API / 第三方数据平台）
        item = KeywordItem(
            keyword=keyword,
            search_volume=self._estimate_volume(keyword),
            trend=self._detect_trend(keyword, days),
            competition_level=self._assess_competition(keyword),
            related_keywords=self._find_related(keyword),
        )
        self._cache[keyword] = item
        return item

    def hot_keywords(self, category: str = "", limit: int = 50) -> list[KeywordItem]:
        """获取当前热搜关键词"""
        # TODO: 对接热搜 API
        return []

    def compare(self, keywords: list[str], days: int = 30) -> pd.DataFrame:
        """对比多个关键词的趋势"""
        items = [self.analyze(kw, days) for kw in keywords]
        rows = []
        for item in items:
            d = asdict(item)
            d.pop("related_keywords", None)
            rows.append(d)
        return pd.DataFrame(rows)

    def find_long_tail(self, seed_keyword: str, max_results: int = 20) -> list[str]:
        """基于种子词挖掘长尾关键词"""
        # TODO: 利用搜索建议 / 下拉框 API 获取
        return []

    def _estimate_volume(self, keyword: str) -> int:
        """估算搜索量（占位）"""
        return 0

    def _detect_trend(self, keyword: str, days: int) -> str:
        """检测趋势方向（占位）"""
        return "stable"

    def _assess_competition(self, keyword: str) -> str:
        """评估竞争程度（占位）"""
        return "medium"

    def _find_related(self, keyword: str) -> list[str]:
        """查找相关关键词（占位）"""
        return []

    def to_report(self, keyword: str) -> dict:
        """生成单个关键词的报告"""
        item = self.analyze(keyword)
        return {
            "keyword": item.keyword,
            "search_volume": item.search_volume,
            "trend": item.trend,
            "competition": item.competition_level,
            "related": item.related_keywords[:10],
        }
