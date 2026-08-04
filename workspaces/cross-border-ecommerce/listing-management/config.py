"""全局配置管理"""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


class Config:
    # --- AI ---
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
    CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "")
    CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

    # --- 平台 API ---
    SHOPEE_PARTNER_ID = os.getenv("SHOPEE_PARTNER_ID", "")
    SHOPEE_PARTNER_KEY = os.getenv("SHOPEE_PARTNER_KEY", "")
    SHOPEE_API_BASE = os.getenv("SHOPEE_API_BASE", "https://partner.shopeemobile.com")
    SHOPEE_SHOP_ID = os.getenv("SHOPEE_SHOP_ID", "")

    LAZADA_APP_KEY = os.getenv("LAZADA_APP_KEY", "")
    LAZADA_APP_SECRET = os.getenv("LAZADA_APP_SECRET", "")
    LAZADA_API_BASE = os.getenv("LAZADA_API_BASE", "https://api.lazada.com/rest")

    TIKTOK_APP_KEY = os.getenv("TIKTOK_APP_KEY", "")
    TIKTOK_APP_SECRET = os.getenv("TIKTOK_APP_SECRET", "")
    TIKTOK_API_BASE = os.getenv("TIKTOK_API_BASE", "https://open-api.tiktokglobalshop.com")

    # --- 图片 ---
    IMAGE_OUTPUT_DIR = BASE_DIR / "output" / "images"
    TEMPLATE_DIR = BASE_DIR / "templates" / "images"

    # --- 输出 ---
    OUTPUT_DIR = BASE_DIR / "output"
    LOG_DIR = BASE_DIR / "logs"


config = Config()

# 确保目录存在
for d in [config.IMAGE_OUTPUT_DIR, config.TEMPLATE_DIR, config.OUTPUT_DIR, config.LOG_DIR]:
    d.mkdir(parents=True, exist_ok=True)
