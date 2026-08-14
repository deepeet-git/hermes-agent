import weakref

from gateway.run import (
    _principal_context_is_restricted,
    _resolve_principal_enabled_toolsets,
)
from gateway.session import Platform, SessionSource


class _DiscordTransport:
    platform = Platform.DISCORD


def _stamp_native_discord(source: SessionSource) -> _DiscordTransport:
    transport = _DiscordTransport()
    setattr(source, "_transport_adapter_ref", weakref.ref(transport))
    return transport


def test_gateway_toolset_bridge_uses_trusted_discord_source(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_DISCORD_PRINCIPAL_TOOLSETS_ENABLED", "true")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="general",
        chat_type="channel",
        user_id="regular-user",
        scope_id="guild",
    )
    transport = _stamp_native_discord(source)
    config = {
        "discord": {
            "principal_toolsets": {
                "owner_user_ids": ["owner"],
                "scope_ids": ["guild"],
                "dm": {"owner": "inherit", "regular": []},
                "channels": {
                    "general": {
                        "owner": "inherit",
                        "regular": ["web", "clarify"],
                    }
                },
            }
        }
    }

    actual = _resolve_principal_enabled_toolsets(
        user_config=config,
        source=source,
        platform_toolsets=["terminal", "web", "clarify"],
    )

    assert actual == ["clarify", "web"]
    assert transport.platform == Platform.DISCORD


def test_serialized_discord_source_without_live_transport_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_DISCORD_PRINCIPAL_TOOLSETS_ENABLED", "true")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="general",
        chat_type="channel",
        user_id="owner",
        scope_id="guild",
    )

    actual = _resolve_principal_enabled_toolsets(
        user_config={"discord": {"principal_toolsets": {}}},
        source=source,
        platform_toolsets=["terminal", "web"],
    )

    assert actual == []


def test_trusted_heimdall_bot_uses_only_its_explicit_toolset_clamp(monkeypatch) -> None:
    """The native intake marker cannot inherit owner tools or private context."""
    monkeypatch.delenv("HERMES_DISCORD_PRINCIPAL_TOOLSETS_ENABLED", raising=False)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="incident-thread",
        parent_chat_id="error-alert",
        thread_id="incident-thread",
        chat_type="thread",
        user_id="trusted-webhook-author",
        scope_id="guild",
        is_bot=True,
    )
    transport = _stamp_native_discord(source)
    setattr(source, "_validated_parent_chat_id", "error-alert")
    setattr(source, "_trusted_heimdall_incident", True)
    config = {
        "discord": {
            "heimdall_incident_intake": {"toolsets": ["safe", "terminal"]},
        }
    }

    actual = _resolve_principal_enabled_toolsets(
        user_config=config,
        source=source,
        platform_toolsets=["safe", "terminal", "web"],
    )

    assert actual == ["safe", "terminal"]
    assert _principal_context_is_restricted(
        source=source,
        platform_toolsets=["safe", "terminal"],
        enabled_toolsets=actual,
        user_config=config,
    ) is True
    assert transport.platform == Platform.DISCORD


def test_unnormalized_relay_source_fails_closed_when_feature_enabled(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_DISCORD_PRINCIPAL_TOOLSETS_ENABLED", "true")
    source = SessionSource(
        platform=Platform.RELAY,
        chat_id="general",
        chat_type="channel",
        user_id="owner",
        scope_id="guild",
        delivered_via_upstream_relay=True,
    )

    actual = _resolve_principal_enabled_toolsets(
        user_config={},
        source=source,
        platform_toolsets=["terminal", "web"],
    )

    assert actual == []


def test_configured_policy_stays_enabled_if_env_flag_is_missing(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_DISCORD_PRINCIPAL_TOOLSETS_ENABLED", raising=False)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="general",
        chat_type="channel",
        user_id="regular-user",
        scope_id="guild",
    )
    transport = _stamp_native_discord(source)
    config = {
        "discord": {
            "principal_toolsets": {
                "owner_user_ids": ["owner"],
                "scope_ids": ["guild"],
                "dm": {"owner": "inherit", "regular": []},
                "channels": {
                    "general": {"owner": "inherit", "regular": ["web"]}
                },
            }
        }
    }

    actual = _resolve_principal_enabled_toolsets(
        user_config=config,
        source=source,
        platform_toolsets=["terminal", "web"],
    )

    assert actual == ["web"]
    assert transport.platform == Platform.DISCORD


def test_explicit_false_env_flag_rolls_back_to_legacy_toolsets(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_DISCORD_PRINCIPAL_TOOLSETS_ENABLED", "false")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="general",
        chat_type="channel",
        user_id="regular-user",
        scope_id="guild",
    )

    actual = _resolve_principal_enabled_toolsets(
        user_config={"discord": {"principal_toolsets": {"malformed": True}}},
        source=source,
        platform_toolsets=["terminal", "web"],
    )

    assert actual == ["terminal", "web"]


def test_native_thread_missing_parent_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_DISCORD_PRINCIPAL_TOOLSETS_ENABLED", "true")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread",
        chat_type="thread",
        thread_id="thread",
        user_id="owner",
        scope_id="guild",
        parent_chat_id=None,
    )
    transport = _stamp_native_discord(source)

    actual = _resolve_principal_enabled_toolsets(
        user_config={"discord": {"principal_toolsets": {}}},
        source=source,
        platform_toolsets=["terminal", "web"],
    )

    assert actual == []
    assert transport.platform == Platform.DISCORD


