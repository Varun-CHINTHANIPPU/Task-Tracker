from datetime import datetime
import json
import sqlite3

import pytest

from app import timeutil
from app.reports import build_report


def freeze(monkeypatch, value):
    moment = datetime.fromisoformat(value).astimezone()
    monkeypatch.setattr(timeutil, "now", lambda: moment)
    return moment


def task_row(service, tid):
    return service._task(tid)


def test_rollover_closes_timer_at_midnight(service, monkeypatch):
    start = freeze(monkeypatch, "2026-01-01T23:30:00")
    tid = service.create_task("Overnight")
    sid = service.start_task(tid)
    freeze(monkeypatch, "2026-01-03T09:00:00")
    assert service.rollover() == 2
    session = service.db.conn.execute(
        "SELECT * FROM activity_sessions WHERE id=?", (sid,)
    ).fetchone()
    assert session["duration_seconds"] == 1800
    assert (
        datetime.fromisoformat(session["end_timestamp"]).timestamp()
        == start.timestamp() + 1800
    )
    assert task_row(service, tid)["active_session_id"] is None
    assert task_row(service, tid)["active_date"] == "2026-01-03"
    assert service.rollover() == 0
    assert service.summary("2026-01-01", "2026-01-03")["seconds"] == 1800


@pytest.mark.parametrize(
    "start, today, expected",
    [
        ("2026-01-31", "2026-03-01", "2026-03-31"),
        ("2028-01-31", "2028-03-01", "2028-03-31"),
        ("2026-12-30", "2027-01-01", "2027-01-30"),
    ],
)
def test_monthly_schedule_keeps_original_day(
    service, monkeypatch, start, today, expected
):
    freeze(monkeypatch, f"{start}T09:00:00")
    tid = service.create_task("Monthly", recurrence="monthly", start_date=start)
    service.rollover(today)
    assert task_row(service, tid)["active_date"] == expected


def test_end_date_is_inclusive_and_rollover_stops(service, monkeypatch):
    freeze(monkeypatch, "2026-01-01T09:00:00")
    tid = service.create_task("Limited", start_date="2026-01-01", end_date="2026-01-03")
    assert service.rollover("2026-04-01") == 3
    assert service.rollover("2026-04-01") == 0
    assert service.summary("2026-01-01", "2026-04-01")["missed"] == 3
    assert task_row(service, tid)["status"] == "Missed"
    assert (
        service.db.conn.execute(
            "SELECT MAX(event_date) FROM journal_entries"
        ).fetchone()[0]
        == "2026-01-03"
    )


def test_completed_final_day_stays_completed(service, monkeypatch):
    freeze(monkeypatch, "2026-01-01T12:00:00")
    tid = service.create_task("Limited", end_date="2026-01-01")
    service.complete_task(tid)
    service.rollover("2026-01-10")
    assert task_row(service, tid)["status"] == "Completed"
    assert service.summary("2026-01-01", "2026-01-10")["missed"] == 0


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "  "},
        {"recurrence": "yearly"},
        {"start_date": "garbage"},
        {"end_date": "2020-01-01"},
        {"priority": 0},
        {"priority": 6},
        {"expected_minutes": -1},
        {"expected_minutes": 1.5},
    ],
)
def test_invalid_task_does_not_create_project(service, fields):
    values = {"name": "Task", "project": "Should not exist", "start_date": "2026-01-01"}
    values.update(fields)
    with pytest.raises(ValueError):
        service.create_task(**values)
    assert not service.tasks()
    assert service.db.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0


@pytest.mark.parametrize("action", ["start_task", "complete_task"])
@pytest.mark.parametrize(
    "fields",
    [
        {"start_date": "2026-01-02"},
        {"start_date": "2025-12-01", "end_date": "2025-12-31"},
    ],
)
def test_cannot_act_on_future_or_expired_task(service, monkeypatch, action, fields):
    freeze(monkeypatch, "2026-01-01T12:00:00")
    tid = service.create_task("Not due", **fields)
    with pytest.raises(ValueError):
        getattr(service, action)(tid)
    assert (
        service.db.conn.execute("SELECT COUNT(*) FROM activity_sessions").fetchone()[0]
        == 0
    )


