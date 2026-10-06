from datetime import datetime
import json

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


def test_edit_targets_schedule_and_preserve_journal(service, clock):
    tid = service.create_task("Daily", expected_minutes=30)
    service.complete_task(tid, journal="Original journal")
    original = [dict(r) for r in service.journal()]
    service.update_task(
        tid,
        name="Weekly",
        recurrence="weekly",
        end_date="2026-12-31",
        target_type="quantity",
        expected_minutes=None,
        quantity_target=3,
        quantity_unit="problems",
    )
    task = service.tasks()[0]
    assert task["quantity_target"] == 3
    assert task["status"] == "Completed"
    assert not service.tasks_for_day("2026-10-05")
    assert service.tasks_for_day("2026-10-11")[0]["name"] == "Weekly"
    assert [(r["id"], r["content"]) for r in service.journal()] == [
        (r["id"], r["content"]) for r in original
    ]
    service.update_task(tid, recurrence="none")
    assert not service.tasks_for_day("2026-10-11")


def test_edit_day_topics_keeps_ids_completion_and_is_atomic(service, clock):
    tid = service.create_task(
        "Study", topics=["Day one", "Day two"], end_date="2026-10-05"
    )
    first = service.topics(tid)[0]
    service.set_topic_completed(first["id"], True)
    completed_at = service.topics(tid)[0]["completed_at"]
    service.update_task(tid, topics=["Updated topic", "Day two"])
    assert service.topics(tid)[0]["id"] == first["id"]
    assert service.topics(tid)[0]["completed_at"] == completed_at
    with pytest.raises(ValueError):
        service.update_task(
            tid,
            name="Must roll back",
            topics=["Invalid change", "Day two", "Past end date"],
        )
    assert service.tasks()[0]["name"] == "Study"
    assert service.topics(tid)[0]["topic"] == "Updated topic"
    service.update_task(
        tid, end_date="2026-10-06", topics=["Updated topic", "Day two", "Day three"]
    )
    assert len(service.topics(tid)) == 3


@pytest.mark.parametrize(
    "fields",
    [
        {"target_type": "unknown"},
        {"target_type": "quantity", "quantity_target": 0},
        {"target_type": "time_range", "target_start": "10:00", "target_end": "09:00"},
        {"target_type": "time_range", "target_start": None, "target_end": "09:00"},
    ],
)
def test_invalid_edit_does_not_partially_save(service, clock, fields):
    tid = service.create_task("Original")
    with pytest.raises(ValueError):
        service.update_task(tid, name="Invalid", **fields)
    assert service.tasks()[0]["name"] == "Original"


def test_edit_timer_target_rearms_reminder_without_stopping(service, clock):
    tid = service.create_task("Timer", expected_minutes=1)
    sid = service.start_task(tid)
    clock("2026-10-04T09:01:00")
    assert service.take_due_reminders()
    service.update_task(tid, expected_minutes=2, end_date=None)
    assert not service.take_due_reminders()
    clock("2026-10-04T09:02:00")
    assert service.take_due_reminders()
    assert service.tasks()[0]["active_session_id"] == sid


def test_delete_running_task_removes_all_related_history(service, clock, tmp_path):
    tid = service.create_task("Recurring")
    service.add_journal(task_id=tid, title="Saved", content="Remove with task")
    service.start_task(tid)
    service.begin_break(tid)
    service.delete_task(tid)
    assert not service.tasks_for_day()
    service.rollover()
    output = tmp_path / "removed.json"
    service.export_json(output)
    payload = json.loads(output.read_text())
    for collection in (
        "tasks",
        "journal_entries",
        "activity_sessions",
        "activity_breaks",
    ):
        assert payload[collection] == []


def test_range_export_filters_nested_and_flat_records_inclusively(
    service, clock, tmp_path
):
    tid = service.create_task("Study", topics=["One", "Two", "Three"])
    service.create_task("Unrelated", recurrence="none", start_date="2099-01-01")
    for day in (4, 5, 6):
        clock(f"2026-10-{day:02}T09:00:00")
        service.rollover()
        service.start_task(tid)
        service.begin_break(tid)
        clock(f"2026-10-{day:02}T09:10:00")
        service.complete_task(tid, journal=f"Journal {day}")
    output = tmp_path / "range.json"
    service.export_json(output, "2026-10-04", "2026-10-05")
    payload = json.loads(output.read_text())
    assert [r["id"] for r in payload["tasks"]] == [tid]
    assert {r["event_date"] for r in payload["journal_entries"]} == {
        "2026-10-04",
        "2026-10-05",
    }
    assert len(payload["activity_sessions"]) == 2
    assert len(payload["activity_breaks"]) == 2
    assert len(payload["task_topics"]) == 2
    task = payload["tasks"][0]
    assert task["journal_entries"] == sorted(
        payload["journal_entries"],
        key=lambda r: (r["event_date"], r["timestamp"], r["id"]),
    )
    assert task["breaks"] == payload["activity_breaks"]
    assert task["topics"] == payload["task_topics"]
    assert all(
        s["breaks"][0]["session_id"] == s["id"] for s in payload["activity_sessions"]
    )
    assert payload["export_metadata"]["date_range"] == {
        "from": "2026-10-04",
        "to": "2026-10-05",
    }
    service.export_json(output, "2026-10-05", "2026-10-05")
    assert len(json.loads(output.read_text())["activity_sessions"]) == 1


