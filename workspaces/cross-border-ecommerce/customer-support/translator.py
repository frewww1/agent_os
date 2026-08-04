"""多语言翻译适配层

支持：
- 自动检测语言
- 翻译到目标语言（繁中、英文、印尼语等）
- 批量翻译（商品描述、FAQ 条目）

后端可选：
- Google Translate API
- DeepL API
- LLM-based 翻译（OpenAI / Claude）
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

from shared.models import Language

logger = logging.getLogger(__name__)


class TranslatorBackend(str, Enum):
    LLM = "llm"           # 用 LLM 翻译
    GOOGLE = "google"     # Google Cloud Translation
    DEEPL = "deepl"       # DeepL API
    MOCK = "mock"         # 测试用，原样返回


class Translator:
    """多语言翻译器"""

    def __init__(self, backend: TranslatorBackend = TranslatorBackend.MOCK, api_key: str = ""):
        self.backend = backend
        self.api_key = api_key

    async def translate(
        self,
        text: str,
        target_lang: Language,
        source_lang: Optional[Language] = None,
    ) -> str:
        """翻译文本到目标语言"""
        if not text.strip():
            return text

        if self.backend == TranslatorBackend.MOCK:
            return self._mock_translate(text, target_lang)

        if self.backend == TranslatorBackend.LLM:
            return await self._llm_translate(text, target_lang, source_lang)

        if self.backend == TranslatorBackend.GOOGLE:
            return await self._google_translate(text, target_lang, source_lang)

        logger.warning(f"未实现的翻译后端: {self.backend}")
        return text

    async def detect_language(self, text: str) -> Language:
        """检测文本语言"""
        # 简易实现：字符集检测
        # 生产环境可用 langdetect / fasttext
        import re

        # 检测中文
        if re.search(r'[\u4e00-\u9fff]', text):
            # 繁简区分：繁体常用字
            tw_chars = set('臺灣麼這隻後會於與無體對發綫實爲')
            has_tw = any(c in text for c in tw_chars)
            return Language.ZH_TW if has_tw else Language.ZH_CN

        # 检测泰语
        if re.search(r'[\u0e00-\u0e7f]', text):
            return Language.TH

        # 默认英文
        return Language.EN

    async def batch_translate(
        self,
        texts: list[str],
        target_lang: Language,
    ) -> list[str]:
        """批量翻译"""
        results = []
        for text in texts:
            results.append(await self.translate(text, target_lang))
        return results

    async def translate_knowledge_entry(
        self,
        question: str,
        answer: str,
        target_langs: list[Language],
    ) -> dict[str, str]:
        """翻译 FAQ 条目到多种语言，返回 {lang_code: translated_text}"""
        translations = {}
        for lang in target_langs:
            translations[f"q_{lang.value}"] = await self.translate(question, lang)
            translations[f"a_{lang.value}"] = await self.translate(answer, lang)
        return translations

    # ---- 后端实现 ----

    async def _llm_translate(
        self, text: str, target_lang: Language, source_lang: Optional[Language]
    ) -> str:
        """用 LLM 翻译"""
        import openai

        lang_names = {
            Language.ZH_TW: "繁体中文（台灣用語）",
            Language.ZH_CN: "简体中文",
            Language.EN: "English",
            Language.ID: "Bahasa Indonesia",
            Language.TH: "ภาษาไทย",
            Language.VI: "Tiếng Việt",
            Language.PT_BR: "Português (Brasil)",
        }
        target_name = lang_names.get(target_lang, str(target_lang))

        client = openai.AsyncOpenAI(api_key=self.api_key or "sk-placeholder")
        try:
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{
                    "role": "system",
                    "content": f"你是一个专业翻译。将以下文本翻译成{target_name}。只输出译文，不要解释。"
                }, {
                    "role": "user",
                    "content": text,
                }],
                temperature=0.1,
                max_tokens=1024,
            )
            return response.choices[0].message.content or text
        except Exception:
            logger.exception("LLM 翻译失败")
            return text

    async def _google_translate(
        self, text: str, target_lang: Language, source_lang: Optional[Language]
    ) -> str:
        """Google Cloud Translation API"""
        # TODO: 接入 Google Cloud Translation
        return text

    def _mock_translate(self, text: str, target_lang: Language) -> str:
        """测试用：在原文前加语言标记"""
        markers = {
            Language.ZH_TW: "[繁中]",
            Language.EN: "[EN]",
            Language.ID: "[ID]",
        }
        return f"{markers.get(target_lang, '')}{text}"
