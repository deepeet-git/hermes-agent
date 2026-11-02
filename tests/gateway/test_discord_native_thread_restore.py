"""Discord native FIFO replay must revalidate thread authority on the live adapter."""

from copy import deepcopy
import weakref

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import (
    GatewayRunner,
    _principal_context_is_restricted,
    _resolve_principal_enabled_toolsets,
)
from gateway.session import SessionSource
from gateway.session_authority import initialize_session_authority
from gateway.session_envelope import prepare_native, restore_native_source
from gateway.session_ingress_context import native_callback


def _policy():
    return {
        "scope_ids": ["guild"],
        "owner_user_ids": ["owner-a", "owner-b"],
        "channels": {"channel": {"owner": "inherit", "regular": []}},
        "dm": {"owner": "inherit", "regular": []},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["owner-a", "owner-b"])
async def test_native_thread_restore_revalidates_both_owners_for_tools_and_private_context(
    tmp_path, monkeypatch, owner
):
    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setattr(GatewayRunner, "_VOICE_MODE_PATH", tmp_path / "voice.json")
    config = GatewayConfig(sessions_dir=tmp_path / "sessions")
    runner = GatewayRunner(config)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test"))
    adapter.gateway_runner = runner
    adapter.verified_parent_chat_id = lambda source: "channel"
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(
        runner, profile_id="default", instance_id="discord-native-thread"
    )
    runner._wire_adapter_handlers(adapter)
    monkeypatch.setattr(runner, "_is_user_authorized_for_source", lambda *args, **kwargs: True)
    source = adapter.build_source(
        chat_id="thread",
        chat_type="thread",
        thread_id="thread",
        parent_chat_id="channel",
        guild_id="guild",
        user_id=owner,
        message_id="message",
    )
    source._transport_adapter_ref = weakref.ref(adapter)
    event = MessageEvent(text="owner request", source=source, message_id="message")
    toolsets = ["terminal", "web", "memory"]
    user_config = {"discord": {"principal_toolsets": _policy()}}

    try:
        with native_callback(runner, event, runner._native_transport_homes[None]):
            payload = await prepare_native(runner, event)
        restored = restore_native_source(payload, runner)
        enabled = _resolve_principal_enabled_toolsets(
            user_config=user_config, source=restored, platform_toolsets=toolsets
        )

        assert enabled == sorted(toolsets)
        assert not _principal_context_is_restricted(
            source=restored,
            platform_toolsets=toolsets,
            enabled_toolsets=enabled,
            user_config=user_config,
        )
    finally:
        authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", ["absent", "mismatch", "unavailable"])
async def test_native_thread_restore_denies_without_current_parent_proof(
    tmp_path, monkeypatch, proof
):
    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setattr(GatewayRunner, "_VOICE_MODE_PATH", tmp_path / "voice.json")
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test"))
    adapter.gateway_runner = runner
    adapter.verified_parent_chat_id = lambda source: "channel"
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(
        runner, profile_id="default", instance_id=f"discord-proof-{proof}"
    )
    runner._wire_adapter_handlers(adapter)
    monkeypatch.setattr(runner, "_is_user_authorized_for_source", lambda *args, **kwargs: True)
    source = adapter.build_source(
        chat_id="thread", chat_type="thread", thread_id="thread",
        parent_chat_id="channel", guild_id="guild", user_id="owner-a", message_id="message",
    )
    source._transport_adapter_ref = weakref.ref(adapter)
    event = MessageEvent(text="owner request", source=source, message_id="message")
    toolsets = ["terminal", "web", "memory"]
    user_config = {"discord": {"principal_toolsets": _policy()}}

    try:
        with native_callback(runner, event, runner._native_transport_homes[None]):
            payload = await prepare_native(runner, event)
        if proof == "absent":
            adapter.verified_parent_chat_id = None
        elif proof == "mismatch":
            adapter.verified_parent_chat_id = lambda source: "other-channel"
        else:
            def unavailable(_source):
                raise RuntimeError("Discord cache unavailable")
            adapter.verified_parent_chat_id = unavailable

        restored = restore_native_source(payload, runner)
        enabled = _resolve_principal_enabled_toolsets(
            user_config=user_config, source=restored, platform_toolsets=toolsets
        )

        assert enabled == []
        assert _principal_context_is_restricted(
            source=restored, platform_toolsets=toolsets, enabled_toolsets=enabled,
            user_config=user_config,
        )
    finally:
        authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("scope_id", "other-guild"), ("user_id", "regular"), ("parent_chat_id", "other-channel")],
)
async def test_native_thread_restore_denies_wrong_guild_principal_or_channel(
    tmp_path, monkeypatch, field, value
):
    from plugins.platforms.discord.adapter import DiscordAdapter
    monkeypatch.setattr(GatewayRunner, "_VOICE_MODE_PATH", tmp_path / "voice.json")
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test"))
    adapter.gateway_runner = runner
    adapter.verified_parent_chat_id = lambda source: source.parent_chat_id
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(
        runner, profile_id="default", instance_id=f"discord-wrong-{field}"
    )
    runner._wire_adapter_handlers(adapter)
    monkeypatch.setattr(runner, "_is_user_authorized_for_source", lambda *args, **kwargs: True)
    source = adapter.build_source(
        chat_id="thread", chat_type="thread", thread_id="thread",
        parent_chat_id="channel", guild_id="guild", user_id="owner-a", message_id="message",
    )
    source._transport_adapter_ref = weakref.ref(adapter)
    event = MessageEvent(text="owner request", source=source, message_id="message")

    try:
        with native_callback(runner, event, runner._native_transport_homes[None]):
            payload = await prepare_native(runner, event)
        candidate = deepcopy(payload)
        candidate["native_text_v1"]["source"][field] = value
        if field == "scope_id":
            candidate["native_text_v1"]["source"]["guild_id"] = value
        restored = restore_native_source(candidate, runner)

        assert _resolve_principal_enabled_toolsets(
            user_config={"discord": {"principal_toolsets": _policy()}},
            source=restored,
            platform_toolsets=["terminal", "web", "memory"],
        ) == []
    finally:
        authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["channel", "dm"])
