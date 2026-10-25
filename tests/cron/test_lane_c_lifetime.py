"""A cron finalizer releases its borrow, never the owner's canonical DB."""
from unittest.mock import Mock

import pytest

from cron import scheduler
from hermes_state_registry import acquire, release_or_close


@pytest.mark.parametrize("metadata_failure", [False, True])
def test_finalizer_releases_exact_borrow_even_after_metadata_failure(tmp_path, monkeypatch, metadata_failure):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = acquire(tmp_path / "state.db")
    borrowed = acquire(tmp_path / "state.db")
    assert borrowed is db
    close = Mock(wraps=db.close)
    monkeypatch.setattr(db, "close", close)
    if metadata_failure:
        monkeypatch.setattr(db, "get_compression_tip", Mock(side_effect=OSError("metadata unavailable")))
        monkeypatch.setattr(scheduler, "_set_cron_session_title", Mock(return_value=True))
    try:
        scheduler._finalize_cron_session(borrowed, None, "lane-c", "lane C", "missing-session")
        close.assert_not_called()
        # Last owner release must close it exactly once: no leaked registry borrow.
        release_or_close(db)
        close.assert_called_once()
    finally:
        release_or_close(db)