def test_scoped_discord_turn_suppresses_private_context() -> None:
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="general",
        chat_type="group",
        user_id="regular",
    )

    assert _principal_context_is_restricted(
        source=source,
        platform_toolsets=["terminal", "web"],
        enabled_toolsets=["web"],
    ) is True
    assert _principal_context_is_restricted(
        source=source,
        platform_toolsets=["terminal", "web"],
        enabled_toolsets=["terminal", "web"],
    ) is False


def test_configured_regular_with_all_tools_still_suppresses_private_context() -> None:
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="general",
        chat_type="group",
        user_id="regular",
    )
    config = {
        "discord": {
            "principal_toolsets": {
                "owner_user_ids": ["owner"],
                "channels": {
                    "general": {
                        "owner": "inherit",
                        "regular": ["terminal", "web"],
                    }
                },
            }
        }
    }

    assert _principal_context_is_restricted(
        source=source,
        platform_toolsets=["terminal", "web"],
        enabled_toolsets=["terminal", "web"],
        user_config=config,
    ) is True


def test_only_owner_inherit_retains_private_context() -> None:
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="general",
        chat_type="group",
        user_id="owner",
    )
    config = {
        "discord": {
            "principal_toolsets": {
                "owner_user_ids": ["owner"],
                "channels": {
                    "general": {
                        "owner": "inherit",
                        "regular": ["web"],
                    }
                },
            }
        }
    }

    assert _principal_context_is_restricted(
        source=source,
        platform_toolsets=["terminal", "web"],
        enabled_toolsets=["terminal", "web"],
        user_config=config,
    ) is False
    config["discord"]["principal_toolsets"]["channels"]["general"]["owner"] = [
        "terminal",
        "web",
    ]
    assert _principal_context_is_restricted(
        source=source,
        platform_toolsets=["terminal", "web"],
        enabled_toolsets=["terminal", "web"],
        user_config=config,
    ) is True


def test_non_discord_toolset_difference_does_not_suppress_context() -> None:
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="dm",
        chat_type="dm",
        user_id="owner",
    )

    assert _principal_context_is_restricted(
        source=source,
        platform_toolsets=["terminal", "web"],
        enabled_toolsets=["web"],
    ) is False


def test_native_thread_requires_matching_nonserialized_parent_provenance(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_DISCORD_PRINCIPAL_TOOLSETS_ENABLED", "true")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread",
        chat_type="thread",
        thread_id="thread",
        user_id="regular-user",
        scope_id="guild",
        parent_chat_id="forged-parent",
    )
    transport = _stamp_native_discord(source)
    setattr(source, "_validated_parent_chat_id", "pdp")
    config = {
        "discord": {
            "principal_toolsets": {
                "owner_user_ids": ["owner"],
                "scope_ids": ["guild"],
                "dm": {"owner": "inherit", "regular": []},
                "channels": {
                    "pdp": {"owner": ["web"], "regular": ["web"]}
                },
            }
        }
    }

    denied = _resolve_principal_enabled_toolsets(
        user_config=config,
        source=source,
        platform_toolsets=["terminal", "web"],
    )
    source.parent_chat_id = "pdp"
    allowed = _resolve_principal_enabled_toolsets(
        user_config=config,
        source=source,
        platform_toolsets=["terminal", "web"],
    )

    assert denied == []
    assert allowed == ["web"]
    assert transport.platform == Platform.DISCORD
