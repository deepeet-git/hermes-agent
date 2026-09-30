"""Skipped cron ticks must not accumulate live SessionDB connections."""
from unittest.mock import MagicMock
import pytest
import cron.scheduler as scheduler
import hermes_state

@pytest.mark.parametrize("mode", ["wake_false", "empty_prompt", "blocked_prompt"])
def test_skipped_ticks_do_not_construct_session_db(monkeypatch, mode):
    db_factory = MagicMock()
    monkeypatch.setattr(hermes_state, "SessionDB", db_factory)
    job = {"id": "test_skipped", "name": "skip regression", "prompt": "test", "deliver": "local"}
    if mode == "wake_false":
        job["script"] = "gate.py"
        monkeypatch.setattr(scheduler, "_run_job_script_with_claim_heartbeat", lambda *a: (True, '{"wakeAgent": false}'))
    elif mode == "empty_prompt":
        monkeypatch.setattr(scheduler, "_build_job_prompt", lambda *a, **kw: None)
    else:
        def blocked(*a, **kw):
            raise scheduler.CronPromptInjectionBlocked("test blocked")
        monkeypatch.setattr(scheduler, "_build_job_prompt", blocked)
    for _ in range(10):
        outcome = scheduler.run_job(job)
        assert outcome[0] is (mode != "blocked_prompt")
    db_factory.assert_not_called()
