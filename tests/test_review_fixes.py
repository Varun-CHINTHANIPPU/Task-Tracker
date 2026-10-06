"""Regressions for lifecycle, migration, and export failures from the code review."""

from datetime import datetime
import json
import sqlite3

import pytest

from app import timeutil
from app.db import Database
from app.services import TrackerService


@pytest.fixture
def clock(monkeypatch):
    moment = datetime.fromisoformat("2026-10-05T09:00:00").astimezone()
    monkeypatch.setattr(timeutil, "now", lambda: moment)

    def advance(value):
        nonlocal moment
        moment = datetime.fromisoformat(value).astimezone()

    return advance


def test_running_oneoff_survives_midnight_and_completes_original_topic(service, clock):
    clock("2026-10-05T23:40:00")
    tid = service.create_task(
        "Overnight",
        recurrence="none",
        end_date="2026-10-05",
        expected_minutes=60,
        topics=["Original topic"],
    )
    sid = service.start_task(tid)
    clock("2026-10-05T23:50:00")
    service.begin_break(tid)
    clock("2026-10-06T00:20:00")
    assert service.rollover() == 0
    task = service.tasks_for_day()[0]
    assert task["active_session_id"] == sid
    assert task["topic"]["topic"] == "Original topic"
    state = service.timer_state(task)
    assert state["elapsed"] == 2400
    assert state["remaining"] == 3000
    service.resume_task(tid)
    clock("2026-10-06T00:30:00")
    service.complete_task(tid, journal="Finished overnight", expected_date="2026-10-05")
    assert service.tasks_for_day()[0]["status"] == "Completed"
    assert service.topics(tid)[0]["completed_at"]
    assert service._task(tid)["active_session_id"] is None
    assert service.summary("2026-10-06", "2026-10-06")["completed"] == 1
    service.update_task(
        tid, end_date="2026-10-07"
    )  # No longer trapped by an open session.
    assert service.clear_history()["activity_sessions"] == 1


def test_never_started_overdue_oneoff_can_start_today_and_disappears_after_completion(
    service, clock
):
    tid = service.create_task(
        "Late",
        recurrence="none",
        start_date="2026-10-01",
        end_date="2026-10-01",
        topics=["Catch up"],
    )
    assert service.tasks_for_day()[0]["status"] == "Pending"
    service.start_task(tid)
    service.complete_task(tid)
    assert service.topics(tid)[0]["completed_at"]
    clock("2026-10-06T09:00:00")
    assert service.tasks_for_day() == []
    assert service.tasks_for_day("2026-10-05")[0]["status"] == "Completed"


def test_oneoff_budget_and_reminder_do_not_reset_at_midnight(service, clock):
    clock("2026-10-05T23:40:00")
    tid = service.create_task("One off", recurrence="none", expected_minutes=30)
    service.start_task(tid)
    clock("2026-10-05T23:50:00")
    service.stop_session(tid)
    clock("2026-10-06T00:10:00")
    service.start_task(tid)
    assert service.timer_state(service.tasks_for_day()[0])["remaining"] == 1200
    clock("2026-10-06T00:30:00")
    assert len(service.take_due_reminders()) == 1
    clock("2026-10-07T00:30:00")
    assert service.take_due_reminders() == []
    service.update_task(tid, expected_minutes=3000)
    assert not list(service.db.conn.execute("SELECT * FROM task_reminders"))


@pytest.mark.parametrize(
    "recurrence,interval", [("daily", 1), ("weekly", 1), ("monthly", 1), ("custom", 3)]
)
def test_create_and_edit_past_schedule_align_without_backfilled_misses(
    service, clock, recurrence, interval
):
    created = service.create_task(
        "Created",
        start_date="2026-09-01",
        recurrence=recurrence,
        recurrence_interval=interval,
    )
    edited = service.create_task("Edited")
    service.update_task(
        edited,
        start_date="2026-09-01",
        recurrence=recurrence,
        recurrence_interval=interval,
    )
    assert service._task(created)["active_date"] == service._task(edited)["active_date"]
    assert service._task(created)["active_date"] >= "2026-10-05"
    service.rollover()
    assert service.journal() == []


def test_extending_expired_end_date_does_not_invent_missed_days(service, clock):
    clock("2026-10-01T09:00:00")
    tid = service.create_task("Range", recurrence="date_range", end_date="2026-10-02")
    clock("2026-10-05T09:00:00")
    service.rollover()
    before = [dict(r) for r in service.journal()]
    assert len(before) == 2
    service.update_task(tid, end_date="2026-10-07")
    assert service._task(tid)["active_date"] == "2026-10-05"
    service.rollover()
    assert [dict(r) for r in service.journal()] == before
    service.start_task(tid)


@pytest.mark.parametrize("target", ["duration", "time_range"])
def test_unstarted_or_stopped_targets_do_not_notify(service, clock, target):
    tid = service.create_task(
        "Target",
        target_type=target,
        expected_minutes=1,
        target_start="09:00",
        target_end="09:01",
    )
    clock("2026-10-05T09:02:00")
    assert service.take_due_reminders() == []
    service.start_task(tid)
    clock("2026-10-05T09:04:00")
    service.stop_session(tid)
    assert service.take_due_reminders() == []
    service.start_task(tid)
    assert len(service.take_due_reminders()) == 1


