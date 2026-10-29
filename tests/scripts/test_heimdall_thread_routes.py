"""Hermetic route repair integration tests: real store and synthetic exact9 plans."""
import asyncio
import copy
import json
import os
from pathlib import Path
import pwd
import signal
import subprocess
import sys
import time

import pytest

from scripts import heimdall_thread_routes as cli

SCOPE = {"guild_id": "100000000000000001", "channel_id": "100000000000000002",
         "webhook_id": "100000000000000003"}
POLICY = {**SCOPE, "author_id": "100000000000000004", "toolsets": ["safe"]}
BOT = "100000000000000005"


def fingerprint(incident):
    import hashlib
    return hashlib.sha256(("incident-id:" + incident).encode()).hexdigest()


def row(incident, tid):
    return {"fingerprint": fingerprint(incident), "scope": dict(SCOPE), "thread_id": tid,
            "lifecycle": "open", "updated_at": 1.0}


class FakeGet:
    def __init__(self, plan):
        self.calls = []
        self.responses = {"/users/@me": (200, {"id": BOT})}
        for item in plan["rows"]:
            tid = item["originalThreadId"]
            channel = copy.deepcopy(item["proof"]["thread"][0]["data"])
            self.responses["/channels/" + tid] = (200, channel)
            source = {"id": tid, "webhook_id": SCOPE["webhook_id"],
                      "author": {"id": POLICY["author_id"]}, "content": "", "attachments": [],
                      "embeds": [{"title": "Incident", "fields": [{"name": "Incident ID", "value": item["incidentId"]}]}]}
            self.responses[f"/channels/{SCOPE['channel_id']}/messages/{tid}"] = (
                (200, source) if item["originalSourceMessagePresent"] else (404, {"code": 10008}))
            if item["expectedRow"]:
                old = item["expectedRow"]["thread_id"]
                self.responses["/channels/" + old] = (200, {"id": old, "last_message_id": "last"})

    def request(self, method, path):
        assert method == "GET", "forbidden outgoing method"
        self.calls.append((method, path))
        value = self.responses[path]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    # No instance construction, including object.__new__, is needed by the CLI.
    monkeypatch.setattr(cli.DiscordAdapter, "__new__", lambda *a, **k: pytest.fail("adapter instantiated"))
    plan = {"schema": "thread-route-reconciliation-plan/1", "operationId": "synthetic-exact9",
            "preparedAt": "2026-01-01T00:00:00+00:00", "rows": [], "preserveExact11": []}
    existing = []
    for i in range(9):
        incident = f"TEST:{i}"
        tid = str(100000000000000100 + i)
        before = row(incident, str(100000000000000200 + i)) if i < 7 else None
        if before:
            before["superseded_thread_ids"] = [str(100000000000000400 + n) for n in range(14)]
            before["unknown_metadata"] = {"nested": [1, 2, {"value": True}]}
            existing.append(before)
        channel = {"id": tid, "type": 11, "guild_id": SCOPE["guild_id"], "parent_id": SCOPE["channel_id"],
                   "owner_id": BOT, "name": f"Alert Incident ID {incident} DETECTED",
                   "thread_metadata": {"locked": False, "archived": True,
                                       "create_timestamp": "2026-01-01T00:00:00+00:00"}}
        proof = {"thread": [{"data": channel}], "starter": [], "laterExactIdentityMessages": []}
        after = copy.deepcopy(before) if before else row(incident, tid)
        after.update(thread_id=tid, updated_at=2.0,
                     route_reconciliation={"operation_id": plan["operationId"], "prepared_at": plan["preparedAt"],
                                           "reason": "restore-proven-original-incident-thread",
                                           "previous_thread_id": before["thread_id"] if before else None,
                                           "previous_row_sha256": cli.digest(before),
                                           "original_proof_sha256": cli.digest(proof),
                                           "historical_delivery_outcome": "UNKNOWN"})
        plan["rows"].append({"incidentId": incident, "fingerprint": fingerprint(incident), "scope": dict(SCOPE),
                             "expectedRow": before, "expectedRowSha256": cli.digest(before),
                             "replacementRow": after, "replacementRowSha256": cli.digest(after),
                             "originalThreadId": tid, "originalThreadCreatedAt": channel["thread_metadata"]["create_timestamp"],
                             "proof": proof, "proofSha256": cli.digest(proof), "originalSourceMessagePresent": i < 5,
                             "rollback": {"expectedRowSha256": cli.digest(after), "replacementRow": before,
                                          "replacementRowSha256": cli.digest(before)}})
    for i in range(11):
        incident = f"GUARD:{i}"
        before = row(incident, str(100000000000000300 + i))
        existing.append(before)
        plan["preserveExact11"].append({"incidentId": incident, "fingerprint": fingerprint(incident), "scope": dict(SCOPE),
                                        "expectedRow": before, "expectedRowSha256": cli.digest(before)})
    existing.append(row("UNRELATED", "100000000000000999"))
    store = cli.HeimdallIncidentThreadStore.default()
    store._path.parent.mkdir(parents=True)
    cli.atomic_json_write(store._path, {"version": 1, "incidents": existing})
    plan["owner"] = {"hermesHome": str(home), "registryPath": str(store._path),
                     "registryOwner": pwd.getpwuid(os.geteuid()).pw_name, "storeVersion": 1,
                     "adapterSha256": cli.file_digest(cli.ROOT / "plugins/platforms/discord/adapter.py"),
                     "cliSha256": cli.file_digest(cli.__file__), "head": "provenance-only"}
    path = tmp_path / "plan.json"

    def save():
        path.write_text(json.dumps(plan))
        return cli.file_digest(path)

    def run(apply=False, client=None):
        return cli.execute(path, save(), apply=apply, policy=POLICY, client=client or FakeGet(plan))

    return plan, store, path, save, run


