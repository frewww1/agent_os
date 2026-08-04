"""AI 商品文案生成器 - 支持多语言"""
from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Optional

from openai import OpenAI
from loguru import logger

from .config import config
from .models import Language, ListingContent, ProductInfo


SYSTEM_PROMPT = """你是跨境电商商品文案专家。根据输入的商品信息，生成多语言商品文案。
要求：
1. 标题：简洁有力，包含核心关键词，字符数符合平台限制
2. 描述：突出卖点，包含规格、材质、适用场景，用 HTML 格式
3. 卖点（highlights）：3-5 条精炼卖点，每条 15 字以内
4. 关键词（keywords）：5-10 个搜索热词

输出格式（JSON）：
{
  "title": "...",
  "description": "...",
  "highlights": ["...", "..."],
  "keywords": ["...", "..."]
}"""

LANG_INSTRUCTIONS = {
    Language.ZH_TW: "用繁體中文，台灣市場風格，語氣親切，適合蝦皮/露天。",
    Language.ZH_CN: "用简体中文，大陆跨境卖家风格。",
    Language.EN: "Use natural English, Southeast Asian marketplace style.",
    Language.TH: "ใช้ภาษาไทย สไตล์ตลาดออนไลน์ เหมาะกับ Shopee/Lazada ประเทศไทย",
    Language.VI: "Dùng tiếng Việt, phong cách thương mại điện tử Việt Nam.",
    Language.ID: "Gunakan Bahasa Indonesia, gaya marketplace Indonesia.",
    Language.MS: "Gunakan Bahasa Melayu, gaya marketplace Malaysia.",
    Language.TL: "Gumamit ng Tagalog, istilong marketplace ng Pilipinas.",
}


class CopyGenerator:
    """多语言商品文案生成器"""

    def __init__(self, model: str | None = None):
        self.client = OpenAI(api_key=config.OPENAI_API_KEY)
        self.model = model or config.OPENAI_MODEL

    def _build_prompt(self, product: ProductInfo, lang: Language) -> str:
        lang_instruction = LANG_INSTRUCTIONS.get(lang, "")
        return f"""{lang_instruction}

商品信息：
- 原始标题：{product.title}
- 类目：{product.category}
- 价格：{product.price} {product.currency}
- 属性：{json.dumps(product.attributes, ensure_ascii=False)}
- 变体：{json.dumps(product.variants, ensure_ascii=False)}

请生成{lang.value}语言的商品文案。"""

    def generate_one(self, product: ProductInfo, lang: Language) -> ListingContent:
        """为单个语言生成文案"""
        prompt = self._build_prompt(product, lang)
        logger.info(f"Generating copy for {lang.value}...")

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content
            data = json.loads(raw)
            return ListingContent(
                title=data["title"],
                description=data["description"],
                highlights=data.get("highlights", []),
                keywords=data.get("keywords", []),
                language=lang,
            )
        except Exception as e:
            logger.error(f"Failed to generate {lang.value}: {e}")
            # 返回占位
            return ListingContent(
                title=product.title,
                description="",
                language=lang,
            )

    def generate_all(
        self,
        product: ProductInfo,
        languages: list[Language] | None = None,
    ) -> dict[Language, ListingContent]:
        """批量生成多语言文案"""
        if languages is None:
            languages = [Language.ZH_TW, Language.EN]
        results = {}
        for lang in languages:
            results[lang] = self.generate_one(product, lang)
        return results

    def optimize_title(
        self,
        title: str,
        lang: Language,
        max_chars: int = 60,
    ) -> str:
        """优化标题长度，适配平台限制"""
        prompt = f"将以下商品标题精简到 {max_chars} 个字符以内，保留核心关键词，语言 {lang.value}：\n{title}"
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100,
            )
            return resp.choices[0].message.content.strip()
        except Exception:
            return title[:max_chars]
