"""Exercise principal ceilings at production construction, cache and auxiliary seams."""
from collections import OrderedDict
from types import SimpleNamespace
import threading
import weakref
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext


class NativeAdapter:
    platform = Platform.DISCORD

    def __init__(self, extra):
        self.config = PlatformConfig(enabled=True, extra=extra)

    def verified_parent_chat_id(self, source):
        return getattr(source, "_validated_parent_chat_id", None)


def policy(regular=None):
    return {"scope_ids": ["guild"], "owner_user_ids": ["owner-a", "owner-b"],
            "channels": {"channel": {"owner": "inherit", "regular": regular or []}},
            "dm": {"owner": "inherit", "regular": []}}


def setup_runtime(user="regular", extra=None):
    runner = object.__new__(GatewayRunner)
    adapter = NativeAdapter(extra if extra is not None else {"principal_toolsets": policy(["web"])})
    runner.config = GatewayConfig(platforms={Platform.DISCORD: adapter.config})
    runner.adapters = {Platform.DISCORD: adapter}
    runner._primary_profile_name = "default"
    runner._prefill_messages = None
    runner._service_tier = None
    runner._session_db = None
    runner._refresh_fallback_model = lambda: None
    runner._resolve_enabled_toolsets_for_source = lambda *args: ["terminal", "web", "memory"]
    source = SessionSource(platform=Platform.DISCORD, chat_id="channel", chat_type="channel",
                           user_id=user, scope_id="guild")
    source._transport_adapter_ref = weakref.ref(adapter)
    return runner, source, adapter


@pytest.mark.parametrize("user,expected,restricted", [
    ("owner-a", ["memory", "terminal", "web"], False),
    ("owner-b", ["memory", "terminal", "web"], False),
    ("regular", ["web"], True),
])
def test_display_applies_final_ceiling_and_private_context(monkeypatch, user, expected, restricted):
    runner, source, _adapter = setup_runtime(user)
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda *args: {})
    disp = runner._run_agent_display_settings(source)
    assert sorted(disp.enabled_toolsets) == expected
    assert disp.restrict_private_context is restricted


@pytest.mark.parametrize("shape", ["root", "platforms", "gateway"])
def test_raw_root_and_nested_policy_bridge_remains_effective(shape):
    runner, source, _adapter = setup_runtime(extra={})
    block = {"principal_toolsets": policy(["web"])}
    config = {"discord": block} if shape == "root" else (
        {"platforms": {"discord": {"extra": block}}} if shape == "platforms" else
        {"gateway": {"platforms": {"discord": block}}})
    assert runner._principal_runtime_ceiling(config, source, ["web", "terminal"]) == (["web"], True)


def test_transport_home_policy_never_borrows_routed_owner_policy(tmp_path, monkeypatch):
    runner, source, adapter = setup_runtime("owner-a", extra={})
    transport_home = tmp_path / "transport"
    transport_home.mkdir()
    (transport_home / "config.yaml").write_text("discord:\n  principal_toolsets: {}\n")
    monkeypatch.setattr(runner, "_authorization_home_for_source", lambda src: transport_home)
    owner_config = {"discord": {"principal_toolsets": policy()}}
    assert runner._principal_runtime_ceiling(owner_config, source, ["terminal"]) == ([], True)
    assert source._transport_adapter_ref() is adapter


@pytest.mark.parametrize("mode", ["serialized", "relay", "wrong-scope", "unverified-thread"])
def test_untrusted_or_wrong_scope_source_has_no_tools_or_private_context(mode):
    runner, source, _adapter = setup_runtime("owner-a")
    if mode == "serialized":
        source = SessionSource.from_dict(source.to_dict())
    elif mode == "relay":
        source.delivered_via_upstream_relay = True
    elif mode == "wrong-scope":
        source.scope_id = "other-guild"
    else:
        source.chat_type, source.thread_id, source.parent_chat_id = "thread", "thread", "channel"
    assert runner._principal_runtime_ceiling({}, source, []) == ([], True)


