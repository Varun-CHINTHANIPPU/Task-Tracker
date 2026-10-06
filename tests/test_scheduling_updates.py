from datetime import datetime
import json
import sqlite3

import pytest

from app import timeutil


@pytest.fixture
def clock(monkeypatch):
    moment = datetime.fromisoformat("2026-10-05T09:00:00").astimezone()
    monkeypatch.setattr(timeutil, "now", lambda: moment)

    def set_time(value):
        nonlocal moment
        moment = datetime.fromisoformat(value).astimezone()

    return set_time


def test_inclusive_end_date_shows_task_without_a_topic(service, clock, tmp_path):
    tid = service.create_task(
        "Study",
        start_date="2026-10-05",
        end_date="2026-10-07",
        topics=["First", "Second"],
    )
    assert service.tasks_for_day("2026-10-07")[0]["id"] == tid
    assert service.tasks_for_day("2026-10-07")[0]["topic"] is None
    assert not service.tasks_for_day("2026-10-08")
    output = tmp_path / "last-day.json"
    service.export_json(output, "2026-10-07", "2026-10-07")
    assert json.loads(output.read_text())["tasks"][0]["id"] == tid
    clock("2026-10-07T09:00:00")
    service.rollover()
    service.start_task(tid)
    service.complete_task(tid, journal="Finished final day without a topic")
    assert service.tasks_for_day()[0]["status"] == "Completed"


def test_custom_interval_is_used_for_topics_rollover_and_export(
    service, clock, tmp_path
):
    tid = service.create_task(
        "Every two days",
        recurrence="custom",
        recurrence_interval=2,
        end_date="2026-10-09",
        topics=["First", "Second", "Third"],
    )
    assert [t["scheduled_date"] for t in service.topics(tid)] == [
        "2026-10-05",
        "2026-10-07",
        "2026-10-09",
    ]
    assert not service.tasks_for_day("2026-10-06")
    assert service.tasks_for_day("2026-10-07")[0]["id"] == tid
    service.complete_task(tid, journal="Day one")
    clock("2026-10-09T09:00:00")
    service.rollover()
    assert service._task(tid)["active_date"] == "2026-10-09"
    assert [
        r["event_date"] for r in service.journal() if r["entry_type"] == "Missed"
    ] == ["2026-10-07"]
    output = tmp_path / "custom.json"
    service.export_json(output)
    assert json.loads(output.read_text())["tasks"][0]["recurrence_interval"] == 2


def test_custom_topic_dates_and_removal_preserve_other_ids_and_journals(service, clock):
    tid = service.create_task(
        "Study",
        end_date="2026-10-10",
        topics=[
            {"scheduled_date": "2026-10-05", "topic": "First"},
            {"scheduled_date": "2026-10-08", "topic": "Third"},
            {"scheduled_date": "2026-10-09", "topic": "Remove me"},
        ],
    )
    service.complete_task(tid, journal="Keep this journal")
    original = [dict(t) for t in service.topics(tid)]
    revised = original[:2]
    revised[1] = dict(revised[1], scheduled_date="2026-10-07", topic="Moved")
    service.update_task(tid, topics=revised, end_date="2026-10-07")
    topics = service.topics(tid)
    assert [t["id"] for t in topics] == [t["id"] for t in original[:2]]
    assert topics[0]["completed_at"] == original[0]["completed_at"]
    assert topics[1]["scheduled_date"] == "2026-10-07"
    assert any(r["content"] == "Keep this journal" for r in service.journal())
    service.update_task(tid, topics=[])
    assert not service.topics(tid)
    assert service.tasks_for_day("2026-10-07")[0]["id"] == tid


def test_date_swaps_preserve_ids_but_do_not_move_completion(service, clock):
    tid = service.create_task("Swap", topics=["One", "Two"])
    service.complete_task(tid, journal="First date done")
    first, second = [dict(t) for t in service.topics(tid)]
    first["scheduled_date"], second["scheduled_date"] = (
        second["scheduled_date"],
        first["scheduled_date"],
    )
    service.update_task(tid, topics=[first, second])
    assert [t["id"] for t in service.topics(tid)] == [second["id"], first["id"]]
    assert not any(t["completed_at"] for t in service.topics(tid))
    assert service.tasks_for_day()[0]["status"] == "Completed"


@pytest.mark.parametrize(
    "topics",
    [
        [{"scheduled_date": "2026-10-08", "topic": "Too late"}],
        [{"scheduled_date": "2026-10-04", "topic": "Too early"}],
        [
            {"scheduled_date": "2026-10-06", "topic": "Duplicate"},
            {"scheduled_date": "2026-10-06", "topic": "Duplicate"},
        ],
    ],
)
def test_invalid_topic_dates_roll_back_edits(service, clock, topics):
    tid = service.create_task("Keep", end_date="2026-10-07", topics=["Original"])
    before = [dict(t) for t in service.topics(tid)]
    with pytest.raises(ValueError):
        service.update_task(tid, name="Invalid", topics=topics)
    assert service._task(tid)["name"] == "Keep"
    assert [dict(t) for t in service.topics(tid)] == before


def test_change_plan_recurrence_and_start_together(service, clock):
    tid = service.create_task("Plan", topics=["One", "Two"])
    original = [dict(t) for t in service.topics(tid)]
    original[0]["scheduled_date"] = "2026-10-06"
    original[1]["scheduled_date"] = "2026-10-09"
    service.update_task(
        tid,
        start_date="2026-10-06",
        recurrence="custom",
        recurrence_interval=3,
        topics=original,
    )
    assert service._task(tid)["active_date"] == "2026-10-06"
    assert service.tasks_for_day("2026-10-09")[0]["topic"]["topic"] == "Two"
    assert not service.tasks_for_day("2026-10-07")


def test_permanent_delete_rolls_back_all_tables_on_failure(service, clock):
    tid = service.create_task("Keep on failure", topics=["One"])
    service.start_task(tid)
    service.begin_break(tid)
    service.db.conn.execute("""CREATE TRIGGER block_task_delete BEFORE DELETE ON tasks
                            BEGIN SELECT RAISE(ABORT, 'cannot delete'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        service.delete_task(tid)
    assert service._task(tid)["active_session_id"]
    assert service.journal()
    assert service.topics(tid)
    assert (
        service.db.conn.execute("SELECT COUNT(*) FROM activity_breaks").fetchone()[0]
        == 1
    )


def test_custom_range_requires_end_and_rolls_over_each_inclusive_day(service, clock):
    with pytest.raises(ValueError, match="Choose an end date"):
        service.create_task("Incomplete range", recurrence="date_range")
    assert not service.tasks()
    tid = service.create_task(
        "Range",
        recurrence="date_range",
        start_date="2026-10-05",
        end_date="2026-10-07",
        topics=["First", "Second"],
    )
    clock("2026-10-07T09:00:00")
    service.rollover()
    assert service._task(tid)["active_date"] == "2026-10-07"
    assert service.tasks_for_day()[0]["topic"] is None
    service.complete_task(tid)
    clock("2026-10-08T09:00:00")
    service.rollover()
    assert not service.tasks_for_day()
    assert service.summary("2026-10-05", "2026-10-07")["missed"] == 2
