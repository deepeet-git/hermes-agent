"""Discord channel-scoped intake must survive the gateway auth layer."""

import weakref
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch):
    for name in (
        "DISCORD_ALLOWED_USERS",
        "DISCORD_ALLOW_ALL_USERS",
        "GATEWAY_ALLOWED_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(name, raising=False)


class _DiscordAdapter:
    platform = Platform.DISCORD

    @staticmethod
    def _discord_channel_ids_allowed(ids):
        return "allowed" in ids


def _runner_and_source(*, chat_id="allowed", parent_chat_id=None, chat_type="channel"):
    adapter = _DiscordAdapter()
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.DISCORD: adapter}
    runner._profile_adapters = {}
    runner.pairing_store = SimpleNamespace(is_approved=lambda *_args: False)
    runner.pairing_stores = {}

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        parent_chat_id=parent_chat_id,
        chat_type=chat_type,
        user_id="regular-user",
    )
    source._transport_adapter_ref = weakref.ref(adapter)
    return runner, source


def test_direct_discord_allowed_channel_authorizes_regular_user():
    runner, source = _runner_and_source()

    assert runner._is_user_authorized(source) is True


def test_discord_allowed_parent_authorizes_thread_user():
    runner, source = _runner_and_source(
        chat_id="thread", parent_chat_id="allowed", chat_type="channel"
    )

    assert runner._is_user_authorized(source) is True


def test_discord_channel_authorization_does_not_open_dms():
    runner, source = _runner_and_source(chat_type="dm")

    assert runner._is_user_authorized(source) is False


def test_discord_unlisted_channel_remains_denied():
    runner, source = _runner_and_source(chat_id="other")

    assert runner._is_user_authorized(source) is False


def test_discord_channel_requires_live_transport_provenance():
    runner, source = _runner_and_source()
    del source._transport_adapter_ref

    assert runner._is_user_authorized(source) is False
