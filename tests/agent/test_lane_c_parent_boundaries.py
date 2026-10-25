"""Release-blocking preservation seams whose source owners are outside lane C.

Do not weaken these tests: parent integration must reconcile the shared memory
fence and the extracted Codex recovery sibling, not only their facade APIs.
"""
import base64
import json
from pathlib import Path


def _jwt(account):
    claims = {"sub": "user", "https://api.openai.com/auth": {"chatgpt_account_id": account}}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def test_actual_turn_memory_fence_does_not_promote_historical_authority():
    # turn_context imports this shared builder, not the manager facade's builder.
    from agent.memory_context import build_memory_context_block
    block = build_memory_context_block("historical evidence")
    assert "Treat as authoritative reference data" not in block
    assert "not automatic current authority" in block


def test_extracted_recovery_does_not_fork_borrowed_root_chain(tmp_path, monkeypatch):
    from hermes_cli import auth, auth_codex
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    token = _jwt("account-a")
    (root / "auth.json").write_text(json.dumps({"version": 1, "providers": {
        "openai-codex": {"tokens": {"access_token": token, "refresh_token": "root-old"}}}}))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setattr(auth, "_import_codex_cli_tokens", lambda: {
        "access_token": token, "refresh_token": "cli-new"})
    before = (root / "auth.json").read_bytes()
    result = auth_codex._recover_codex_tokens_from_cli("lane C regression", observed_access_token=token)
    assert result is None
    assert (root / "auth.json").read_bytes() == before
    assert not (profile / "auth.json").exists(), "automatic recovery forked a root-owned single-use grant"


def test_recovery_does_not_copy_a_sibling_owned_refresh_chain(tmp_path, monkeypatch):
    from hermes_cli import auth, auth_codex

    root = tmp_path / ".hermes"
    profile = root / "profiles" / "worker"
    sibling = root / "profiles" / "sibling"
    profile.mkdir(parents=True)
    sibling.mkdir()
    token = _jwt("account-a")
    local = profile / "auth.json"
    local.write_text(json.dumps({"version": 1, "providers": {
        "openai-codex": {"tokens": {"access_token": token, "refresh_token": "local-old"}}}}))
    (sibling / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {
        "openai-codex": [{"id": "sibling-account", "refresh_token": "already-owned"}]}}))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setattr(auth, "_import_codex_cli_tokens", lambda: {
        "access_token": token, "refresh_token": "already-owned"})
    before = local.read_bytes()

    result = auth_codex._recover_codex_tokens_from_cli("sibling-chain regression", observed_access_token=token)

    assert result is None
    assert local.read_bytes() == before
