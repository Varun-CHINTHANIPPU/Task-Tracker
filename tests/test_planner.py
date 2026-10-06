from datetime import datetime
import json

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

    return set_time


def test_date_browser_matches_schedule_and_restores_past_completion(service, clock):
    daily = service.create_task("Daily")
    weekly = service.create_task("Weekly", recurrence="weekly")
    future = service.create_task("Future", start_date="2026-10-05", recurrence="none")
    old = service.create_task("Old one off", start_date="2026-10-03", recurrence="none")
    limited = service.create_task("Limited", end_date="2026-10-04")
    assert {t["id"] for t in service.tasks_for_day()} == {daily, weekly, limited, old}
    assert {t["id"] for t in service.tasks_for_day("2026-10-05")} == {daily, future}
    assert {t["id"] for t in service.tasks_for_day("2026-10-03")} == {old}
    service.complete_task(daily, journal="Day one")
    clock("2026-10-05T09:00:00")
    service.rollover()
    assert (
        next(t for t in service.tasks_for_day("2026-10-04") if t["id"] == daily)[
            "status"
        ]
        == "Completed"
    )
    assert (
        next(t for t in service.tasks_for_day() if t["id"] == daily)["status"]
        == "Pending"
    )
    assert weekly in {t["id"] for t in service.tasks_for_day("2026-10-11")}
    assert weekly not in {t["id"] for t in service.tasks_for_day("2026-10-12")}


def test_monthly_calendar_preserves_anchor_and_end_date(service, clock):
    tid = service.create_task(
        "Monthly", recurrence="monthly", start_date="2026-01-31", end_date="2026-03-31"
    )
    assert [t["id"] for t in service.tasks_for_day("2026-02-28")] == [tid]
    assert not service.tasks_for_day("2026-03-28")
    assert [t["id"] for t in service.tasks_for_day("2026-03-31")] == [tid]
    assert not service.tasks_for_day("2026-04-30")


def test_duration_reminder_pauses_on_break_and_fires_once_after_resume(service, clock):
    tid = service.create_task("Study", expected_minutes=10)
    sid = service.start_task(tid)
    clock("2026-10-04T09:05:00")
    service.begin_break(tid)
    clock("2026-10-04T09:09:59")
    assert service.take_due_reminders() == []
    clock("2026-10-04T09:15:00")
    state = service.timer_state(service.tasks_for_day()[0])
    assert state["elapsed"] == 900
    assert state["remaining"] == 300
    assert service.take_due_reminders() == []
    db = Database(service.db.path)
    try:
        reopened = TrackerService(db)
        assert reopened.timer_state(reopened.tasks_for_day()[0]) == state
        assert reopened.take_due_reminders() == []
        reopened.resume_task(tid)
    finally:
        db.close()
    clock("2026-10-04T09:19:59")
    assert service.take_due_reminders() == []
    clock("2026-10-04T09:20:00")
    assert [r["task_id"] for r in service.take_due_reminders()] == [tid]
    assert service.tasks()[0]["active_session_id"] == sid
    assert service.take_due_reminders() == []
    service.complete_task(tid, journal="Done, after a break")
    assert service.tasks()[0]["total_seconds"] == 1200
    assert service.summary("2026-10-04", "2026-10-04")["seconds"] == 1200
    assert service.timer_state(service.tasks_for_day()[0])["day_elapsed"] == 1200


def test_duration_budget_accumulates_multiple_sessions_per_day(service, clock):
    tid = service.create_task("Study", expected_minutes=10)
    service.start_task(tid)
    clock("2026-10-04T09:05:00")
    service.stop_session(tid)
    clock("2026-10-04T10:00:00")
    service.start_task(tid)
    assert service.timer_state(service.tasks_for_day()[0])["remaining"] == 300
    clock("2026-10-04T10:05:00")
    assert len(service.take_due_reminders()) == 1


