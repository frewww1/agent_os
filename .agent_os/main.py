"""
Agent OS — Web terminal for Claude CLI.

启动后打开浏览器访问 http://127.0.0.1:8420
在输入框中输入 prompt，实时看到 Claude agent 的流式输出。

Usage:
    python main.py
    python main.py --port 8420
"""
import argparse
import asyncio
import importlib.util
import os
import socket
import sys
import webbrowser
from pathlib import Path

import uvicorn

# 此目录 .agent_os 因含 "." 前缀不能直接作为 Python 包名导入。
# 用 importlib 把它注册为名为 "agent_os" 的虚拟包，使 dashboard/app.py 等
# 子模块的相对导入（from ..agent_os import ...）能正常工作。
_this_dir = Path(__file__).parent
_pkg_name = "agent_os"
if _pkg_name not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        _pkg_name,
        _this_dir / "__init__.py",
        submodule_search_locations=[str(_this_dir)],
    )
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules[_pkg_name] = _pkg
    _spec.loader.exec_module(_pkg)

# 注册 agent_os.src 子包
_src_pkg_name = "agent_os.src"
if _src_pkg_name not in sys.modules:
    _src_spec = importlib.util.spec_from_file_location(
        _src_pkg_name,
        _this_dir / "src" / "__init__.py",
        submodule_search_locations=[str(_this_dir / "src")],
    )
    _src_pkg = importlib.util.module_from_spec(_src_spec)
    sys.modules[_src_pkg_name] = _src_pkg
    _src_spec.loader.exec_module(_src_pkg)

from agent_os.src.core.agent_os import AgentOS
from agent_os.dashboard.app import app, set_agent_os


def _bind_socket(host: str, base_port: int) -> tuple[socket.socket | None, int]:
    """从 base 起找空闲端口并原子占用（保持绑定，直接交给 uvicorn 复用）。

    支持多开：端口被占用时自动递增；socket 保持打开可避免"探测后被抢"的竞态
    （uvicorn 绑定失败会自行退出且不抛异常，无法用重试兜底，故必须预占用）。
    """
    for p in range(base_port, base_port + 20):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # 注意：不能设置 SO_REUSEADDR —— Windows 上它会允许两个 socket
            # 绑定同一端口，导致多开时端口不递增、实例互相抢请求。
            s.bind((host, p))
            s.listen(1)
            # 用实际绑定端口（--port 0 时由系统随机分配）
            return s, s.getsockname()[1]
        except OSError:
            s.close()
            continue
    return None, base_port


def _load_cli_config():
    """从 cli_config.json 读取配置，fallback 到 codebuddy + native。

    返回 (cli, backend, default_model)；default_model 可为空字符串，
    表示未配置，由调用方决定兜底默认值。
    """
    import json
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cli_config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return (
            data.get("cli", "codebuddy"),
            data.get("backend", "native"),
            data.get("default_model", "") or "",
        )
    except Exception:
        return "codebuddy", "native", ""


def main():
    default_cli, default_backend, default_model_cfg = _load_cli_config()
    default_model = default_model_cfg or "deepseek-v4-pro"
    parser = argparse.ArgumentParser(description="Agent OS - Web Terminal for Claude CLI")
    parser.add_argument("--port", type=int, default=8420, help="Server port (default: 8420)")
    parser.add_argument("--host", default="127.0.0.1", help="Server host (default: 127.0.0.1)")
    parser.add_argument("--cli", default=default_cli,
                        help=f"Backend CLI command (default from config: {default_cli})")
    parser.add_argument("--backend", default=None,
                        help=f"Agent backend: native, codebuddy-sdk, sdk (default from config: {default_backend})")
    parser.add_argument("--model", default=default_model,
                        help=f"Default model for all agents (default from config: {default_model}). "
                             "Can be overridden per-agent in Dashboard.")
    parser.add_argument("--root", default=None,
                        help="Working directory for the CLI process (default: current directory)")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    # Initialize process manager
    # 默认 project_root 为 CLI 运行目录（cwd），实现"部署在一处、按运行目录工作"；
    # 显式 --root 可覆盖。
    if args.root:
        project_root = os.path.abspath(args.root)
    else:
        project_root = os.getcwd()
    backend_type = args.backend or default_backend
    # 预绑定端口并原子占用（多开：被占用自动递增）。socket 保持打开，
    # 交给 uvicorn 复用，避免"探测后被抢"竞态导致启动失败。
    sock, port = _bind_socket(args.host, args.port)
    if sock is None:
        print(f"[ERROR] No free port found from {args.port} to {args.port + 20}")
        sys.exit(1)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    agent_os = AgentOS(project_root=project_root, cli_command=args.cli, port=port,
                        default_model=args.model, loop=loop,
                        backend_type=backend_type)
    set_agent_os(agent_os)

    url = f"http://{args.host}:{port}"
    print()
    print("=" * 48)
    print("  Agent OS — Multi-Agent Orchestration")
    print("=" * 48)
    print(f"  URL:      {url}")
    print(f"  Backend:  {backend_type}")
    print(f"  CLI:      {args.cli}")
    if args.model:
        print(f"  Model:    {args.model}")
    print(f"  Root:     {project_root}")
    print("=" * 48)
    print()

    # Auto-open browser
    if not args.no_browser:
        webbrowser.open(url)

    # Start server：把已绑定的 socket 直接交给 uvicorn（不重新 bind）。
    # 注：不能传 uvicorn 的 fd 参数（Windows 上其内部硬编码 AF_UNIX），
    # 用 Server.run(sockets=[sock]) 复用 TCP socket 最稳。
    config = uvicorn.Config(app, host=args.host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