def test_completed_occurrence_cannot_restart_but_next_day_can(service, clock):
    tid = service.create_task("Daily")
    service.complete_task(tid)
    with pytest.raises(ValueError, match="already completed"):
        service.start_task(tid)
    assert (
        service.db.conn.execute("SELECT COUNT(*) FROM activity_sessions").fetchone()[0]
        == 0
    )
    clock("2026-10-06T09:00:00")
    service.start_task(tid)
    assert service.tasks()[0]["status"] == "In Progress"


def test_stale_legacy_status_is_ignored_and_never_updated(service, clock, tmp_path):
    tid = service.create_task("Derived")
    service.db.conn.execute("UPDATE tasks SET status='Completed' WHERE id=?", (tid,))
    service.db.conn.commit()
    assert service.tasks(status="Pending")[0]["id"] == tid
    service.start_task(tid)
    assert service.tasks()[0]["status"] == "In Progress"
    service.stop_session(tid)
    assert service.tasks()[0]["status"] == "Pending"
    output = tmp_path / "state.json"
    service.export_json(output)
    assert json.loads(output.read_text())["tasks"][0]["status"] == "Pending"
    assert (
        service.db.conn.execute(
            "SELECT status FROM tasks WHERE id=?", (tid,)
        ).fetchone()[0]
        == "Completed"
    )
    service.complete_task(tid)
    service.clear_history()
    assert service.tasks()[0]["status"] == "Pending"


def test_versioned_open_does_not_repeat_archive_purge(service, clock):
    tid = service.create_task("Keep")
    assert (
        service.db.conn.execute("PRAGMA user_version").fetchone()[0]
        == Database.SCHEMA_VERSION
    )
    # An external legacy writer cannot cause an unexpected delete next launch.
    service.db.conn.execute("UPDATE tasks SET archived=1 WHERE id=?", (tid,))
    service.db.conn.commit()
    reopened = Database(service.db.path)
    try:
        assert TrackerService(reopened)._task(tid)["name"] == "Keep"
    finally:
        reopened.close()


def test_migration_failure_rolls_back_ddl_and_version(service, monkeypatch):
    service.create_task("Preserve")
    service.db.conn.execute("ALTER TABLE tasks DROP COLUMN target_type")
    service.db.conn.execute("PRAGMA user_version=0")
    service.db.conn.commit()
    migrate = Database._migrate_legacy_schema

    def fail(db):
        migrate(db)
        raise RuntimeError("Interrupted migration")

    monkeypatch.setattr(Database, "_migrate_legacy_schema", fail)
    with pytest.raises(RuntimeError, match="Interrupted migration"):
        Database(service.db.path)
    assert service.db.conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert "target_type" not in {
        r["name"] for r in service.db.conn.execute("PRAGMA table_info(tasks)")
    }
    monkeypatch.setattr(Database, "_migrate_legacy_schema", migrate)
    upgraded = Database(service.db.path)
    try:
        assert TrackerService(upgraded).tasks()[0]["name"] == "Preserve"
        assert not upgraded.conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        upgraded.close()


def test_future_schema_is_rejected_without_downgrade(service):
    service.db.conn.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError, match="newer application"):
        Database(service.db.path)
    assert service.db.conn.execute("PRAGMA user_version").fetchone()[0] == 999


@pytest.mark.parametrize("failure", ["serialize", "replace"])
def test_failed_export_keeps_previous_file_and_removes_temporary(
    service, tmp_path, monkeypatch, failure
):
    import app.services as module

    output = tmp_path / "export.json"
    output.write_text('{"previous": true}')
    service.create_task("Task")

    def fail(*args, **kwargs):
        if failure == "serialize":
            args[1].write("partial")
        raise OSError("Simulated failure")

    monkeypatch.setattr(
        module.json if failure == "serialize" else module.os,
        "dump" if failure == "serialize" else "replace",
        fail,
    )
    with pytest.raises(OSError, match="Simulated failure"):
        service.export_json(output)
    assert json.loads(output.read_text()) == {"previous": True}
    assert list(tmp_path.glob(".export.json.*.tmp")) == []


def test_dashboard_queries_do_not_grow_per_task_and_idle_reminders_do_not_write(
    service, clock
):
    for i in range(20):
        service.create_task(f"Task {i}", topics=["Topic"])
    statements = []
    service.db.conn.set_trace_callback(statements.append)
    try:
        assert len(service.tasks_for_day()) == 20
        assert sum(s.lstrip().upper().startswith("SELECT") for s in statements) == 3
        statements.clear()
        assert service.take_due_reminders() == []
        assert not any(
            s.upper().startswith(("BEGIN", "INSERT", "UPDATE")) for s in statements
        )
    finally:
        service.db.conn.set_trace_callback(None)