def test_session_policy_override_cannot_expand_principal_ceiling(monkeypatch):
    runner, source, _adapter = setup_runtime()
    session_policy = SimpleNamespace(platform="discord", config=lambda *a, **kw: {}, ignore_rules=False)
    monkeypatch.setattr("gateway.session_policy.policy_for_source", lambda *a: session_policy)
    monkeypatch.setattr("gateway.session_authorities.active_authority", lambda *a: None)
    monkeypatch.setattr("gateway.session_policy.policy_turn_toolsets", lambda *a: ["terminal", "web"])
    disp = runner._run_agent_display_settings(source)
    assert disp.enabled_toolsets == ["web"]
    assert disp.restrict_private_context


def make_turn(runner, source, restricted=True, factory=None):
    ctx = TurnContext(source=source, message="hello", session_id="session", session_key="same-key",
                      user_config={}, enabled_toolsets=[], restrict_private_context=restricted,
                      AIAgent=factory or (lambda **kw: SimpleNamespace(**kw)))
    return TurnRunner(runner, ctx)


def test_fresh_construction_rejects_private_prompt_and_memory():
    runner, source, _adapter = setup_runtime()
    turn = make_turn(runner, source)
    agent = turn._build_fresh_agent({"model": "test-model", "runtime": {}}, "discord", "", 4, None, {}, False)
    assert agent.skip_memory and agent.skip_context_files and not agent.load_soul_identity
    assert agent._reject_stored_system_prompt
    assert agent.enabled_toolsets == []


def test_regular_listing_every_tool_still_excludes_private_context():
    runner, source, _adapter = setup_runtime(extra={"principal_toolsets": policy(["terminal", "web", "memory"])})
    tools, restricted = runner._principal_runtime_ceiling({}, source, ["terminal", "web", "memory"])
    assert sorted(tools) == ["memory", "terminal", "web"]
    assert restricted


def test_empty_adapter_override_is_an_explicit_tools_empty_surface():
    runner, source, adapter = setup_runtime("owner-a")
    del runner._resolve_enabled_toolsets_for_source
    adapter.toolsets_for_source = lambda source: []
    assert runner._resolve_turn_toolsets({}, source, "discord")[0] == []


def test_auxiliary_honors_session_no_tools_and_ignore_rules(monkeypatch):
    runner, source, _adapter = setup_runtime("owner-a")
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda *a: {})
    monkeypatch.setattr("gateway.session_policy.policy_for_source", lambda *a: SimpleNamespace(ignore_rules=True))
    monkeypatch.setattr("gateway.session_authorities.active_authority", lambda *a: None)
    monkeypatch.setattr("gateway.session_policy.policy_turn_toolsets", lambda *a: [])
    settings, restricted = runner._principal_auxiliary_settings(source, False)
    assert settings["enabled_toolsets"] == []
    assert restricted and settings["skip_memory"] and settings["skip_context_files"]


@pytest.mark.asyncio
async def test_raw_policy_refuses_proxy_before_remote_dispatch(monkeypatch):
    runner, source, _adapter = setup_runtime(extra={})
    runner._get_proxy_url = lambda: "https://proxy.invalid"
    runner._run_agent_via_proxy = AsyncMock()
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda *a: {"discord": {"principal_toolsets": policy()}})
    result = await runner._run_agent_inner("hello", "", [], source, "session")
    assert "Proxy mode is disabled" in result["final_response"]
    runner._run_agent_via_proxy.assert_not_awaited()


def test_real_agent_construction_is_tools_empty_and_context_isolated(tmp_path, monkeypatch):
    from run_agent import AIAgent
    runner, source, _adapter = setup_runtime()
    (tmp_path / "SOUL.md").write_text("PRIVATE_OWNER_MARKER")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    turn = make_turn(runner, source, factory=AIAgent)
    agent = turn._build_fresh_agent(
        {"model": "gpt-4o-mini", "runtime": {"provider": "openai", "api_key": "test-only",
         "base_url": "http://127.0.0.1:9/v1"}}, "discord", "", 2, None, {}, False)
    try:
        assert not agent.tools
        assert agent._reject_stored_system_prompt
        assert "PRIVATE_OWNER_MARKER" not in str(getattr(agent, "_cached_system_prompt", ""))
    finally:
        agent.close()


