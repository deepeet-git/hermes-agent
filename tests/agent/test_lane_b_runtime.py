"""Lane B integration contracts: cache-safe routing and local-failure termination."""

from copy import deepcopy
import errno
from types import SimpleNamespace

import pytest

from agent import conversation_loop as loop
from agent.agent_init import _apply_agent_section
from agent.prompt_builder import build_skills_system_prompt


@pytest.mark.parametrize("raw, expected", [(True, True), ("yes", True), ("false", False), (1, False)])
def test_routing_config_is_opt_in_and_session_local(raw, expected):
    agent = SimpleNamespace(run_budget_seconds=None)
    _apply_agent_section(agent, {"agent": {
        "intent_aware_routing": raw, "lean_chat_fast_path": raw,
        "lean_chat_reasoning_effort": "HIGH", "environment_probe": False,
    }})
    assert agent._intent_aware_routing is expected
    assert agent._lean_chat_fast_path is expected
    assert agent._lean_chat_reasoning_effort == "low"


@pytest.mark.parametrize("message, effort", [
    ("고마워", "low"),
    ("우리 로직은 뭐야?", "high"),
    ("현재 DB 상태는 어때?", "high"),
    ("Console 적용 프로세스를 확인해줘", "high"),
])
def test_real_turn_request_keeps_tool_identity_and_cached_prefix(message, effort):
    tools = [{"type": "function", "function": {"name": "terminal", "parameters": {"type": "object"}}}]
    messages = [{"role": "system", "content": "frozen private-free prefix"}, {"role": "user", "content": message}]
    kwargs = {"tools": tools, "tool_choice": "auto", "messages": messages, "reasoning_effort": "high"}
    before = deepcopy(kwargs)
    state = SimpleNamespace(api_call_count=1, original_user_message=message, api_kwargs=kwargs)
    agent = SimpleNamespace(_lean_chat_fast_path=True, _lean_chat_reasoning_effort="low")
    loop._apply_lean_chat_turn_request(agent, state)
    assert kwargs["tools"] is tools
    assert kwargs["messages"] is messages
    assert kwargs["tools"] == before["tools"]
    assert kwargs["messages"] == before["messages"]
    assert kwargs["tool_choice"] == "auto"
    assert kwargs["reasoning_effort"] == effort


def test_tools_empty_chat_never_acquires_a_tool_choice():
    kwargs = {"reasoning_effort": "high"}
    agent = SimpleNamespace(_lean_chat_fast_path=True, _lean_chat_reasoning_effort="low")
    state = SimpleNamespace(api_call_count=1, original_user_message="thanks", api_kwargs=kwargs)
    loop._apply_lean_chat_turn_request(agent, state)
    assert kwargs == {"reasoning_effort": "low"}


def test_later_iteration_keeps_normal_reasoning():
    kwargs = {"reasoning_effort": "high"}
    agent = SimpleNamespace(_lean_chat_fast_path=True)
    state = SimpleNamespace(api_call_count=2, original_user_message="thanks", api_kwargs=kwargs)
    loop._apply_lean_chat_turn_request(agent, state)
    assert kwargs == {"reasoning_effort": "high"}


def _wrapped_descriptor_error():
    error = RuntimeError("connection failed")
    error.__cause__ = OSError(errno.EMFILE, "descriptor limit")
    return error


@pytest.mark.parametrize("error", [OSError(errno.EMFILE, "too many open files"), OSError(errno.ENFILE, "file table overflow"), _wrapped_descriptor_error()])
def test_retry_loop_fails_fast_without_provider_recovery(monkeypatch, error):
    phases = []
    def phase(fn, agent, state, **extra):
        phases.append(fn.__name__)
        if fn is loop.perform_api_call:
            raise error
        if fn is loop.handle_api_error:
            pytest.fail("Local exhaustion reached provider recovery")
        return SimpleNamespace(action="fallthrough")
    monkeypatch.setattr(loop, "_run_phase", phase)
    notices = []
    agent = SimpleNamespace(_emit_status=notices.append, _lean_chat_fast_path=False)
    state = SimpleNamespace(retry_count=0, max_retries=3, api_call_count=1, thinking_spinner=None,
                            messages=[{"role": "user", "content": "check source"}])
    result = loop._run_api_retry_loop(agent, state)
    assert result["failure_reason"] == "local_resource_exhaustion"
    assert result["failure_retryable"] is False
    assert result["failed"] is True
    assert result["messages"] is state.messages
    assert phases.count("perform_api_call") == 1
    assert notices


def test_skill_policy_cache_separates_routing_and_explicit_profile_homes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    roots = [tmp_path / "first" / "skills", tmp_path / "second" / "skills"]
    for root, name in zip(roots, ("alpha-private", "beta-private")):
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: scoped procedure\n---\nDo work.\n")
    tools = {"skill_view", "skill_manage"}
    standard = build_skills_system_prompt(available_tools=tools, skills_dir_override=roots[0])
    routed = build_skills_system_prompt(available_tools=tools, skills_dir_override=roots[0], intent_aware_routing=True)
    other = build_skills_system_prompt(available_tools=tools, skills_dir_override=roots[1], intent_aware_routing=True)
    assert "alpha-private" in standard and "alpha-private" in routed
    assert "on demand" in routed and routed != standard
    assert "beta-private" in other and "alpha-private" not in other
    assert build_skills_system_prompt(available_tools=tools, skills_dir_override=roots[0]) == standard
