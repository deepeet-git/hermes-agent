import json
from types import SimpleNamespace

from agent.agent_runtime_helpers import invoke_tool


def test_restricted_agent_rejects_tool_missing_from_assembled_surface() -> None:
    agent = SimpleNamespace(
        enabled_toolsets=["web"],
        valid_tool_names={"web_search", "web_extract"},
    )

    result = json.loads(invoke_tool(agent, "terminal", {"command": "true"}, "task"))

    assert "not available in this session" in result["error"]