def test_export_cross_midnight_session_preserves_complete_break_context(
    service, clock, tmp_path
):
    tid = service.create_task("One off", recurrence="none")
    clock("2026-10-04T23:50:00")
    service.start_task(tid)
    clock("2026-10-05T00:05:00")
    service.begin_break(tid)
    clock("2026-10-05T00:10:00")
    service.complete_task(tid, journal="Finished after midnight")
    output = tmp_path / "range.json"
    service.export_json(output, "2026-10-04", "2026-10-04")
    payload = json.loads(output.read_text())
    assert payload["activity_sessions"][0]["duration_seconds"] == 1200
    assert payload["activity_sessions"][0]["breaks"][0]["duration_seconds"] == 300
    assert all(r["event_date"] == "2026-10-04" for r in payload["journal_entries"])
    service.export_json(output, "2026-10-05", "2026-10-05")
    payload = json.loads(output.read_text())
    assert not payload["activity_sessions"]
    assert not payload["activity_breaks"]
    assert any(r["entry_type"] == "Journal" for r in payload["journal_entries"])


@pytest.mark.parametrize(
    "start,end",
    [("2026-10-06", "2026-10-04"), ("bad", "2026-10-04"), (None, "2026-10-04")],
)
def test_invalid_export_range_does_not_overwrite_file(service, tmp_path, start, end):
    output = tmp_path / "existing.json"
    output.write_text("Keep previous export")
    with pytest.raises(ValueError):
        service.export_json(output, start, end)
    assert output.read_text() == "Keep previous export"


def test_permanent_delete_removes_only_selected_task(service, clock, tmp_path):
    tid = service.create_task("Delete", expected_minutes=1, topics=["First"])
    service.start_task(tid)
    service.begin_break(tid)
    keep = service.create_task("Keep", topics=["Kept topic"])
    service.complete_task(keep, journal="Keep journal")
    keep_topics = [dict(t) for t in service.topics(keep)]
    service.delete_task(tid)
    assert [t["id"] for t in service.tasks()] == [keep]
    assert [dict(t) for t in service.topics(keep)] == keep_topics
    assert all(r["task_id"] == keep for r in service.journal())
    for table in ("activity_sessions", "activity_breaks", "task_reminders"):
        assert (
            service.db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        )
    output = tmp_path / "deleted.json"
    service.export_json(output)
    assert [t["id"] for t in json.loads(output.read_text())["tasks"]] == [keep]
    assert not service.db.conn.execute("PRAGMA foreign_key_check").fetchall()


def test_upgrade_purges_old_deleted_tasks_without_touching_active_tasks(service, clock):
    from app.db import Database
    from app.services import TrackerService

    tid = service.create_task("Old deletion", topics=["Remove"])
    service.start_task(tid)
    service.begin_break(tid)
    service.db.conn.execute("UPDATE tasks SET archived=1 WHERE id=?", (tid,))
    service.db.conn.execute("PRAGMA user_version=0")
    service.db.conn.commit()
    keep = service.create_task("Keep", topics=["Keep"])
    service.complete_task(keep, journal="Kept")
    upgraded = Database(service.db.path)
    try:
        svc = TrackerService(upgraded)
        assert [t["id"] for t in svc.tasks()] == [keep]
        assert svc.topics(keep)[0]["topic"] == "Keep"
        assert any(r["content"] == "Kept" for r in svc.journal())
        assert (
            upgraded.conn.execute("SELECT COUNT(*) FROM activity_breaks").fetchone()[0]
            == 0
        )
        assert not upgraded.conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        upgraded.close()


@pytest.mark.parametrize("with_history", [False, True])
def test_deleted_tasks_are_omitted_from_all_export_forms(
    service, clock, tmp_path, with_history
):
    tid = service.create_task("Deleted", topics=["Topic"])
    if with_history:
        service.complete_task(tid, journal="Deleted journal")
    service.delete_task(tid)
    output = tmp_path / "export.json"
    for period in ((None, None), ("2026-10-04", "2026-10-04")):
        service.export_json(output, *period)
        payload = json.loads(output.read_text())
        assert payload["tasks"] == []
        assert payload["task_topics"] == []
        assert payload["journal_entries"] == []
