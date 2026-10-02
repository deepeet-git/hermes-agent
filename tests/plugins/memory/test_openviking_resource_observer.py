"""Durable acceptance must survive a shorter observer deadline."""
import json
from unittest.mock import MagicMock

import httpx
import pytest

import plugins.memory.openviking as ov


@pytest.fixture
def provider():
    p = ov.OpenVikingMemoryProvider()
    p._client = MagicMock()
    p._client.post.return_value = {"result": {"status": "pending", "task_id": "task-1"}}
    return p


def invoke(p, **kwargs):
    return json.loads(p._tool_add_resource({"url": "https://example.com/reference.md", **kwargs}))


def test_default_is_nonblocking_and_preserves_handle(provider):
    result = invoke(provider)
    assert result["task_id"] == "task-1"
    assert result["status"] == "pending"
    provider._client.post.assert_called_once()
    assert provider._client.post.call_args.args[1]["wait"] is False
    provider._client.get.assert_not_called()


def test_long_wait_never_sent_to_submission(provider):
    provider._client.get.return_value = {"result": {"status": "completed", "result": {"root_uri": "viking://resources/test"}}}
    result = invoke(provider, wait=True, timeout=60)
    assert result["status"] == "completed"
    assert result["root_uri"] == "viking://resources/test"
    payload = provider._client.post.call_args.args[1]
    assert payload["wait"] is False
    assert "timeout" not in payload
    provider._client.post.assert_called_once()
    assert provider._client.get.call_args.kwargs["timeout"] <= ov._TIMEOUT


@pytest.mark.parametrize("error", [TimeoutError(), httpx.ReadTimeout("observer expired")])
def test_observer_timeout_keeps_accepted_task_without_retry(provider, error):
    provider._client.get.side_effect = error
    result = invoke(provider, wait=True, timeout=0.1)
    assert result["task_id"] == "task-1"
    assert result["status"] == "pending"
    assert result["observation_status"] == "timeout"
    provider._client.post.assert_called_once()
    assert provider._client.get.call_args.kwargs["timeout"] <= 0.1


def test_running_task_at_deadline_is_not_failure(provider, monkeypatch):
    clock = iter([0, 0, 2, 2])
    monkeypatch.setattr(ov.time, "monotonic", lambda: next(clock))
    provider._client.get.return_value = {"result": {"status": "running"}}
    result = invoke(provider, wait=True, timeout=1)
    assert result["status"] == "running"
    assert result["observation_status"] == "deadline_reached"
    assert result["task_id"] == "task-1"
    provider._client.post.assert_called_once()


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_terminal_non_success_is_not_reported_completed(provider, status):
    provider._client.get.return_value = {"result": {"status": status}}
    result = invoke(provider, wait=True)
    assert result["status"] == status
    assert result["observation_status"] == "terminal"
    assert "did not complete successfully" in result["message"]


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf"), "invalid"])
def test_bad_deadline_rejected_before_any_submission(provider, timeout):
    result = invoke(provider, wait=True, timeout=timeout)
    assert "error" in result
    provider._client.post.assert_not_called()


def test_missing_handle_does_not_claim_completion(provider):
    provider._client.post.return_value = {"result": {"root_uri": "viking://resources/test"}}
    result = invoke(provider)
    assert result["status"] == "unknown"
    assert "Do not resubmit" in result["message"]


def test_completed_warnings_and_queue_evidence_preserved(provider):
    provider._client.get.return_value = {"result": {"status": "completed", "result": {"queue_status": {"Semantic": {"error_count": 0}}, "warnings": ["optional linking unavailable"]}}}
    result = invoke(provider, wait=True)
    assert result["warnings"] == ["optional linking unavailable"]
    assert result["queue_status"]["Semantic"]["error_count"] == 0


def test_local_upload_uses_same_durable_submission(provider, tmp_path):
    path = tmp_path / "reference.md"
    path.write_text("reference")
    provider._client.upload_temp_file.return_value = "upload-1"
    result = json.loads(provider._tool_add_resource({"url": str(path)}))
    assert result["task_id"] == "task-1"
    provider._client.upload_temp_file.assert_called_once_with(path)
    assert provider._client.post.call_args.args[1]["temp_file_id"] == "upload-1"
