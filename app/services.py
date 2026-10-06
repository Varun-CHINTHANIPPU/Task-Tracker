import calendar
from datetime import date, datetime, time, timedelta
from functools import wraps
import json
import os
import tempfile
from pathlib import Path
from .timeutil import iso, date_str, add_days

STATUSES = ("Pending", "In Progress", "Completed", "Missed")
RECURRENCES = ("daily", "weekly", "monthly", "none", "custom", "date_range")


def atomic(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.db.transaction():
            return method(self, *args, **kwargs)

    return wrapped


class TrackerService:
    def __init__(self, db):
        self.db = db

    def _task(self, task_id):
        row = self.db.conn.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Task not found")
        task = dict(row)
        task["status"] = self._status(task, self._events(task_id))
        return task

    def _events(self, task_id=None):
        sql = "SELECT task_id, event_date, entry_type FROM journal_entries WHERE entry_type IN ('Completed','Missed')"
        args = ()
        if task_id is not None:
            sql += " AND task_id=?"
            args = (task_id,)
        events = {}
        for row in self.db.conn.execute(sql, args):
            events.setdefault(row["task_id"], {}).setdefault(
                row["entry_type"], set()
            ).add(row["event_date"])
        return events

    @staticmethod
    def _status(task, events):
        if task["active_session_id"]:
            return "In Progress"
        recorded = events.get(task["id"], {})
        completed = recorded.get("Completed", set())
        missed = recorded.get("Missed", set())
        day = task["active_date"]
        if task["recurrence"] == "none":
            return "Completed" if completed else "Pending"
        if task["end_date"] and day > task["end_date"]:
            dated = [d for d in completed | missed if d <= task["end_date"]]
            day = max(dated, default=day)
        return (
            "Completed"
            if day in completed
            else "Missed"
            if day in missed
            else "Pending"
        )

    @classmethod
    def first_current_occurrence(cls, task, today=None):
        """Align new/edited schedules without inventing historical activity."""
        current = task["start_date"]
        today = today or date_str()
        while (
            task["recurrence"] != "none"
            and current < today
            and (not task["end_date"] or current <= task["end_date"])
        ):
            current = cls.next_applicable_date(task, current)
        return current

    @atomic
    def ensure_project(self, name):
        name = (name or "").strip()
        if not name:
            return None
        self.db.conn.execute(
            "INSERT OR IGNORE INTO projects(name, created_at) VALUES(?,?)",
            (name, iso()),
        )
        return self.db.conn.execute(
            "SELECT id FROM projects WHERE name=?", (name,)
        ).fetchone()["id"]

    @atomic
    def rollover(self, today=None, *, skip_running=False):
        today = date_str(today)
        date.fromisoformat(today)
        conn = self.db.conn
        rows = conn.execute(
            """
            SELECT * FROM tasks
            WHERE recurrence!='none' AND active_date < ?
        """,
            (today,),
        ).fetchall()
        changed = 0
        for task in rows:
            if skip_running and task["active_session_id"]:
                continue
            current = task["active_date"]
            # Once the next occurrence is past the inclusive end date, this
            # schedule is exhausted. Keep its records for history and reports.
            if task["end_date"] and current > task["end_date"]:
                continue
            if task["active_session_id"]:
                # End an overdue recurring timer at local midnight, before
                # clearing the pointer. One-off timers are not rolled over.
                boundary = datetime.combine(
                    date.fromisoformat(current) + timedelta(days=1), time.min
                ).astimezone()
                self._close_session(task, iso(boundary), end_reason="rollover")
            while current < today and (
                not task["end_date"] or current <= task["end_date"]
            ):
                completed = conn.execute(
                    """
                    SELECT 1 FROM journal_entries
                    WHERE task_id=? AND event_date=? AND entry_type='Completed'
                """,
                    (task["id"], current),
                ).fetchone()
                if not completed:
                    self._event(task, "Missed", current, iso())
                current = self.next_applicable_date(task, current)
                changed += 1
            conn.execute(
                """
                UPDATE tasks SET active_date=?, active_session_id=NULL, updated_at=?
                WHERE id=?
            """,
                (current, iso(), task["id"]),
            )
        return changed

    @staticmethod
    def next_applicable_date(task, current):
        if task["recurrence"] == "custom":
            return add_days(current, task["recurrence_interval"])
        if task["recurrence"] == "weekly":
            return add_days(current, 7)
        if task["recurrence"] == "monthly":
            d = date.fromisoformat(current)
            year, month = d.year + d.month // 12, d.month % 12 + 1
            # Anchor to the original day so Jan 31 -> Feb 28 -> Mar 31.
            day = min(
                date.fromisoformat(task["start_date"]).day,
                calendar.monthrange(year, month)[1],
            )
            return date(year, month, day).isoformat()
        return add_days(current, 1)

    @staticmethod
    def _validate_task(fields):
        fields["name"] = fields["name"].strip()
        if not fields["name"]:
            raise ValueError("Task name is required")
        if fields["recurrence"] not in RECURRENCES:
            raise ValueError("Unsupported recurrence")
        interval = fields.get("recurrence_interval", 1)
        if not isinstance(interval, int) or not 1 <= interval <= 3650:
            raise ValueError("Repeat interval must be between 1 and 3650 days")
        fields["start_date"] = date_str(fields["start_date"])
        start = date.fromisoformat(fields["start_date"])
        if fields["end_date"]:
            fields["end_date"] = date_str(fields["end_date"])
            if date.fromisoformat(fields["end_date"]) < start:
                raise ValueError("End date cannot be before start date")
        else:
            fields["end_date"] = None
        if fields["recurrence"] == "date_range" and not fields["end_date"]:
            raise ValueError("Choose an end date for a custom date range")
        if not isinstance(fields["priority"], int) or not 1 <= fields["priority"] <= 5:
            raise ValueError("Priority must be between 1 and 5")
        minutes = fields["expected_minutes"]
        if minutes is not None and (not isinstance(minutes, int) or minutes <= 0):
            raise ValueError("Expected minutes must be a positive integer")

    @staticmethod
    def _validate_target(fields):
        target_type = fields["target_type"]
        target_start = fields["target_start"]
        target_end = fields["target_end"]
        quantity_target = fields["quantity_target"]
        quantity_unit = fields["quantity_unit"]
        if target_type not in ("none", "duration", "time_range", "quantity"):
            raise ValueError("Unsupported target type")
        if target_type == "time_range":
            try:
                start_time = time.fromisoformat(target_start)
                end_time = time.fromisoformat(target_end)
            except (TypeError, ValueError):
                raise ValueError("Choose a valid start and end time") from None
            if start_time.tzinfo or end_time.tzinfo or end_time <= start_time:
                raise ValueError("End time must be after start time on the same day")
            target_start = start_time.strftime("%H:%M")
            target_end = end_time.strftime("%H:%M")
        if target_type == "quantity":
            if (
                not isinstance(quantity_target, int)
                or quantity_target <= 0
                or not quantity_unit.strip()
            ):
                raise ValueError(
                    "Enter a positive quantity and a unit, such as problems"
                )
        fields["target_start"] = target_start
        fields["target_end"] = target_end
        fields["quantity_unit"] = quantity_unit.strip()

    @atomic
    def create_task(
        self,
        name,
        description="",
        project=None,
        recurrence="daily",
        start_date=None,
        end_date=None,
        priority=2,
        tags="",
        notes="",
        expected_minutes=None,
        target_type=None,
        target_start=None,
        target_end=None,
        quantity_target=None,
        quantity_unit="problems",
        topics=None,
        recurrence_interval=1,
    ):
        fields = dict(
            name=name,
            recurrence=recurrence,
            recurrence_interval=recurrence_interval,
            start_date=start_date or date_str(),
            end_date=end_date,
            priority=priority,
            expected_minutes=expected_minutes,
        )
        self._validate_task(fields)
        target_type = target_type or ("duration" if expected_minutes else "none")
        target_fields = dict(
            target_type=target_type,
            target_start=target_start,
            target_end=target_end,
            quantity_target=quantity_target,
            quantity_unit=quantity_unit,
        )
        self._validate_target(target_fields)
        target_start = target_fields["target_start"]
        target_end = target_fields["target_end"]
        project_id = (
            project if isinstance(project, int) else self.ensure_project(project)
        )
        stamp = iso()
        cur = self.db.conn.execute(
            """
            INSERT INTO tasks
            (name, description, project_id, recurrence, start_date, end_date,
             priority, tags, notes, expected_minutes, active_date,
             created_at, updated_at, sort_order)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,
                    (SELECT COALESCE(MAX(sort_order), -1) + 1 FROM tasks))
        """,
            (
                fields["name"],
                description,
                project_id,
                recurrence,
                fields["start_date"],
                fields["end_date"],
                priority,
                tags,
                notes,
                expected_minutes,
                self.first_current_occurrence(fields),
                stamp,
                stamp,
            ),
        )
        task_id = cur.lastrowid
        self.db.conn.execute(
            """UPDATE tasks SET target_type=?, target_start=?, target_end=?,
               quantity_target=?, quantity_unit=?, recurrence_interval=? WHERE id=?""",
            (
                target_type,
                target_start,
                target_end,
                quantity_target,
                quantity_unit.strip(),
                recurrence_interval,
                task_id,
            ),
        )
        self._replace_topics(task_id, topics or [])
        return task_id

    @atomic
    def update_task(self, task_id, **fields):
        topics = fields.pop("topics", None)
        allowed = {
            "name",
            "description",
            "project_id",
            "recurrence",
            "recurrence_interval",
            "start_date",
            "end_date",
            "priority",
            "tags",
            "notes",
            "expected_minutes",
            "target_type",
            "target_start",
            "target_end",
            "quantity_target",
            "quantity_unit",
        }
        unknown = fields.keys() - allowed
        if unknown:
            raise ValueError("Unsupported task fields: " + ", ".join(sorted(unknown)))
        task = self._task(task_id)
        if not fields and topics is None:
            return
        candidate = dict(task)
        candidate.update(fields)
        self._validate_task(candidate)
        self._validate_target(candidate)
        fields = {key: candidate[key] for key in fields}
        schedule_changed = any(
            key in fields and fields[key] != task[key]
            for key in ("start_date", "recurrence", "recurrence_interval", "end_date")
        )
        proposed_topics = (
            topics if topics is not None else [dict(t) for t in self.topics(task_id)]
        )
        self._validate_topics(candidate, proposed_topics, self.topics(task_id))
        if task["active_session_id"] and (
            schedule_changed or candidate["end_date"] != task["end_date"]
        ):
            raise ValueError("Stop the active timer before changing its schedule")
        if schedule_changed:
            fields["active_date"] = self.first_current_occurrence(candidate)
        if any(
            candidate[key] != task[key]
            for key in ("target_type", "expected_minutes", "target_start", "target_end")
        ):
            self.db.conn.execute(
                "DELETE FROM task_reminders WHERE task_id=? AND event_date=?",
                (task_id, fields.get("active_date", task["active_date"])),
            )
        fields["updated_at"] = iso()
        sql = "UPDATE tasks SET " + ", ".join(f"{k}=?" for k in fields) + " WHERE id=?"
        self.db.conn.execute(sql, [*fields.values(), task_id])
        if topics is not None:
            self._replace_topics(task_id, topics)

    @atomic
    def delete_task(self, task_id):
        self._task(task_id)
        self.db.delete_task_records(task_id)

    def _actionable_task(self, task_id):
        self.rollover()
        task = self._task(task_id)
        today = date_str()
        if task["active_date"] > today:
            raise ValueError("Task is not due yet")
        if (
            task["recurrence"] != "none"
            and task["end_date"]
            and today > task["end_date"]
        ):
            raise ValueError("Task schedule has ended")
        return task

    def _event(self, task, kind, event_date, stamp):
        self.db.conn.execute(
            """
            INSERT OR IGNORE INTO journal_entries
            (task_id,project_id,entry_type,title,content,event_date,timestamp,created_at)
            VALUES(?,?,?,?,?,?,?,?)
        """,
            (
                task["id"],
                task["project_id"],
                kind,
                f"Task {kind.lower()}",
                f"{kind}: {task['name']}",
                event_date,
                stamp,
                stamp,
            ),
        )

    @atomic
    def start_task(self, task_id):
        task = self._actionable_task(task_id)
        if task["active_session_id"]:
            raise ValueError("Task already has an active session")
        if task["status"] == "Completed":
            raise ValueError("This task occurrence is already completed")
        stamp = iso()
        cur = self.db.conn.execute(
            """
            INSERT INTO activity_sessions(task_id,start_timestamp,date,session_type)
            VALUES(?,?,?,'work')
        """,
            (task_id, stamp, stamp[:10]),
        )
        self.db.conn.execute(
            """
            UPDATE tasks SET active_session_id=?, updated_at=? WHERE id=?
        """,
            (cur.lastrowid, stamp, task_id),
        )
        self._event(task, "Started", stamp[:10], stamp)
        return cur.lastrowid

    @atomic
    def begin_break(self, task_id, reason="break"):
        if reason not in ("break", "distraction"):
            raise ValueError("Choose break or distraction")
        task = self._actionable_task(task_id)
        session = self.db.conn.execute(
            "SELECT * FROM activity_sessions WHERE id=? AND task_id=? AND end_timestamp IS NULL",
            (task["active_session_id"], task_id),
        ).fetchone()
        if session is None:
            raise ValueError("Start the task before taking a break")
        if self.db.conn.execute(
            "SELECT 1 FROM activity_breaks WHERE session_id=? AND end_timestamp IS NULL",
            (session["id"],),
        ).fetchone():
            raise ValueError("Resume the task before starting another break")
        stamp = iso()
        cur = self.db.conn.execute(
            "INSERT INTO activity_breaks(session_id, reason, start_timestamp) VALUES(?,?,?)",
            (session["id"], reason, stamp),
        )
        self.add_journal(
            f"{'Break' if reason == 'break' else 'Distraction'} started: {task['name']}",
            "The estimated countdown is paused until Resume. The work timer keeps running.",
            task_id=task_id,
            timestamp=stamp,
            entry_type="Break started",
        )
        return cur.lastrowid

    def _close_break(self, session_id, stamp, end_reason):
        interval = self.db.conn.execute(
            "SELECT * FROM activity_breaks WHERE session_id=? AND end_timestamp IS NULL",
            (session_id,),
        ).fetchone()
        if interval is None:
            return
        start = datetime.fromisoformat(interval["start_timestamp"])
        end = datetime.fromisoformat(stamp)
        if end.timestamp() < start.timestamp():
            stamp = interval["start_timestamp"]
        seconds = max(0, int(end.timestamp() - start.timestamp()))
        self.db.conn.execute(
            "UPDATE activity_breaks SET end_timestamp=?, duration_seconds=?, end_reason=? WHERE id=?",
            (stamp, seconds, end_reason, interval["id"]),
        )
        task_id = self.db.conn.execute(
            "SELECT task_id FROM activity_sessions WHERE id=?",
            (session_id,),
        ).fetchone()["task_id"]
        label = "Break" if interval["reason"] == "break" else "Distraction"
        self.add_journal(
            f"{label} ended",
            f"{label} from {interval['start_timestamp']} to {stamp} "
            f"({seconds} seconds; {end_reason}). Included in elapsed time; excluded from the estimated countdown.",
            task_id=task_id,
            timestamp=stamp,
            entry_type="Break ended",
        )

    @atomic
    def resume_task(self, task_id):
        self.rollover()
        task = self._task(task_id)
        session_id = task["active_session_id"]
        if not self.db.conn.execute(
            "SELECT 1 FROM activity_breaks WHERE session_id=? AND end_timestamp IS NULL",
            (session_id,),
        ).fetchone():
            raise ValueError("Task is not on a break")
        self._close_break(session_id, iso(), "resumed")

    def _close_session(self, task, stamp, end_reason="completed"):
        if not task["active_session_id"]:
            return
        session = self.db.conn.execute(
            "SELECT * FROM activity_sessions WHERE id=? AND task_id=?",
            (task["active_session_id"], task["id"]),
        ).fetchone()
        if session and not session["end_timestamp"]:
            start = datetime.fromisoformat(session["start_timestamp"])
            end = datetime.fromisoformat(stamp)
            # Timestamps, rather than wall-time subtraction, handle DST changes.
            if end.timestamp() < start.timestamp():
                stamp = session["start_timestamp"]
            seconds = max(0, int(end.timestamp() - start.timestamp()))
            self._close_break(session["id"], stamp, end_reason)
            self.db.conn.execute(
                """
                UPDATE activity_sessions SET end_timestamp=?, duration_seconds=? WHERE id=?
            """,
                (stamp, seconds, session["id"]),
            )

    @atomic
    def complete_task(self, task_id, journal=None, expected_date=None):
        if journal is not None and not journal.strip():
            raise ValueError("Please enter a journal for this task")
        task = self._actionable_task(task_id)
        if expected_date is not None and task["active_date"] != expected_date:
            raise ValueError(
                "The scheduled day changed. Reopen the task before completing it."
            )
        stamp = iso()
        self._close_session(task, stamp)
        self.db.conn.execute(
            """
            UPDATE tasks SET active_session_id=NULL, updated_at=? WHERE id=?
        """,
            (stamp, task_id),
        )
        # A one-off task can be completed late, but only once in the journal.
        event_date = task["active_date"] if task["recurrence"] != "none" else stamp[:10]
        already_completed = self.db.conn.execute(
            "SELECT 1 FROM journal_entries WHERE task_id=? AND entry_type='Completed'",
            (task_id,),
        ).fetchone()
        if task["recurrence"] != "none" or not already_completed:
            self._event(task, "Completed", event_date, stamp)
        self.db.conn.execute(
            "UPDATE task_topics SET completed_at=COALESCE(completed_at,?) WHERE task_id=? AND scheduled_date=?",
            (stamp, task_id, task["active_date"]),
        )
        if journal is not None:
            self.add_journal(
                f"Journal: {task['name']}",
                journal.strip(),
                task_id=task_id,
                timestamp=stamp,
                entry_type="Journal",
            )

    @atomic
    def stop_session(self, task_id):
        self.rollover()
        task = self._task(task_id)
        if not task["active_session_id"]:
            return
        stamp = iso()
        self._close_session(task, stamp, end_reason="stopped")
        self.db.conn.execute(
            "UPDATE tasks SET active_session_id=NULL, updated_at=? WHERE id=?",
            (stamp, task_id),
        )

    @atomic
    def add_journal(
        self,
        title,
        content,
        task_id=None,
        project_id=None,
        timestamp=None,
        entry_type="Note",
    ):
        if not title.strip():
            raise ValueError("Journal title is required")
        if entry_type in ("Started", "Completed", "Missed"):
            raise ValueError("Automatic event types are reserved for task actions")
        stamp = iso(datetime.fromisoformat(timestamp)) if timestamp else iso()
        if task_id is not None:
            task = self._task(task_id)
            if project_id is None:
                project_id = task["project_id"]
        self.db.conn.execute(
            """
            INSERT INTO journal_entries
            (task_id,project_id,entry_type,title,content,event_date,timestamp,created_at)
            VALUES(?,?,?,?,?,?,?,?)
        """,
            (
                task_id,
                project_id,
                entry_type,
                title.strip(),
                content,
                stamp[:10],
                stamp,
                iso(),
            ),
        )

    def tasks(self, status=None, *, events=None):
        sql = """
            SELECT t.*, p.name project_name, b.id active_break_id,
            b.reason break_reason, b.start_timestamp break_started_at,
            COALESCE((SELECT SUM(duration_seconds) FROM activity_sessions s
                      WHERE s.task_id=t.id),0) total_seconds
            FROM tasks t LEFT JOIN projects p ON p.id=t.project_id
            LEFT JOIN activity_breaks b ON b.session_id=t.active_session_id AND b.end_timestamp IS NULL
            WHERE 1=1
        """
        sql += " ORDER BY t.sort_order, t.id"
        events = self._events() if events is None else events
        tasks = []
        for row in self.db.conn.execute(sql):
            task = dict(row)
            task["status"] = self._status(task, events)
            if not status or status == "All" or task["status"] == status:
                tasks.append(task)
        return tasks

    def topics(self, task_id):
        return self.db.conn.execute(
            "SELECT * FROM task_topics WHERE task_id=? ORDER BY day_number",
            (task_id,),
        ).fetchall()

    @classmethod
    def _validate_topics(cls, task, topics, existing=()):
        existing_by_id = {t["id"]: t for t in existing}
        result = []
        ids, dates = set(), set()
        next_date = task["start_date"]
        for index, value in enumerate(topics):
            if isinstance(value, str):
                old = existing[index] if index < len(existing) else None
                item = {
                    "topic": value,
                    "scheduled_date": old["scheduled_date"] if old else next_date,
                    "id": old["id"] if old else None,
                }
            else:
                item = dict(value)
            text = item.get("topic", "").strip()
            if not text:
                raise ValueError("Enter a topic or remove its row")
            scheduled = date.fromisoformat(item["scheduled_date"]).isoformat()
            if not cls._scheduled_on(task, scheduled):
                raise ValueError(
                    "Topic dates must be within the start/end dates and match the recurrence"
                )
            if scheduled in dates:
                raise ValueError("Choose a different date for each topic")
            dates.add(scheduled)
            topic_id = item.get("id")
            if topic_id is not None and (
                topic_id not in existing_by_id or topic_id in ids
            ):
                raise ValueError("Topic does not belong to this task or appears twice")
            if topic_id is not None:
                ids.add(topic_id)
            old = existing_by_id.get(topic_id)
            completed_at = (
                old["completed_at"]
                if old and old["scheduled_date"] == scheduled
                else None
            )
            result.append(
                dict(
                    id=topic_id,
                    topic=text,
                    scheduled_date=scheduled,
                    completed_at=completed_at,
                )
            )
            next_date = cls.next_applicable_date(task, scheduled)
        return sorted(result, key=lambda t: t["scheduled_date"])

    def _replace_topics(self, task_id, topics):
        rows = self._validate_topics(self._task(task_id), topics, self.topics(task_id))
        self.db.conn.execute("DELETE FROM task_topics WHERE task_id=?", (task_id,))
        for number, row in enumerate(rows, 1):
            self.db.conn.execute(
                "INSERT INTO task_topics(id,task_id,day_number,scheduled_date,topic,completed_at) VALUES(?,?,?,?,?,?)",
                (
                    row["id"],
                    task_id,
                    number,
                    row["scheduled_date"],
                    row["topic"],
                    row["completed_at"],
                ),
            )

    @atomic
    def add_day_topic(self, task_id, topic, scheduled_date=None):
        task = self._task(task_id)
        rows = [dict(t) for t in self.topics(task_id)]
        scheduled = scheduled_date or (
            self.next_applicable_date(task, rows[-1]["scheduled_date"])
            if rows
            else task["start_date"]
        )
        rows.append(dict(topic=topic, scheduled_date=scheduled))
        self._replace_topics(task_id, rows)
        return next(
            t["id"] for t in self.topics(task_id) if t["scheduled_date"] == scheduled
        )

    @atomic
    def set_topic_completed(self, topic_id, completed):
        topic = self.db.conn.execute(
            "SELECT * FROM task_topics WHERE id=?", (topic_id,)
        ).fetchone()
        if topic is None:
            raise ValueError("Topic not found")
        if topic["scheduled_date"] != date_str():
            raise ValueError("Only today's topic can be checked off")
        self.db.conn.execute(
            "UPDATE task_topics SET completed_at=? WHERE id=?",
            (iso() if completed else None, topic_id),
        )

    @staticmethod
    def _scheduled_on(task, day):
        start = date.fromisoformat(task["start_date"])
        chosen = date.fromisoformat(day)
        if chosen < start or (task["end_date"] and day > task["end_date"]):
            return False
        if task["recurrence"] == "none":
            return chosen == start
        if task["recurrence"] == "custom":
            return (chosen - start).days % task["recurrence_interval"] == 0
        if task["recurrence"] == "weekly":
            return (chosen - start).days % 7 == 0
        if task["recurrence"] == "monthly":
            return chosen.day == min(
                start.day, calendar.monthrange(chosen.year, chosen.month)[1]
            )
        return True

    def tasks_for_day(self, day=None):
        day = date_str(day)
        date.fromisoformat(day)
        today = date_str()
        rows = []
        events = self._events()
        topics_by_task = {}
        for topic in self.db.conn.execute(
            "SELECT * FROM task_topics ORDER BY task_id, day_number"
        ):
            topics_by_task.setdefault(topic["task_id"], []).append(dict(topic))
        for task in self.tasks(events=events):
            plan = topics_by_task.get(task["id"], [])
            completed_dates = events.get(task["id"], {}).get("Completed", set())
            completed = day in completed_dates
            oneoff = task["recurrence"] == "none"
            overdue_oneoff = (
                oneoff
                and day == today
                and task["active_date"] < today
                and not completed_dates
            )
            running = bool(task["active_session_id"]) and (
                task["active_date"] == day or (oneoff and day == today)
            )
            if (
                not running
                and not completed
                and not overdue_oneoff
                and not self._scheduled_on(task, day)
            ):
                continue
            topic_date = (
                task["active_date"]
                if oneoff and (overdue_oneoff or completed or running)
                else day
            )
            topic = next((t for t in plan if t["scheduled_date"] == topic_date), None)
            task["view_date"] = day
            task["topic"] = topic
            task["has_topics"] = bool(plan)
            task["status"] = (
                "In Progress"
                if running
                else (
                    "Completed" if completed else "Missed" if day < today else "Pending"
                )
            )
            if not running:
                task["active_session_id"] = None
                task["active_break_id"] = None
                task["break_reason"] = None
            rows.append(task)
        return sorted(
            rows,
            key=lambda task: (
                task["status"] == "Completed",
                task["sort_order"],
                task["id"],
            ),
        )

    @staticmethod
    def _break_seconds(intervals, start, end):
        """Count the union of recorded breaks inside a timestamp window."""
        ranges = sorted(
            (
                max(start, datetime.fromisoformat(b["start_timestamp"]).timestamp()),
                min(end, datetime.fromisoformat(b["end_timestamp"]).timestamp())
                if b["end_timestamp"]
                else end,
            )
            for b in intervals
        )
        seconds = 0
        cursor = start
        for left, right in ranges:
            left = max(left, cursor)
            if right > left:
                seconds += right - left
                cursor = right
        return int(seconds)

    def timer_state(self, task, at=None):
        at = at or datetime.fromisoformat(iso())
        day = (
            task.get("view_date", task["active_date"])
            if isinstance(task, dict)
            else task["active_date"]
        )
        if task["recurrence"] == "none":
            day = task["active_date"]
        elapsed = saved = target_elapsed = 0
        sessions = self.db.conn.execute(
            "SELECT * FROM activity_sessions WHERE task_id=? AND (date=? OR id=? OR ?)",
            (task["id"], day, task["active_session_id"], task["recurrence"] == "none"),
        ).fetchall()
        breaks = self.db.conn.execute(
            """SELECT b.* FROM activity_breaks b
               JOIN activity_sessions s ON s.id=b.session_id
               WHERE s.task_id=? AND (s.date=? OR s.id=? OR ?)""",
            (task["id"], day, task["active_session_id"], task["recurrence"] == "none"),
        ).fetchall()
        breaks_by_session = {}
        for interval in breaks:
            breaks_by_session.setdefault(interval["session_id"], []).append(interval)
        for session in sessions:
            start = datetime.fromisoformat(session["start_timestamp"]).timestamp()
            end = (
                datetime.fromisoformat(session["end_timestamp"]).timestamp()
                if session["end_timestamp"]
                else at.timestamp()
            )
            work = max(
                0,
                int(end - start)
                - self._break_seconds(
                    breaks_by_session.get(session["id"], []), start, end
                ),
            )
            # Elapsed tracking includes breaks. Only the notification budget
            # uses active work time, so the two timers stay independent.
            if session["end_timestamp"]:
                saved += max(0, int(end - start))
                target_elapsed += work
            elif session["id"] == task["active_session_id"]:
                elapsed = max(0, int(end - start))
                target_elapsed += work
        remaining = None
        before_start = None
        kind = task["target_type"]
        if kind == "duration" and task["expected_minutes"]:
            remaining = task["expected_minutes"] * 60 - target_elapsed
        elif kind == "time_range":
            start = datetime.combine(
                date.fromisoformat(day), time.fromisoformat(task["target_start"])
            ).astimezone()
            end = datetime.combine(
                date.fromisoformat(day), time.fromisoformat(task["target_end"])
            ).astimezone()
            before_start = max(0, int(start.timestamp() - at.timestamp()))
            # A clock-time deadline moves later by time spent on break after
            # the scheduled start. An open break freezes the countdown.
            remaining = int(end.timestamp() - at.timestamp()) + self._break_seconds(
                breaks, start.timestamp(), at.timestamp()
            )
        return {
            "elapsed": elapsed,
            "day_elapsed": saved + elapsed,
            "remaining": remaining,
            "before_start": before_start,
        }

    def reminder_candidates(self):
        """Read pending alarms once; no write lock for idle or paused tasks."""
        notified = {
            (r["task_id"], r["event_date"])
            for r in self.db.conn.execute(
                "SELECT task_id,event_date FROM task_reminders"
            )
        }
        candidates = []
        for task in self.tasks_for_day():
            occurrence = task["active_date"]
            if (
                not task["active_session_id"]
                or task["status"] == "Completed"
                or (task["id"], occurrence) in notified
                or task["target_type"] not in ("duration", "time_range")
            ):
                continue
            state = self.timer_state(task)
            if state["remaining"] is not None:
                candidates.append((task, state))
        return candidates

    def take_due_reminders(self, candidates=None):
        candidates = self.reminder_candidates() if candidates is None else candidates
        due = [(task, state) for task, state in candidates if state["remaining"] <= 0]
        if not due:
            return []
        reminders = []
        with self.db.transaction():
            for task, state in due:
                cursor = self.db.conn.execute(
                    "INSERT OR IGNORE INTO task_reminders(task_id,event_date,notified_at) VALUES(?,?,?)",
                    (task["id"], task["active_date"], iso()),
                )
                if cursor.rowcount:
                    reminders.append(
                        {
                            "task_id": task["id"],
                            "name": task["name"],
                            "message": f"Time is up for {task['name']}. Complete it when you are ready.",
                        }
                    )
        return reminders

    @atomic
    def reorder_tasks(self, task_ids):
        """Reorder the visible subset without moving hidden tasks."""
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("Task order contains duplicate IDs")
        current = [row["id"] for row in self.tasks()]
        selected = set(task_ids)
        if not selected.issubset(current):
            raise ValueError("Task list changed. Refresh before reordering.")
        ordered = iter(task_ids)
        merged = [next(ordered) if tid in selected else tid for tid in current]
        self.db.conn.executemany(
            "UPDATE tasks SET sort_order=?, updated_at=? WHERE id=?",
            [(position, iso(), tid) for position, tid in enumerate(merged)],
        )

    def journal(self, start=None, end=None, search=None):
        sql = """
        SELECT j.*, t.name task_name, p.name project_name
        FROM journal_entries j
        LEFT JOIN tasks t ON t.id=j.task_id
        LEFT JOIN projects p ON p.id=j.project_id WHERE 1=1
        """
        args = []
        if start:
            sql += " AND j.event_date>=?"
            args.append(start)
        if end:
            sql += " AND j.event_date<=?"
            args.append(end)
        if search:
            sql += " AND (j.title LIKE ? OR j.content LIKE ? OR t.name LIKE ?)"
            q = f"%{search}%"
            args += [q, q, q]
        sql += " ORDER BY j.timestamp DESC"
        return self.db.conn.execute(sql, args).fetchall()

    @atomic
    def clear_history(self, start_date=None, end_date=None):
        """Delete dated records atomically, retaining task and topic definitions."""
        start_date, end_date = self._validate_date_range(start_date, end_date)
        conn = self.db.conn
        # Clearing history must not close timers or change any running task's
        # records, including earlier sessions that contribute to elapsed totals.
        self.rollover(skip_running=True)
        protected_tasks = """SELECT task_id FROM activity_sessions WHERE end_timestamp IS NULL
                             UNION SELECT id FROM tasks WHERE active_session_id IS NOT NULL"""
        args = (start_date, end_date) if start_date is not None else ()

        def where(column, task_column="task_id"):
            dates = f"{column} BETWEEN ? AND ?" if args else "1=1"
            return f"{dates} AND ({task_column} IS NULL OR {task_column} NOT IN ({protected_tasks}))"

        session_filter = where("date")
        counts = {}
        counts["activity_breaks"] = conn.execute(
            f"DELETE FROM activity_breaks WHERE session_id IN (SELECT id FROM activity_sessions WHERE {session_filter})",
            args,
        ).rowcount
        counts["activity_sessions"] = conn.execute(
            f"DELETE FROM activity_sessions WHERE {session_filter}", args
        ).rowcount
        counts["journal_entries"] = conn.execute(
            f"DELETE FROM journal_entries WHERE {where('event_date')}", args
        ).rowcount
        conn.execute(f"DELETE FROM task_reminders WHERE {where('event_date')}", args)
        conn.execute(
            f"UPDATE task_topics SET completed_at=NULL WHERE {where('scheduled_date')}",
            args,
        )
        return counts

    def summary(self, start, end):
        """Summarize history; seconds is elapsed wall time including breaks."""
        c = self.db.conn
        completed = c.execute(
            "SELECT COUNT(*) n FROM journal_entries WHERE entry_type='Completed' AND event_date BETWEEN ? AND ?",
            (start, end),
        ).fetchone()["n"]
        missed = c.execute(
            "SELECT COUNT(*) n FROM journal_entries WHERE entry_type='Missed' AND event_date BETWEEN ? AND ?",
            (start, end),
        ).fetchone()["n"]
        sessions = c.execute(
            """
            SELECT COUNT(*) n, COALESCE(SUM(duration_seconds),0) seconds
            FROM activity_sessions
            WHERE date BETWEEN ? AND ?
        """,
            (start, end),
        ).fetchone()
        notes = c.execute(
            "SELECT COUNT(*) n FROM journal_entries WHERE entry_type NOT IN ('Started','Completed','Missed','Break started','Break ended') AND event_date BETWEEN ? AND ?",
            (start, end),
        ).fetchone()["n"]
        return {
            "completed": completed,
            "missed": missed,
            "sessions": sessions["n"],
            "seconds": sessions["seconds"],
            "notes": notes,
        }

    @staticmethod
    def _validate_date_range(start_date, end_date):
        if (start_date is None) != (end_date is None):
            raise ValueError("Choose both From and To dates")
        if start_date is not None:
            start_date = date.fromisoformat(start_date).isoformat()
            end_date = date.fromisoformat(end_date).isoformat()
            if end_date < start_date:
                raise ValueError("To date cannot be before From date")
        return start_date, end_date

    def export_json(self, path, start_date=None, end_date=None):
        start_date, end_date = self._validate_date_range(start_date, end_date)
        c = self.db.conn
        payload = {
            "export_metadata": {"generated_at": iso(), "schema": "1.3"},
            "projects": [
                dict(r) for r in c.execute("SELECT * FROM projects").fetchall()
            ],
            "tasks": [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM tasks ORDER BY sort_order, id"
                ).fetchall()
            ],
            "journal_entries": [
                dict(r) for r in c.execute("SELECT * FROM journal_entries").fetchall()
            ],
            "activity_sessions": [
                dict(r) for r in c.execute("SELECT * FROM activity_sessions").fetchall()
            ],
            "task_topics": [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM task_topics ORDER BY task_id, day_number"
                )
            ],
            "task_reminders": [
                dict(r) for r in c.execute("SELECT * FROM task_reminders")
            ],
            "activity_breaks": [
                dict(r)
                for r in c.execute(
                    """SELECT b.*, s.task_id FROM activity_breaks b
                       JOIN activity_sessions s ON s.id=b.session_id ORDER BY b.id"""
                ).fetchall()
            ],
        }
        events = self._events()
        for task in payload["tasks"]:
            task["status"] = self._status(task, events)
            task.pop("archived", None)
        payload["export_metadata"].update(
            duration_basis="Session duration_seconds is wall time. work_duration_seconds excludes breaks and distractions; unfinished sessions have null durations.",
            date_range={"from": start_date, "to": end_date} if start_date else None,
            date_range_basis="Inclusive local dates. Journals, topics and reminders use their event/scheduled date. Sessions use their start date and include all associated breaks. Task definitions are current settings.",
        )
        if start_date:

            def within(value):
                return start_date <= value <= end_date

            for collection, key in (
                ("journal_entries", "event_date"),
                ("task_topics", "scheduled_date"),
                ("task_reminders", "event_date"),
                ("activity_sessions", "date"),
            ):
                payload[collection] = [r for r in payload[collection] if within(r[key])]
            session_ids = {r["id"] for r in payload["activity_sessions"]}
            payload["activity_breaks"] = [
                r for r in payload["activity_breaks"] if r["session_id"] in session_ids
            ]
            recorded = {
                r["task_id"]
                for collection in (
                    "journal_entries",
                    "task_reminders",
                    "activity_sessions",
                )
                for r in payload[collection]
            }
            related = recorded | {t["task_id"] for t in payload["task_topics"]}

            def scheduled_in_range(task):
                current = task["start_date"]
                if task["recurrence"] == "none":
                    return within(current)
                last = min(end_date, task["end_date"] or end_date)
                while current < start_date and current <= last:
                    current = self.next_applicable_date(task, current)
                return start_date <= current <= last

            payload["tasks"] = [
                t
                for t in payload["tasks"]
                if t["id"] in related or scheduled_in_range(t)
            ]
            project_ids = {t["project_id"] for t in payload["tasks"]}
            project_ids.update(r["project_id"] for r in payload["journal_entries"])
            payload["projects"] = [
                p for p in payload["projects"] if p["id"] in project_ids
            ]
        # Keep the flat records for compatibility and include each task's
        # journals directly under that task for readable, self-contained exports.
        entries_by_task = {}
        for entry in payload["journal_entries"]:
            entries_by_task.setdefault(entry["task_id"], []).append(entry)
        breaks_by_task = {}
        breaks_by_session = {}
        for interval in payload["activity_breaks"]:
            breaks_by_task.setdefault(interval["task_id"], []).append(interval)
            breaks_by_session.setdefault(interval["session_id"], []).append(interval)
        for session in payload["activity_sessions"]:
            session["breaks"] = breaks_by_session.get(session["id"], [])
            session["work_duration_seconds"] = (
                max(
                    0,
                    session["duration_seconds"]
                    - self._break_seconds(
                        session["breaks"],
                        datetime.fromisoformat(session["start_timestamp"]).timestamp(),
                        datetime.fromisoformat(session["end_timestamp"]).timestamp(),
                    ),
                )
                if session["end_timestamp"]
                else None
            )
        for task in payload["tasks"]:
            task["breaks"] = breaks_by_task.get(task["id"], [])
            task["topics"] = [
                t for t in payload["task_topics"] if t["task_id"] == task["id"]
            ]
            task["journal_entries"] = sorted(
                entries_by_task.get(task["id"], []),
                key=lambda entry: (
                    entry["event_date"],
                    entry["timestamp"],
                    entry["id"],
                ),
            )
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as f:
                temporary = Path(f.name)
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