def test_normal_and_zero_write_rerun(setup, monkeypatch):
    plan, store, path, save, run = setup
    before = cli.snapshot(store)
    result = run(True)
    assert result["status"] == "APPLIED"
    assert list(result["rows"].values()) == ["APPLIED"] * 9
    after = cli.snapshot(store)
    for item in plan["rows"]:
        assert after[cli.key(item)] == item["replacementRow"]
    for k, value in before.items():
        if k not in {cli.key(r) for r in plan["rows"]}:
            assert after[k] == value
    assert len(after) == len(before) + 2
    monkeypatch.setattr(cli.HeimdallIncidentThreadStore, "replace", lambda *a: pytest.fail("rerun wrote"))
    assert set(run(True)["rows"].values()) == {"ALREADY_APPLIED"}
    assert set(run()["rows"].values()) == {"AFTER"}


def test_check_writes_nothing(setup, monkeypatch):
    plan, store, path, save, run = setup
    save()
    original = {p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}
    monkeypatch.setattr(cli.HeimdallIncidentThreadStore, "claim", lambda *a: pytest.fail("check claimed"))
    monkeypatch.setattr(cli, "atomic_json_write", lambda *a: pytest.fail("check wrote"))
    assert run()["status"] == "CHECKED"
    assert {p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file()} == original


@pytest.mark.parametrize("bad", ["corrupt", "extra", "duplicate", "pending", "stale", "missing"])
def test_bad_registry_holds_without_network(setup, bad):
    plan, store, path, save, run = setup
    data = json.loads(store._path.read_bytes())
    if bad == "corrupt":
        store._path.write_bytes(b"invalid")
    elif bad == "missing":
        store._path.unlink()
    else:
        if bad == "extra":
            data["future"] = {}
        elif bad == "duplicate":
            data["incidents"].append(copy.deepcopy(data["incidents"][0]))
        elif bad == "pending":
            data["incidents"][0]["pending"] = True
        else:
            data["incidents"][0]["updated_at"] = 3
        store._path.write_text(json.dumps(data))
    client = FakeGet(plan)
    if bad in {"pending", "stale"}:
        result = run(client=client)
        assert result["status"] == "HOLD" and len(result["rows"]) == 9
        assert "OTHER" in result["rows"].values()
    else:
        with pytest.raises((cli.Hold, OSError)):
            run(client=client)
    assert client.calls == []