def test_same_tool_list_cache_rebuilds_when_private_context_changes(monkeypatch):
    runner, source, _adapter = setup_runtime("owner-a")
    runner._agent_cache_lock = threading.Lock()
    runner._agent_cache = OrderedDict()
    runner._extract_cache_busting_config = lambda config: {}
    runner._enforce_agent_cache_cap = lambda: None
    runner._init_cached_agent_for_turn = lambda *args: None
    runner._apply_fallback_chain_to_agent = lambda *args: None
    turn = make_turn(runner, source, restricted=False)
    route = {"model": "test-model", "runtime": {}}
    first, reused = turn._resolve_turn_agent(route, "discord", "", 4, None, {})
    assert not reused and not first.skip_memory
    second, reused = turn._resolve_turn_agent(route, "discord", "", 4, None, {})
    assert reused and second is first
    turn._ctx.restrict_private_context = True
    third, reused = turn._resolve_turn_agent(route, "discord", "", 4, None, {})
    assert not reused and third is not first and third.skip_memory


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["manual", "hygiene"])
@pytest.mark.parametrize("user,restricted", [("regular", True), ("owner-a", False)])
async def test_auxiliary_construction_inherits_ceiling_and_prompt(monkeypatch, kind, user, restricted):
    runner, source, _adapter = setup_runtime(user)
    runner._session_db = SimpleNamespace(get_session=AsyncMock(return_value={"system_prompt": "OWNER_PRIVATE"}))
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda *a: {})
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"compression": {"checkpoint_required": True}} if not restricted else {})
    built = []
    def construct(**kwargs):
        agent = SimpleNamespace(**kwargs)
        built.append(agent)
        return agent
    monkeypatch.setattr("run_agent.AIAgent", construct)
    if kind == "manual":
        agent = await runner._build_manual_compression_agent("session", "test-model", {}, source=source)
    else:
        agent, _db = await runner._hmwa_hygiene_build_agent("test-model", {}, SimpleNamespace(session_id="session"), source=source)
    assert agent.skip_memory is restricted
    assert agent._reject_stored_system_prompt is restricted
    assert agent.enabled_toolsets == ([] if restricted else ["memory"])
    assert ("OWNER_PRIVATE" in str(getattr(agent, "_cached_system_prompt", ""))) is (not restricted)


def test_required_checkpoint_is_refused_not_silently_removed(monkeypatch):
    runner, source, _adapter = setup_runtime()
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda *a: {})
    with pytest.raises(RuntimeError, match="Required compression checkpoint"):
        runner._principal_auxiliary_settings(source, True)


@pytest.mark.asyncio
async def test_background_agent_construction_is_restricted(monkeypatch):
    runner, source, adapter = setup_runtime()
    adapter.send = AsyncMock()
    runner._resolve_session_agent_runtime = lambda **kw: ("test-model", {"api_key": "test-only"})
    runner._provider_routing = {}
    runner._resolve_session_reasoning_config = lambda **kw: None
    runner._resolve_session_service_tier = lambda **kw: None
    runner._resolve_turn_agent_config = lambda *a: {"model": "test-model", "runtime": {}}
    runner._cleanup_agent_resources = MagicMock()
    async def execute(fn):
        return fn()
    runner._run_in_executor_with_context = execute
    built = []
    def construct(**kwargs):
        agent = SimpleNamespace(**kwargs, run_conversation=lambda **kw: {})
        built.append(agent)
        return agent
    monkeypatch.setattr("run_agent.AIAgent", construct)
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda *a: {})
    await runner._run_background_task_inner("hello", source, "background")
    assert len(built) == 1
    assert built[0].enabled_toolsets == ["web"]
    assert built[0].skip_memory and built[0].skip_context_files and built[0]._reject_stored_system_prompt
