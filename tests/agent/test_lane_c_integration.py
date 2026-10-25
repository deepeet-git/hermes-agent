"""Fork invariants exercised through upstream ownership and provider interfaces."""
import base64
import json
import threading
from pathlib import Path

from agent import credential_pool as cp
from hermes_cli import auth


def _jwt(account, subject="user"):
    claims = {"sub": subject, "https://api.openai.com/auth": {"chatgpt_account_id": account}}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _row(name, account, chain, *, source="manual:device_code"):
    return {"id": name, "label": name, "source": source, "auth_type": "oauth",
            "priority": 0, "access_token": _jwt(account), "refresh_token": chain}


def _homes(tmp_path, monkeypatch, rows):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    (root / "auth.json").write_text(json.dumps({"version": 1, "providers": {},
        "credential_pool": {"openai-codex": rows}}))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    return root, profile


def test_borrowed_refresh_preserves_root_lifecycle(tmp_path, monkeypatch):
    root, profile = _homes(tmp_path, monkeypatch, [_row("root", "a", "old")])
    pool = cp.load_pool("openai-codex")
    pool._persist(removed_ids=["root"])
    pool._entries.append(cp.PooledCredential.from_dict("openai-codex", _row("foreign", "b", "b")))
    pool._persist()
    rows = json.loads((root / "auth.json").read_text())["credential_pool"]["openai-codex"]
    assert [row["id"] for row in rows] == ["root"]
    assert not (profile / "auth.json").exists()


def test_manual_profile_login_does_not_replace_borrowed_account(tmp_path, monkeypatch):
    root, profile = _homes(tmp_path, monkeypatch, [_row("root", "a", "old")])
    before = (root / "auth.json").read_bytes()
    pool = cp.load_pool("openai-codex")
    added = pool.add_entry(cp.PooledCredential.from_dict("openai-codex", _row("local", "a", "new")))
    assert added.id == "local"
    assert pool._store_path == profile / "auth.json"
    assert (root / "auth.json").read_bytes() == before
    assert [row["id"] for row in auth.read_credential_pool("openai-codex")] == ["local"]


def test_owned_relogin_updates_only_same_account(tmp_path, monkeypatch):
    root, _ = _homes(tmp_path, monkeypatch, [_row("old", "a", "old"), _row("other", "b", "b")])
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "os-home")
    monkeypatch.setenv("HERMES_HOME", str(root))
    pool = cp.load_pool("openai-codex")
    added = pool.add_entry(cp.PooledCredential.from_dict("openai-codex", _row("new", "a", "new")))
    assert added.id == "old"
    assert {row["id"]: row["refresh_token"] for row in auth.read_credential_pool("openai-codex")} == {
        "old": "new", "other": "b"}


def test_shared_refresh_serializes_and_adopts_peer_rotation(tmp_path, monkeypatch):
    root, _ = _homes(tmp_path, monkeypatch, [_row("root", "a", "old")])
    first, second = cp.load_pool("openai-codex"), cp.load_pool("openai-codex")
    calls = []

    def refresh(access, chain, **kwargs):
        calls.append(chain)
        return {"access_token": _jwt("a"), "refresh_token": "rotated"}

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", refresh)
    results = []
    threads = [threading.Thread(target=lambda p=p: results.append(p._refresh_entry(p.entries()[0], force=True)))
               for p in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert calls == ["old"]
    assert len(results) == 2 and all(row.refresh_token == "rotated" for row in results)
    assert json.loads((root / "auth.json").read_text())["credential_pool"]["openai-codex"][0]["refresh_token"] == "rotated"


def test_independent_manual_account_never_adopts_singleton(tmp_path, monkeypatch):
    root, _ = _homes(tmp_path, monkeypatch, [_row("manual", "b", "manual-chain")])
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "os-home")
    monkeypatch.setenv("HERMES_HOME", str(root))
    store = json.loads((root / "auth.json").read_text())
    store["providers"] = {"openai-codex": {"tokens": {"access_token": _jwt("a"), "refresh_token": "singleton"}}}
    (root / "auth.json").write_text(json.dumps(store))
    pool = cp.load_pool("openai-codex")
    manual = next(row for row in pool.entries() if row.id == "manual")
    assert pool._sync_entry_from_auth_store(manual).refresh_token == "manual-chain"


def test_memory_context_is_evidence_not_current_authority():
    from agent.memory_manager import build_memory_context_block
    block = build_memory_context_block("historical sale state")
    assert "not automatic current authority" in block
    assert "verify the original current source" in block
    assert "Treat as authoritative reference data" not in block
    assert block.startswith("<memory-context>\n") and block.endswith("</memory-context>")


def test_openviking_loads_through_real_provider_discovery(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from plugins.memory import load_memory_provider
    from agent.memory_provider import MemoryProvider
    provider = load_memory_provider("openviking", register_skills=False)
    assert isinstance(provider, MemoryProvider)
    assert provider.name == "openviking"
    assert "viking_add_resource" in {schema["name"] for schema in provider.get_tool_schemas()}
