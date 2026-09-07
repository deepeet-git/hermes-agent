"""Owner-only Telegram recovery for terminal OpenAI Codex OAuth failures."""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


def _load_recovery_module():
    try:
        from . import recovery
        return recovery
    except (ImportError, ValueError):
        path = Path(__file__).with_name("recovery.py")
        name = "hermes_codex_auth_recovery_core"
        existing = sys.modules.get(name)
        if existing is not None:
            return existing
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module


recovery = _load_recovery_module()
_tracker = recovery.TerminalAuthTracker()
_service = None


def _config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        entries = ((cfg.get("plugins") or {}).get("entries") or {})
        value = entries.get("codex-auth-recovery") or {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _owners() -> set[str]:
    raw = _config().get("owner_telegram_ids") or []
    if isinstance(raw, str):
        import json

        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = [part.strip() for part in raw.split(",")]
        raw = parsed if isinstance(parsed, list) else [parsed]
    return {str(value).strip() for value in raw if str(value).strip()}


def _get_service():
    global _service
    if _service is None:
        from hermes_constants import get_hermes_home

        cfg = _config()
        _service = recovery.CodexAuthRecoveryService(
            hermes_home=get_hermes_home(),
            cooldown_seconds=int(cfg.get("cooldown_seconds") or 60),
        )
    return _service


def _telegram_target(source: Any) -> str:
    chat_id = str(getattr(source, "chat_id", "") or "").strip()
    thread_id = str(getattr(source, "thread_id", "") or "").strip()
    if not chat_id:
        return "telegram"
    return f"telegram:{chat_id}:{thread_id}" if thread_id else f"telegram:{chat_id}"


def _pre_gateway_dispatch(*, event=None, **_: Any):
    text = str(getattr(event, "text", "") or "").strip()
    if text not in {"/auth", "/auth force"}:
        return None

    matched, force = recovery.match_owner_auth_command(event, _owners())
    if not matched:
        # Fail closed before pairing/auth/model dispatch. No context-free slash
        # handler is registered because it cannot verify the invoker identity.
        return {"action": "skip", "reason": "codex-auth-owner-only"}

    target = _telegram_target(event.source)
    message = _get_service().start(target=target, force=force)
    _get_service().sender(target, message)
    return {"action": "skip", "reason": "codex-auth-recovery-started"}


def _api_request_error(**kwargs: Any) -> None:
    _tracker.note_api_error(**kwargs)


def _on_session_end(**kwargs: Any) -> None:
    if not bool(_config().get("auto_start", True)):
        return
    if not _tracker.finish_turn(**kwargs):
        return
    service = _get_service()
    snapshot = service.current_snapshot()
    if not snapshot.needs_login:
        return
    message = service.start(target="telegram", force=False)
    service.sender("telegram", message)


def register(ctx) -> None:
    # /auth is intercepted by the source-aware pre-dispatch hook rather than a
    # context-free plugin command, so only the configured Telegram owner DM can
    # initiate an OAuth flow.
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
    ctx.register_hook("api_request_error", _api_request_error)
    ctx.register_hook("on_session_end", _on_session_end)
