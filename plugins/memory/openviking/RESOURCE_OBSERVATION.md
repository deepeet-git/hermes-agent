# OpenViking resource acceptance and observation

## Timeout failure boundary

`viking_add_resource` previously passed `wait=true` to `/api/v1/resources` while its HTTP client used a 30-second request timeout. Slow summarization could complete after the client disconnected. The tool also discarded the native task ID, leaving no handle to distinguish accepted work from failed ingestion.

## Corrected contract

- Submit once with server `wait=false` and preserve `task_id`, `root_uri`, and a task-specific status endpoint.
- Default calls return the accepted pending/queued state without waiting. Acceptance is not indexing completion.
- Explicit `wait=true` observes `/api/v1/tasks/{task_id}` under an independent finite deadline (default 30 seconds). Each GET is bounded by the remaining budget.
- Deadline expiry or observation failure preserves the task handle and last observed state. It does not cancel or resubmit work.
- Completed, failed, and cancelled tasks remain distinct; completed queue evidence and optional warnings are preserved.
- Missing task handles report an unknown outcome, not success. Check durable task metadata before any manual retry.
- Existing local-file privacy checks, temporary ZIP cleanup and API-key tenant isolation remain unchanged.

## Verification

Focused regression suite: `tests/plugins/memory/test_openviking_resource_observer.py`, alongside `tests/plugins/memory/test_openviking_provider.py`.

Run via `scripts/run_tests.sh` with a development Python containing pytest. Runtime acceptance should use a committed non-secret document, verify the returned task reaches completed, and retrieve its content; never repeatedly submit the same snapshot after an observer deadline.