def test_duration_budget_excludes_repeated_breaks_across_sessions(service, clock):
    tid = service.create_task("Study", expected_minutes=10)
    service.start_task(tid)
    clock("2026-10-04T09:02:00")
    service.begin_break(tid)
    clock("2026-10-04T09:12:00")
    service.resume_task(tid)
    clock("2026-10-04T09:15:00")
    service.stop_session(tid)
    clock("2026-10-04T10:00:00")
    service.start_task(tid)
    assert service.timer_state(service.tasks_for_day()[0])["remaining"] == 300
    clock("2026-10-04T10:02:00")
    service.begin_break(tid, reason="distraction")
    clock("2026-10-04T10:20:00")
    state = service.timer_state(service.tasks_for_day()[0])
    assert (state["elapsed"], state["day_elapsed"], state["remaining"]) == (
        1200,
        2100,
        180,
    )
    assert service.take_due_reminders() == []
    service.resume_task(tid)
    clock("2026-10-04T10:23:00")
    assert len(service.take_due_reminders()) == 1


def test_time_range_deadline_pauses_during_breaks(service, clock):
    tid = service.create_task(
        "Clock target",
        target_type="time_range",
        target_start="10:00",
        target_end="11:00",
    )
    clock("2026-10-04T09:50:00")
    service.start_task(tid)
    clock("2026-10-04T09:55:00")
    service.begin_break(tid)
    clock("2026-10-04T10:10:00")
    # Only the ten break minutes after the scheduled start delay the deadline.
    assert service.timer_state(service.tasks_for_day()[0])["remaining"] == 3600
    service.resume_task(tid)
    clock("2026-10-04T10:40:00")
    service.begin_break(tid, reason="distraction")
    clock("2026-10-04T11:20:00")
    assert service.timer_state(service.tasks_for_day()[0])["remaining"] == 1800
    assert service.take_due_reminders() == []
    service.resume_task(tid)
    clock("2026-10-04T11:49:59")
    assert service.take_due_reminders() == []
    clock("2026-10-04T11:50:00")
    assert len(service.take_due_reminders()) == 1
    assert service.tasks()[0]["active_session_id"] is not None


def test_reminder_resets_for_next_daily_occurrence(service, clock):
    tid = service.create_task("Daily target", expected_minutes=1)
    service.start_task(tid)
    clock("2026-10-04T09:01:00")
    assert len(service.take_due_reminders()) == 1
    service.complete_task(tid)
    clock("2026-10-05T09:00:00")
    service.start_task(tid)
    assert service.take_due_reminders() == []
    clock("2026-10-05T09:01:00")
    assert len(service.take_due_reminders()) == 1


def test_time_range_countdown_and_notification_do_not_complete_task(service, clock):
    tid = service.create_task(
        "Session", target_type="time_range", target_start="10:00", target_end="11:00"
    )
    before = service.timer_state(service.tasks_for_day()[0])
    assert before["before_start"] == 3600
    clock("2026-10-04T10:30:00")
    assert service.timer_state(service.tasks_for_day()[0])["remaining"] == 1800
    service.start_task(tid)
    clock("2026-10-04T11:00:00")
    assert len(service.take_due_reminders()) == 1
    assert service.tasks()[0]["status"] == "In Progress"
    assert service.tasks()[0]["active_session_id"] is not None


def test_quantity_target_has_no_automatic_completion_or_timer_alarm(
    service, clock, tmp_path
):
    tid = service.create_task(
        "Problems", target_type="quantity", quantity_target=3, quantity_unit="problems"
    )
    service.start_task(tid)
    clock("2026-10-04T18:00:00")
    assert service.take_due_reminders() == []
    assert service.tasks()[0]["status"] == "In Progress"
    service.complete_task(tid, journal="Solved all three")
    file = tmp_path / "quantity.json"
    service.export_json(file)
    task = json.loads(file.read_text())["tasks"][0]
    assert task["quantity_target"] == 3
    assert task["quantity_unit"] == "problems"
    assert task["status"] == "Completed"