async def test_native_restore_keeps_non_thread_discord_owner_behavior(
    tmp_path, monkeypatch, chat_type
):
    from plugins.platforms.discord.adapter import DiscordAdapter
    monkeypatch.setattr(GatewayRunner, "_VOICE_MODE_PATH", tmp_path / "voice.json")
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test"))
    adapter.gateway_runner = runner
    adapter.verified_parent_chat_id = None
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(
        runner, profile_id="default", instance_id=f"discord-{chat_type}"
    )
    runner._wire_adapter_handlers(adapter)
    monkeypatch.setattr(runner, "_is_user_authorized_for_source", lambda *args, **kwargs: True)
    chat_id = "channel" if chat_type == "channel" else "owner-dm"
    source = adapter.build_source(
        chat_id=chat_id, chat_type=chat_type, guild_id="guild", user_id="owner-a",
        message_id="message",
    )
    source._transport_adapter_ref = weakref.ref(adapter)
    event = MessageEvent(text="owner request", source=source, message_id="message")
    toolsets = ["terminal", "web", "memory"]
    user_config = {"discord": {"principal_toolsets": _policy()}}

    try:
        with native_callback(runner, event, runner._native_transport_homes[None]):
            payload = await prepare_native(runner, event)
        restored = restore_native_source(payload, runner)
        enabled = _resolve_principal_enabled_toolsets(
            user_config=user_config, source=restored, platform_toolsets=toolsets
        )

        assert enabled == sorted(toolsets)
        assert not _principal_context_is_restricted(
            source=restored, platform_toolsets=toolsets, enabled_toolsets=enabled,
            user_config=user_config,
        )
    finally:
        authority.db.close()


def test_non_discord_principal_resolution_is_unchanged():
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="dm", chat_type="dm", user_id="owner-a"
    )

    assert _resolve_principal_enabled_toolsets(
        user_config={"discord": {"principal_toolsets": _policy()}},
        source=source,
        platform_toolsets=["terminal", "web"],
    ) == ["terminal", "web"]
    assert not _principal_context_is_restricted(
        source=source,
        platform_toolsets=["terminal", "web"],
        enabled_toolsets=["web"],
        user_config={"discord": {"principal_toolsets": _policy()}},
    )


def test_relay_source_cannot_borrow_discord_parent_verifier():
    from gateway.session_ingress_context import _restore_verified_discord_parent

    class Adapter:
        platform = Platform.DISCORD

        @staticmethod
        def verified_parent_chat_id(_source):
            return "channel"

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread",
        chat_type="thread",
        thread_id="thread",
        parent_chat_id="channel",
        user_id="owner-a",
        scope_id="guild",
        delivered_via_upstream_relay=True,
    )

    _restore_verified_discord_parent(Adapter(), source)

    assert getattr(source, "_validated_parent_chat_id", None) is None


@pytest.mark.asyncio
async def test_heimdall_sealed_restore_keeps_its_clamp_after_parent_revalidation(
    tmp_path, monkeypatch
):
    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setattr(GatewayRunner, "_VOICE_MODE_PATH", tmp_path / "voice.json")
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test"))
    adapter.gateway_runner = runner
    adapter.verified_parent_chat_id = lambda source: "channel"
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(
        runner, profile_id="default", instance_id="heimdall-parent-proof"
    )
    runner._wire_adapter_handlers(adapter)
    monkeypatch.setattr(runner, "_is_user_authorized_for_source", lambda *args, **kwargs: True)
    source = adapter.build_source(
        chat_id="thread", chat_type="thread", thread_id="thread",
        parent_chat_id="channel", guild_id="guild", user_id="heimdall", is_bot=True,
        message_id="message",
    )
    source._trusted_heimdall_incident = True
    source._transport_adapter_ref = weakref.ref(adapter)
    event = MessageEvent(text="incident evidence", source=source, message_id="message")
    user_config = {"discord": {"heimdall_incident_intake": {"toolsets": ["web"]}}}

    try:
        with native_callback(runner, event, runner._native_transport_homes[None]):
            payload = await prepare_native(runner, event)
        restored = restore_native_source(payload, runner)

        assert restored._trusted_heimdall_incident is True
        assert restored._validated_parent_chat_id == "channel"
        assert _resolve_principal_enabled_toolsets(
            user_config=user_config,
            source=restored,
            platform_toolsets=["terminal", "web"],
        ) == ["web"]
        assert _principal_context_is_restricted(
            source=restored,
            platform_toolsets=["terminal", "web"],
            enabled_toolsets=["web"],
            user_config=user_config,
        )
    finally:
        authority.db.close()
