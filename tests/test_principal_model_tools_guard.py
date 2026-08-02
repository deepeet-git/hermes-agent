import json
from unittest.mock import patch

from model_tools import handle_function_call


def test_direct_dispatch_rejects_tool_outside_enabled_toolsets() -> None:
    with patch("model_tools.registry.dispatch") as dispatch:
        result = json.loads(
            handle_function_call(
                "terminal",
                {"command": "true"},
                enabled_toolsets=["web"],
            )
        )

    assert "not available in this session" in result["error"]
    dispatch.assert_not_called()


def test_empty_enabled_toolsets_deny_all_direct_dispatch() -> None:
    with patch("model_tools.registry.dispatch") as dispatch:
        result = json.loads(
            handle_function_call(
                "terminal",
                {"command": "true"},
                enabled_toolsets=[],
            )
        )

    assert "not available in this session" in result["error"]
    dispatch.assert_not_called()
