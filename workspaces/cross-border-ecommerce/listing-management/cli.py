"""命令行入口 - 批量上架任务"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from listing_management.models import ProductInfo, Platform, Language
from listing_management.scheduler import ListingScheduler


def load_products(filepath: str) -> list[ProductInfo]:
    """从 JSON 文件加载选品数据"""
    data = json.loads(Path(filepath).read_text(encoding="utf-8"))
    products = []
    for item in data if isinstance(data, list) else [data]:
        products.append(ProductInfo(
            source_id=item.get("source_id", str(hash(item.get("title", "")))),
            title=item["title"],
            category=item.get("category", "未分类"),
            price=float(item.get("price", 0)),
            currency=item.get("currency", "TWD"),
            images=item.get("images", []),
            attributes=item.get("attributes", {}),
            variants=item.get("variants", []),
        ))
    return products


async def main():
    parser = argparse.ArgumentParser(description="批量上架工具")
    parser.add_argument("--input", "-i", required=True, help="选品数据 JSON 文件路径")
    parser.add_argument(
        "--platforms", "-p", nargs="+", default=["shopee"],
        choices=["shopee", "lazada", "tiktok_shop"],
        help="目标平台"
    )
    parser.add_argument(
        "--languages", "-l", nargs="+", default=["zh-tw", "en"],
        choices=["zh-tw", "zh-cn", "en", "th", "vi", "id", "ms", "tl"],
        help="文案语言"
    )
    parser.add_argument("--dry-run", action="store_true", help="仅生成文案不实际上架")
    parser.add_argument("--delay", type=float, default=1.0, help="商品间延迟（秒）")

    args = parser.parse_args()

    products = load_products(args.input)
    print(f"加载 {len(products)} 个商品")

    platforms = [Platform(p) for p in args.platforms]
    languages = [Language(l) for l in args.languages]

    scheduler = ListingScheduler()

    for product in products:
        print(f"\n处理: {product.title[:40]}...")
        listings = scheduler.process_product(product, platforms, languages)

        if not args.dry_run:
            listings = scheduler.publish(listings)

        for platform, listing in listings.items():
            status = "OK" if listing.status.value == "published" else listing.status.value
            print(f"  [{platform.value}] {status} | ID: {listing.platform_id or 'N/A'}")


if __name__ == "__main__":
    asyncio.run(main())
