"""Behavior contracts for the Codex auth recovery plugin."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / "plugins" / "codex-auth-recovery"


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_pool_snapshot_distinguishes_usable_quota_and_dead_credentials():
    recovery = _load_module("codex_auth_recovery_core", "recovery.py")
    entries = [
        SimpleNamespace(last_status=None),
        SimpleNamespace(last_status="exhausted", last_error_reason="usage_limit_reached"),
        SimpleNamespace(last_status="dead", last_error_reason="invalid_grant"),
    ]

    snapshot = recovery.pool_snapshot(entries)

    assert snapshot.available == 1
    assert snapshot.rate_limited == 1
    assert snapshot.dead == 1
    assert snapshot.needs_login is False


def test_rate_limited_only_pool_does_not_request_reauthentication():
    recovery = _load_module("codex_auth_recovery_quota", "recovery.py")
    entries = [
        SimpleNamespace(last_status="exhausted", last_error_reason="usage_limit_reached"),
        SimpleNamespace(last_status="exhausted", last_error_reason="rate_limit"),
    ]

    snapshot = recovery.pool_snapshot(entries)

    assert snapshot.available == 0
    assert snapshot.rate_limited == 2
    assert snapshot.needs_login is False


def test_dead_or_empty_pool_requests_reauthentication():
    recovery = _load_module("codex_auth_recovery_dead", "recovery.py")

    assert recovery.pool_snapshot([]).needs_login is True
    assert recovery.pool_snapshot(
        [SimpleNamespace(last_status="dead", last_error_reason="invalid_grant")]
    ).needs_login is True


@pytest.mark.parametrize(
    ("platform", "chat_type", "user_id", "text", "expected"),
    [
        ("telegram", "dm", "6834626936", "/auth", (True, False)),
        ("telegram", "dm", "6834626936", " /auth force ", (True, True)),
        ("telegram", "group", "6834626936", "/auth", (False, False)),
        ("discord", "dm", "6834626936", "/auth", (False, False)),
        ("telegram", "dm", "999", "/auth", (False, False)),
        ("telegram", "dm", "6834626936", "/auth now", (False, False)),
    ],
)
def test_auth_command_is_exact_and_owner_dm_only(
    platform, chat_type, user_id, text, expected
):
    recovery = _load_module(f"codex_auth_event_{platform}_{chat_type}_{user_id}_{len(text)}", "recovery.py")
    event = SimpleNamespace(
        text=text,
        source=SimpleNamespace(
            platform=SimpleNamespace(value=platform),
            chat_type=chat_type,
            user_id=user_id,
        ),
    )

    assert recovery.match_owner_auth_command(event, {"6834626936"}) == expected


def test_device_login_output_parser_strips_ansi_and_emits_once():
    recovery = _load_module("codex_auth_recovery_parser", "recovery.py")
    parser = recovery.DeviceLoginOutputParser()

    assert parser.feed("  1. Open this URL in your browser:\n") is None
    assert parser.feed("     \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\n") is None
    material = parser.feed("     \x1b[94mABCD-EFGH\x1b[0m\n")

    assert material == recovery.LoginMaterial(
        url="https://auth.openai.com/codex/device",
        code="ABCD-EFGH",
    )
    assert parser.feed("Waiting for sign-in...\n") is None


def test_terminal_auth_error_survives_successful_fallback_for_pool_check():
    recovery = _load_module("codex_auth_recovery_failure", "recovery.py")
    tracker = recovery.TerminalAuthTracker()
    tracker.note_api_error(
        provider="openai-codex",
        session_id="s1",
        turn_id="t1",
        status_code=401,
        reason="auth",
        error={"message": "invalid_grant"},
    )

    assert tracker.finish_turn(
        session_id="s1", turn_id="t1", failed=False, interrupted=False
    ) is True

    tracker.note_api_error(
        provider="openai-codex",
        session_id="s1",
        turn_id="t2",
        status_code=401,
        reason="auth",
        error={"message": "refresh token revoked"},
    )
    assert tracker.finish_turn(
        session_id="s1", turn_id="t2", failed=True, interrupted=False
    ) is True
    assert tracker.finish_turn(
        session_id="s1", turn_id="t2", failed=True, interrupted=False
    ) is False


def test_terminal_refresh_400_is_tracked_but_generic_400_is_not():
    recovery = _load_module("codex_auth_recovery_refresh_400", "recovery.py")
    tracker = recovery.TerminalAuthTracker()
    tracker.note_api_error(
        provider="openai-codex",
        session_id="s1",
        turn_id="terminal",
        status_code=400,
        reason="auth",
        error={"message": "invalid_grant"},
    )
    tracker.note_api_error(
        provider="openai-codex",
        session_id="s1",
        turn_id="generic",
        status_code=400,
        reason="bad_request",
        error={"message": "invalid request body"},
    )

    assert tracker.finish_turn(
        session_id="s1", turn_id="terminal", failed=True, interrupted=False
    ) is True
    assert tracker.finish_turn(
        session_id="s1", turn_id="generic", failed=True, interrupted=False
    ) is False


def test_non_auth_and_quota_errors_never_trigger_auto_recovery():
    recovery = _load_module("codex_auth_recovery_non_auth", "recovery.py")
    tracker = recovery.TerminalAuthTracker()
    for turn_id, status, reason, message in (
        ("t1", 429, "rate_limit", "usage_limit_reached"),
        ("t2", 403, "entitlement", "subscription required"),
        ("t3", 401, "auth", "token expired"),
    ):
        tracker.note_api_error(
            provider="openai-codex",
            session_id="s1",
            turn_id=turn_id,
            status_code=status,
            reason=reason,
            error={"message": message},
        )
        assert tracker.finish_turn(
            session_id="s1", turn_id=turn_id, failed=True, interrupted=False
        ) is False


def test_plugin_parses_cli_persisted_owner_list_and_routes_owner_dm(monkeypatch):
    plugin = _load_module("codex_auth_recovery_routing", "__init__.py")
    sent = []

    class FakeService:
        def start(self, *, target, force):
            sent.append(("start", target, force))
            return "healthy"

        def sender(self, target, message):
            sent.append(("send", target, message))
            return True

    monkeypatch.setattr(
        plugin,
        "_config",
        lambda: {"owner_telegram_ids": '["6834626936"]'},
    )
    monkeypatch.setattr(plugin, "_service", FakeService())
    event = SimpleNamespace(
        text="/auth",
        source=SimpleNamespace(
            platform=SimpleNamespace(value="telegram"),
            chat_type="dm",
            user_id="6834626936",
            chat_id="6834626936",
            thread_id=None,
        ),
    )

    assert plugin._pre_gateway_dispatch(event=event) == {
        "action": "skip",
        "reason": "codex-auth-recovery-started",
    }
    assert sent == [
        ("start", "telegram:6834626936", False),
        ("send", "telegram:6834626936", "healthy"),
    ]


def test_plugin_registers_only_hooks_not_an_unsafe_context_free_slash_handler():
    plugin = _load_module("codex_auth_recovery_plugin", "__init__.py")

    class FakeContext:
        def __init__(self):
            self.hooks = {}
            self.commands = []

        def register_hook(self, name, handler):
            self.hooks[name] = handler

        def register_command(self, *args, **kwargs):
            self.commands.append((args, kwargs))

    ctx = FakeContext()
    plugin.register(ctx)

    assert set(ctx.hooks) == {
        "pre_gateway_dispatch",
        "api_request_error",
        "on_session_end",
    }
    assert ctx.commands == []
