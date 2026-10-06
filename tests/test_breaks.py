from datetime import datetime
import json
import sqlite3

import pytest

from app import timeutil
from app.db import Database
from app.services import TrackerService


@pytest.fixture
def clock(monkeypatch):
    moment = datetime.fromisoformat("2026-10-04T09:00:00").astimezone()
    monkeypatch.setattr(timeutil, "now", lambda: moment)

    def set_time(value):
        nonlocal moment
        moment = datetime.fromisoformat(value).astimezone()
        return timeutil.iso()

    return set_time


def intervals(service):
    return service.db.conn.execute(
        "SELECT * FROM activity_breaks ORDER BY id"
    ).fetchall()


def test_break_resume_keeps_same_session_and_elapsed_time_includes_break(
    service, clock
):
    tid = service.create_task("Study", expected_minutes=60)
    sid = service.start_task(tid)
    start = clock("2026-10-04T09:10:00")
    bid = service.begin_break(tid)
    task = service.tasks()[0]
    assert task["active_session_id"] == sid
    assert task["active_break_id"] == bid
    assert task["status"] == "In Progress"
    end = clock("2026-10-04T09:20:00")
    service.resume_task(tid)
    assert service.tasks()[0]["active_session_id"] == sid
    assert service.tasks()[0]["active_break_id"] is None
    interval = intervals(service)[0]
    assert interval["start_timestamp"] == start
    assert interval["end_timestamp"] == end
    assert interval["duration_seconds"] == 600
    assert interval["end_reason"] == "resumed"
    clock("2026-10-04T09:30:00")
    service.complete_task(tid, journal="Studied proofs.")
    session = service.db.conn.execute("SELECT * FROM activity_sessions").fetchone()
    assert session["id"] == sid
    assert session["duration_seconds"] == 1800  # Original wall time stays available.
    assert service.tasks()[0]["total_seconds"] == 1800
    assert service.summary("2026-10-04", "2026-10-04")["seconds"] == 1800
    assert service.summary("2026-10-04", "2026-10-04")["notes"] == 1


def test_json_has_breaks_and_distractions_under_correct_task_and_session(
    service, clock, tmp_path
):
    tid = service.create_task("Study")
    untouched = service.create_task("Other task")
    sid = service.start_task(tid)
    first_start = clock("2026-10-04T09:05:00")
    service.begin_break(tid)
    first_end = clock("2026-10-04T09:08:00")
    service.resume_task(tid)
    second_start = clock("2026-10-04T09:20:00")
    service.begin_break(tid, reason="distraction")
    second_end = clock("2026-10-04T09:25:00")
    service.complete_task(tid, journal="Finished study.")
    output = tmp_path / "export.json"
    service.export_json(output)
    payload = json.loads(output.read_text())
    tasks = {t["id"]: t for t in payload["tasks"]}
    breaks = tasks[tid]["breaks"]
    assert [
        (b["reason"], b["start_timestamp"], b["end_timestamp"], b["duration_seconds"])
        for b in breaks
    ] == [
        ("break", first_start, first_end, 180),
        ("distraction", second_start, second_end, 300),
    ]
    assert all(b["session_id"] == sid and b["task_id"] == tid for b in breaks)
    assert breaks[1]["end_reason"] == "completed"
    assert payload["activity_breaks"] == breaks
    assert payload["activity_sessions"][0]["breaks"] == breaks
    assert payload["activity_sessions"][0]["duration_seconds"] == 1500
    assert payload["activity_sessions"][0]["work_duration_seconds"] == 1020
    task = next(t for t in service.tasks_for_day() if t["id"] == tid)
    assert service.timer_state(task)["day_elapsed"] == 1500
    assert tasks[untouched]["breaks"] == []
    assert tasks[tid]["journal_entries"][-1]["content"] == "Finished study."


def test_break_survives_reopen_and_open_interval_exports_honestly(
    service, clock, tmp_path
):
    tid = service.create_task("Study")
    service.start_task(tid)
    stamp = clock("2026-10-04T09:05:00")
    bid = service.begin_break(tid)
    db = Database(service.db.path)
    try:
        reopened = TrackerService(db)
        assert reopened.tasks()[0]["active_break_id"] == bid
        output = tmp_path / "open.json"
        reopened.export_json(output)
        payload = json.loads(output.read_text())
        assert payload["activity_sessions"][0]["work_duration_seconds"] is None
        interval = payload["tasks"][0]["breaks"][0]
        assert interval["start_timestamp"] == stamp
        assert interval["end_timestamp"] is None
        assert interval["duration_seconds"] is None
        clock("2026-10-04T09:15:00")
        reopened.resume_task(tid)
        assert intervals(reopened)[0]["duration_seconds"] == 600
    finally:
        db.close()