def test_unreadable_reread_not_absent(setup, monkeypatch):
    _, _, _, _, run = setup
    monkeypatch.setattr(cli.HeimdallIncidentThreadStore, "reread", lambda self: False)
    with pytest.raises(cli.Hold, match="reread"):
        run()


@pytest.mark.parametrize("failure", ["deletion", "key", "pending", "metadata", "history", "duplicate", "hash", "authority"])
def test_invalid_plan(setup, failure):
    plan, _, _, _, run = setup
    item = plan["rows"][0]
    if failure == "deletion":
        item["replacementRow"] = None
    elif failure == "key":
        item["replacementRow"]["fingerprint"] = "wrong"
    elif failure == "pending":
        item["replacementRow"]["pending"] = True
    elif failure == "metadata":
        item["replacementRow"]["monitor_status"] = "RESOLVED"
    elif failure == "history":
        item["replacementRow"]["superseded_thread_ids"] = []
    elif failure == "duplicate":
        plan["rows"][1] = copy.deepcopy(item)
    elif failure == "authority":
        plan["approval"] = "not authorization"
    else:
        item["proofSha256"] = "wrong"
    if failure not in {"hash", "duplicate"}:
        item["replacementRowSha256"] = cli.digest(item["replacementRow"])
        item["rollback"]["expectedRowSha256"] = item["replacementRowSha256"]
    with pytest.raises(cli.Hold):
        run()


@pytest.mark.parametrize("field,value", [("guild_id", "wrong"), ("parent_id", "wrong"), ("type", 12),
                                          ("owner_id", "wrong"), ("id", "wrong"), ("locked", True),
                                          ("create_timestamp", "changed"), ("name", "changed")])
def test_fresh_channel_identity(setup, field, value):
    plan, _, _, _, run = setup
    client = FakeGet(plan)
    item = plan["rows"][5]  # Source404 path has strict recorded-name proof.
    channel = client.responses["/channels/" + item["originalThreadId"]][1]
    (channel["thread_metadata"] if field in {"locked", "create_timestamp"} else channel)[field] = value
    with pytest.raises(cli.Hold):
        run(True, client)


@pytest.mark.parametrize("response", [(403, {}), (429, {}), (500, {}), (404, {"code": 10003}),
                                       (200, {}), TimeoutError()])
def test_source404_exact_status(setup, response):
    plan, _, _, _, run = setup
    client = FakeGet(plan)
    tid = plan["rows"][5]["originalThreadId"]
    client.responses[f"/channels/{SCOPE['channel_id']}/messages/{tid}"] = response
    with pytest.raises(cli.Hold):
        run(True, client)


@pytest.mark.parametrize("field", ["gone", "fingerprint", "author", "webhook", "content", "attachments"])
def test_source_present_identity(setup, field):
    plan, _, _, _, run = setup
    client = FakeGet(plan)
    tid = plan["rows"][0]["originalThreadId"]
    endpoint = f"/channels/{SCOPE['channel_id']}/messages/{tid}"
    message = client.responses[endpoint][1]
    if field == "gone":
        client.responses[endpoint] = (404, {"code": 10008})
    elif field == "fingerprint":
        message["embeds"][0]["fields"][0]["value"] = "wrong"
    elif field == "author":
        message["author"]["id"] = "wrong"
    elif field == "webhook":
        message["webhook_id"] = "wrong"
    elif field == "attachments":
        message["attachments"] = [{}]
    else:
        message["content"] = "unexpected"
    with pytest.raises(cli.Hold):
        run(True, client)


@pytest.mark.parametrize("failure", ["home-unset", "home-wrong", "path-wrong", "adapter", "cli", "version", "owner"])
def test_binding_failures(setup, monkeypatch, failure):
    plan, _, _, _, run = setup
    if failure == "home-unset":
        monkeypatch.delenv("HERMES_HOME")
    elif failure == "home-wrong":
        plan["owner"]["hermesHome"] += "wrong"
    elif failure == "path-wrong":
        plan["owner"]["registryPath"] += "wrong"
    elif failure in {"adapter", "cli"}:
        plan["owner"][failure + "Sha256"] = "0" * 64
    elif failure == "version":
        plan["owner"]["storeVersion"] = 2
    else:
        plan["owner"]["registryOwner"] = "not-the-owner"
    with pytest.raises(cli.Hold):
        run()


