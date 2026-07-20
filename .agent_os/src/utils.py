"""Agent OS 共享工具函数。"""

import os
import re
import subprocess


def safe_run(cmd, **kwargs):
    """subprocess.run with utf-8 encoding by default for text=True calls.

    Windows 上 subprocess 默认使用 GBK/cp936 编码解码管道输出，
    git/node 等工具的 UTF-8 中文输出会被 _readerthread 抛出
    UnicodeDecodeError。此 wrapper 对 text=True 的调用自动追加
    encoding="utf-8" + errors="replace"。
    """
    if kwargs.get("text") and "encoding" not in kwargs:
        kwargs["encoding"] = "utf-8"
        kwargs.setdefault("errors", "replace")
    return subprocess.run(cmd, **kwargs)


def sanitize(obj):
    """递归清除字符串中的 surrogate 字符，避免 JSON 序列化失败。"""
    if isinstance(obj, str):
        # json.dumps 对 surrogate 字符 (\\uD800-\\uDFFF) 会直接报错，
        # 必须在进入 json 序列化前清除。
        # encode surrogatepass -> decode replace 是最可靠的方式
        return obj.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj


def sanitize_workspace_name(name: str) -> str:
    """把外部传入的 workspace 名清洗成单层安全目录名。

    仅作通用安全处理（防路径穿越/非法字符），不含任何业务语义：
    - 取 basename，剥离任何目录分隔，杜绝 ``../`` 穿越；
    - 非 ``[A-Za-z0-9._-]`` 的字符替换为 ``_``；
    - 去掉首尾 ``.``，为空时回退为 ``workspace``。
    """
    base = os.path.basename(str(name).strip().replace("\\", "/").rstrip("/"))
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip(".")
    return safe or "workspace"


def cwd_to_session_key(cwd: str) -> str:
    """把 cwd 转成 CLI 的项目 key（实测自 `~/.<cli>/projects/` 下的目录名）。

    规则：
      - Windows 盘符：开头 `X:` → 小写盘符 + 直接去掉冒号（不替换为 `-`）
        例：`g:\\svn\\...` → `g\\svn\\...`、`C:\\Users\\...` → `c\\Users\\...`
      - 路径分隔符 `\\` `/` → `-`
      - 保留 `.`（`.agent_os` 不会被吃掉）
      - 去除首尾 `-`
    在非 Windows 平台（cwd 不以盘符开头），上述盘符规则不会触发，
    `/home/u/foo` → `home-u-foo`，与 CLI 实际目录命名相符。
    """
    if len(cwd) >= 2 and cwd[1] == ":" and cwd[0].isalpha():
        cwd = cwd[0].lower() + cwd[2:]
    key = re.sub(r"[\\/]", "-", cwd)
    return key.strip("-")
