"""批量上架调度器 - 编排多商品、多平台上架流程"""
from __future__ import annotations
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

from .config import config
from .models import (
    ProductInfo,
    PlatformListing,
    Platform,
    Language,
    ListingStatus,
    ListingContent,
    ListingImage,
)
from .copy_generator import CopyGenerator
from .image_generator import ImageGenerator
from .platform_adapter import get_adapter, BasePlatformAdapter


class ListingScheduler:
    """批量上架调度器：文案生成 -> 图片生成 -> 平台适配 -> 上架"""

    def __init__(self):
        self.copy_gen = CopyGenerator()
        self.image_gen = ImageGenerator()
        self.output_dir = config.OUTPUT_DIR

    def process_product(
        self,
        product: ProductInfo,
        target_platforms: list[Platform],
        languages: list[Language] | None = None,
    ) -> dict[Platform, PlatformListing]:
        """处理单个商品：生成文案和图片，返回各平台 Listing"""
        if languages is None:
            languages = [Language.ZH_TW, Language.EN]

        logger.info(f"Processing product: {product.title[:30]}...")

        # Step 1: AI 生成多语言文案
        contents = self.copy_gen.generate_all(product, languages)

        # Step 2: 生成商品主图
        main_content = contents.get(Language.ZH_TW) or list(contents.values())[0]
        main_image_path = self.image_gen.generate_main_image(
            product_title=main_content.title,
            price_text=f"${product.price:.0f}",
        )

        # Step 3: 生成详情图
        detail_paths = self.image_gen.generate_detail_images(
            main_content.highlights if main_content.highlights else ["优质商品", "快速发货"]
        )

        # Step 4: 按平台构建 Listing
        results: dict[Platform, PlatformListing] = {}
        for platform in target_platforms:
            listing = PlatformListing(
                platform=platform,
                product_info=product,
                contents=contents,
                images=[
                    ListingImage(url=main_image_path, type="main"),
                ] + [
                    ListingImage(url=p, type="detail") for p in detail_paths
                ],
                price=product.price,
                stock=100,  # 默认库存，后续可同步
                status=ListingStatus.DRAFT,
            )
            results[platform] = listing

        # 保存中间结果
        self._save_draft(product, results)
        return results

    def publish(
        self,
        listings: dict[Platform, PlatformListing],
    ) -> dict[Platform, PlatformListing]:
        """将 Listing 推送到各平台"""
        for platform, listing in listings.items():
            adapter = get_adapter(platform)
            logger.info(f"Publishing to {platform.value}...")
            # 先上传图片
            for img in listing.images:
                if img.url and not img.url.startswith("http"):
                    uploaded_url = adapter.upload_image(img.url)
                    if uploaded_url:
                        img.url = uploaded_url
            # 上架
            updated = adapter.create_listing(listing)
            listings[platform] = updated
        return listings

    async def batch_process(
        self,
        products: list[ProductInfo],
        target_platforms: list[Platform],
        languages: list[Language] | None = None,
        delay_between: float = 1.0,
    ) -> list[dict]:
        """异步批量处理多个商品"""
        results = []
        for product in products:
            try:
                listings = self.process_product(product, target_platforms, languages)
                listings = self.publish(listings)
                results.append({
                    "product": product.title,
                    "status": "ok",
                    "platforms": {
                        p.value: l.status.value for p, l in listings.items()
                    },
                })
            except Exception as e:
                logger.error(f"Failed to process {product.title}: {e}")
                results.append({
                    "product": product.title,
                    "status": "error",
                    "error": str(e),
                })
            await asyncio.sleep(delay_between)
        return results

    def sync_stock(
        self,
        platform: Platform,
        stock_map: dict[str, int],  # platform_id -> new_stock
    ) -> dict[str, bool]:
        """同步库存到平台"""
        adapter = get_adapter(platform)
        results = {}
        for pid, stock in stock_map.items():
            ok = adapter.update_stock(pid, stock)
            results[pid] = ok
            logger.info(f"Stock sync {pid}: {stock} -> {'ok' if ok else 'fail'}")
        return results

    def _save_draft(self, product: ProductInfo, listings: dict[Platform, PlatformListing]):
        """保存草稿到本地 JSON"""
        draft_dir = self.output_dir / "drafts"
        draft_dir.mkdir(exist_ok=True)
        draft = {
            "product_title": product.title,
            "timestamp": datetime.now().isoformat(),
            "listings": {},
        }
        for platform, listing in listings.items():
            draft["listings"][platform.value] = {
                "status": listing.status.value,
                "price": listing.price,
                "stock": listing.stock,
                "contents": {
                    lang.value: {
                        "title": c.title,
                        "highlights": c.highlights,
                    }
                    for lang, c in listing.contents.items()
                },
            }
        filename = f"{product.source_id or hash(product.title) & 0x7FFFFFFF:08x}.json"
        filepath = draft_dir / filename
        filepath.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"Draft saved: {filepath}")
