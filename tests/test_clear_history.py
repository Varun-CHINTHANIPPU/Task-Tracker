from datetime import datetime
import json
import sqlite3

import pytest

from app import timeutil


@pytest.fixture
def clock(monkeypatch):
    moment = datetime.fromisoformat("2026-10-04T09:00:00").astimezone()
    monkeypatch.setattr(timeutil, "now", lambda: moment)

    def set_time(value):
        nonlocal moment
        moment = datetime.fromisoformat(value).astimezone()

    return set_time


def record_day(service, clock, tid, day):
    clock(f"2026-10-{day:02}T09:00:00")
    service.rollover()
    service.start_task(tid)
    clock(f"2026-10-{day:02}T09:01:00")
    service.begin_break(tid)
    service.take_due_reminders()
    clock(f"2026-10-{day:02}T09:05:00")
    service.complete_task(tid, journal=f"Journal {day}")


def test_clear_range_preserves_other_dates_and_task_definitions(
    service, clock, tmp_path
):
    tid = service.create_task(
        "Study", expected_minutes=1, topics=["One", "Two", "Three"]
    )
    for day in (4, 5, 6):
        record_day(service, clock, tid, day)
    original_topics = [dict(t) for t in service.topics(tid)]
    counts = service.clear_history("2026-10-04", "2026-10-05")
    assert counts["activity_sessions"] == 2
    assert counts["activity_breaks"] == 2
    assert counts["journal_entries"] > 0
    assert {r["event_date"] for r in service.journal()} == {"2026-10-06"}
    assert service.tasks()[0]["expected_minutes"] == 1
    assert service.tasks()[0]["status"] == "Completed"
    topics = service.topics(tid)
    assert [t["id"] for t in topics] == [t["id"] for t in original_topics]
    assert [t["completed_at"] for t in topics[:2]] == [None, None]
    assert topics[2]["completed_at"] == original_topics[2]["completed_at"]
    output = tmp_path / "remaining.json"
    service.export_json(output)
    payload = json.loads(output.read_text())
    assert len(payload["activity_sessions"]) == 1
    assert len(payload["activity_breaks"]) == 1
    assert payload["activity_breaks"] == payload["tasks"][0]["breaks"]
    assert {r["event_date"] for r in payload["task_reminders"]} == {"2026-10-06"}
    assert not service.db.conn.execute("PRAGMA foreign_key_check").fetchall()


def test_clear_all_resets_history_and_completion_but_keeps_plans(service, clock):
    tid = service.create_task("Study", expected_minutes=1, topics=["One", "Two"])
    record_day(service, clock, tid, 4)
    removed = service.create_task("Removed task")
    service.complete_task(removed, journal="Archived journal")
    service.delete_task(removed)
    service.clear_history()
    for table in (
        "journal_entries",
        "activity_sessions",
        "activity_breaks",
        "task_reminders",
    ):
        assert (
            service.db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        )
    assert len(service.tasks()) == 1
    assert len(service.topics(tid)) == 2
    assert service.tasks_for_day()[0]["status"] == "Pending"
    assert not any(t["completed_at"] for t in service.topics(tid))
    service.rollover()
    assert not service.journal()


@pytest.mark.parametrize("dates", [(None, None), ("2026-10-04", "2026-10-05")])
def test_clear_skips_running_task_and_clears_others(service, clock, dates):
    tid = service.create_task("Running", expected_minutes=1, topics=["Keep topic"])
    service.set_topic_completed(service.topics(tid)[0]["id"], True)
    sid = service.start_task(tid)
    clock("2026-10-04T09:01:00")
    service.take_due_reminders()
    service.begin_break(tid)
    entries = [dict(r) for r in service.journal()]
    task = dict(service._task(tid))
    topic = dict(service.topics(tid)[0])
    other = service.create_task("Finished")
    service.start_task(other)
    service.begin_break(other)
    service.complete_task(other, journal="Remove this")
    service.add_journal(title="Unlinked", content="Remove unlinked entry")
    counts = service.clear_history(*dates)
    assert counts["activity_sessions"] == 1
    assert counts["activity_breaks"] == 1
    assert [dict(r) for r in service.journal()] == entries
    assert dict(service._task(tid)) == task
    assert dict(service.topics(tid)[0]) == topic
    assert service._task(tid)["active_session_id"] == sid
    assert service.tasks()[0]["active_break_id"] is not None
    assert (
        service.db.conn.execute("SELECT COUNT(*) FROM task_reminders").fetchone()[0]
        == 1
    )
    service.resume_task(tid)
    service.complete_task(tid, journal="Completed after clearing")
    assert service._task(tid)["status"] == "Completed"
    assert not service.db.conn.execute("PRAGMA foreign_key_check").fetchall()


def test_clear_old_dates_preserves_all_running_tasks_history(service, clock):
    tid = service.create_task("Study", expected_minutes=1)
    record_day(service, clock, tid, 4)
    other = service.create_task("Finished", recurrence="none")
    service.complete_task(other, journal="Delete")
    clock("2026-10-05T09:00:00")
    service.rollover()
    sid = service.start_task(tid)
    service.begin_break(tid)
    service.clear_history("2026-10-04", "2026-10-04")
    assert service.tasks()[0]["active_session_id"] == sid
    assert service.tasks()[0]["active_break_id"] is not None
    assert service.tasks()[0]["total_seconds"] == 300
    assert {r["event_date"] for r in service.journal()} == {"2026-10-04", "2026-10-05"}
    assert all(r["task_id"] == tid for r in service.journal())


def test_clear_does_not_roll_over_running_task(service, clock):
    tid = service.create_task("Running")
    sid = service.start_task(tid)
    clock("2026-10-05T09:00:00")
    service.clear_history()
    assert service._task(tid)["active_session_id"] == sid
    assert service._task(tid)["active_date"] == "2026-10-04"
    assert (
        service.db.conn.execute(
            "SELECT end_timestamp FROM activity_sessions WHERE id=?", (sid,)
        ).fetchone()[0]
        is None
    )


def test_clear_advances_overdue_tasks_without_recreating_old_history(service, clock):
    service.create_task("Daily")
    clock("2026-10-07T09:00:00")
    service.clear_history("2026-10-04", "2026-10-06")
    service.rollover()
    assert service.tasks()[0]["active_date"] == "2026-10-07"
    assert not service.journal()


def test_clear_is_atomic_on_database_error(service, clock):
    tid = service.create_task("Study", expected_minutes=1)
    record_day(service, clock, tid, 4)
    service.db.conn.execute("""CREATE TRIGGER prevent_delete BEFORE DELETE ON journal_entries
                               BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        service.clear_history()
    assert service.journal()
    assert (
        service.db.conn.execute("SELECT COUNT(*) FROM activity_sessions").fetchone()[0]
        == 1
    )
    assert (
        service.db.conn.execute("SELECT COUNT(*) FROM activity_breaks").fetchone()[0]
        == 1
    )
    assert service.tasks()[0]["status"] == "Completed"


@pytest.mark.parametrize(
    "dates", [("2026-10-05", "2026-10-04"), ("bad", "2026-10-04"), (None, "2026-10-04")]
)
def test_clear_validates_dates_before_changing_data(service, clock, dates):
    tid = service.create_task("Study")
    service.complete_task(tid, journal="Keep")
    with pytest.raises(ValueError):
        service.clear_history(*dates)
    assert any(r["content"] == "Keep" for r in service.journal())
