import json
import sqlite3
from datetime import datetime

import pytest
from app import timeutil
from app.db import Database
from app.services import TrackerService


def test_completion_and_journal_are_atomic(service):
    tid = service.create_task("Study", project="Research")
    sid = service.start_task(tid)
    service.db.conn.executescript("""
        CREATE TRIGGER fail_journal BEFORE INSERT ON journal_entries
        WHEN NEW.entry_type = 'Journal'
        BEGIN SELECT RAISE(ABORT, 'simulated journal failure'); END;
    """)
    with pytest.raises(sqlite3.IntegrityError):
        service.complete_task(tid, journal="My work")
    task = service.tasks()[0]
    assert task["status"] == "In Progress"
    assert task["active_session_id"] == sid
    assert not [r for r in service.journal() if r["entry_type"] == "Completed"]
    assert (
        service.db.conn.execute(
            "SELECT end_timestamp FROM activity_sessions WHERE id=?", (sid,)
        ).fetchone()[0]
        is None
    )


def test_json_groups_each_days_journal_under_correct_task(
    service, monkeypatch, tmp_path
):
    moment = datetime.fromisoformat("2026-10-04T12:00:00").astimezone()
    monkeypatch.setattr(timeutil, "now", lambda: moment)
    first = service.create_task("Study", project="Learning")
    second = service.create_task("Exercise")
    service.complete_task(first, journal="Learned hashing.")
    service.complete_task(second, journal="Walked 3 km.")
    moment = datetime.fromisoformat("2026-10-05T12:00:00").astimezone()
    service.complete_task(first, journal="Learned signatures.")
    output = tmp_path / "export.json"
    service.export_json(output)
    data = json.loads(output.read_text())
    tasks = {task["id"]: task for task in data["tasks"]}
    entries = [
        entry
        for entry in tasks[first]["journal_entries"]
        if entry["entry_type"] == "Journal"
    ]
    assert [(e["event_date"], e["content"]) for e in entries] == [
        ("2026-10-04", "Learned hashing."),
        ("2026-10-05", "Learned signatures."),
    ]
    assert all(
        e["task_id"] == first and e["project_id"] == tasks[first]["project_id"]
        for e in entries
    )
    assert [
        e["content"]
        for e in tasks[second]["journal_entries"]
        if e["entry_type"] == "Journal"
    ] == ["Walked 3 km."]
    assert data["export_metadata"]["schema"] == "1.3"
    assert len(data["journal_entries"]) == sum(
        len(t["journal_entries"]) for t in data["tasks"]
    )


def test_blank_journal_cannot_complete(service):
    tid = service.create_task("Study")
    with pytest.raises(ValueError):
        service.complete_task(tid, journal="  \n")
    assert service.tasks()[0]["status"] == "Pending"


def test_journal_does_not_attach_to_new_day_if_dialog_crosses_midnight(
    service, monkeypatch
):
    moment = datetime.fromisoformat("2026-10-04T23:59:00").astimezone()
    monkeypatch.setattr(timeutil, "now", lambda: moment)
    tid = service.create_task("Study")
    service.start_task(tid)
    moment = datetime.fromisoformat("2026-10-05T00:01:00").astimezone()
    with pytest.raises(ValueError, match="scheduled day changed"):
        service.complete_task(tid, journal="Yesterday", expected_date="2026-10-04")
    assert not [r for r in service.journal() if r["entry_type"] == "Journal"]


def test_manual_order_persists_on_reopen_and_new_tasks_append(service):
    ids = [service.create_task(name) for name in ["Z", "A", "B"]]
    service.reorder_tasks([ids[2], ids[0], ids[1]])
    db = Database(service.db.path)
    try:
        reopened = TrackerService(db)
        assert [r["id"] for r in reopened.tasks()] == [ids[2], ids[0], ids[1]]
        fourth = reopened.create_task("New")
        assert [r["id"] for r in reopened.tasks()] == [ids[2], ids[0], ids[1], fourth]
    finally:
        db.close()


def test_bad_reorder_is_atomic(service):
    first = service.create_task("A")
    second = service.create_task("B")
    for ids in [[first, first], [second, 999]]:
        with pytest.raises(ValueError):
            service.reorder_tasks(ids)
        assert [r["id"] for r in service.tasks()] == [first, second]


def test_existing_database_migrates_order_without_losing_data(tmp_path):
    path = tmp_path / "legacy.db"
    db = Database(path)
    old = TrackerService(db)
    first = old.create_task("Z", priority=1)
    second = old.create_task("A", priority=5)
    old.add_journal("Existing note", "Keep this", task_id=first)
    db.conn.execute("PRAGMA user_version=0")
    db.conn.execute("ALTER TABLE tasks DROP COLUMN sort_order")
    db.conn.commit()
    db.close()
    upgraded = Database(path)
    try:
        service = TrackerService(upgraded)
        assert [r["id"] for r in service.tasks()] == [second, first]
        assert service.journal()[0]["content"] == "Keep this"
        service.reorder_tasks([first, second])
    finally:
        upgraded.close()
    reopened = Database(path)
    try:
        assert [r["id"] for r in TrackerService(reopened).tasks()] == [first, second]
    finally:
        reopened.close()
