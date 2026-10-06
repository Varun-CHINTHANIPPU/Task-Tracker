from pathlib import Path
import sqlite3
import os
from contextlib import contextmanager

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    project_id INTEGER,
    recurrence TEXT NOT NULL DEFAULT 'daily',
    start_date TEXT NOT NULL,
    end_date TEXT,
    priority INTEGER NOT NULL DEFAULT 2,
    sort_order INTEGER NOT NULL DEFAULT 0,
    tags TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    expected_minutes INTEGER,
    active_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Pending',
    active_session_id INTEGER,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS journal_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    project_id INTEGER,
    entry_type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT DEFAULT '',
    event_date TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    duration_seconds INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE SET NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_auto_journal
ON journal_entries(task_id, event_date, entry_type)
WHERE entry_type IN ('Started','Completed','Missed');

CREATE TABLE IF NOT EXISTS activity_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    start_timestamp TEXT NOT NULL,
    end_timestamp TEXT,
    duration_seconds INTEGER,
    session_type TEXT NOT NULL DEFAULT 'work',
    date TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS activity_breaks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT 'break' CHECK(reason IN ('break', 'distraction')),
    start_timestamp TEXT NOT NULL,
    end_timestamp TEXT,
    duration_seconds INTEGER,
    end_reason TEXT,
    FOREIGN KEY(session_id) REFERENCES activity_sessions(id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_open_break
ON activity_breaks(session_id) WHERE end_timestamp IS NULL;
CREATE INDEX IF NOT EXISTS ix_breaks_session ON activity_breaks(session_id);

CREATE TABLE IF NOT EXISTS task_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    day_number INTEGER NOT NULL,
    scheduled_date TEXT NOT NULL,
    topic TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(task_id, day_number),
    UNIQUE(task_id, scheduled_date)
);

CREATE TABLE IF NOT EXISTS task_reminders (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    event_date TEXT NOT NULL,
    notified_at TEXT NOT NULL,
    PRIMARY KEY(task_id, event_date)
);

CREATE INDEX IF NOT EXISTS ix_tasks_active_date ON tasks(active_date);
CREATE INDEX IF NOT EXISTS ix_journal_timestamp ON journal_entries(timestamp);
CREATE INDEX IF NOT EXISTS ix_sessions_task ON activity_sessions(task_id);
CREATE INDEX IF NOT EXISTS ix_sessions_date ON activity_sessions(date);
"""


class Database:
    SCHEMA_VERSION = 2

    def __init__(self, path=None):
        if path is None:
            path = (
                Path(
                    os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
                )
                / "tracker-application"
                / "progress_tracker.db"
            )
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > self.SCHEMA_VERSION:
            self.conn.close()
            raise ValueError("This database was created by a newer application version")
        if version == self.SCHEMA_VERSION:
            return
        try:
            with self.transaction():
                # Another instance may have finished upgrading while this
                # connection waited for the migration's write lock.
                version = self.conn.execute("PRAGMA user_version").fetchone()[0]
                if version > self.SCHEMA_VERSION:
                    raise ValueError(
                        "This database was created by a newer application version"
                    )
                if version < 1:
                    self._migrate_legacy_schema()
                    self.conn.execute("PRAGMA user_version=1")
                if version < 2:
                    # Run the pre-0.8 Delete conversion exactly once. There is
                    # no archive API in the current application.
                    for row in self.conn.execute(
                        "SELECT id FROM tasks WHERE archived=1"
                    ).fetchall():
                        self.delete_task_records(row["id"])
                    self.conn.execute(
                        "CREATE INDEX IF NOT EXISTS ix_journal_task_kind_date "
                        "ON journal_entries(task_id, entry_type, event_date)"
                    )
                    self.conn.execute("PRAGMA user_version=2")
        except Exception:
            self.conn.close()
            raise

    def _migrate_legacy_schema(self):
        # Only unversioned databases require column detection. DDL and the
        # version stamp share one transaction so an interrupted upgrade retries.
        for statement in SCHEMA.split(";"):
            if statement.strip():
                self.conn.execute(statement)
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(tasks)")}
        if "sort_order" not in columns:
            self.conn.execute(
                "ALTER TABLE tasks ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
            )
            rows = self.conn.execute(
                "SELECT id FROM tasks ORDER BY active_date, priority DESC, name, id"
            ).fetchall()
            self.conn.executemany(
                "UPDATE tasks SET sort_order=? WHERE id=?",
                [(position, row["id"]) for position, row in enumerate(rows)],
            )
        additions = {
            "recurrence_interval": "INTEGER NOT NULL DEFAULT 1",
            "target_type": "TEXT NOT NULL DEFAULT 'none'",
            "target_start": "TEXT",
            "target_end": "TEXT",
            "quantity_target": "INTEGER",
            "quantity_unit": "TEXT NOT NULL DEFAULT 'problems'",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
                if name == "target_type":
                    self.conn.execute(
                        "UPDATE tasks SET target_type='duration' WHERE expected_minutes IS NOT NULL"
                    )

    def delete_task_records(self, task_id):
        with self.transaction():
            self.conn.execute(
                "DELETE FROM activity_breaks WHERE session_id IN (SELECT id FROM activity_sessions WHERE task_id=?)",
                (task_id,),
            )
            for table in (
                "journal_entries",
                "task_reminders",
                "task_topics",
                "activity_sessions",
            ):
                self.conn.execute(f"DELETE FROM {table} WHERE task_id=?", (task_id,))
            self.conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    @contextmanager
    def transaction(self):
        nested = self.conn.in_transaction
        if nested:
            self.conn.execute("SAVEPOINT tracker_write")
        else:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            if nested:
                self.conn.execute("RELEASE SAVEPOINT tracker_write")
            else:
                self.conn.commit()
        except Exception:
            if nested:
                self.conn.execute("ROLLBACK TO SAVEPOINT tracker_write")
                self.conn.execute("RELEASE SAVEPOINT tracker_write")
            else:
                self.conn.rollback()
            raise

    def close(self):
        self.conn.close()
