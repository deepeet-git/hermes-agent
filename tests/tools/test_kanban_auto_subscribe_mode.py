"""Profile-local defaults must persist at real tool creation, not via later repair."""
import hermes_bootstrap

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tools import kanban_tools as kt
from tools.registry import registry


@pytest.fixture
def origin(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_HOME", "HERMES_PROFILE", "HERMES_SESSION_KEY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        "PLATFORM": "telegram", "CHAT_ID": "test-chat", "THREAD_ID": "42",
        "CHAT_TYPE": "dm", "USER_ID": "principal", "USER_ID_ALT": "canonical",
        "PROFILE": "default", "SCOPE_ID": "scope", "PARENT_CHAT_ID": "parent",
        "MESSAGE_ID": "123",
    }.items():
        monkeypatch.setenv("HERMES_SESSION_" + key, value)
    kb.init_db()
    return home


def create(**kwargs):
    result = json.loads(registry.dispatch("kanban_create", {
        "title": "notification policy", "assignee": "peer", **kwargs,
    }))
    assert result["ok"], result
    with kbc.connect() as conn:
        subs = kbn.list_notify_subs(conn, result["task_id"])
    return result, subs


def test_configured_wake_persists_at_creation(origin):
    (origin / "config.yaml").write_text("kanban:\n  auto_subscribe_delivery_mode: wake\n")
    result, subs = create()
    assert result["subscribed"] is True
    assert len(subs) == 1
    assert subs[0]["delivery_mode"] == "wake"
    assert subs[0]["user_id_alt"] == "canonical"
    assert subs[0]["user_id"] == "principal"
    assert subs[0]["notifier_profile"] == "default"
    assert subs[0]["delivery_metadata"] == {
        "thread_id": "42", "chat_type": "dm", "scope_id": "scope",
        "parent_chat_id": "parent", "telegram_dm_topic_reply_fallback": True,
        "direct_messages_topic_id": "42", "telegram_reply_to_message_id": "123",
    }


@pytest.mark.parametrize("mode", [None, "notify", "notify+wake"])
def test_compatible_modes(origin, mode):
    if mode is not None:
        (origin / "config.yaml").write_text(f"kanban:\n  auto_subscribe_delivery_mode: {mode}\n")
    result, subs = create()
    assert result["subscribed"]
    assert subs[0]["delivery_mode"] == (mode or "notify+wake")


@pytest.mark.parametrize("mode", ["typo", "null", "false", "[]", "{}"])
def test_invalid_mode_refuses_new_subscription_without_affecting_manual_reports(origin, mode, caplog):
    (origin / "config.yaml").write_text(f"kanban:\n  auto_subscribe_delivery_mode: {mode}\n")
    result, subs = create()
    assert result["subscribed"] is False
    assert subs == []
    assert "kanban.auto_subscribe_delivery_mode must be" in caplog.text
    with kbc.connect() as conn:
        kbn.add_notify_sub(conn, task_id=result["task_id"], platform="telegram",
                           chat_id="requested-report", delivery_mode="notify")
        assert kbn.list_notify_subs(conn, result["task_id"])[0]["delivery_mode"] == "notify"


@pytest.mark.parametrize("yaml", ["kanban: [", "kanban: false", "kanban: null", "kanban: []"])
def test_malformed_config_never_falls_back_to_outward_delivery(origin, yaml):
    (origin / "config.yaml").write_text(yaml)
    result, subs = create()
    assert result["subscribed"] is False
    assert subs == []


@pytest.mark.parametrize("mode", ["notify", "notify+wake", "wake"])
def test_inherited_and_explicit_modes_are_not_rewritten(origin, mode):
    parent, _ = create()
    with kbc.connect() as conn:
        kbn.add_notify_sub(conn, task_id=parent["task_id"], platform="telegram",
                           chat_id="test-chat", thread_id="42", delivery_mode=mode)
    (origin / "config.yaml").write_text("kanban:\n  auto_subscribe_delivery_mode: wake\n")
    child, subs = create(parents=[parent["task_id"]])
    assert subs[0]["delivery_mode"] == mode
    with kbc.connect() as conn:
        kbn.add_notify_sub(conn, task_id=child["task_id"], platform="telegram",
                           chat_id="test-chat", thread_id="42", delivery_mode=mode)
        assert kt._maybe_auto_subscribe(conn, child["task_id"])
        assert kbn.list_notify_subs(conn, child["task_id"])[0]["delivery_mode"] == mode


