"""Codex root ledger and account availability regressions."""

import base64
import json
import threading
from pathlib import Path

from agent import credential_pool as CP
from hermes_cli import auth as A


def _jwt(account):
    payload = base64.urlsafe_b64encode(json.dumps({
        "https://api.openai.com/auth": {"chatgpt_account_id": account},
    }).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _row(name, account, chain, *, source="manual:device_code"):
    return {"id": name, "label": name, "source": source, "auth_type": "oauth",
            "priority": 0, "access_token": _jwt(account), "refresh_token": chain}


def _setup(tmp_path, monkeypatch, rows):
    root = tmp_path / ".hermes"
    root.mkdir()
    (root / "auth.json").write_text(json.dumps({"version": 1, "providers": {},
        "credential_pool": {"openai-codex": rows}}))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    profile = root / "profiles" / "one"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    return root, profile


def test_root_cooldown_shared_by_profiles(tmp_path, monkeypatch):
    root, profile = _setup(tmp_path, monkeypatch, [
        _row("a1", "account-a", "chain-a"),
        _row("a2", "account-a", "chain-b"),
        _row("b", "account-b", "chain-c"),
    ])
    pool = CP.load_pool("openai-codex")
    assert pool.available_account_count() == 2
    pool.mark_exhausted_and_rotate(status_code=429, credential_id="a1")
    assert pool.available_account_count() == 1
    rows = json.loads((root / "auth.json").read_text())["credential_pool"]["openai-codex"]
    assert {row["id"] for row in rows if row.get("last_status") == "exhausted"} == {"a1", "a2"}
    assert not (profile / "auth.json").exists()
    other = root / "profiles" / "two"
    other.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(other))
    assert {e.id for e in CP.load_pool("openai-codex").entries() if e.last_status != "exhausted"} == {"b"}


def test_same_chain_deduplicated_in_owner(tmp_path, monkeypatch):
    rows = [_row(f"a{i}", "account-a", "one-chain") for i in range(3)]
    rows.append(_row("seed", "account-a", "one-chain", source="device_code"))
    root, profile = _setup(tmp_path, monkeypatch, rows)
    pool = CP.load_pool("openai-codex")
    assert [entry.id for entry in pool.entries()] == ["seed"]
    assert len(json.loads((root / "auth.json").read_text())["credential_pool"]["openai-codex"]) == 1
    assert not (profile / "auth.json").exists()


def test_new_login_replaces_same_account_without_touching_other_account(tmp_path, monkeypatch):
    root, _profile = _setup(tmp_path, monkeypatch, [
        _row("old", "account-a", "old-chain"),
        _row("other", "account-b", "other-chain"),
    ])
    pool = CP.load_pool("openai-codex")
    added = pool.add_entry(CP.PooledCredential.from_dict("openai-codex", _row(
        "new", "account-a", "new-chain")))
    assert added.id == "old"
    rows = json.loads((root / "auth.json").read_text())["credential_pool"]["openai-codex"]
    assert {row["id"]: row["refresh_token"] for row in rows} == {
        "old": "new-chain", "other": "other-chain"}


def test_two_profiles_refresh_root_chain_once(tmp_path, monkeypatch):
    root, _profile = _setup(tmp_path, monkeypatch, [_row("a", "account-a", "old-chain")])
    calls = []
    def refresh(_access, refresh_token, **_kwargs):
        calls.append(refresh_token)
        return {"access_token": _jwt("account-a"), "refresh_token": "new-chain"}
    monkeypatch.setattr(A, "refresh_codex_oauth_pure", refresh)
    first = CP.load_pool("openai-codex")
    second_profile = root / "profiles" / "two"
    second_profile.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(second_profile))
    second = CP.load_pool("openai-codex")
    pools = [first, second]
    results = []
    threads = [threading.Thread(target=lambda pool=pool: results.append(pool._refresh_entry(pool.entries()[0], force=True))) for pool in pools]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert calls == ["old-chain"]
    assert all(result.refresh_token == "new-chain" for result in results)
    assert json.loads((root / "auth.json").read_text())["credential_pool"]["openai-codex"][0]["refresh_token"] == "new-chain"


def test_stale_snapshot_cannot_restore_rotated_chain(tmp_path, monkeypatch):
    root, _ = _setup(tmp_path, monkeypatch, [
        {**_row("a", "account-a", "old-chain"), "last_refresh": "2026-01-01T00:00:00Z"},
        _row("b", "account-b", "other-chain"),
    ])
    stale = CP.load_pool("openai-codex")
    second_profile = root / "profiles" / "two"
    second_profile.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(second_profile))
    fresh = CP.load_pool("openai-codex")
    calls = []

    def refresh(_access, chain, **_kwargs):
        calls.append(chain)
        return {"access_token": _jwt("account-a"), "refresh_token": "new-chain",
                "last_refresh": "2026-01-02T00:00:00Z"}

    monkeypatch.setattr(A, "refresh_codex_oauth_pure", refresh)
    rotated = fresh._refresh_entry(next(e for e in fresh.entries() if e.id == "a"), force=True)
    assert rotated.refresh_token == "new-chain"
    stale.mark_exhausted_and_rotate(status_code=429, credential_id="b")
    rows = json.loads((root / "auth.json").read_text())["credential_pool"]["openai-codex"]
    assert next(row for row in rows if row["id"] == "a")["refresh_token"] == "new-chain"
    assert fresh._sync_codex_entry_from_pool_store(rotated).refresh_token == "new-chain"
    assert calls == ["old-chain"]


def test_long_lived_profile_sees_other_profiles_cooldown(tmp_path, monkeypatch):
    root, _ = _setup(tmp_path, monkeypatch, [
        _row("a", "account-a", "chain-a"), _row("b", "account-b", "chain-b"),
    ])
    first = CP.load_pool("openai-codex")
    second_profile = root / "profiles" / "two"
    second_profile.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(second_profile))
    second = CP.load_pool("openai-codex")
    second.mark_exhausted_and_rotate(status_code=429, credential_id="a")
    assert first.available_account_count() == 1
    assert {first.select().id for _ in range(4)} == {"b"}
