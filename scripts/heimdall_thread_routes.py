#!/usr/bin/env python3
"""Exact nine route-only CAS repair. A plan hash binds input, never authority.

Nine independent row commits; failures preserve the committed subset. No rollback.
Run with explicit HERMES_HOME and the owning package's Python interpreter.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import AsyncExitStack, contextmanager
from types import SimpleNamespace
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_constants import get_hermes_home
from plugins.platforms.discord.adapter import DiscordAdapter, HeimdallIncidentThreadStore
from utils import atomic_json_write

CLAIM_SECONDS = 5.0
HOLD_SECONDS = 8.0
FRESH_SECONDS = 120.0
USER_AGENT = "HeimdallThreadRoutes/1.0 (+https://github.com/deepeet-git/hermes-agent)"


class Hold(Exception):
    """Fail closed; no inference of absence or delivery."""


def require(condition, reason):
    if not condition:
        raise Hold(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON property")
            result[key] = value
        return result
    def invalid(_):
        raise Hold("nonfinite JSON number")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def key(row):
    return (row["fingerprint"], digest(row["scope"]))


def validate_plan(plan):
    require(plan.get("schema") == "thread-route-reconciliation-plan/1", "plan schema")
    # Legacy design packets must be reissued by the owner, never upgraded here.
    require(not ({"state", "executionAllowedNow", "approval"} & plan.keys()),
            "reissue design packet without authority fields")
    op = plan.get("operationId", "")
    require(isinstance(op, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", op),
            "operation identity")
    rows, guards = plan.get("rows", []), plan.get("preserveExact11", [])
    require(isinstance(rows, list) and len(rows) == 9, "exact nine rows required")
    require(isinstance(guards, list) and len(guards) == 11, "exact eleven guards required")
    identities, keys, targets = set(), set(), set()
    for row in rows + guards:
        incident = row.get("incidentId")
        require(isinstance(incident, str) and 0 < len(incident) <= 256, "incident identity")
        require(row.get("fingerprint") == hashlib.sha256(("incident-id:" + incident).encode()).hexdigest(),
                "incident fingerprint")
        scope = row.get("scope")
        require(isinstance(scope, dict) and set(scope) == {"guild_id", "channel_id", "webhook_id"}
                and all(DiscordAdapter._is_canonical_discord_snowflake(v) for v in scope.values()),
                "canonical scope")
        require(incident not in identities and key(row) not in keys, "duplicate allowlist identity")
        identities.add(incident)
        keys.add(key(row))
        before = row.get("expectedRow")
        require(digest(before) == row.get("expectedRowSha256"), "before hash")
        if before is not None:
            require(HeimdallIncidentThreadStore._valid_incident(before)
                    and key(before) == key(row) and not before.get("pending"), "invalid before row")
    for row in rows:
        before, after = row.get("expectedRow"), row.get("replacementRow")
        require(isinstance(after, dict) and HeimdallIncidentThreadStore._valid_incident(after),
                "invalid replacement or deletion")
        require(key(after) == key(row) and not after.get("pending"), "replacement key or pending")
        require(digest(after) == row.get("replacementRowSha256"), "after hash")
        require(after["thread_id"] == row.get("originalThreadId")
                and DiscordAdapter._is_canonical_discord_snowflake(after["thread_id"]), "target identity")
        require(after["thread_id"] not in targets, "duplicate target")
        targets.add(after["thread_id"])
        metadata = after.get("route_reconciliation", {})
        require(set(metadata) == {"operation_id", "prepared_at", "reason", "previous_thread_id",
                                  "previous_row_sha256", "original_proof_sha256", "historical_delivery_outcome"}
                and metadata.get("historical_delivery_outcome") == "UNKNOWN"
                and metadata.get("prepared_at") == plan.get("preparedAt")
                and metadata.get("reason") == "restore-proven-original-incident-thread"
                and metadata.get("operation_id") == op
                and metadata.get("previous_row_sha256") == digest(before)
                and metadata.get("previous_thread_id") == (before or {}).get("thread_id")
                and metadata.get("original_proof_sha256") == digest(row.get("proof")), "operation metadata")
        require(digest(row.get("proof")) == row.get("proofSha256"), "proof hash")
        rollback = row.get("rollback", {})
        require(rollback.get("replacementRow") == before
                and rollback.get("replacementRowSha256") == digest(before)
                and rollback.get("expectedRowSha256") == digest(after), "reverse evidence hash")
        if before is not None:
            old = {k: v for k, v in before.items() if k not in {"thread_id", "updated_at", "route_reconciliation"}}
            new = {k: v for k, v in after.items() if k not in {"thread_id", "updated_at", "route_reconciliation"}}
            require(old == new and before["thread_id"] != after["thread_id"], "route-only replacement")
        else:
            require(set(after) == {"fingerprint", "scope", "thread_id", "lifecycle", "updated_at",
                                   "route_reconciliation"} and after["lifecycle"] == "open", "insert fields")
        require(type(row.get("originalSourceMessagePresent")) is bool, "source proof kind")
        thread = row.get("proof", {}).get("thread", [])
        require(len(thread) == 1 and isinstance(thread[0].get("data"), dict), "recorded thread proof")
    require(sum(r["expectedRow"] is None for r in rows) == 2, "seven replacements and two inserts")
    require(all(g["expectedRow"] is not None for g in guards), "guard absence")


def bindings(plan):
    owner = plan["owner"]
    require(bool(os.environ.get("HERMES_HOME")), "explicit HERMES_HOME required")
    require(get_hermes_home().resolve() == Path(owner["hermesHome"]).resolve(), "home mismatch")
    store = HeimdallIncidentThreadStore.default()
    require(store._path.resolve() == Path(owner["registryPath"]).resolve(), "registry path mismatch")
    stat = store._path.stat()  # A missing/unreadable registry is never an absent row.
    require(stat.st_uid == os.geteuid()
            and pwd.getpwuid(stat.st_uid).pw_name == owner["registryOwner"], "registry owner mismatch")
    require(HeimdallIncidentThreadStore._VERSION == owner["storeVersion"] == 1, "store version")
    require(file_digest(ROOT / "plugins/platforms/discord/adapter.py") == owner["adapterSha256"],
            "adapter source mismatch")
    require(file_digest(__file__) == owner["cliSha256"], "CLI source mismatch")
    return store


def snapshot(store):
    require(store.reread() is True, "strict registry reread failed")
    data = strict_json(store._path.read_bytes())
    require(isinstance(data, dict) and set(data) == {"version", "incidents"}
            and data["version"] == store._VERSION, "registry top-level keys/version")
    require(isinstance(data["incidents"], list)
            and all(store._valid_incident(r) for r in data["incidents"]), "invalid registry rows")
    require(data["incidents"] == store._incidents, "registry changed during snapshot")
    index = {}
    for row in data["incidents"]:
        require(key(row) not in index, "duplicate scoped registry row")
        index[key(row)] = row
    return index


def classifications(plan, index):
    result = {}
    for row in plan["rows"]:
        current = index.get(key(row))
        result[row["incidentId"]] = ("AFTER" if current == row["replacementRow"] else
                                      "BEFORE" if current == row["expectedRow"] else "OTHER")
    return result


def validate_guards(plan, index):
    for guard in plan["preserveExact11"]:
        current = index.get(key(guard))
        require(current is not None and not current.get("pending")
                and current["thread_id"] == guard["expectedRow"]["thread_id"], "guard route changed")


def readback_report(plan, index):
    states = classifications(plan, index)
    report = {"readback": states, "exactAfterSubset": [k for k, v in states.items() if v == "AFTER"],
              "guardStatus": "VALID"}
    try:
        validate_guards(plan, index)
    except Hold as exc:
        report.update(guardStatus="HOLD", guardReason=str(exc))
    return report


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class GetOnly:
    """One attempt, ten-second timeout; no credentials in errors or redirects."""
    def __init__(self, token):
        self._token = token
        self._opener = request.build_opener(NoRedirect())
        self._opener.addheaders = [("User-agent", USER_AGENT)]

    def request(self, method, path):
        require(method == "GET", "HTTP method forbidden")
        require(re.fullmatch(r"/(users/@me|channels/[0-9]+(?:/messages/[0-9]+)?)", path), "HTTP path forbidden")
        req = request.Request("https://discord.com/api/v10" + path,
                              headers={"Authorization": "Bot " + self._token}, method="GET")
        try:
            with self._opener.open(req, timeout=10) as response:
                return response.status, strict_json(response.read())
        except error.HTTPError as exc:
            try:
                return exc.code, strict_json(exc.read())
            except Exception:
                raise Hold("Discord HTTP error") from None
        except Exception:
            raise Hold("Discord GET unavailable") from None


def config_and_client():
    # Existing read-only env parser, unlike dotenv sanitization (which writes).
    from hermes_cli.config import load_env
    from gateway.config import Platform, load_gateway_config
    # Gateway configuration normally discovers plugins, which can seed SOUL.md
    # and run arbitrary plugin registration. The owner's existing safe mode
    # suppresses discovery for this standalone read-only configuration probe.
    previous_safe_mode = os.environ.get("HERMES_SAFE_MODE")
    os.environ["HERMES_SAFE_MODE"] = "1"
    try:
        config = load_gateway_config().platforms.get(Platform.DISCORD)
    finally:
        if previous_safe_mode is None:
            os.environ.pop("HERMES_SAFE_MODE", None)
        else:
            os.environ["HERMES_SAFE_MODE"] = previous_safe_mode
    require(config is not None, "Discord configuration absent")
    policy = config.extra.get("heimdall_incident_intake")
    required = {"guild_id", "channel_id", "webhook_id", "author_id"}
    require(isinstance(policy, dict) and set(policy) == required | {"toolsets"}
            and all(DiscordAdapter._is_canonical_discord_snowflake(policy[k]) for k in required)
            and isinstance(policy["toolsets"], list) and bool(policy["toolsets"])
            and all(isinstance(v, str) and v.strip() for v in policy["toolsets"]), "intake configuration")
    token = load_env().get("DISCORD_BOT_TOKEN") or config.token
    require(isinstance(token, str) and bool(token), "Discord credential absent")
    return policy, GetOnly(token)


def phase_a(plan, policy, client):
    def get(path):
        try:
            return client.request("GET", path)
        except Exception:
            raise Hold("identity GET failed") from None
    oldest = time.monotonic()
    status, bot = get("/users/@me")
    require(status == 200 and DiscordAdapter._is_canonical_discord_snowflake(bot.get("id")), "bot identity")
    later = {}
    for row in plan["rows"]:
        scope, tid = row["scope"], row["originalThreadId"]
        require(all(scope[k] == policy[k] for k in scope), "configured scope mismatch")
        status, channel = get("/channels/" + tid)
        meta = channel.get("thread_metadata", {})
        require(status == 200 and channel.get("id") == tid and channel.get("type") == 11
                and channel.get("guild_id") == scope["guild_id"]
                and channel.get("parent_id") == scope["channel_id"]
                and channel.get("owner_id") == bot["id"] and meta.get("locked") is False
                and meta.get("create_timestamp") == row["originalThreadCreatedAt"], "thread identity")
        status, message = get(f"/channels/{scope['channel_id']}/messages/{tid}")
        if row["originalSourceMessagePresent"]:
            require(status == 200 and message.get("id") == tid
                    and message.get("webhook_id") == scope["webhook_id"]
                    and message.get("author", {}).get("id") == policy["author_id"]
                    and message.get("content") == "" and message.get("attachments") == [], "source identity")
            embeds = [SimpleNamespace(**{**e, "fields": [SimpleNamespace(**f) for f in e.get("fields", [])]})
                      for e in message.get("embeds", [])]
            require(DiscordAdapter._heimdall_incident_fingerprint_for_embeds(embeds) == row["fingerprint"],
                    "source fingerprint")
        else:
            recorded = row["proof"]["thread"][0]["data"]
            require(status == 404 and message.get("code") == 10008, "source must remain exact unknown-message")
            require(channel.get("name") == recorded.get("name")
                    and isinstance(channel.get("name"), str)
                    and f"Incident ID {row['incidentId']} " in channel["name"], "source404 exact name")
            require(meta["create_timestamp"] == recorded.get("thread_metadata", {}).get("create_timestamp"),
                    "recorded creation identity")
        before = row["expectedRow"]
        if before is not None:
            status, previous = get("/channels/" + before["thread_id"])
            require(status == 200 and previous.get("id") == before["thread_id"], "later target unavailable")
            later[before["thread_id"]] = previous.get("last_message_id")
    return {"oldestMonotonic": oldest, "completedMonotonic": time.monotonic(), "laterLastMessageIds": later}


@contextmanager
def delayed_signals():
    pending = []
    old = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in old:
            signal.signal(sig, lambda number, frame: pending.append(number))
        yield pending
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


def audit_start(plan, raw, sha, evidence):
    directory = get_hermes_home() / "gateway" / "route-reconciliation" / plan["operationId"]
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    packet = directory / "operation.json"
    # Publish fully fsynced bytes with a no-clobber hard link. Two invocations
    # may race here, but neither can observe the other's half-written packet.
    fd, temporary = tempfile.mkstemp(prefix=".packet-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, packet)
        except FileExistsError:
            require(packet.read_bytes() == raw, "operation ID already bound to another packet")
    finally:
        os.unlink(temporary)
    require(file_digest(packet) == sha, "audit packet hash")
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.SubprocessError):
        head = "unavailable"
    journal = {"operationId": plan["operationId"], "packetSha256": sha, "gitHeadProvenance": head,
               "atomicity": "independent row CAS; committed subset retained; no rollback",
               "phaseA": evidence, "rows": {}, "status": "STARTED"}
    return directory / f"journal-{time.time_ns()}-{os.getpid()}.json", journal


def write_journal(path, journal):
    atomic_json_write(path, journal)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def commit_claimed(plan, store, evidence, journal_path, journal, pending, started):
    """No await/network here. Every exit attempts exact readback under all claims."""
    baseline = None
    try:
        require(time.monotonic() - evidence["oldestMonotonic"] <= FRESH_SECONDS, "identity expired")
        baseline = snapshot(store)
        validate_guards(plan, baseline)
        require("OTHER" not in classifications(plan, baseline).values(), "stale full row")
        for row in plan["rows"]:
            require(not pending and time.monotonic() - started < HOLD_SECONDS, "claim budget/interrupted")
            current = snapshot(HeimdallIncidentThreadStore.default())
            validate_guards(plan, current)
            require("OTHER" not in classifications(plan, current).values(), "concurrent target change")
            state = current.get(key(row))
            if state == row["replacementRow"]:
                outcome = "ALREADY_APPLIED"
            else:
                store._STORE_LOCK_TIMEOUT_SECONDS = max(0, HOLD_SECONDS - (time.monotonic() - started) - .05)
                journal["rows"][row["incidentId"]] = "HOLD_UNKNOWN"
                try:
                    returned = store.replace(row["fingerprint"], row["scope"],
                                             copy.deepcopy(row["expectedRow"]),
                                             copy.deepcopy(row["replacementRow"]))
                except Exception:
                    returned = None
                # False, exception and lost return are ambiguous until exact readback.
                after = snapshot(HeimdallIncidentThreadStore.default()).get(key(row))
                outcome = ("APPLIED" if after == row["replacementRow"] else
                           "NOT_APPLIED" if after == row["expectedRow"] else "HOLD_UNKNOWN")
                journal["lastReplaceReturned"] = returned
            journal["rows"][row["incidentId"]] = outcome
            write_journal(journal_path, journal)
            require(outcome in {"APPLIED", "ALREADY_APPLIED"}, "CAS stopped: " + outcome)
        journal["status"] = "APPLIED"
    except Exception as exc:
        journal["status"] = "HOLD"
        journal["reason"] = str(exc) if isinstance(exc, Hold) else "local I/O failure; readback required"
    finally:
        try:
            final = snapshot(HeimdallIncidentThreadStore.default())
            journal.update(readback_report(plan, final))
            if journal["guardStatus"] == "HOLD":
                journal.update(status="HOLD", reason=journal["guardReason"])
            if journal["status"] == "APPLIED":
                require(set(journal["readback"].values()) == {"AFTER"}, "final rows mismatch")
            if baseline is not None:
                target_keys = {key(r) for r in plan["rows"]}
                journal["unrelatedChangesByKey"] = [list(k) for k in sorted(set(baseline) | set(final))
                                                      if k not in target_keys and baseline.get(k) != final.get(k)]
        except Exception:
            journal["status"] = "HOLD"
            journal["readbackStatus"] = "HOLD_UNKNOWN"
        try:
            write_journal(journal_path, journal)
        except Exception:
            journal["status"] = "HOLD"
            journal["journalStatus"] = "FAILED; registry readback required on restart"
    return journal


async def phase_b(plan, store, evidence, journal_path, journal, pending):
    started = time.monotonic()
    deadline = started + CLAIM_SECONDS
    try:
        async with AsyncExitStack() as stack:
            for row in sorted(plan["rows"], key=key):
                require(not pending and time.monotonic() < deadline, "claim deadline/interrupted")
                store._CLAIM_LOCK_TIMEOUT_SECONDS = max(0, deadline - time.monotonic() - .25)
                acquired = await stack.enter_async_context(store.claim(row["fingerprint"], row["scope"]))
                require(acquired and time.monotonic() <= deadline, "claim unavailable/deadline")
            commit_claimed(plan, store, evidence, journal_path, journal, pending, started)
    except Exception as exc:
        journal["status"] = "HOLD"
        journal["reason"] = str(exc) if isinstance(exc, Hold) else "local claim failure"
    journal["claimElapsedSeconds"] = time.monotonic() - started
    return journal


async def protected_phase_b(*args):
    # A cancelled caller cannot release claims while a synchronous CAS is running.
    task = asyncio.create_task(phase_b(*args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def execute(plan_path, expected_sha, *, apply=False, policy=None, client=None):
    raw = Path(plan_path).read_bytes()
    require(re.fullmatch(r"[0-9a-f]{64}", expected_sha) is not None
            and hashlib.sha256(raw).hexdigest() == expected_sha, "packet hash")
    plan = strict_json(raw)
    require(isinstance(plan, dict), "plan object")
    validate_plan(plan)
    store = bindings(plan)
    initial = snapshot(store)
    report = readback_report(plan, initial)
    states = report["readback"]
    if not apply and ("OTHER" in states.values() or report["guardStatus"] == "HOLD"):
        return {**report, "status": "HOLD", "snapshotOnly": True, "rows": states,
                "reason": report.get("guardReason", "stale full row")}
    validate_guards(plan, initial)
    require("OTHER" not in states.values(), "stale full row")
    if policy is None or client is None:
        policy, client = config_and_client()
    evidence = phase_a(plan, policy, client)
    if not apply:
        return {**report, "status": "CHECKED", "snapshotOnly": True, "rows": states, "phaseA": evidence}
    journal_path, journal = audit_start(plan, raw, expected_sha, evidence)
    write_journal(journal_path, journal)
    with delayed_signals() as pending:
        result = asyncio.run(protected_phase_b(plan, store, evidence, journal_path, journal, pending))
        # Claims are released. Keep signal deferral through the durable outcome.
        result["laterLastMessageIdsAfter"] = {}
        for tid in evidence["laterLastMessageIds"]:
            if pending:
                break
            try:
                status, body = client.request("GET", "/channels/" + tid)
                result["laterLastMessageIdsAfter"][tid] = body.get("last_message_id") if status == 200 else "UNKNOWN"
            except Exception:
                result["laterLastMessageIdsAfter"][tid] = "UNKNOWN"
        result["deferredSignals"] = list(pending)
        if pending:
            result.update(status="HOLD", reason="interrupted after safe cleanup", interruptedSignal=pending[0])
        try:
            write_journal(journal_path, result)
        except Exception:
            result["status"] = "HOLD"
            result["journalStatus"] = "FAILED; registry readback required on restart"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--expected-plan-sha256", required=True)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = execute(args.plan, args.expected_plan_sha256, apply=args.apply)
    except Exception as exc:
        result = {"status": "HOLD", "reason": str(exc) if isinstance(exc, Hold) else "local preflight/I/O failure"}
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    if result.get("interruptedSignal"):
        return 128 + result["interruptedSignal"]
    return 0 if result["status"] in {"CHECKED", "APPLIED"} else 2


if __name__ == "__main__":
    sys.exit(main())
