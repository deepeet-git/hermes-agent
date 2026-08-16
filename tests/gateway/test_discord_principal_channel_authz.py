"""Discord 채널별 principal allowlist의 Gateway 권한 회귀 테스트."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource


def _runner(policy, *, verified_parent=False):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=True,
                extra={"principal_toolsets": policy},
            )
        }
    )
    adapter = SimpleNamespace(
        config=runner.config.platforms[Platform.DISCORD],
        verified_parent_chat_id=(
            lambda source: source.parent_chat_id if verified_parent else None
        ),
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    return runner


def _clear_auth_env(monkeypatch):
    for key in (
        "DISCORD_ALLOWED_USERS",
        "DISCORD_ALLOW_ALL_USERS",
        "GATEWAY_ALLOWED_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)


def _policy():
    return {
        "owner_user_ids": ["owner"],
        "scope_ids": ["guild-1"],
        "channels": {
            "pdp-channel": {
                "owner": ["deepeet-pdp", "web"],
                "regular": ["deepeet-pdp", "web"],
                "allowed_user_ids": ["owner", "jhm"],
            }
        },
    }


def test_trusted_heimdall_intake_is_not_erased_by_generic_principal_toolsets():
    """The dedicated verified intake clamp, not a human channel rule, is authoritative."""
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="incident-thread",
        parent_chat_id="error-alert",
        thread_id="incident-thread",
        chat_type="thread",
        user_id="trusted-webhook-author",
        scope_id="guild-1",
        is_bot=True,
    )
    setattr(source, "_trusted_heimdall_incident", True)

    platform_toolsets = ["terminal", "file", "delegation", "web"]
    effective = _runner(_policy())._principal_effective_toolsets(
        source,
        platform_toolsets,
    )

    assert effective == platform_toolsets


def test_discord_channel_principal_allowlist_authorizes_regular_user(monkeypatch):
    _clear_auth_env(monkeypatch)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="pdp-channel",
        chat_type="channel",
        user_id="jhm",
        scope_id="guild-1",
    )

    assert _runner(_policy())._is_user_authorized(source) is True


def test_discord_channel_without_allowlist_authorizes_restricted_regular_role(monkeypatch):
    _clear_auth_env(monkeypatch)
    policy = _policy()
    policy["channels"]["pdp-channel"].pop("allowed_user_ids")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="pdp-channel",
        chat_type="channel",
        user_id="regular-user",
        scope_id="guild-1",
    )

    runner = _runner(policy)
    assert runner._is_user_authorized(source) is True
    assert runner._principal_effective_toolsets(
        source, ["terminal", "deepeet-pdp", "web"]
    ) == ["deepeet-pdp", "web"]


def test_discord_channel_without_allowlist_or_regular_capability_denies(monkeypatch):
    _clear_auth_env(monkeypatch)
    policy = _policy()
    policy["channels"]["pdp-channel"].pop("allowed_user_ids")
    policy["channels"]["pdp-channel"].pop("regular")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="pdp-channel",
        chat_type="channel",
        user_id="regular-user",
        scope_id="guild-1",
    )

    runner = _runner(policy)
    assert runner._is_user_authorized(source) is False
    assert runner._principal_effective_toolsets(
        source, ["deepeet-pdp", "web"]
    ) == []


def test_discord_thread_inherits_verified_parent_principal_allowlist(monkeypatch):
    _clear_auth_env(monkeypatch)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        thread_id="thread-1",
        parent_chat_id="pdp-channel",
        chat_type="thread",
        user_id="jhm",
        scope_id="guild-1",
    )

    assert _runner(_policy(), verified_parent=True)._is_user_authorized(source) is True


def test_adapter_history_auth_rebuilds_verified_discord_thread_context(monkeypatch):
    """Fetched Discord history must not downgrade an approved user to unverified."""
    _clear_auth_env(monkeypatch)
    runner = _runner(_policy(), verified_parent=True)
    adapter = runner.adapters[Platform.DISCORD]
    setattr(
        adapter,
        "authorization_context_for_chat",
        lambda chat_id: {
            "chat_id": chat_id,
            "chat_type": "thread",
            "thread_id": chat_id,
            "parent_chat_id": "pdp-channel",
            "scope_id": "guild-1",
        },
    )

    check = runner._make_adapter_auth_check(Platform.DISCORD)

    assert check("jhm", "thread", "thread-1") is True


def test_discord_thread_parent_requires_live_adapter_verification(monkeypatch):
    _clear_auth_env(monkeypatch)
    live_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        thread_id="thread-1",
        parent_chat_id="pdp-channel",
        chat_type="thread",
        user_id="jhm",
        scope_id="guild-1",
    )
    source = SessionSource.from_dict(live_source.to_dict())

    assert _runner(_policy())._is_user_authorized(source) is False
    assert _runner(_policy(), verified_parent=True)._is_user_authorized(source) is True


def test_discord_channel_principal_allowlist_does_not_authorize_dm(monkeypatch):
    _clear_auth_env(monkeypatch)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="dm-1",
        chat_type="dm",
        user_id="jhm",
    )

    assert _runner(_policy())._is_user_authorized(source) is False


def test_discord_channel_principal_allowlist_rejects_wrong_scope(monkeypatch):
    _clear_auth_env(monkeypatch)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="pdp-channel",
        chat_type="channel",
        user_id="jhm",
        scope_id="guild-2",
    )

    assert _runner(_policy())._is_user_authorized(source) is False


def test_principal_policy_denial_cannot_fall_through_legacy_allow_all(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("DISCORD_ALLOW_ALL_USERS", "true")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="pdp-channel",
        chat_type="channel",
        user_id="attacker",
        scope_id="guild-1",
    )

    assert _runner(_policy())._is_user_authorized(source) is False


def test_regular_principal_cannot_inherit_owner_toolsets(monkeypatch):
    _clear_auth_env(monkeypatch)
    policy = _policy()
    policy["channels"]["pdp-channel"]["regular"] = "inherit"
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="pdp-channel",
        chat_type="channel",
        user_id="jhm",
        scope_id="guild-1",
    )

    runner = _runner(policy)
    assert runner._is_user_authorized(source) is False
    assert runner._principal_effective_toolsets(
        source, ["terminal", "file", "deepeet-pdp", "web"]
    ) == []


def test_principal_policy_rejects_wildcard_user_and_scope(monkeypatch):
    _clear_auth_env(monkeypatch)
    policy = _policy()
    policy["scope_ids"] = ["*"]
    policy["channels"]["pdp-channel"]["allowed_user_ids"] = ["*"]
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="pdp-channel",
        chat_type="channel",
        user_id="attacker",
        scope_id="guild-2",
    )

    runner = _runner(policy)
    assert runner._is_user_authorized(source) is False
    assert runner._principal_effective_toolsets(source, ["deepeet-pdp", "web"]) == []


def test_principal_policy_requires_nonempty_scope_ids(monkeypatch):
    _clear_auth_env(monkeypatch)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="pdp-channel",
        chat_type="channel",
        user_id="jhm",
        scope_id="guild-1",
    )

    for raw_scopes in (None, []):
        policy = _policy()
        if raw_scopes is None:
            policy.pop("scope_ids")
        else:
            policy["scope_ids"] = raw_scopes
        runner = _runner(policy)
        assert runner._is_user_authorized(source) is False
        assert runner._principal_effective_toolsets(
            source, ["deepeet-pdp", "web"]
        ) == []


def test_principal_policy_rejects_unknown_chat_type(monkeypatch):
    _clear_auth_env(monkeypatch)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="pdp-channel",
        chat_type="mystery",
        user_id="jhm",
        scope_id="guild-1",
    )

    runner = _runner(_policy())
    assert runner._is_user_authorized(source) is False
    assert runner._principal_effective_toolsets(
        source, ["deepeet-pdp", "web"]
    ) == []


def test_discord_regular_principal_receives_only_channel_toolsets():
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="pdp-channel",
        chat_type="channel",
        user_id="jhm",
        scope_id="guild-1",
    )

    effective = _runner(_policy())._principal_effective_toolsets(
        source,
        ["terminal", "file", "deepeet-pdp", "web"],
    )

    assert effective == ["deepeet-pdp", "web"]


def test_discord_unlisted_principal_receives_no_channel_toolsets():
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="pdp-channel",
        chat_type="channel",
        user_id="attacker",
        scope_id="guild-1",
    )

    effective = _runner(_policy())._principal_effective_toolsets(
        source,
        ["terminal", "file", "deepeet-pdp", "web"],
    )

    assert effective == []


def test_principal_policy_falls_back_to_gateway_config_when_adapter_missing():
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="pdp-channel",
        chat_type="channel",
        user_id="jhm",
        scope_id="guild-1",
    )
    runner = _runner(_policy())
    runner.adapters = {}

    effective = runner._principal_effective_toolsets(
        source,
        ["terminal", "file", "deepeet-pdp", "web"],
    )

    assert effective == ["deepeet-pdp", "web"]


def test_discord_owner_inherit_preserves_platform_toolsets():
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="pdp-channel",
        chat_type="channel",
        user_id="owner",
        scope_id="guild-1",
    )
    policy = _policy()
    policy["channels"]["pdp-channel"]["owner"] = "inherit"

    effective = _runner(policy)._principal_effective_toolsets(
        source,
        ["terminal", "file", "deepeet-pdp", "web"],
    )

    assert effective == ["terminal", "file", "deepeet-pdp", "web"]


def test_discord_regular_dm_is_explicit_deny_all():
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="dm-1",
        chat_type="dm",
        user_id="jhm",
    )

    effective = _runner(_policy())._principal_effective_toolsets(
        source,
        ["terminal", "file", "deepeet-pdp", "web"],
    )

    assert effective == []


def test_discord_principal_policy_blocks_proxy_dispatch():
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="pdp-channel",
        chat_type="group",
        user_id="jhm",
        scope_id="guild-1",
    )

    assert _runner(_policy())._principal_policy_blocks_proxy(source) is True


def test_proxy_dispatch_is_not_called_for_principal_policy():
    import asyncio
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._get_proxy_url = MagicMock(return_value="https://proxy.invalid")
    runner._principal_policy_blocks_proxy = MagicMock(return_value=True)
    runner._run_agent_via_proxy = AsyncMock()
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="pdp-channel",
        chat_type="group",
        user_id="jhm",
        scope_id="guild-1",
    )

    result = asyncio.run(
        runner._run_agent_inner(
            message="make pdp",
            context_prompt="",
            history=[],
            source=source,
            session_id="session-1",
        )
    )

    assert "Proxy mode is disabled" in result["final_response"]
    runner._run_agent_via_proxy.assert_not_awaited()


def test_discord_thread_toolsets_require_verified_parent():
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        thread_id="thread-1",
        parent_chat_id="pdp-channel",
        chat_type="thread",
        user_id="jhm",
        scope_id="guild-1",
    )

    effective = _runner(_policy())._principal_effective_toolsets(
        source,
        ["terminal", "file", "deepeet-pdp", "web"],
    )

    assert effective == []


def test_verified_thread_ignores_thread_specific_intake_rule(monkeypatch):
    _clear_auth_env(monkeypatch)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        thread_id="thread-1",
        parent_chat_id="pdp-channel",
        chat_type="thread",
        user_id="attacker",
        scope_id="guild-1",
    )
    policy = _policy()
    policy["channels"]["thread-1"] = {
        "owner": "inherit",
        "regular": ["terminal", "file"],
        "allowed_user_ids": ["attacker"],
    }

    assert _runner(policy, verified_parent=True)._is_user_authorized(source) is False


def test_verified_thread_uses_parent_toolsets_not_thread_override():
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        thread_id="thread-1",
        parent_chat_id="pdp-channel",
        chat_type="thread",
        user_id="jhm",
        scope_id="guild-1",
    )
    policy = _policy()
    policy["channels"]["thread-1"] = {
        "owner": "inherit",
        "regular": "inherit",
        "allowed_user_ids": ["jhm"],
    }

    effective = _runner(policy, verified_parent=True)._principal_effective_toolsets(
        source,
        ["terminal", "file", "deepeet-pdp", "web"],
    )

    assert effective == ["deepeet-pdp", "web"]


def test_discord_adapter_revalidates_parent_and_scope_from_live_cache(monkeypatch):
    from plugins.platforms.discord import adapter as discord_adapter_module

    class FakeThread:
        def __init__(self, *, channel_id, parent_id, guild_id):
            self.id = channel_id
            self.parent_id = parent_id
            self.guild = SimpleNamespace(id=guild_id)

    monkeypatch.setattr(discord_adapter_module.discord, "Thread", FakeThread)
    adapter = object.__new__(discord_adapter_module.DiscordAdapter)
    channel = FakeThread(channel_id=789, parent_id=123, guild_id=456)
    adapter._client = SimpleNamespace(get_channel=lambda channel_id: channel)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="789",
        thread_id="789",
        parent_chat_id="123",
        chat_type="thread",
        user_id="jhm",
        scope_id="456",
    )

    assert adapter.authorization_context_for_chat("789") == {
        "chat_id": "789",
        "chat_type": "thread",
        "scope_id": "456",
        "thread_id": "789",
        "parent_chat_id": "123",
    }
    assert adapter.verified_parent_chat_id(source) == "123"

    channel.parent_id = 999
    assert adapter.verified_parent_chat_id(source) is None

    channel.parent_id = 123
    channel.guild.id = 999
    assert adapter.verified_parent_chat_id(source) is None

    adapter._client = SimpleNamespace(get_channel=lambda channel_id: SimpleNamespace())
    assert adapter.verified_parent_chat_id(source) is None
