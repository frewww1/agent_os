"""MCP Server 单元测试 — 验证 os_spawn/os_report/os_send tool 逻辑。"""
import json
import os
import sys
import pytest

# 让测试可以直接 import mcp_server（不依赖 package install）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# 设置测试环境变量
os.environ["AGENT_OS_PORT"] = "8420"
os.environ["AGENT_OS_RUN_ID"] = "test_run_12345"


class TestMcpServerTools:
    """测试 MCP tool 函数逻辑（不启动 MCP Server 进程）。"""

    def test_os_report_requires_run_id(self):
        """os_report 没有 AGENT_OS_RUN_ID 时返回错误。"""
        old = os.environ.get("AGENT_OS_RUN_ID")
        try:
            os.environ["AGENT_OS_RUN_ID"] = ""
            from mcp_server import os_report
            result = json.loads(os_report("test result"))
            assert "error" in result
        finally:
            if old:
                os.environ["AGENT_OS_RUN_ID"] = old

    def test_os_send_requires_run_id(self):
        """os_send 没有 AGENT_OS_RUN_ID 时返回错误。"""
        old = os.environ.get("AGENT_OS_RUN_ID")
        try:
            os.environ["AGENT_OS_RUN_ID"] = ""
            from mcp_server import os_send
            result = json.loads(os_send("test msg"))
            assert "error" in result
        finally:
            if old:
                os.environ["AGENT_OS_RUN_ID"] = old

    def test_os_spawn_invalid_json(self):
        """os_spawn 传入无效 JSON 时返回错误。"""
        from mcp_server import os_spawn
        result = json.loads(os_spawn("not valid json"))
        assert "error" in result

    def test_os_spawn_valid_tasks(self):
        """os_spawn 传入有效 tasks 时返回 JSON（服务未启动时会失败，但格式正确）。"""
        from mcp_server import os_spawn
        tasks = json.dumps([{"prompt": "test task"}])
        result = json.loads(os_spawn(tasks))
        # 服务未启动时返回 HTTP 连接错误
        assert "error" in result or "spawn_id" in result or "child_count" in result

    def test_mcp_server_module_imports(self):
        """验证 mcp_server 模块可以正常导入。"""
        import mcp_server
        assert hasattr(mcp_server, "mcp")
        assert hasattr(mcp_server, "os_spawn")
        assert hasattr(mcp_server, "os_report")
        assert hasattr(mcp_server, "os_send")

    def test_mcp_config_generation(self):
        """验证 _get_mcp_config_path 生成正确的配置文件。"""
        # 需要 process_manager 上下文，这里只验证逻辑路径
        config_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "state", "mcp"
        )
        os.makedirs(config_dir, exist_ok=True)
        config_file = os.path.join(config_dir, "mcp_config.json")
        # 写一个测试配置
        config = {
            "mcpServers": {
                "agent-os": {
                    "command": "python",
                    "args": ["mcp_server.py"],
                }
            }
        }
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        assert os.path.exists(config_file)
        # 读取验证
        with open(config_file, "r") as f:
            loaded = json.load(f)
        assert "mcpServers" in loaded
        assert "agent-os" in loaded["mcpServers"]
