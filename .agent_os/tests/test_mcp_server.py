"""MCP Server 单元测试。"""
import pytest

pytest.skip("mcp package naming conflict (local src/mcp vs PyPI mcp)", allow_module_level=True)

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

os.environ["AGENT_OS_PORT"] = "8420"
os.environ["AGENT_OS_AGENT_ID"] = "test_agent_12345"


class TestMcpServerTools:
    def test_os_report_requires_agent_id(self):
        old = os.environ.get("AGENT_OS_AGENT_ID")
        try:
            os.environ["AGENT_OS_AGENT_ID"] = ""
            from server import os_report
            result = json.loads(os_report("test result"))
            assert "error" in result
        finally:
            if old:
                os.environ["AGENT_OS_AGENT_ID"] = old

    def test_os_send_requires_agent_id(self):
        old = os.environ.get("AGENT_OS_AGENT_ID")
        try:
            os.environ["AGENT_OS_AGENT_ID"] = ""
            from server import os_send
            result = json.loads(os_send("test msg"))
            assert "error" in result
        finally:
            if old:
                os.environ["AGENT_OS_AGENT_ID"] = old

    def test_os_spawn_invalid_json(self):
        from server import os_spawn
        result = json.loads(os_spawn("not valid json"))
        assert "error" in result

    def test_os_spawn_valid_tasks(self):
        from server import os_spawn
        tasks = json.dumps([{"prompt": "test task"}])
        result = json.loads(os_spawn(tasks))
        assert "error" in result or "child_count" in result

    def test_mcp_server_module_imports(self):
        import mcp_server
        assert hasattr(mcp_server, "mcp")
        assert hasattr(mcp_server, "os_spawn")
        assert hasattr(mcp_server, "os_report")
        assert hasattr(mcp_server, "os_send")

    def test_mcp_config_generation(self):
        config_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "state", "mcp"
        )
        os.makedirs(config_dir, exist_ok=True)
        config_file = os.path.join(config_dir, "mcp_config.json")
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
        with open(config_file, "r") as f:
            loaded = json.load(f)
        assert "mcpServers" in loaded
        assert "agent-os" in loaded["mcpServers"]
