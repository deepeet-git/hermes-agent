"""Normal event waits must not become failed attempts or loop-breaker alerts."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    kb.init_db()
    conn = kbc.connect()
    yield conn
    conn.close()


def make_task(conn):
    return kb.create_task(conn, title='Observe genuine native traffic', assignee='default')


def make_triage(conn, task):
    assert kb.block_task(conn, task, kind='transient', reason='Actual service failure')
    assert kb.unblock_task(conn, task)
    assert kb.block_task(conn, task, kind='transient', reason='Actual service failure')
    assert kb.get_task(conn, task).status == 'triage'


def test_orchestrator_recovers_triage_to_wait_without_erasing_failures(board):
    task = make_task(board)
    make_triage(board, task)
    events = kb.list_events(board, task)
    runs = kb.list_runs(board, task)
    counters = tuple(board.execute(
        'SELECT block_kind, block_recurrences FROM tasks WHERE id=?', (task,)
    ).fetchone())
    assert kb.schedule_task(board, task, reason='Wake on genuine post-release traffic; Head owns observation')
    assert kb.get_task(board, task).status == 'scheduled'
    assert kb.list_events(board, task)[:len(events)] == events
    assert kb.list_runs(board, task)[:len(runs)] == runs
    assert tuple(board.execute(
        'SELECT block_kind, block_recurrences FROM tasks WHERE id=?', (task,)
    ).fetchone()) == counters
    assert kb.list_events(board, task)[-1].kind == 'scheduled'


def test_repeated_expected_waits_never_emit_block_notifications(board):
    from gateway.kanban_watchers_notifier import TERMINAL_KINDS

    task = make_task(board)
    for condition in ('Wait for safe idle drain', 'Wait for genuine incident traffic'):
        assert kb.schedule_task(board, task, reason=condition)
        kb.recompute_ready(board)
        assert kb.get_task(board, task).status == 'scheduled'
        assert kb.unblock_task(board, task)
    kinds = [event.kind for event in kb.list_events(board, task)]
    assert kinds.count('scheduled') == 2
    assert not {'blocked', 'block_loop_detected'}.intersection(kinds)
    assert 'scheduled' not in TERMINAL_KINDS
    assert all(run.outcome == 'scheduled' for run in kb.list_runs(board, task))


def test_genuine_repeated_failure_still_escalates(board):
    task = make_task(board)
    make_triage(board, task)
    assert kb.list_events(board, task)[-1].kind == 'block_loop_detected'


@pytest.mark.parametrize('triage', [False, True])
def test_stale_worker_cannot_schedule_another_run(board, triage):
    task = make_task(board)
    if triage:
        make_triage(board, task)
    before = kb.get_task(board, task).status
    assert not kb.schedule_task(board, task, reason='Stale wait', expected_run_id=-1)
    assert kb.get_task(board, task).status == before


def test_worker_context_teaches_wait_and_wake_ownership(board):
    task = make_task(board)
    context = kb.build_worker_context(board, task)
    assert 'kanban_schedule' in context
    assert 'wake' in context
    assert 'kanban_block' in context