def test_profile_override_reads_own_yaml_not_launch_profile(origin):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    (origin / "config.yaml").write_text("kanban:\n  auto_subscribe_delivery_mode: notify\n")
    secondary = origin / "profiles" / "secondary"
    secondary.mkdir(parents=True)
    (secondary / "config.yaml").write_text("kanban:\n  auto_subscribe_delivery_mode: wake\n")
    for home, mode in [(origin, "notify"), (secondary, "wake"), (origin, "notify")]:
        token = set_hermes_home_override(home)
        try:
            _, subs = create()
            assert subs[0]["delivery_mode"] == mode
        finally:
            reset_hermes_home_override(token)


def test_tui_retains_passive_policy(origin, monkeypatch):
    (origin / "config.yaml").write_text("kanban:\n  auto_subscribe_delivery_mode: wake\n")
    monkeypatch.delenv("HERMES_SESSION_PLATFORM")
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID")
    monkeypatch.setenv("HERMES_SESSION_KEY", "tui-session")
    _, subs = create()
    assert subs[0]["platform"] == "tui"
    assert subs[0]["delivery_mode"] == "notify"


@pytest.mark.asyncio
async def test_newborn_wake_consumes_review_changes_done_without_upload(origin, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from gateway.run import GatewayRunner
    from gateway.config import Platform

    (origin / "config.yaml").write_text("kanban:\n  auto_subscribe_delivery_mode: wake\n")
    result, subs = create()
    tid = result["task_id"]
    artifact = origin / "actual-report.txt"
    artifact.write_text("requested report remains durable")

    class Adapter:
        send = AsyncMock()
        send_document = AsyncMock()
        send_image = AsyncMock()
        send_video = AsyncMock()
        send_audio = AsyncMock()

        def __init__(self):
            self.handled = []
            self.reject = True

        async def handle_message(self, event):
            if self.reject:
                raise RuntimeError("retryable admission failure")
            self.handled.append(event)
            event._gateway_accepted = True

    adapter = Adapter()
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    real_sleep = asyncio.sleep

    async def one_tick_sleep(delay):
        if delay != 5:
            runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", one_tick_sleep)

    async def tick():
        runner._running = True
        await runner._kanban_notifier_watcher(interval=1)

    with kbc.connect() as conn:
        assert kb.request_review(conn, tid, summary="review handoff", reviewer="reviewer",
                                 metadata={"artifacts": [str(artifact)]})
    await tick()
    with kbc.connect() as conn:
        retry_sub = kbn.list_notify_subs(conn, tid)[0]
        assert retry_sub["last_event_id"] == subs[0]["last_event_id"]
    assert list(runner._kanban_sub_fail_counts.values()) == [1]
    adapter.reject = False
    await tick()
    assert len(adapter.handled) == 1
    source = adapter.handled[0].source
    assert source.user_id == "principal"
    assert source.user_id_alt == "canonical"
    assert source.thread_id == "42"
    assert source.profile == "default"
    with kbc.connect() as conn:
        review_sub = kbn.list_notify_subs(conn, tid)[0]
        assert review_sub["last_event_id"] > retry_sub["last_event_id"]
        assert review_sub["last_ping_event_id"] == subs[0]["last_ping_event_id"]
        task = kb.claim_review_task(conn, tid)
        assert task is not None
        assert kb.request_changes(conn, tid, reason="needs correction",
                                  expected_run_id=task.current_run_id)[0]
    await tick()
    assert len(adapter.handled) == 2
    with kbc.connect() as conn:
        changes_sub = kbn.list_notify_subs(conn, tid)[0]
        assert changes_sub["last_event_id"] > review_sub["last_event_id"]
        assert kb.complete_task(conn, tid, summary="final result",
                                metadata={"artifacts": [str(artifact)]})
    await tick()
    assert len(adapter.handled) == 3
    await tick()
    assert len(adapter.handled) == 3
    for method in (adapter.send, adapter.send_document, adapter.send_image,
                   adapter.send_video, adapter.send_audio):
        method.assert_not_awaited()
    assert artifact.read_text() == "requested report remains durable"
    with kbc.connect() as conn:
        done_sub = kbn.list_notify_subs(conn, tid)[0]
        assert done_sub["last_event_id"] > changes_sub["last_event_id"]
        assert done_sub["last_ping_event_id"] == subs[0]["last_ping_event_id"]
        assert {ev.kind for ev in kb.list_events(conn, tid)} >= {
            "review_requested", "changes_requested", "completed"}
