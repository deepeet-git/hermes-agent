"""Aggregate recovery releases only its own stop, never caps or stronger failures."""

from agent.tool_guardrails import ToolCallGuardrailConfig, ToolCallGuardrailController


def _controller():
    return ToolCallGuardrailController(ToolCallGuardrailConfig.from_mapping({
        "hard_stop_enabled": True,
        "hard_stop_after": {"exact_failure": 1},
        "loop_caps": {"max_web_searches": 1},
    }))


def test_aggregate_recovery_does_not_refund_the_search_cap():
    guard = _controller()
    assert guard.before_call("web_search", {"query": "first"}).allows_execution
    capped = guard.before_call("web_search", {"query": "second"})
    assert capped.code == "loop_web_search_cap"
    assert guard.release_aggregate_halt_for_recovery(capped)
    assert guard.halt_decision is None
    assert guard.before_call("write_file", {"path": "/tmp/example"}).allows_execution
    assert guard.before_call("web_search", {"query": "third"}).code == "loop_web_search_cap"


def test_aggregate_recovery_cannot_clear_a_later_stronger_failure():
    guard = _controller()
    guard.before_call("web_search", {"query": "first"})
    capped = guard.before_call("web_search", {"query": "second"})
    args = {"command": "synthetic-command"}
    guard.after_call("terminal", args, '{"error": "synthetic failure"}', failed=True)
    stronger = guard.before_call("terminal", args)
    assert stronger.should_halt
    assert not stronger.allows_recovery_iteration
    assert not guard.release_aggregate_halt_for_recovery(capped)
    assert guard.halt_decision is stronger
    assert not guard.release_aggregate_halt_for_recovery(stronger)
