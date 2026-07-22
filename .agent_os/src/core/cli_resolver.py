"""Agent OS CLI 路径解析与模型管理。

CLI 后端抽象：resolve_node_cli 支持 CLI 后端适配器模式，
新增 CLI 后端只需注册新的适配函数。
"""
import json as _json
import logging
import os
import re
import shutil
import subprocess

from ..utils import safe_run

logger = logging.getLogger("agent_os")

# 模型列表缓存文件
MODELS_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "state", "models.json"
)

# ============================================================================
# CLI 后端适配器
# ============================================================================

def resolve_node_cli(cli_path: str) -> list[str]:
    """解析 CLI 路径，返回可执行的命令列表（如 ['node', 'xxx.js'] 或 ['claude']）。

    Windows .CMD shim 绕过：npm 安装的 CLI 工具是 .CMD 包装脚本，
    对含换行的 prompt 有截断 bug。此处解析 .CMD 内容找到底层 .js 文件，
    返回 ['node', target_js] 绕过 shim。

    支持格式：
      - 直接可执行（Linux/macOS 或已安装的 .exe）
      - npm .CMD shim
    """
    # 非 .CMD 文件直接返回
    if not cli_path.lower().endswith('.cmd'):
        return [cli_path]

    # 解析 .CMD shim
    target = _parse_cmd_shim(cli_path)
    if target is None:
        return [cli_path]

    # 校验 Node 能否加载该模块
    if not _can_node_load(target):
        logger.info(
            f"resolve_node_cli: node can't load {target}, using .cmd directly"
        )
        return [cli_path]

    return ['node', target]


def _parse_cmd_shim(cli_path: str) -> str | None:
    """解析 npm .CMD shim，提取底层 .js / bin 文件路径。"""
    try:
        with open(cli_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        m = re.search(r'"%_prog%"\s+"(%dp0%[^"]+)"', content)
        if not m:
            return None
        target_rel = m.group(1)
        dp0 = os.path.dirname(cli_path) + os.sep
        target_abs = os.path.normpath(target_rel.replace('%dp0%', dp0))
        if not os.path.exists(target_abs):
            return None
        return target_abs
    except Exception as e:
        logger.warning(f"_parse_cmd_shim failed: {e}")
        return None


def _can_node_load(target: str) -> bool:
    """用 require.resolve 验证 Node 能否加载目标模块。"""
    try:
        escaped = target.replace('\\', '\\\\')
        check = subprocess.run(
            ['node', '-e', f'require.resolve("{escaped}")'],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=5, cwd=os.path.dirname(target),
        )
        return check.returncode == 0
    except Exception:
        return False


# ============================================================================
# 模型列表管理
# ============================================================================

def read_models_cache_file() -> list[str]:
    """从 state/models.json 读取模型列表。"""
    try:
        if os.path.exists(MODELS_CACHE_FILE):
            with open(MODELS_CACHE_FILE, encoding="utf-8-sig") as f:
                data = _json.load(f)
            if isinstance(data, list):
                return [str(x) for x in data if x]
    except Exception as e:
        logger.warning(f"read models cache failed: {e}")
    return []


def write_models_cache_file(models: list[str]) -> None:
    """把模型列表写入 state/models.json。"""
    try:
        os.makedirs(os.path.dirname(MODELS_CACHE_FILE), exist_ok=True)
        with open(MODELS_CACHE_FILE, "w", encoding="utf-8") as f:
            _json.dump(models, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"write models cache failed: {e}")


def parse_models_from_cli_inner(cli_prefix: list[str]) -> list[str]:
    """运行 CLI 解析模型列表（独立函数，供 backend 复用）。

    策略：
    1. 先尝试 --help 解析（claude CLI: "Currently supported: (m1, m2)"）
    2. 失败则通过 -p "/model" 交互获取（codebuddy CLI）
    """
    models: list[str] = []

    # 策略1：--help 解析（claude CLI 旧格式）
    try:
        cmd = list(cli_prefix) + ["--help"]
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=15, shell=False, stdin=subprocess.DEVNULL,
        )
        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
        m = re.search(r"Currently supported:\s*\(([^)]+)\)", text, re.DOTALL)
        if m:
            raw = re.sub(r"\s+", "", m.group(1))
            models = [s for s in raw.split(",") if s]
        if not models:
            logger.warning("parse_models_from_cli: failed to parse models from --help output")
    except Exception as e:
        logger.warning(f"parse_models_from_cli --help failed: {e}")

    # 策略2：-p "/model" 交互获取（codebuddy CLI）
    if not models:
        try:
            cmd = list(cli_prefix) + ["-p", "/model", "--output-format", "text"]
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=60, shell=False, stdin=subprocess.DEVNULL,
            )
            text = proc.stdout or ""
            # 匹配: - **modelname** — description 或 * **modelname** —
            model_matches = re.findall(r'[-*/]\s*\*{0,2}(\w+)\*{0,2}\s*[—–-]', text)
            if model_matches:
                models = [m for m in model_matches if m.lower() != "model"]
            if models:
                logger.info(f"parse_models_from_cli: parsed via /model: {models}")
            else:
                logger.warning("parse_models_from_cli: /model fallback returned no models")
        except Exception as e:
            logger.warning(f"parse_models_from_cli /model fallback failed: {e}")

    return models


def parse_models_from_cli(pm) -> list[str]:
    """运行 <cli> --help 解析模型列表（兼容旧接口）。"""
    cli_prefix = resolve_node_cli(pm.cli_command)
    return parse_models_from_cli_inner(cli_prefix)


def list_models(pm, refresh: bool = False) -> list[str]:
    """返回当前 CLI 支持的模型 ID 列表。

    数据来源：
    1. 缓存文件 state/models.json（由外部脚本在有 TTY 环境预写入）
    2. 兜底直接运行 CLI --help 解析
    """
    if pm._models_cache is not None and not refresh:
        return pm._models_cache
    models = read_models_cache_file()
    if not models:
        models = parse_models_from_cli(pm)
        if models:
            write_models_cache_file(models)
    pm._models_cache = models
    return models
