"""NativeBackend — 直接 subprocess.Popen 启动 CLI。"""
import logging
import shutil
import subprocess

from .base import BaseAgentBackend
from .cli_resolver import parse_models_from_cli_inner, resolve_node_cli

logger = logging.getLogger("agent_os")


class NativeBackend(BaseAgentBackend):
    """原生 CLI 后端。

    配置：cli_config.json: {"cli": "codebuddy"}
    """

    def __init__(self, cli_command: str = "codebuddy"):
        self.cli_command = cli_command
        self.cli_prefix = self._resolve_cli(cli_command)
        logger.info(f"NativeBackend: cli={cli_command}, prefix={self.cli_prefix}")

    def _discover_models(self) -> list[str]:
        return parse_models_from_cli_inner(self.cli_prefix)

    def launch(self, prompt: str,
               model: str | None = None,
               session_id: str | None = None,
               resume_session: str | None = None,
               system_prompt: str | None = None,
               cwd: str | None = None,
               env: dict | None = None,
               ) -> subprocess.Popen:
        cmd = list(self.cli_prefix) + [
            "-p", prompt,
            "--permission-mode", "bypassPermissions",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
        ]
        if model:
            cmd.extend(["--model", model])
        if resume_session:
            cmd.extend(["--resume", resume_session])
        elif session_id:
            cmd.extend(["--session-id", session_id])
        if system_prompt:
            import tempfile
            spf = tempfile.NamedTemporaryFile(
                mode="w", suffix=".md", delete=False,
                encoding="utf-8", dir=cwd or ".", prefix="agent_os_sp_"
            )
            spf.write(system_prompt)
            spf.close()
            cmd.extend(["--system-prompt-file", spf.name])
            if not hasattr(self, '_sp_cleanup_files'):
                self._sp_cleanup_files = []
            self._sp_cleanup_files.append(spf.name)

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=cwd or ".", bufsize=1,
            encoding="utf-8", errors="replace",
            env=env,
        )
        # Popen 自带 poll/wait/terminate/returncode/pid，直接返回
        process.session_id = session_id or resume_session or ""
        return process

    @staticmethod
    def _resolve_cli(cli_command: str) -> list[str]:
        resolved = shutil.which(cli_command)
        cli_path = resolved if resolved else cli_command
        return resolve_node_cli(cli_path)
