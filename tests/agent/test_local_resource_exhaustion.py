"""Local FD exhaustion must not rotate credentials or open retry sockets."""
import pytest

from agent.error_classifier import FailoverReason, classify_api_error


@pytest.mark.parametrize("error", [
    OSError(24, "Too many open files"),
    OSError(23, "File table overflow"),
    RuntimeError("HTTP transport failed: [Errno 24] Too many open files"),
])
def test_local_resource_exhaustion_stops_provider_recovery(error):
    result = classify_api_error(error, provider="openai-codex", model="gpt-test")
    assert result.reason == FailoverReason.local_resource_exhaustion
    assert result.retryable is False
    assert result.should_fallback is False
