# Deepeet fork integration — 2026-10-26

This integration merges upstream `f219708926c55975bcd2202f180655fe179797a9`
into the fork previously at `d9bf28ffeb059b3b146d0e563335d79bf1ebfe55`.
The shared base is `0e2b5f3835832301bb6b081f94ee9fbb7e58db5a`.
Upstream's extracted modules and package manager remain the structural baseline.

## Preserved local contracts

- Discord principal/channel ceilings are enforced at admission, turn and
  background construction, cached-agent reuse, auxiliary compression/hygiene,
  proxy dispatch, and direct tool dispatch. Explicit empty tools stay empty.
  Private context and stored owner prompts cannot cross a restricted principal.
- Verified Heimdall intake retains durable incident-thread identity, uncertain
  create receipts, restart recovery and copy-before-delete webhook routing.
  A generic final Discord 4xx does not prove that a thread is absent.
- Codex OAuth runtime updates use the owning ledger with root-lock serialization
  and upstream's generation-aware, update-only borrowing semantics. Explicit
  profile logins remain independent; automatic CLI recovery cannot clone a
  root/sibling-owned refresh chain. Workspace identity, opt-out and CAS checks
  remain enforced.
- OpenViking remains a local bundled compatibility provider until the external
  catalog implementation preserves authority-scoped recall, exact fallback,
  and durable resource-task observation. Provider discovery and ABC compatibility
  are tested; source integration is not proof of live server operation.
- Recalled memory is historical reference evidence, not automatic current
  authority. Shared fencing/scrubbing stays publicly re-exported.
- Opt-in conversational routing never removes tool schemas or changes the cached
  prefix. Source-dependent questions retain normal reasoning and real tools.
- Aggregate search/worker caps permit one strategy recovery without refunding
  counters, repeating blocked work, editing history, or clearing a stronger halt.
  Repeated tool failures remain hard stops.
- Local descriptor exhaustion stops retry/fallback. Cron cleanup releases the
  canonical borrowed SessionDB rather than closing the owner's live connection.
- Telegram SVG validation and audio-only Discord voice are preserved. Voice
  delivery retains conversation persistence, speaker identity and TTS dedup.

## Superseded implementation removed

- Old inline agent/gateway implementations were replaced by upstream's extracted
  turn, cache, authorization, delivery and lifecycle modules.
- The old high-FD collector patch is superseded by upstream's selector-based
  `fd_wait` implementation. NOFILE startup uses upstream's shared helper.
- Old installer release-fetch workarounds are superseded by the isolated local
  bare-clone installer test driver.
- The duplicate legacy resource-limit test file was removed: every named case
  exists in the canonical `tests/hermes_cli/test_resource_limits.py`, which also
  adds failure/infinity/import coverage and current extracted-module seams.
- Stale mock targets/signatures were migrated instead of adding obsolete
  production facade aliases or weakening authority/crypto guards.

## Verification scope

The selected regression matrix covers the custom delta and verified affected
call paths, not the complete upstream suite. Run files in independent processes
with the canonical `scripts/run_tests_parallel.py`, isolated HOME/HERMES_HOME,
credential-free environment, UTC and zero retries. The integration environment
uses the pinned PM Python 3.14 runtime and locked Discord voice extra.

Run the historical codec file separately with `-m integration`; default marker
selection is not evidence that those real crypto/codec tests executed. Preserve
explicit plaintext protocol-0 and active E2EE protocol-1 test states.

Check both working and staged diffs, absence of unmerged entries, and the
MERGE_HEAD-relative custom delta. Inherited upstream whitespace is compared
against exact upstream blobs rather than broadly rewriting unrelated files.
Dependency patch overrides retain newer upstream pins; audit the workspace
lockfile with scripts disabled.

## Publication versus activation

A repository merge does not migrate the running installation. This upstream
requires the supported PM Python 3.14 runtime. Do not replace the live source
beneath an older interpreter or install the new dependencies into its venv.
Runtime activation requires the supported migration, independent recovery path,
and post-restart Telegram/Discord/provider/memory verification. Credentials,
profiles and session databases are not fixtures or disposable update artifacts.
