"""公共工具函数"""
import json
import time
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


def timestamp_to_date(ts: int) -> str:
    """时间戳转日期字符串"""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def date_range(days: int) -> tuple[str, str]:
    """返回 (N天前, 今天) 日期范围"""
    end = datetime.now()
    start = end - timedelta(days=days)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def load_json(path: str | Path) -> dict:
    """加载 JSON 文件"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(data: dict | list, path: str | Path) -> None:
    """保存 JSON 文件"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def md5_hash(text: str) -> str:
    """生成 MD5 hash"""
    return hashlib.md5(text.encode()).hexdigest()


def safe_float(value, default=0.0) -> float:
    """安全转为浮点数"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0) -> int:
    """安全转为整数"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class RateLimiter:
    """简易请求频率限制器"""

    def __init__(self, max_calls: int = 10, period: float = 1.0):
        self.max_calls = max_calls
        self.period = period
        self.calls: list[float] = []

    def wait(self) -> None:
        now = time.time()
        self.calls = [t for t in self.calls if t > now - self.period]
        if len(self.calls) >= self.max_calls:
            sleep_time = self.calls[0] + self.period - now
            if sleep_time > 0:
                time.sleep(sleep_time)
        self.calls.append(time.time())


# 台湾市场常量
TAIWAN_CURRENCY = "TWD"
TAIWAN_VAT_RATE = 0.05  # 营业税
PLATFORM_FEE_RATE = 0.06  # Shopee 平台佣金（假设）
PAYMENT_FEE_RATE = 0.02  # 金流手续费（假设）