def test_archiving_api_is_removed_and_field_rejected(service):
    tid = service.create_task("Keep")
    assert not hasattr(service, "archive_task")
    with pytest.raises(ValueError, match="Unsupported task fields"):
        service.update_task(tid, archived=1)
    assert service._task(tid)["name"] == "Keep"


def test_failed_start_rolls_back_session(service):
    tid = service.create_task("Atomic start")
    service.db.conn.executescript("""
        CREATE TRIGGER fail_started BEFORE INSERT ON journal_entries
        WHEN NEW.entry_type = 'Started'
        BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END;
    """)
    with pytest.raises(sqlite3.IntegrityError):
        service.start_task(tid)
    assert task_row(service, tid)["status"] == "Pending"
    assert (
        service.db.conn.execute("SELECT COUNT(*) FROM activity_sessions").fetchone()[0]
        == 0
    )
    assert not service.db.conn.in_transaction


def test_invalid_journal_is_rejected_and_next_write_works(service):
    with pytest.raises(ValueError, match="Task not found"):
        service.add_journal("Invalid task", "text", task_id=999)
    with pytest.raises(ValueError):
        service.add_journal(" ", "text")
    with pytest.raises(ValueError):
        service.add_journal("Fake completion", "text", entry_type="Completed")
    service.add_journal("Valid", "text")
    assert len(service.journal()) == 1


def test_one_off_completion_is_not_counted_twice(service, monkeypatch):
    freeze(monkeypatch, "2026-01-01T12:00:00")
    tid = service.create_task("One off", recurrence="none")
    service.complete_task(tid)
    freeze(monkeypatch, "2026-01-02T12:00:00")
    with pytest.raises(ValueError, match="already completed"):
        service.start_task(tid)
    service.complete_task(tid)
    assert service.summary("2026-01-01", "2026-01-02")["completed"] == 1


def test_schedule_update_realigns_active_date(service, monkeypatch):
    freeze(monkeypatch, "2026-01-01T12:00:00")
    tid = service.create_task("Schedule")
    service.update_task(tid, start_date="2026-02-01")
    assert task_row(service, tid)["active_date"] == "2026-02-01"
    with pytest.raises(ValueError):
        service.update_task(tid, end_date="2026-01-01")
    assert task_row(service, tid)["end_date"] is None


def test_pdf_escapes_user_text_and_wraps_long_cells(service, tmp_path):
    pytest.importorskip("reportlab")
    service.create_task("<font> & " + "long task name " * 100, project="<b>unfinished")
    service.add_journal("<font>unfinished & title", "First line\n<tag> & second line")
    output = tmp_path / "nested" / "report.pdf"
    build_report(service, "2000-01-01", "2100-01-01", output)
    assert output.read_bytes().startswith(b"%PDF-")
    assert output.stat().st_size > 1000


def test_invalid_pdf_range_does_not_write(service, tmp_path):
    output = tmp_path / "report.pdf"
    with pytest.raises(ValueError):
        build_report(service, "2026-02-01", "2026-01-01", output)
    assert not output.exists()


def test_json_creates_destination_directory(service, tmp_path):
    service.create_task("Export")
    output = tmp_path / "nested" / "export.json"
    service.export_json(output)
    assert json.loads(output.read_text())["tasks"][0]["name"] == "Export"


def test_stopping_restarted_late_one_off_preserves_completion(service, monkeypatch):
    freeze(monkeypatch, "2026-01-02T12:00:00")
    tid = service.create_task(
        "Late one off", start_date="2026-01-01", recurrence="none"
    )
    service.complete_task(tid)
    with pytest.raises(ValueError, match="already completed"):
        service.start_task(tid)
    service.stop_session(tid)
    assert task_row(service, tid)["status"] == "Completed"


def test_stop_overdue_timer_applies_midnight_cutoff(service, monkeypatch):
    freeze(monkeypatch, "2026-01-01T23:30:00")
    tid = service.create_task("Overnight")
    service.start_task(tid)
    freeze(monkeypatch, "2026-01-02T09:00:00")
    service.stop_session(tid)
    assert service.summary("2026-01-01", "2026-01-02")["seconds"] == 1800
    assert task_row(service, tid)["active_date"] == "2026-01-02"
