"""后端注册表 + 工厂函数。"""
import logging
import os

from .base import AgentBackend
from .native import NativeBackend
from .sdk_base import SDKBackend
from .codebuddy_sdk import CodeBuddySDKBackend

logger = logging.getLogger("agent_os")

_BACKEND_REGISTRY: dict[str, type] = {
    "native": NativeBackend,
    "sdk": SDKBackend,
    "codebuddy-sdk": CodeBuddySDKBackend,
}


def register_backend(name: str, backend_cls: type):
    """注册自定义后端。"""
    _BACKEND_REGISTRY[name] = backend_cls


def get_backend(backend_type: str = "native", cli_command: str = "codebuddy", **kwargs) -> AgentBackend:
    """根据配置创建后端实例。

    环境变量：
        AGENT_OS_BACKEND=native|codebuddy-sdk|sdk
        AGENT_OS_SDK_BACKEND=my_package.MyCustomBackend
    """
    if backend_type == "sdk":
        custom = os.environ.get("AGENT_OS_SDK_BACKEND", "")
        if custom:
            try:
                mod_path, cls_name = custom.rsplit(".", 1)
                mod = __import__(mod_path, fromlist=[cls_name])
                custom_cls = getattr(mod, cls_name)
                if issubclass(custom_cls, SDKBackend):
                    return custom_cls(**kwargs)
                logger.warning(f"{custom} is not a SDKBackend subclass, falling back")
            except Exception as e:
                logger.warning(f"Failed to load custom SDK backend {custom}: {e}")

    cls = _BACKEND_REGISTRY.get(backend_type, NativeBackend)
    if backend_type == "native":
        return cls(cli_command=cli_command, **kwargs)
    return cls(**kwargs)
