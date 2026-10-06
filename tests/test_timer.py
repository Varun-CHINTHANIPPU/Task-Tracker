def test_start_creates_session_and_event(service):
    tid = service.create_task("Work")
    sid = service.start_task(tid)
    task = service._task(tid)
    session = service.db.conn.execute(
        "SELECT * FROM activity_sessions WHERE id=?", (sid,)
    ).fetchone()
    assert task["status"] == "In Progress"
    assert task["active_session_id"] == sid
    assert session["start_timestamp"]
    assert (
        service.db.conn.execute(
            "SELECT COUNT(*) n FROM journal_entries WHERE task_id=? AND entry_type='Started'",
            (tid,),
        ).fetchone()["n"]
        == 1
    )


def test_multiple_sessions_preserved(service):
    tid = service.create_task("Work")
    service.start_task(tid)
    service.stop_session(tid)
    service.start_task(tid)
    service.complete_task(tid)
    assert (
        service.db.conn.execute(
            "SELECT COUNT(*) n FROM activity_sessions WHERE task_id=?", (tid,)
        ).fetchone()["n"]
        == 2
    )
    assert (
        service.db.conn.execute(
            "SELECT COUNT(*) n FROM activity_sessions WHERE task_id=? AND duration_seconds IS NOT NULL",
            (tid,),
        ).fetchone()["n"]
        == 2
    )