def test_head_is_provenance_only(setup):
    plan, _, _, _, run = setup
    plan["owner"]["head"] = "different-deployed-commit"
    assert run(True)["status"] == "APPLIED"


def test_false_return_after_real_commit_recovers(setup, monkeypatch):
    _, _, _, _, run = setup
    real = cli.HeimdallIncidentThreadStore.replace
    def lost(self, *args):
        assert real(self, *args)
        return False
    monkeypatch.setattr(cli.HeimdallIncidentThreadStore, "replace", lost)
    result = run(True)
    assert result["status"] == "APPLIED"
    assert set(result["rows"].values()) == {"APPLIED"}


def test_partial_commit_stops_remaining(setup, monkeypatch):
    plan, store, _, _, run = setup
    real = cli.HeimdallIncidentThreadStore.replace
    calls = []
    def fail_fourth(self, *args):
        calls.append(args)
        return False if len(calls) == 4 else real(self, *args)
    monkeypatch.setattr(cli.HeimdallIncidentThreadStore, "replace", fail_fourth)
    result = run(True)
    assert result["status"] == "HOLD" and len(calls) == 4
    assert list(result["rows"].values()) == ["APPLIED"] * 3 + ["NOT_APPLIED"]
    current = cli.snapshot(store)
    assert sum(current.get(cli.key(r)) == r["replacementRow"] for r in plan["rows"]) == 3


def test_unrelated_concurrent_writer_retained(setup, monkeypatch):
    _, store, _, _, run = setup
    real = cli.HeimdallIncidentThreadStore.replace
    def mutate(self, *args):
        other = cli.HeimdallIncidentThreadStore.default()
        old = other.get(fingerprint("UNRELATED"), SCOPE)
        changed = {**old, "updated_at": old["updated_at"] + 1}
        assert real(other, old["fingerprint"], SCOPE, old, changed)
        return real(self, *args)
    monkeypatch.setattr(cli.HeimdallIncidentThreadStore, "replace", mutate)
    result = run(True)
    assert result["status"] == "APPLIED"
    assert len(result["unrelatedChangesByKey"]) == 1
    assert cli.snapshot(store)[(fingerprint("UNRELATED"), cli.digest(SCOPE))]["updated_at"] == 10


def test_signal_delayed_through_readback(setup, monkeypatch):
    plan, store, _, _, run = setup
    real = cli.HeimdallIncidentThreadStore.replace
    def interrupted(self, *args):
        os.kill(os.getpid(), signal.SIGTERM)
        return real(self, *args)
    monkeypatch.setattr(cli.HeimdallIncidentThreadStore, "replace", interrupted)
    result = run(True)
    assert result["status"] == "HOLD"
    assert list(result["rows"].values()) == ["APPLIED"]
    assert cli.snapshot(store)[cli.key(plan["rows"][0])] == plan["rows"][0]["replacementRow"]


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
def test_get_wrapper_rejects_mutation(method):
    client = cli.GetOnly("never-used")
    with pytest.raises(cli.Hold, match="method"):
        client.request(method, "/users/@me")


