"""图片模板生成器 - 生成商品主图/详情图"""
from __future__ import annotations
from pathlib import Path
from typing import Optional
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont
from loguru import logger

from .config import config
from .models import ListingImage


class ImageGenerator:
    """商品图片处理：主图生成、水印、尺寸适配"""

    def __init__(self):
        self.output_dir = config.IMAGE_OUTPUT_DIR
        self.template_dir = config.TEMPLATE_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _get_font(self, size: int = 36) -> ImageFont.FreeTypeFont:
        """获取字体，fallback 到默认"""
        font_paths = [
            "C:/Windows/Fonts/msjh.ttc",       # 微软正黑体
            "C:/Windows/Fonts/msyh.ttc",       # 微软雅黑
            "C:/Windows/Fonts/arial.ttf",
        ]
        for fp in font_paths:
            if Path(fp).exists():
                try:
                    return ImageFont.truetype(fp, size)
                except Exception:
                    continue
        return ImageFont.load_default()

    def generate_main_image(
        self,
        product_title: str,
        base_image_path: Optional[str] = None,
        price_text: str = "",
        width: int = 800,
        height: int = 800,
    ) -> str:
        """生成商品主图：底图 + 标题 + 价格标签"""
        if base_image_path and Path(base_image_path).exists():
            img = Image.open(base_image_path).convert("RGBA")
            img = img.resize((width, height), Image.LANCZOS)
        else:
            # 纯色底图
            img = Image.new("RGBA", (width, height), (245, 245, 245, 255))

        draw = ImageDraw.Draw(img)
        font_title = self._get_font(32)
        font_price = self._get_font(28)

        # 底部半透明条
        bar_height = 120
        overlay = Image.new("RGBA", (width, bar_height), (0, 0, 0, 140))
        img.paste(overlay, (0, height - bar_height), overlay)

        # 标题（截断）
        max_chars = 30
        title = product_title[:max_chars] + ("..." if len(product_title) > max_chars else "")
        draw.text((20, height - bar_height + 10), title, fill=(255, 255, 255), font=font_title)

        # 价格标签
        if price_text:
            bbox = draw.textbbox((0, 0), price_text, font=font_price)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            rx, ry = width - tw - 30, height - bar_height + 60
            draw.rounded_rectangle(
                (rx - 10, ry - 5, rx + tw + 10, ry + th + 5),
                radius=8,
                fill=(255, 87, 34, 255),
            )
            draw.text((rx, ry), price_text, fill=(255, 255, 255), font=font_price)

        # 保存
        filename = f"main_{hash(product_title) & 0x7FFFFFFF:08x}.png"
        out_path = str(self.output_dir / filename)
        img = img.convert("RGB")
        img.save(out_path, "PNG", quality=95)
        logger.info(f"Main image saved: {out_path}")
        return out_path

    def generate_detail_images(
        self,
        highlights: list[str],
        width: int = 800,
        height: int = 600,
    ) -> list[str]:
        """生成卖点详情图（多张）"""
        paths = []
        for i, highlight in enumerate(highlights):
            img = Image.new("RGBA", (width, height), (255, 255, 255, 255))
            draw = ImageDraw.Draw(img)
            font = self._get_font(36)

            # 居中文字
            bbox = draw.textbbox((0, 0), highlight, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            x = (width - tw) // 2
            y = (height - th) // 2
            draw.text((x, y), highlight, fill=(50, 50, 50), font=font)

            filename = f"detail_{i:02d}_{hash(highlight) & 0x7FFFFFFF:08x}.png"
            out_path = str(self.output_dir / filename)
            img = img.convert("RGB")
            img.save(out_path, "PNG", quality=95)
            paths.append(out_path)
        logger.info(f"Generated {len(paths)} detail images")
        return paths

    def resize_for_platform(
        self,
        image_path: str,
        platform: str,
    ) -> str:
        """按平台要求调整图片尺寸"""
        platform_sizes = {
            "shopee": (1024, 1024),
            "lazada": (800, 800),
            "tiktok_shop": (800, 800),
        }
        size = platform_sizes.get(platform, (800, 800))
        img = Image.open(image_path)
        img = img.resize(size, Image.LANCZOS)
        stem = Path(image_path).stem
        out_path = str(self.output_dir / f"{stem}_{platform}.png")
        img.save(out_path, "PNG", quality=95)
        return out_path
