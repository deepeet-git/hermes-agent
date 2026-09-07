"""Codex OAuth recovery primitives used by the gateway plugin."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_URL_RE = re.compile(r"https://auth\.openai\.com/codex/device")
_CODE_RE = re.compile(r"\b[A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+\b")
_TERMINAL_AUTH_MARKERS = (
    "invalid_grant",
    "invalid grant",
    "invalid_token",
    "token_invalidated",
    "token revoked",
    "refresh token revoked",
    "refresh_token_reused",
    "refresh token reused",
    "codex_refresh_failed",
    "missing refresh token",
)
_RATE_LIMIT_MARKERS = ("usage_limit", "rate_limit", "rate limit", "quota")


@dataclass(frozen=True)
class PoolSnapshot:
    available: int
    rate_limited: int
    dead: int
    other_unavailable: int
    total: int

    @property
    def needs_login(self) -> bool:
        if self.available or self.rate_limited:
            return False
        return self.total == 0 or self.dead > 0 or self.other_unavailable > 0


@dataclass(frozen=True)
class LoginMaterial:
    url: str
    code: str


def _entry_value(entry: Any, name: str, default: Any = None) -> Any:
    if isinstance(entry, dict):
        return entry.get(name, default)
    return getattr(entry, name, default)


def pool_snapshot(entries: Iterable[Any]) -> PoolSnapshot:
    available = rate_limited = dead = other = total = 0
    for entry in entries:
        total += 1
        status = str(_entry_value(entry, "last_status", "") or "").strip().lower()
        reason = str(_entry_value(entry, "last_error_reason", "") or "").strip().lower()
        if status in {"", "ok", "active", "available"}:
            available += 1
        elif status == "dead":
            dead += 1
        elif status in {"exhausted", "rate-limited", "rate_limited"} and any(
            marker in reason for marker in _RATE_LIMIT_MARKERS
        ):
            rate_limited += 1
        else:
            other += 1
    return PoolSnapshot(available, rate_limited, dead, other, total)


def _platform_value(platform: Any) -> str:
    return str(getattr(platform, "value", platform) or "").strip().lower()


def match_owner_auth_command(event: Any, owner_ids: set[str]) -> tuple[bool, bool]:
    text = str(getattr(event, "text", "") or "").strip()
    if text not in {"/auth", "/auth force"}:
        return False, False
    source = getattr(event, "source", None)
    if source is None:
        return False, False
    if _platform_value(getattr(source, "platform", "")) != "telegram":
        return False, False
    if str(getattr(source, "chat_type", "") or "").strip().lower() != "dm":
        return False, False
    if str(getattr(source, "user_id", "") or "").strip() not in owner_ids:
        return False, False
    return True, text == "/auth force"


class DeviceLoginOutputParser:
    def __init__(self) -> None:
        self.url: Optional[str] = None
        self.code: Optional[str] = None
        self.emitted = False

    def feed(self, line: str) -> Optional[LoginMaterial]:
        clean = _ANSI_RE.sub("", str(line or "")).strip()
        url_match = _URL_RE.search(clean)
        if url_match:
            self.url = url_match.group(0)
        code_match = _CODE_RE.search(clean)
        if code_match:
            self.code = code_match.group(0)
        if self.url and self.code and not self.emitted:
            self.emitted = True
            return LoginMaterial(self.url, self.code)
        return None


class TerminalAuthTracker:
    def __init__(self) -> None:
        self._terminal_turns: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def note_api_error(
        self,
        *,
        provider: str = "",
        session_id: str = "",
        turn_id: str = "",
        status_code: Optional[int] = None,
        reason: str = "",
        error: Any = None,
        **_: Any,
    ) -> None:
        if (
            str(provider).strip().lower() != "openai-codex"
            or status_code not in {400, 401, 403}
        ):
            return
        message = ""
        if isinstance(error, dict):
            message = str(error.get("message") or "")
        else:
            message = str(error or "")
        haystack = f"{reason} {message}".lower()
        if not any(marker in haystack for marker in _TERMINAL_AUTH_MARKERS):
            return
        key = (str(session_id or ""), str(turn_id or ""))
        with self._lock:
            self._terminal_turns.add(key)

    def finish_turn(
        self,
        *,
        session_id: str = "",
        turn_id: str = "",
        failed: bool = False,
        interrupted: bool = False,
        **_: Any,
    ) -> bool:
        key = (str(session_id or ""), str(turn_id or ""))
        with self._lock:
            seen = key in self._terminal_turns
            self._terminal_turns.discard(key)
        # A successful fallback does not make a dead Codex pool healthy. The
        # caller checks the pool before starting login, so a recovered/rotated
        # credential suppresses the prompt while an all-dead pool still alerts.
        return bool(seen and not interrupted)


class CodexAuthRecoveryService:
    """Single-flight device login broker with Telegram-only notifications."""

    def __init__(
        self,
        *,
        hermes_home: Path,
        sender: Optional[Callable[[str, str], bool]] = None,
        executable: Optional[str] = None,
        cooldown_seconds: int = 60,
    ) -> None:
        self.hermes_home = Path(hermes_home).expanduser().resolve()
        self.sender = sender or self._send_with_cli
        self.executable = executable or shutil.which("hermes") or "hermes"
        self.cooldown_seconds = max(1, int(cooldown_seconds))
        self._thread_lock = threading.Lock()
        self._running = False
        self._last_started = 0.0

    def current_snapshot(self) -> PoolSnapshot:
        try:
            from agent.credential_pool import load_pool

            return pool_snapshot(load_pool("openai-codex").entries())
        except Exception:
            return pool_snapshot([])

    def start(self, *, target: str = "telegram", force: bool = False) -> str:
        snapshot = self.current_snapshot()
        if not force and not snapshot.needs_login:
            if snapshot.available:
                return (
                    "✅ Codex OAuth 정상\n"
                    f"사용 가능 {snapshot.available}개 · 사용량 제한 {snapshot.rate_limited}개"
                )
            return (
                "⏳ Codex OAuth 인증은 유효하지만 현재 계정이 사용량 제한 상태입니다.\n"
                "재로그인 대신 제한 해제를 기다립니다."
            )

        now = time.monotonic()
        with self._thread_lock:
            if self._running:
                return "🔐 Codex 재인증이 이미 진행 중입니다."
            if now - self._last_started < self.cooldown_seconds:
                return "⏳ Codex 로그인 발급 요청이 최근 실행되었습니다. 잠시 후 다시 시도해 주세요."
            self._running = True
            self._last_started = now

        thread = threading.Thread(
            target=self._run_login,
            kwargs={"target": target},
            name="codex-auth-recovery",
            daemon=True,
        )
        thread.start()
        return "🔐 Codex 재인증을 시작했습니다. 로그인 URL과 코드를 곧 보내드리겠습니다."

    def _send_with_cli(self, target: str, message: str) -> bool:
        env = os.environ.copy()
        env["HERMES_HOME"] = str(self.hermes_home)
        try:
            result = subprocess.run(
                [self.executable, "send", "--to", target, "--quiet", message],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _acquire_process_lock(self) -> Optional[int]:
        state_dir = self.hermes_home / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        lock_path = state_dir / "codex-auth-recovery.lock"
        for _ in range(2):
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, f"{os.getpid()} {int(time.time())}\n".encode("ascii"))
                return fd
            except FileExistsError:
                try:
                    if time.time() - lock_path.stat().st_mtime > 20 * 60:
                        lock_path.unlink()
                        continue
                except OSError:
                    pass
                return None
        return None

    def _release_process_lock(self, fd: Optional[int]) -> None:
        if fd is None:
            return
        try:
            os.close(fd)
        finally:
            try:
                (self.hermes_home / "state" / "codex-auth-recovery.lock").unlink()
            except FileNotFoundError:
                pass

    def _run_login(self, *, target: str) -> None:
        fd = self._acquire_process_lock()
        if fd is None:
            self.sender(target, "🔐 Codex 재인증이 다른 프로세스에서 이미 진행 중입니다.")
            with self._thread_lock:
                self._running = False
            return

        env = os.environ.copy()
        env["HERMES_HOME"] = str(self.hermes_home)
        env["PYTHONUNBUFFERED"] = "1"
        parser = DeviceLoginOutputParser()
        material_sent = False
        returncode = 1
        try:
            process = subprocess.Popen(
                [self.executable, "auth", "add", "openai-codex"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                material = parser.feed(line)
                if material is not None:
                    material_sent = True
                    self.sender(
                        target,
                        "🔐 Hermes Codex 재로그인이 필요합니다.\n\n"
                        f"URL:\n{material.url}\n\n"
                        f"Device Code:\n`{material.code}`\n\n"
                        "15분 안에 iPhone에서 승인해 주세요. 토큰은 Mac mini에만 저장됩니다.",
                    )
            returncode = process.wait(timeout=30)

            status = subprocess.run(
                [self.executable, "auth", "status", "openai-codex"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=30,
                check=False,
            )
            auth_ok = status.returncode == 0 and "logged in" in status.stdout.lower()
            auth_path = self.hermes_home / "auth.json"
            mode_ok = auth_path.exists() and (auth_path.stat().st_mode & 0o777) == 0o600
            if returncode == 0 and auth_ok and mode_ok:
                self.sender(
                    target,
                    "✅ Codex 재인증이 완료되었습니다. 다음 Codex 요청부터 새 인증이 적용됩니다.",
                )
            else:
                self.sender(
                    target,
                    "❌ Codex 재인증이 완료되지 않았습니다. `/auth force`로 다시 시도해 주세요.",
                )
        except Exception:
            self.sender(
                target,
                "❌ Codex 재인증 프로세스가 실패했습니다. `/auth force`로 다시 시도해 주세요.",
            )
        finally:
            if not material_sent and returncode != 0:
                # Detailed subprocess output is intentionally never forwarded: it may
                # contain one-time authorization material.
                pass
            self._release_process_lock(fd)
            with self._thread_lock:
                self._running = False
