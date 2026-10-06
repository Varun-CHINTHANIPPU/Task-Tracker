def test_task_rolls_forward_without_duplication(service, monkeypatch):
    from datetime import datetime
    from app import timeutil

    monkeypatch.setattr(
        timeutil,
        "now",
        lambda: datetime.fromisoformat("2026-09-28T09:00:00").astimezone(),
    )
    tid = service.create_task("Daily study", start_date="2026-09-28")
    service.rollover("2026-09-30")
    task = service.db.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    assert task["active_date"] == "2026-09-30"
    missed = service.db.conn.execute(
        """
        SELECT COUNT(*) n FROM journal_entries
        WHERE task_id=? AND entry_type='Missed'
    """,
        (tid,),
    ).fetchone()["n"]
    assert missed == 2
    assert service.db.conn.execute("SELECT COUNT(*) n FROM tasks").fetchone()["n"] == 1


def test_rollover_idempotent(service, monkeypatch):
    from datetime import datetime
    from app import timeutil

    monkeypatch.setattr(
        timeutil,
        "now",
        lambda: datetime.fromisoformat("2026-09-28T09:00:00").astimezone(),
    )
    tid = service.create_task("Daily study", start_date="2026-09-28")
    service.rollover("2026-09-30")
    service.rollover("2026-09-30")
    n = service.db.conn.execute(
        """
        SELECT COUNT(*) n FROM journal_entries
        WHERE task_id=? AND entry_type='Missed'
    """,
        (tid,),
    ).fetchone()["n"]
    assert n == 2


def test_completed_day_not_missed(service):
    from app.timeutil import date_str, add_days

    today = date_str()
    tomorrow = add_days(today, 1)
    tid = service.create_task("Study", start_date=today)
    service.start_task(tid)
    service.complete_task(tid)
    # Completing does not generate a second task; rollover sees today's completion event.
    service.rollover(tomorrow)
    assert (
        service.db.conn.execute(
            "SELECT COUNT(*) n FROM journal_entries WHERE task_id=? AND entry_type='Missed'",
            (tid,),
        ).fetchone()["n"]
        == 0
    )