@pytest.mark.parametrize(
    "action, end_reason",
    [
        ("complete_task", "completed"),
        ("stop_session", "stopped"),
    ],
)
def test_ending_a_session_closes_its_open_break(service, clock, action, end_reason):
    tid = service.create_task("Study")
    service.start_task(tid)
    clock("2026-10-04T09:05:00")
    service.begin_break(tid)
    stamp = clock("2026-10-04T09:15:00")
    getattr(service, action)(tid)
    interval = intervals(service)[0]
    assert interval["end_timestamp"] == stamp
    assert interval["duration_seconds"] == 600
    assert interval["end_reason"] == end_reason
    assert (
        service.db.conn.execute(
            "SELECT duration_seconds FROM activity_sessions"
        ).fetchone()[0]
        == 900
    )


def test_midnight_rollover_closes_break_at_same_boundary_as_session(service, clock):
    clock("2026-10-04T23:30:00")
    tid = service.create_task("Study")
    service.start_task(tid)
    clock("2026-10-04T23:45:00")
    service.begin_break(tid)
    clock("2026-10-05T09:00:00")
    service.rollover()
    interval = intervals(service)[0]
    session = service.db.conn.execute("SELECT * FROM activity_sessions").fetchone()
    assert interval["end_timestamp"] == session["end_timestamp"]
    assert interval["duration_seconds"] == 900
    assert interval["end_reason"] == "rollover"
    assert session["duration_seconds"] == 1800
    assert service.tasks()[0]["active_break_id"] is None
    assert service.rollover() == 0


def test_break_transition_validation_prevents_duplicates(service, clock):
    tid = service.create_task("Study")
    with pytest.raises(ValueError, match="Start the task"):
        service.begin_break(tid)
    service.start_task(tid)
    with pytest.raises(ValueError, match="not on a break"):
        service.resume_task(tid)
    with pytest.raises(ValueError, match="Choose break"):
        service.begin_break(tid, reason="unknown")
    service.begin_break(tid)
    with pytest.raises(ValueError, match="Resume the task"):
        service.begin_break(tid, reason="distraction")
    assert len(intervals(service)) == 1


def test_failed_completion_does_not_end_break_or_session(service, clock):
    tid = service.create_task("Study")
    sid = service.start_task(tid)
    bid = service.begin_break(tid)
    service.db.conn.executescript("""
        CREATE TRIGGER fail_journal BEFORE INSERT ON journal_entries
        WHEN NEW.entry_type = 'Journal'
        BEGIN SELECT RAISE(ABORT, 'simulated failure'); END;
    """)
    clock("2026-10-04T09:15:00")
    with pytest.raises(sqlite3.IntegrityError):
        service.complete_task(tid, journal="My journal")
    task = service.tasks()[0]
    assert task["active_session_id"] == sid
    assert task["active_break_id"] == bid
    assert intervals(service)[0]["end_timestamp"] is None
    assert not [
        entry for entry in service.journal() if entry["entry_type"] == "Break ended"
    ]


def test_pre_break_database_upgrades_preserving_tasks_and_sessions(tmp_path, clock):
    path = tmp_path / "old.db"
    db = Database(path)
    old = TrackerService(db)
    tid = old.create_task(
        "Existing",
        description="Legacy description",
        project="Legacy project",
        tags="legacy",
        notes="Keep me",
        expected_minutes=30,
    )
    sid = old.start_task(tid)
    db.conn.execute("PRAGMA user_version=0")
    db.conn.execute("DROP TABLE activity_breaks")
    db.conn.commit()
    db.close()
    db = Database(path)
    try:
        service = TrackerService(db)
        task = service.tasks()[0]
        assert task["id"] == tid
        assert task["active_session_id"] == sid
        assert task["description"] == "Legacy description"
        assert task["project_name"] == "Legacy project"
        assert task["tags"] == "legacy"
        assert task["notes"] == "Keep me"
        assert task["expected_minutes"] == 30
        service.begin_break(tid)
        assert len(intervals(service)) == 1
    finally:
        db.close()
