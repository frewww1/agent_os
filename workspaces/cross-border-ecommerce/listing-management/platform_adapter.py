"""平台适配器 - 对接各电商平台 API"""
from __future__ import annotations
import hashlib
import hmac
import time
import json
from abc import ABC, abstractmethod
from typing import Optional

import requests
from loguru import logger

from .config import config
from .models import (
    Platform,
    PlatformListing,
    ListingStatus,
    ListingContent,
    Language,
    ProductInfo,
)


class BasePlatformAdapter(ABC):
    """平台适配器基类"""

    platform: Platform

    @abstractmethod
    def _sign(self, params: dict) -> dict:
        """签名请求"""
        ...

    @abstractmethod
    def upload_image(self, image_path: str) -> str:
        """上传图片，返回图片 URL"""
        ...

    @abstractmethod
    def create_listing(self, listing: PlatformListing) -> PlatformListing:
        """创建/上架商品"""
        ...

    @abstractmethod
    def update_listing(self, listing: PlatformListing) -> PlatformListing:
        """更新商品信息"""
        ...

    @abstractmethod
    def update_stock(self, platform_id: str, stock: int) -> bool:
        """更新库存"""
        ...

    @abstractmethod
    def get_categories(self) -> list[dict]:
        """获取平台类目树"""
        ...

    def _post(self, url: str, payload: dict, headers: dict | None = None) -> dict:
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"API error [{self.platform.value}]: {e}")
            raise


class ShopeeAdapter(BasePlatformAdapter):
    """Shopee API 适配器"""

    platform = Platform.SHOPEE

    def __init__(self):
        self.api_base = config.SHOPEE_API_BASE
        self.partner_id = config.SHOPEE_PARTNER_ID
        self.partner_key = config.SHOPEE_PARTNER_KEY
        self.shop_id = config.SHOPEE_SHOP_ID

    def _sign(self, params: dict) -> dict:
        """Shopee 签名算法"""
        params["partner_id"] = self.partner_id
        params["timestamp"] = int(time.time())
        # 排序拼接
        sorted_keys = sorted(params.keys())
        sign_str = self.partner_key + "".join(
            f"{k}{params[k]}" for k in sorted_keys
        )
        sign = hashlib.sha256(sign_str.encode()).hexdigest()
        params["sign"] = sign
        return params

    def upload_image(self, image_path: str) -> str:
        # Shopee 图片上传需要 multipart
        url = f"{self.api_base}/api/v2/media_space/upload_image"
        # 实际实现需要 multipart/form-data
        logger.info(f"[Shopee] Upload image: {image_path}")
        return ""

    def create_listing(self, listing: PlatformListing) -> PlatformListing:
        """调用 Shopee AddItem API"""
        url = f"{self.api_base}/api/v2/product/add_item"
        payload = self._build_item_payload(listing)
        payload = self._sign(payload)
        try:
            data = self._post(url, payload)
            listing.platform_id = str(data.get("item_id", ""))
            listing.status = ListingStatus.PUBLISHED
            logger.info(f"[Shopee] Created listing {listing.platform_id}")
        except Exception as e:
            listing.status = ListingStatus.FAILED
            listing.error_message = str(e)
        return listing

    def update_listing(self, listing: PlatformListing) -> PlatformListing:
        url = f"{self.api_base}/api/v2/product/update_item"
        payload = self._build_item_payload(listing)
        payload["item_id"] = int(listing.platform_id)
        payload = self._sign(payload)
        try:
            self._post(url, payload)
            listing.status = ListingStatus.PUBLISHED
        except Exception as e:
            listing.status = ListingStatus.FAILED
            listing.error_message = str(e)
        return listing

    def update_stock(self, platform_id: str, stock: int) -> bool:
        url = f"{self.api_base}/api/v2/product/update_stock"
        payload = self._sign({
            "item_id": int(platform_id),
            "stock": stock,
            "shop_id": self.shop_id,
        })
        try:
            self._post(url, payload)
            return True
        except Exception:
            return False

    def get_categories(self) -> list[dict]:
        url = f"{self.api_base}/api/v2/product/get_category"
        payload = self._sign({"language": "zh-hant"})
        try:
            return self._post(url, payload).get("category_list", [])
        except Exception:
            return []

    def _build_item_payload(self, listing: PlatformListing) -> dict:
        """构建 Shopee item 请求体"""
        zh = listing.contents.get(Language.ZH_TW)
        en = listing.contents.get(Language.EN)
        return {
            "shop_id": self.shop_id,
            "name": zh.title if zh else listing.product_info.title,
            "description": zh.description if zh else "",
            "price": listing.price,
            "stock": listing.stock,
            "category_id": listing.product_info.category,
            "images": [img.url for img in listing.images],
        }


class LazadaAdapter(BasePlatformAdapter):
    """Lazada API 适配器（骨架实现）"""

    platform = Platform.LAZADA

    def __init__(self):
        self.api_base = config.LAZADA_API_BASE
        self.app_key = config.LAZADA_APP_KEY
        self.app_secret = config.LAZADA_APP_SECRET

    def _sign(self, params: dict) -> dict:
        # Lazada 签名：按 key 排序后拼接 secret，MD5 并转大写
        sorted_keys = sorted(params.keys())
        sign_str = self.app_secret + "".join(
            f"{k}{params[k]}" for k in sorted_keys
        )
        params["sign"] = hashlib.md5(sign_str.encode()).hexdigest().upper()
        return params

    def upload_image(self, image_path: str) -> str:
        logger.info(f"[Lazada] Upload image: {image_path}")
        return ""

    def create_listing(self, listing: PlatformListing) -> PlatformListing:
        logger.info(f"[Lazada] Create listing placeholder")
        listing.status = ListingStatus.READY
        return listing

    def update_listing(self, listing: PlatformListing) -> PlatformListing:
        return listing

    def update_stock(self, platform_id: str, stock: int) -> bool:
        return False

    def get_categories(self) -> list[dict]:
        return []


class TikTokShopAdapter(BasePlatformAdapter):
    """TikTok Shop API 适配器（骨架实现）"""

    platform = Platform.TIKTOK_SHOP

    def __init__(self):
        self.api_base = config.TIKTOK_API_BASE
        self.app_key = config.TIKTOK_APP_KEY
        self.app_secret = config.TIKTOK_APP_SECRET

    def _sign(self, params: dict) -> dict:
        return params

    def upload_image(self, image_path: str) -> str:
        logger.info(f"[TikTok Shop] Upload image: {image_path}")
        return ""

    def create_listing(self, listing: PlatformListing) -> PlatformListing:
        logger.info(f"[TikTok Shop] Create listing placeholder")
        listing.status = ListingStatus.READY
        return listing

    def update_listing(self, listing: PlatformListing) -> PlatformListing:
        return listing

    def update_stock(self, platform_id: str, stock: int) -> bool:
        return False

    def get_categories(self) -> list[dict]:
        return []


# 适配器工厂
ADAPTERS = {
    Platform.SHOPEE: ShopeeAdapter,
    Platform.LAZADA: LazadaAdapter,
    Platform.TIKTOK_SHOP: TikTokShopAdapter,
}


def get_adapter(platform: Platform) -> BasePlatformAdapter:
    cls = ADAPTERS.get(platform)
    if not cls:
        raise ValueError(f"Unknown platform: {platform}")
    return cls()