def test_claim_contention_has_bounded_wait(setup, monkeypatch):
    plan, store, _, _, run = setup
    target = sorted(plan["rows"], key=cli.key)[0]
    script = """
import asyncio,json,sys
from plugins.platforms.discord.adapter import HeimdallIncidentThreadStore
async def main():
 s=HeimdallIncidentThreadStore.default()
 async with s.claim(sys.argv[1],json.loads(sys.argv[2])) as ok:
  print('READY' if ok else 'FAIL',flush=True)
  await asyncio.sleep(30)
asyncio.run(main())
"""
    process = subprocess.Popen([sys.executable, "-c", script, target["fingerprint"], json.dumps(SCOPE)],
                               stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "READY"
        before = store._path.read_bytes()
        monkeypatch.setattr(cli, "CLAIM_SECONDS", .5)
        started = time.monotonic()
        result = run(True)
        assert result["status"] == "HOLD" and result["rows"] == {}
        assert time.monotonic() - started < 3
        assert store._path.read_bytes() == before
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_real_subprocess_missing_key_race(setup):
    plan, store, path, save, _ = setup
    sha = save()
    # A wrapper injects only a fake GET transport; the whole apply engine/store are real.
    script = """
import importlib.util,json,sys
from scripts import heimdall_thread_routes as cli
spec=importlib.util.spec_from_file_location('fixtures',sys.argv[1]); m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
p=json.load(open(sys.argv[2]))
# Both processes complete preflight/GET before either enters claims.
import os,time
from pathlib import Path
original=cli.phase_a
def barrier(*args):
 evidence=original(*args)
 directory=Path(sys.argv[2]).parent
 (directory / ('phase-a-'+str(os.getpid()))).touch()
 deadline=time.monotonic()+10
 while len(list(directory.glob('phase-a-*'))) < 2:
  assert time.monotonic() < deadline
  time.sleep(.005)
 return evidence
cli.phase_a=barrier
print(json.dumps(cli.execute(sys.argv[2],sys.argv[3],apply=True,policy=m.POLICY,client=m.FakeGet(p))))
"""
    workers = [subprocess.Popen([sys.executable, "-c", script, __file__, str(path), sha],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    results = []
    for worker in workers:
        output, errors = worker.communicate(timeout=15)
        assert worker.returncode == 0, errors
        results.append(json.loads(output))
    assert all(r["status"] == "APPLIED" for r in results)
    assert sum(sum(v == "APPLIED" for v in r["rows"].values()) for r in results) == 9
    assert len(cli.snapshot(store)) == 21


def test_journal_crash_recovers_exact_after(setup, monkeypatch):
    plan, store, _, _, run = setup
    original = cli.write_journal
    def crash(path, journal):
        if journal["rows"]:
            raise OSError("simulated crash before per-row journal")
        return original(path, journal)
    monkeypatch.setattr(cli, "write_journal", crash)
    failed = run(True)
    assert failed["status"] == "HOLD"
    assert len(failed["exactAfterSubset"]) == 1
    assert "FAILED" in failed["journalStatus"]
    current = cli.snapshot(store)
    assert sum(current.get(cli.key(r)) == r["replacementRow"] for r in plan["rows"]) == 1
    monkeypatch.setattr(cli, "write_journal", original)
    result = run(True)
    assert result["status"] == "APPLIED"
    assert list(result["rows"].values()) == ["ALREADY_APPLIED"] + ["APPLIED"] * 8


@pytest.mark.asyncio
async def test_cancellation_waits_for_claim_cleanup(setup, monkeypatch):
    plan, store, _, _, _ = setup
    entered, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def work(*args):
        async with store.claim(plan["rows"][0]["fingerprint"], SCOPE) as ok:
            assert ok
            entered.set()
            await release.wait()
        cleaned.set()
    monkeypatch.setattr(cli, "phase_b", work)
    task = asyncio.create_task(cli.protected_phase_b())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not cleaned.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()


def test_no_network_under_claims_and_readback_inside(setup, monkeypatch):
    from contextlib import asynccontextmanager
    plan, _, _, _, run = setup
    original_claim = cli.HeimdallIncidentThreadStore.claim
    original_snapshot = cli.snapshot
    held = set()
    readbacks = []
    @asynccontextmanager
    async def claim(self, fp, scope):
        async with original_claim(self, fp, scope) as acquired:
            if acquired:
                held.add(fp)
            try:
                yield acquired
            finally:
                held.discard(fp)
    def snapshot(store):
        if held:
            readbacks.append(len(held))
        return original_snapshot(store)
    client = FakeGet(plan)
    original_request = client.request
    def get(method, path):
        assert not held, "network under claims"
        return original_request(method, path)
    monkeypatch.setattr(cli.HeimdallIncidentThreadStore, "claim", claim)
    monkeypatch.setattr(cli, "snapshot", snapshot)
    monkeypatch.setattr(client, "request", get)
    result = run(True, client)
    assert result["status"] == "APPLIED" and not held
    assert readbacks and set(readbacks) == {9}
    assert result["claimElapsedSeconds"] < cli.HOLD_SECONDS


def test_hold_budget_stops_remaining_cas(setup, monkeypatch):
    _, _, _, _, run = setup
    real_replace = cli.HeimdallIncidentThreadStore.replace
    real_clock = time.monotonic
    offset = [0]
    calls = []
    def replace(self, *args):
        result = real_replace(self, *args)
        calls.append(result)
        if len(calls) == 3:
            offset[0] = 9
        return result
    monkeypatch.setattr(cli.HeimdallIncidentThreadStore, "replace", replace)
    monkeypatch.setattr(cli.time, "monotonic", lambda: real_clock() + offset[0])
    result = run(True)
    assert result["status"] == "HOLD" and len(calls) == 3
    assert len(result["exactAfterSubset"]) == 3
    assert "budget" in result["reason"]


def test_oldest_get_freshness_checked_after_claims(setup, monkeypatch):
    plan, _, _, _, run = setup
    real_phase_a = cli.phase_a
    def old(*args):
        evidence = real_phase_a(*args)
        evidence["oldestMonotonic"] -= cli.FRESH_SECONDS + 1
        return evidence
    monkeypatch.setattr(cli, "phase_a", old)
    monkeypatch.setattr(cli.HeimdallIncidentThreadStore, "replace", lambda *a: pytest.fail("stale identity wrote"))
    result = run(True)
    assert result["status"] == "HOLD" and result["rows"] == {}
    assert "expired" in result["reason"]


def test_store_lock_wait_bounded_by_hold_budget(setup, monkeypatch):
    _, store, _, _, run = setup
    script = """
import sys,time
from plugins.platforms.discord.adapter import HeimdallIncidentThreadStore
s=HeimdallIncidentThreadStore.default();h=s._open_lock_file('store');assert s._try_lock(h)
print('READY',flush=True);time.sleep(30)
"""
    process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "READY"
        original = store._path.read_bytes()
        monkeypatch.setattr(cli, "HOLD_SECONDS", .75)
        started = time.monotonic()
        result = run(True)
        assert result["status"] == "HOLD"
        assert list(result["rows"].values()) == ["NOT_APPLIED"]
        assert time.monotonic() - started < 3
        assert store._path.read_bytes() == original
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_readback_failure_is_unknown_not_absent(setup, monkeypatch):
    _, _, _, _, run = setup
    original = cli.HeimdallIncidentThreadStore.replace
    def ambiguous(self, *args):
        assert original(self, *args)
        monkeypatch.setattr(cli.HeimdallIncidentThreadStore, "reread", lambda self: False)
        return False
    monkeypatch.setattr(cli.HeimdallIncidentThreadStore, "replace", ambiguous)
    result = run(True)
    assert result["status"] == "HOLD"
    assert set(result["rows"].values()) == {"HOLD_UNKNOWN"}
    assert result["readbackStatus"] == "HOLD_UNKNOWN"


def test_oserror_reread_without_absent_classification(setup, monkeypatch):
    _, store, _, _, run = setup
    read = Path.read_bytes
    def unavailable(path):
        if path == store._path:
            raise PermissionError("synthetic inaccessible registry")
        return read(path)
    monkeypatch.setattr(Path, "read_bytes", unavailable)
    with pytest.raises(cli.Hold, match="reread"):
        run()


def test_changed_guard_route_blocks_all(setup):
    _, store, _, _, run = setup
    data = json.loads(store._path.read_bytes())
    data["incidents"][7]["thread_id"] = "100000000000000999"
    store._path.write_text(json.dumps(data))
    with pytest.raises(cli.Hold, match="guard"):
        run(True)


def test_new_guard_event_fields_preserved(setup):
    plan, store, _, _, run = setup
    data = json.loads(store._path.read_bytes())
    data["incidents"][7].update(updated_at=99, new_metadata="new event")
    store._path.write_text(json.dumps(data))
    assert run(True)["status"] == "APPLIED"
    assert cli.snapshot(store)[cli.key(plan["preserveExact11"][0])]["new_metadata"] == "new event"


def test_hash_flag_is_exact_input_binding(setup):
    _, _, path, save, _ = setup
    save()
    with pytest.raises(cli.Hold, match="packet hash"):
        cli.execute(path, "0" * 64)


def test_real_config_loader_is_read_only(setup):
    import yaml
    _, store, _, _, _ = setup
    home = store._path.parent.parent
    (home / "config.yaml").write_text(yaml.safe_dump({"platforms": {"discord": {"heimdall_incident_intake": POLICY}}}))
    # CRLF forces in-memory normalization; this parser must never sanitize to disk.
    env = home / ".env"
    env.write_bytes(b'DISCORD_BOT_TOKEN="synthetic-secret"\r\n')
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    policy, client = cli.config_and_client()
    assert policy == POLICY and isinstance(client, cli.GetOnly)
    assert {p: p.read_bytes() for p in home.rglob("*") if p.is_file()} == before


def test_get_opener_has_fixed_truthful_client_identification():
    client = cli.GetOnly("synthetic-secret")
    assert client._opener.addheaders == [
        ("User-agent", "HeimdallThreadRoutes/1.0 (+https://github.com/deepeet-git/hermes-agent)")
    ]


def test_get_single_attempt_timeout_and_redirect_refusal(monkeypatch):
    from urllib.error import HTTPError
    import io
    client = cli.GetOnly("synthetic-secret")
    calls = []
    def unavailable(req, timeout):
        calls.append((req.get_method(), timeout))
        raise HTTPError(req.full_url, 429, "rate limit", {}, io.BytesIO(b'{"code":0}'))
    monkeypatch.setattr(client._opener, "open", unavailable)
    assert client.request("GET", "/users/@me") == (429, {"code": 0})
    assert calls == [("GET", 10)]
    assert cli.NoRedirect().redirect_request(None, None, None, None, None, None) is None


def test_cli_modes_and_forbidden_extra_flags():
    for flags in (["--check", "--apply"], ["--rollback"], ["--state", "/tmp/forbidden", "--check"],
                  ["--profile", "other", "--check"], []):
        with pytest.raises(SystemExit) as result:
            cli.main(["--plan", "/does-not-exist", "--expected-plan-sha256", "0" * 64, *flags])
        assert result.value.code == 2


def test_durable_audit_failure_prevents_registry_mutation(setup, monkeypatch):
    _, store, _, _, run = setup
    original = store._path.read_bytes()
    def unavailable(*args):
        raise OSError("audit unavailable")
    monkeypatch.setattr(cli, "write_journal", unavailable)
    monkeypatch.setattr(cli.HeimdallIncidentThreadStore, "claim", lambda *a: pytest.fail("audit failure claimed"))
    with pytest.raises(OSError):
        run(True)
    assert store._path.read_bytes() == original


def test_same_operation_cannot_replace_audit_packet(setup):
    plan, store, _, _, run = setup
    assert run(True)["status"] == "APPLIED"
    original = store._path.read_bytes()
    plan["owner"]["head"] = "different provenance makes distinct packet"
    with pytest.raises(cli.Hold, match="another packet"):
        run(True)
    assert store._path.read_bytes() == original


def test_real_process_crash_releases_claims_and_recovers(setup):
    plan, store, path, save, run = setup
    sha = save()
    script = """
import importlib.util,json,os,sys
from scripts import heimdall_thread_routes as cli
spec=importlib.util.spec_from_file_location('fixtures',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
p=json.load(open(sys.argv[2])); original=cli.write_journal
def crash(path,journal):
 if journal['rows']:
  os._exit(73)
 return original(path,journal)
cli.write_journal=crash
cli.execute(sys.argv[2],sys.argv[3],apply=True,policy=m.POLICY,client=m.FakeGet(p))
"""
    process = subprocess.run([sys.executable, "-c", script, __file__, str(path), sha], capture_output=True, text=True, timeout=15)
    assert process.returncode == 73, process.stderr
    before = cli.snapshot(store)
    assert sum(before.get(cli.key(r)) == r["replacementRow"] for r in plan["rows"]) == 1
    result = run(True)
    assert result["status"] == "APPLIED"
    assert list(result["rows"].values()) == ["ALREADY_APPLIED"] + ["APPLIED"] * 8


@pytest.mark.parametrize("change_target", [False, True])
def test_guard_change_preserves_subset_and_check(setup, monkeypatch, capsys, change_target):
    plan, store, path, save, run = setup
    real = cli.HeimdallIncidentThreadStore.replace
    calls = []
    def replace(self, *args):
        calls.append(args[0])
        ok = real(self, *args)
        if len(calls) == 3:
            guard = plan['preserveExact11'][0]
            assert cli.HeimdallIncidentThreadStore.default().remember(
                guard['fingerprint'], guard['scope'], '100000000000000777')
            if change_target:
                target = plan['rows'][4]
                assert cli.HeimdallIncidentThreadStore.default().remember(
                    target['fingerprint'], target['scope'], '100000000000000778')
        return ok
    monkeypatch.setattr(cli.HeimdallIncidentThreadStore, 'replace', replace)
    result = run(True)
    expected = [r['incidentId'] for r in plan['rows'][:3]]
    assert result['status'] == 'HOLD' and len(calls) == 3
    assert result['exactAfterSubset'] == expected
    assert len(result['readback']) == 9 and result['guardStatus'] == 'HOLD'
    before = {p: p.read_bytes() for p in store._path.parent.parent.rglob('*') if p.is_file()}
    monkeypatch.setattr(cli.HeimdallIncidentThreadStore, 'claim', lambda *a: pytest.fail('check claimed'))
    monkeypatch.setattr(cli, 'atomic_json_write', lambda *a: pytest.fail('check wrote'))
    checked = run()
    assert checked['status'] == 'HOLD' and checked['guardStatus'] == 'HOLD'
    assert checked['exactAfterSubset'] == expected and checked['rows'] == result['readback']
    assert ('OTHER' in checked['rows'].values()) == change_target
    assert cli.main(['--plan', str(path), '--expected-plan-sha256', save(), '--check']) == 2
    assert json.loads(capsys.readouterr().out)['exactAfterSubset'] == expected
    assert {p: p.read_bytes() for p in store._path.parent.parent.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('sig', [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize('at', [1, 9])
def test_signal_main_exit_cleanup_and_no_optional_http(setup, monkeypatch, sig, at, capsys):
    plan, store, path, save, _ = setup
    real = cli.HeimdallIncidentThreadStore.replace
    calls = []
    client = FakeGet(plan)
    phase_a_calls = 1 + 2 * 9 + 7
    def replace(self, *args):
        calls.append(args[0])
        if len(calls) == at:
            os.kill(os.getpid(), sig)
        return real(self, *args)
    monkeypatch.setattr(cli.HeimdallIncidentThreadStore, 'replace', replace)
    monkeypatch.setattr(cli, 'config_and_client', lambda: (POLICY, client))
    code = cli.main(['--plan', str(path), '--expected-plan-sha256', save(), '--apply'])
    result = json.loads(capsys.readouterr().out)
    assert code == 128 + sig and result['status'] == 'HOLD'
    assert result['deferredSignals'] == [sig] and result['interruptedSignal'] == sig
    assert len(client.calls) == phase_a_calls and result['laterLastMessageIdsAfter'] == {}
    assert len(calls) == at and len(result['exactAfterSubset']) == at
    journals = list((store._path.parent / 'route-reconciliation' / plan['operationId']).glob('journal-*.json'))
    durable = json.loads(journals[0].read_text())
    assert durable['readback'] == result['readback'] and durable['interruptedSignal'] == sig
    async def reacquire():
        for item in plan['rows']:
            async with cli.HeimdallIncidentThreadStore.default().claim(item['fingerprint'], item['scope']) as ok:
                assert ok
    asyncio.run(reacquire())
