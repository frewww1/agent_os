"""CodeBuddySDKBackend — 通过 codebuddy-agent-sdk 启动 agent。"""
import asyncio
import dataclasses
import json as _json
import logging
import os
import threading

from .backend import SDKBackend, SDKHandle
from .cli_resolver import parse_models_from_cli_inner

logger = logging.getLogger("agent_os")


class CodeBuddySDKBackend(SDKBackend):
    """CodeBuddy Agent SDK 后端。

    使用 codebuddy-agent-sdk，SDK 消息直接转为事件 dict 推送。
    模型发现：~/.codebuddy/models.json -> CLI --help -> FALLBACK（继承模板方法）。
    """

    DEFAULT_SETTING_SOURCES: list[str] = []

    def _get_setting_sources(self) -> list[str]:
        raw = os.environ.get("AGENT_OS_SDK_SETTINGS", "")
        if raw:
            return [s.strip() for s in raw.split(",") if s.strip()]
        return list(self.DEFAULT_SETTING_SOURCES)

    def _discover_models(self) -> list[str]:
        """从 ~/.codebuddy/models.json 或 CLI --help 发现模型。"""
        models = self._read_codebuddy_models_json()
        if models:
            return models
        return parse_models_from_cli_inner(["codebuddy"])

    @staticmethod
    def _read_codebuddy_models_json() -> list[str]:
        candidates = [os.path.join(os.path.expanduser("~"), ".codebuddy", "models.json")]
        try:
            cwd_models = os.path.join(os.getcwd(), ".codebuddy", "models.json")
            if os.path.isfile(cwd_models):
                candidates.insert(0, cwd_models)
        except Exception:
            pass
        for path in candidates:
            try:
                if os.path.isfile(path):
                    with open(path, encoding="utf-8-sig") as f:
                        data = _json.load(f)
                    available = data.get("availableModels", [])
                    if isinstance(available, list) and available:
                        return [str(m) for m in available]
                    models_arr = data.get("models", [])
                    if isinstance(models_arr, list):
                        ids = [m.get("id", "") for m in models_arr if isinstance(m, dict) and m.get("id")]
                        if ids:
                            return ids
            except Exception as e:
                logger.debug(f"Failed to read models.json: {e}")
        return []

    def _call_sdk(self, handle: SDKHandle, prompt: str, model: str,
                  session_id: str | None, resume_session: str | None,
                  system_prompt: str | None,
                  cwd: str | None, env: dict | None,
                  stop: threading.Event):
        try:
            from codebuddy_agent_sdk import query, CodeBuddyAgentOptions
        except ImportError as e:
            self._emit_event(handle, "error", error=f"codebuddy-agent-sdk not installed: {e}")
            return

        opts = CodeBuddyAgentOptions(
            model=model or None,
            system_prompt=system_prompt or None,
            permission_mode="bypassPermissions",
            include_partial_messages=True,
            setting_sources=self._get_setting_sources(),
            cwd=cwd or None,
            env=dict(env) if env else {},
        )
        if resume_session:
            opts.resume = resume_session
        elif session_id:
            opts.session_id = session_id

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._run_sdk_query(handle, loop, prompt, opts, stop))
        finally:
            loop.close()

    async def _run_sdk_query(self, handle: SDKHandle, loop, prompt: str, opts, stop: threading.Event):
        from codebuddy_agent_sdk import query
        from codebuddy_agent_sdk import (
            AssistantMessage, SystemMessage, ResultMessage, StreamEvent,
            UserMessage, TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock,
        )

        try:
            async for msg in query(prompt=prompt, options=opts):
                if stop.is_set():
                    break

                if isinstance(msg, SystemMessage):
                    data = dataclasses.asdict(msg)
                    inner = dict(data.get("data", {}))
                    inner["subtype"] = data.get("subtype", "init")
                    inner["session_id"] = inner.get("session_id", "")
                    self._emit_event(handle, "system", **inner)
                    continue

                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            self._emit_event(handle, "text", text=block.text)
                        elif isinstance(block, ThinkingBlock):
                            self._emit_event(handle, "thinking",
                                thinking=block.thinking, signature=block.signature)
                        elif isinstance(block, ToolUseBlock):
                            self._emit_event(handle, "tool_use",
                                tool=block.name, id=block.id,
                                summary=_json.dumps(block.input, ensure_ascii=False)[:200],
                                input=block.input)
                        elif isinstance(block, ToolResultBlock):
                            text = block.content
                            if isinstance(text, list):
                                text = "\n".join(
                                    rc.get("text", "") for rc in text
                                    if isinstance(rc, dict) and rc.get("type") == "text"
                                )
                            self._emit_event(handle, "tool_result",
                                text=str(text)[:800] if text else "",
                                tool_use_id=block.tool_use_id,
                                is_error=block.is_error)
                    continue

                if isinstance(msg, StreamEvent):
                    data = dataclasses.asdict(msg)
                    inner = data.get("event", {})
                    inner_type = inner.get("type", "")
                    if inner_type in ("content_block_start", "content_block_delta"):
                        delta = inner.get("delta", {})
                        if delta.get("type") == "text_delta":
                            self._emit_event(handle, "text_delta", text=delta.get("text", ""))
                    continue

                if isinstance(msg, ResultMessage):
                    data = dataclasses.asdict(msg)
                    usage = dataclasses.asdict(msg.usage) if msg.usage else {}
                    self._emit_event(handle, "result",
                        subtype=data.get("subtype", ""),
                        is_error=data.get("is_error", False),
                        result=data.get("result", ""),
                        session_id=data.get("session_id", ""),
                        duration_ms=data.get("duration_ms", 0),
                        num_turns=data.get("num_turns", 0),
                        total_cost_usd=data.get("total_cost_usd"),
                        usage=usage)
                    continue

        except Exception as e:
            logger.exception(f"CodeBuddy SDK query error: {e}")
            self._emit_event(handle, "error", error=str(e))