@pytest.mark.parametrize(
    "values",
    [
        dict(target_type="time_range", target_start="11:00", target_end="10:00"),
        dict(target_type="time_range", target_start="bad", target_end="10:00"),
        dict(target_type="quantity", quantity_target=0),
        dict(target_type="quantity", quantity_target=2, quantity_unit=" "),
        dict(target_type="unknown"),
    ],
)
def test_invalid_targets_do_not_create_partial_tasks(service, values):
    with pytest.raises(ValueError):
        service.create_task("Invalid", **values)
    assert not service.tasks()


def test_day_plan_shows_only_chosen_topic_and_exports_all_days(
    service, clock, tmp_path
):
    tid = service.create_task("ZKP", topics=["Proof basics", "Commitments"])
    assert service.tasks_for_day()[0]["topic"]["topic"] == "Proof basics"
    assert service.tasks_for_day("2026-10-05")[0]["topic"]["topic"] == "Commitments"
    assert service.tasks_for_day("2026-10-06")[0]["topic"] is None
    service.add_day_topic(tid, "Sigma protocols")
    assert service.tasks_for_day("2026-10-06")[0]["topic"]["day_number"] == 3
    first = service.topics(tid)[0]
    service.set_topic_completed(first["id"], True)
    assert service.topics(tid)[0]["completed_at"] is not None
    assert (
        service.tasks()[0]["status"] == "Pending"
    )  # Subtask check doesn't complete parent.
    service.complete_task(tid, journal="Learned the three proof properties")
    output = tmp_path / "plan.json"
    service.export_json(output)
    data = json.loads(output.read_text())
    assert data["tasks"][0]["topics"] == data["task_topics"]
    assert [t["topic"] for t in data["task_topics"]] == [
        "Proof basics",
        "Commitments",
        "Sigma protocols",
    ]
    assert data["tasks"][0]["topics"][0]["completed_at"] is not None
    assert data["tasks"][0]["topics"][1]["completed_at"] is None


def test_plan_validation_and_upgrade_preserve_existing_data(service, clock):
    with pytest.raises(ValueError):
        service.create_task("Bad plan", topics=["One", ""], project="Should rollback")
    assert service.db.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert service.db.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
    weekly = service.create_task("Weekly topics", recurrence="weekly", topics=["One"])
    service.delete_task(weekly)
    with pytest.raises(ValueError):
        service.create_task("Too long", end_date="2026-10-04", topics=["One", "Two"])
    tid = service.create_task("ZKP", topics=["One", "Two"])
    with pytest.raises(ValueError):
        service.set_topic_completed(service.topics(tid)[1]["id"], True)
    with pytest.raises(ValueError):
        service.update_task(tid, start_date="2026-10-05")
    clock("2026-10-10T09:00:00")
    service.rollover()
    assert service.summary("2026-10-04", "2026-10-10")["missed"] == 6


def test_existing_expected_minutes_upgrade_to_duration_target(tmp_path, clock):
    path = tmp_path / "old.db"
    db = Database(path)
    service = TrackerService(db)
    tid = service.create_task("Existing", expected_minutes=25)
    service.add_journal("Old note", "Preserve me", task_id=tid)
    db.conn.execute("PRAGMA user_version=0")
    for column in [
        "target_type",
        "target_start",
        "target_end",
        "quantity_target",
        "quantity_unit",
    ]:
        db.conn.execute(f"ALTER TABLE tasks DROP COLUMN {column}")
    db.conn.commit()
    db.close()
    db = Database(path)
    try:
        upgraded = TrackerService(db)
        assert upgraded.tasks()[0]["target_type"] == "duration"
        assert upgraded.tasks()[0]["expected_minutes"] == 25
        assert upgraded.journal()[0]["content"] == "Preserve me"
    finally:
        db.close()
