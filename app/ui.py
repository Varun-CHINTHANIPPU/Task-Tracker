import sys
import sqlite3
import math
import time
from pathlib import Path
from datetime import datetime, timedelta
from PySide6.QtCore import (
    QDate,
    QTimer,
    Qt,
    Signal,
    QMimeData,
    QTime,
    QStandardPaths,
    QEvent,
)
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QMenu,
    QLineEdit,
    QTextEdit,
    QComboBox,
    QDialog,
    QFormLayout,
    QDateEdit,
    QSpinBox,
    QMessageBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QFileDialog,
    QAbstractItemView,
    QCheckBox,
    QTimeEdit,
    QScrollArea,
    QSystemTrayIcon,
    QStyle,
)
from .db import Database
from .services import TrackerService
from .timeutil import date_str, iso, fmt_seconds
from .alerts import ReminderSound


class TaskDialog(QDialog):
    deleteRequested = Signal()

    def __init__(self, parent=None, initial_date=None, task=None, topics=None):
        super().__init__(parent)
        self._loading = True
        self.original_topics = topics or []
        self.setWindowTitle("Edit Task" if task is not None else "Create Task")
        self.resize(620, 440)
        self.form = QFormLayout(self)
        f = self.form
        self.name = QLineEdit()
        self.recurrence = QComboBox()
        for value in ("daily", "weekly", "monthly", "none"):
            self.recurrence.addItem(value, value)
        self.recurrence.addItem("custom", "date_range")
        # Preserve old interval-based plans without offering intervals for new tasks.
        self.legacy_interval = task["recurrence_interval"] if task is not None else 1
        if task is not None and task["recurrence"] == "custom":
            self.recurrence.addItem("existing interval schedule", "custom")
        initial_date = initial_date or QDate.fromString(date_str(), "yyyy-MM-dd")
        self.start = QDateEdit(initial_date)
        self.end = QDateEdit(
            max(initial_date, QDate.fromString(date_str(), "yyyy-MM-dd"))
        )
        for field in (self.start, self.end):
            field.setCalendarPopup(True)
            field.setDisplayFormat("dd MMM yyyy")
        self.end_enabled = QCheckBox("Set end date")
        end_row = QWidget()
        end_layout = QHBoxLayout(end_row)
        end_layout.setContentsMargins(0, 0, 0, 0)
        end_layout.addWidget(self.end_enabled)
        end_layout.addWidget(self.end)
        f.addRow("Name *", self.name)
        f.addRow("Recurrence", self.recurrence)
        f.addRow("Start date", self.start)
        f.addRow("End date", end_row)
        self.target = QComboBox()
        for label, value in [
            ("Expected minutes", "duration"),
            ("Start / end time", "time_range"),
            ("Quantity (e.g. problems)", "quantity"),
            ("No target", "none"),
        ]:
            self.target.addItem(label, value)
        f.addRow("Target", self.target)
        self.expected = QSpinBox()
        self.expected.setRange(0, 100000)
        self.expected.setSpecialValueText("Not specified")
        f.addRow("Expected minutes", self.expected)
        self.target_start = QTimeEdit(QTime(9, 0))
        self.target_end = QTimeEdit(QTime(10, 0))
        for field in (self.target_start, self.target_end):
            field.setDisplayFormat("HH:mm")
        f.addRow("From", self.target_start)
        f.addRow("Until", self.target_end)
        self.quantity = QSpinBox()
        self.quantity.setRange(1, 100000)
        self.unit = QLineEdit("problems")
        f.addRow("How many", self.quantity)
        f.addRow("Unit", self.unit)
        self.target.currentIndexChanged.connect(self.update_target_fields)
        self.update_target_fields()
        self.day_plan = QCheckBox("Add dated topics")
        f.addRow(self.day_plan)
        self.topic_fields, self.topic_dates, self.topic_ids, self.topic_rows = (
            [],
            [],
            [],
            [],
        )
        self.topic_box = QWidget()
        self.topic_layout = QVBoxLayout(self.topic_box)
        self.topic_layout.setContentsMargins(0, 0, 0, 0)
        self.topic_layout.setAlignment(Qt.AlignTop)
        self.add_day = QPushButton("+ Add topic")
        self.add_day.clicked.connect(lambda: self.add_topic_field())
        self.topic_layout.addWidget(self.add_day)
        self.topic_scroll = QScrollArea()
        self.topic_scroll.setWidgetResizable(True)
        self.topic_scroll.setWidget(self.topic_box)
        self.topic_scroll.setMaximumHeight(220)
        f.addRow(self.topic_scroll)
        self.validation = QLabel()
        self.validation.setWordWrap(True)
        self.validation.setStyleSheet("color: #b03030")
        f.addRow(self.validation)
        buttons = QHBoxLayout()
        self.save = QPushButton("Save changes" if task is not None else "Create")
        self.save.setDefault(True)
        cancel = QPushButton("Cancel")
        self.save.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(self.save)
        buttons.addWidget(cancel)
        if task is not None:
            delete = QPushButton("Delete")
            delete.setObjectName("delete_task")
            delete.setAutoDefault(False)
            delete.setMaximumWidth(80)
            delete.setToolTip("Permanently delete this task and its history")
            delete.clicked.connect(lambda: self.deleteRequested.emit())
            buttons.addStretch()
            buttons.addWidget(delete)
            self.name.setText(task["name"])
            self.recurrence.setCurrentIndex(
                self.recurrence.findData(task["recurrence"])
            )
            self.start.setDate(QDate.fromString(task["start_date"], "yyyy-MM-dd"))
            self.end.setDate(
                max(self.start.date(), QDate.fromString(date_str(), "yyyy-MM-dd"))
            )
            self.end_enabled.setChecked(bool(task["end_date"]))
            if task["end_date"]:
                self.end.setDate(QDate.fromString(task["end_date"], "yyyy-MM-dd"))
            self.target.setCurrentIndex(self.target.findData(task["target_type"]))
            self.expected.setValue(task["expected_minutes"] or 0)
            if task["target_start"]:
                self.target_start.setTime(
                    QTime.fromString(task["target_start"], "HH:mm")
                )
            if task["target_end"]:
                self.target_end.setTime(QTime.fromString(task["target_end"], "HH:mm"))
            self.quantity.setValue(task["quantity_target"] or 1)
            self.unit.setText(task["quantity_unit"])
            if task["active_session_id"]:
                for field in (
                    self.start,
                    self.end_enabled,
                    self.recurrence,
                ):
                    field.setEnabled(False)
                    field.setToolTip(
                        "Complete the running task before changing its schedule."
                    )
            hint = QLabel(
                "Changes apply to this task's schedule. Removing a topic keeps saved journals."
            )
            hint.setWordWrap(True)
            f.insertRow(0, hint)
        self.schedule_locked = bool(task is not None and task["active_session_id"])
        f.addRow(buttons)
        self.day_plan.setChecked(bool(topics))
        for topic in topics or []:
            self.add_topic_field(topic)
        if not topics:
            self.add_topic_field()
        self.name.textChanged.connect(self.validate_form)
        self.start.dateChanged.connect(self.start_changed)
        self.end.dateChanged.connect(self.end_date_changed)
        self.recurrence.currentIndexChanged.connect(self.recurrence_changed)
        self.end_enabled.toggled.connect(self.end_enabled_changed)
        self.day_plan.toggled.connect(self.update_schedule)
        self.target.currentIndexChanged.connect(self.validate_form)
        self.expected.valueChanged.connect(self.validate_form)
        self.quantity.valueChanged.connect(self.validate_form)
        self.unit.textChanged.connect(self.validate_form)
        self.target_start.timeChanged.connect(self.validate_form)
        self.target_end.timeChanged.connect(self.validate_form)
        self._loading = False
        self.update_schedule()

    def update_target_fields(self):
        kind = self.target.currentData()
        for field, visible in [
            (self.expected, kind == "duration"),
            (self.target_start, kind == "time_range"),
            (self.target_end, kind == "time_range"),
            (self.quantity, kind == "quantity"),
            (self.unit, kind == "quantity"),
        ]:
            self.form.setRowVisible(field, visible)

    def schedule(self):
        return dict(
            start_date=self.start.date().toString("yyyy-MM-dd"),
            end_date=self.end.date().toString("yyyy-MM-dd")
            if self.end_enabled.isChecked()
            else None,
            recurrence=self.recurrence.currentData(),
            recurrence_interval=self.legacy_interval
            if self.recurrence.currentData() == "custom"
            else 1,
        )

    def end_enabled_changed(self, enabled):
        if self._loading:
            return
        self._loading = True
        if enabled:
            self.recurrence.setCurrentIndex(self.recurrence.findData("date_range"))
        elif self.recurrence.currentData() == "date_range":
            self.recurrence.setCurrentIndex(self.recurrence.findData("daily"))
        self._loading = False
        self.update_schedule()

    def end_date_changed(self):
        if self._loading:
            return
        if self.end_enabled.isChecked():
            self.end_enabled_changed(True)
        else:
            self.update_schedule()

    def recurrence_changed(self):
        if self._loading:
            return
        self._loading = True
        self.end_enabled.setChecked(self.recurrence.currentData() == "date_range")
        self._loading = False
        self.update_schedule()

    def next_topic_date(self):
        schedule = self.schedule()
        used = {field.date().toString("yyyy-MM-dd") for field in self.topic_dates}
        candidate = schedule["start_date"]
        while candidate in used:
            if schedule["recurrence"] == "none":
                return None
            candidate = TrackerService.next_applicable_date(schedule, candidate)
        if schedule["end_date"] and candidate > schedule["end_date"]:
            return None
        return QDate.fromString(candidate, "yyyy-MM-dd")

    def add_topic_field(self, topic=None):
        chosen = (
            QDate.fromString(topic["scheduled_date"], "yyyy-MM-dd")
            if topic is not None
            else self.next_topic_date()
        )
        if chosen is None:
            return
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        day = QDateEdit(chosen)
        day.setCalendarPopup(True)
        day.setDisplayFormat("dd MMM yyyy")
        field = QLineEdit(topic["topic"] if topic is not None else "")
        field.setPlaceholderText("Topic")
        remove = QPushButton("Remove")
        remove.setAutoDefault(False)
        remove.clicked.connect(lambda: self.remove_topic_field(field))
        self.topic_fields.append(field)
        self.topic_dates.append(day)
        self.topic_ids.append(topic["id"] if topic is not None else None)
        self.topic_rows.append(row)
        for widget in (day, field, remove):
            layout.addWidget(widget)
        self.topic_layout.insertWidget(self.topic_layout.count() - 1, row)
        day.dateChanged.connect(self.update_schedule)
        field.textChanged.connect(self.validate_form)
        if not self._loading:
            self.update_schedule()

    def remove_topic_field(self, field):
        index = self.topic_fields.index(field)
        row = self.topic_rows.pop(index)
        self.topic_fields.pop(index)
        self.topic_dates.pop(index)
        self.topic_ids.pop(index)
        self.topic_layout.removeWidget(row)
        row.deleteLater()
        self.update_schedule()

    def start_changed(self):
        if not self.day_plan.isChecked():
            for field, day, tid in zip(
                self.topic_fields, self.topic_dates, self.topic_ids
            ):
                if tid is None and not field.text().strip():
                    day.setDateRange(self.start.date(), QDate(9999, 12, 31))
                    day.setDate(self.start.date())
        self.update_schedule()

    def update_schedule(self):
        if self._loading:
            return
        self._loading = True
        self.end.setMinimumDate(self.start.date())
        self.end.setEnabled(self.end_enabled.isChecked() and not self.schedule_locked)
        self.topic_scroll.setVisible(self.day_plan.isChecked())
        limit = self.end.date() if self.end_enabled.isChecked() else QDate(9999, 12, 31)
        for field in self.topic_dates:
            # Keep invalid existing dates visible rather than silently moving a topic.
            # The inline error disables saving until it is moved or removed.
            current = field.date()
            field.setDateRange(min(self.start.date(), current), max(limit, current))
        self.add_day.setEnabled(self.next_topic_date() is not None)
        self.add_day.setToolTip(
            "No more scheduled dates within the end date"
            if not self.add_day.isEnabled()
            else "Add another dated topic"
        )
        self._loading = False
        self.validate_form()

    def validate_form(self):
        if self._loading:
            return
        values = self.values()
        try:
            TrackerService._validate_task(dict(values, priority=2))
            TrackerService._validate_target(values)
            TrackerService._validate_topics(
                values, values["topics"], self.original_topics
            )
            message = ""
        except (ValueError, TypeError) as error:
            message = str(error)
        self.validation.setText(message)
        self.save.setEnabled(not message)

    def accept(self):
        self.validate_form()
        if self.save.isEnabled():
            super().accept()

    def values(self):
        kind = self.target.currentData()
        return dict(
            name=self.name.text(),
            **self.schedule(),
            expected_minutes=(self.expected.value() or None)
            if kind == "duration"
            else None,
            target_type=kind,
            target_start=self.target_start.time().toString("HH:mm")
            if kind == "time_range"
            else None,
            target_end=self.target_end.time().toString("HH:mm")
            if kind == "time_range"
            else None,
            quantity_target=self.quantity.value() if kind == "quantity" else None,
            quantity_unit=self.unit.text(),
            topics=[
                dict(
                    id=tid,
                    scheduled_date=day.date().toString("yyyy-MM-dd"),
                    topic=field.text(),
                )
                for tid, day, field in zip(
                    self.topic_ids, self.topic_dates, self.topic_fields
                )
            ]
            if self.day_plan.isChecked()
            else [],
        )


class DateRangeDialog(QDialog):
    def __init__(self, initial_date, parent=None, *, title, all_label, hint, action):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(460, 250)
        form = QFormLayout(self)
        self.all_dates = QCheckBox(all_label)
        self.start = QDateEdit(initial_date)
        self.end = QDateEdit(initial_date)
        for field in (self.start, self.end):
            field.setCalendarPopup(True)
            field.setDisplayFormat("dd MMM yyyy")
        form.addRow(self.all_dates)
        form.addRow("From (inclusive)", self.start)
        form.addRow("To (inclusive)", self.end)
        self.all_dates.toggled.connect(
            lambda checked: self.start.setEnabled(not checked)
        )
        self.all_dates.toggled.connect(lambda checked: self.end.setEnabled(not checked))
        hint = QLabel(hint)
        hint.setWordWrap(True)
        form.addRow(hint)
        buttons = QHBoxLayout()
        export = QPushButton(action)
        export.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(export)
        buttons.addWidget(cancel)
        form.addRow(buttons)

    def values(self):
        if self.all_dates.isChecked():
            return {"start_date": None, "end_date": None}
        return {
            "start_date": self.start.date().toString("yyyy-MM-dd"),
            "end_date": self.end.date().toString("yyyy-MM-dd"),
        }

    def accept(self):
        if not self.all_dates.isChecked() and self.end.date() < self.start.date():
            QMessageBox.warning(
                self, "Invalid dates", "To date cannot be before From date."
            )
            return
        super().accept()


class ExportDialog(DateRangeDialog):
    def __init__(self, initial_date, parent=None):
        super().__init__(
            initial_date,
            parent,
            title="Export",
            all_label="All dates (full backup)",
            hint="Includes journals, topics and activity for these dates. Sessions are selected by their start date, including all their breaks.",
            action="Choose file and export",
        )


class ClearHistoryDialog(DateRangeDialog):
    def __init__(self, initial_date, parent=None):
        super().__init__(
            initial_date,
            parent,
            title="Clear History",
            all_label="All history",
            hint="Permanently removes journals and activity for the selected dates and resets completion marks. Sessions use their start date, including all their breaks. Tasks, targets and planned topics stay saved. Running tasks and all their history are left untouched.",
            action="Continue",
        )


class CompletionDialog(QDialog):
    def __init__(self, task_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Complete task & journal")
        self.resize(540, 360)
        layout = QVBoxLayout(self)
        task_label = QLabel(task_name)
        task_label.setTextFormat(Qt.PlainText)
        task_label.setWordWrap(True)
        layout.addWidget(task_label)
        layout.addWidget(QLabel("What did you do or learn for this task today?"))
        self.content = QTextEdit()
        self.content.setPlaceholderText("Write your journal for this task…")
        layout.addWidget(self.content)
        buttons = QHBoxLayout()
        save = QPushButton("Save journal and complete")
        save.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(save)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def accept(self):
        if not self.content.toPlainText().strip():
            QMessageBox.warning(
                self, "Journal required", "Write a journal before completing this task."
            )
            return
        super().accept()


class TaskTable(QTableWidget):
    orderRequested = Signal(list)
    MIME_TYPE = "application/x-tracker-task-id"

    def __init__(self, parent=None):
        super().__init__(0, 6, parent)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(True)

    def startDrag(self, supported_actions):
        row = self.currentRow()
        if row < 0:
            return
        mime = QMimeData()
        mime.setData(self.MIME_TYPE, str(self.item(row, 0).data(Qt.UserRole)).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.MoveAction)

    def dragEnterEvent(self, event):
        if event.source() is self and event.mimeData().hasFormat(self.MIME_TYPE):
            event.setDropAction(Qt.MoveAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        if event.source() is not self or not event.mimeData().hasFormat(self.MIME_TYPE):
            event.ignore()
            return
        try:
            task_id = int(bytes(event.mimeData().data(self.MIME_TYPE)))
        except ValueError:
            event.ignore()
            return
        point = event.position().toPoint()
        target = self.indexAt(point)
        slot = target.row() if target.isValid() else self.rowCount()
        if target.isValid() and point.y() > self.visualRect(target).center().y():
            slot += 1
        if self.move_task(task_id, slot):
            event.setDropAction(Qt.MoveAction)
            event.accept()
        else:
            event.ignore()

    def move_task(self, task_id, slot):
        ids = [self.item(row, 0).data(Qt.UserRole) for row in range(self.rowCount())]
        if task_id not in ids or not 0 <= slot <= len(ids):
            return False
        previous = ids.index(task_id)
        ids.remove(task_id)
        ids.insert(slot - (previous < slot), task_id)
        self.orderRequested.emit(ids)
        return True


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Progress Tracker")
        self.resize(1250, 800)
        self.db = Database()
        self.service = TrackerService(self.db)
        self.service.rollover()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self._rollover_day = date_str()
        self._closed = False
        self._history_signature = None
        self._clock_sample = (
            datetime.fromisoformat(iso()).timestamp(),
            time.monotonic(),
        )
        # A cheap watchdog detects suspend/clock changes and another app instance;
        # deadlines, rather than this watchdog, drive ordinary notifications.
        self.watchdog = QTimer(self)
        self.watchdog.timeout.connect(self.check_clock)
        self.watchdog.start(30000)
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.visible_tasks = []
        self.reminder_sound = ReminderSound(self)
        self.time_up_notice = None
        self.build_dashboard()
        self.build_history()
        self.tabs.currentChanged.connect(
            lambda index: self.refresh_history() if index == 1 else None
        )
        for table in (
            self.task_table,
            self.history_table,
        ):
            table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tray = QSystemTrayIcon(
            self.style().standardIcon(QStyle.SP_MessageBoxInformation), self
        )
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        self.refresh()

    def build_dashboard(self):
        w = QWidget()
        l = QVBoxLayout(w)
        top = QHBoxLayout()
        self.progress = QLabel()
        add = QPushButton("+ New Task")
        add.clicked.connect(self.new_task)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        top.addWidget(self.progress)
        top.addStretch()
        top.addWidget(add)
        top.addWidget(refresh)
        export = QPushButton("Export")
        export.clicked.connect(self.export_json)
        top.addWidget(export)
        l.addLayout(top)
        controls = QHBoxLayout()
        self.view_date = QDateEdit(QDate.fromString(date_str(), "yyyy-MM-dd"))
        self.view_date.setCalendarPopup(True)
        self.view_date.setDisplayFormat("ddd, dd MMM yyyy")
        previous = QPushButton("← Previous day")
        previous.clicked.connect(
            lambda: self.view_date.setDate(self.view_date.date().addDays(-1))
        )
        today = QPushButton("Today")
        today.clicked.connect(
            lambda: self.view_date.setDate(QDate.fromString(date_str(), "yyyy-MM-dd"))
        )
        following = QPushButton("Next day →")
        following.clicked.connect(
            lambda: self.view_date.setDate(self.view_date.date().addDays(1))
        )
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search this day's tasks…")
        self.search.textChanged.connect(self.refresh)
        for widget in (previous, self.view_date, today, following, self.search):
            controls.addWidget(widget)
        self.info = QToolButton()
        self.info.setIcon(self.style().standardIcon(QStyle.SP_MessageBoxInformation))
        self.info.setFixedSize(26, 26)
        self.info.setAccessibleName("Storage information")
        self.info.setToolTip(
            f"Database: {self.db.path.resolve()}\nLocal-first storage. The SQLite database is the source of truth."
        )
        self.info.setAutoRaise(True)
        controls.addWidget(self.info)
        l.addLayout(controls)
        self.reminder_banner = QLabel()
        self.reminder_banner.setTextFormat(Qt.PlainText)
        self.reminder_banner.setWordWrap(True)
        self.reminder_banner.hide()
        l.addWidget(self.reminder_banner)
        l.addWidget(
            QLabel(
                "Double-click a task name to edit or delete. Drag to reorder. Completed tasks move to the bottom."
            )
        )
        self.task_table = TaskTable()
        self.task_table.orderRequested.connect(self.reorder_tasks)
        self.task_table.cellDoubleClicked.connect(self.open_task_name)
        self.task_table.setHorizontalHeaderLabels(
            ["Task", "Topic", "Timer", "Elapsed", "Total time", "Actions"]
        )
        self.task_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.task_table.setColumnWidth(1, 280)
        for column in (2, 3, 4):
            self.task_table.setColumnHidden(column, True)
        self.task_table.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.ResizeToContents
        )
        l.addWidget(self.task_table)
        self.view_date.dateChanged.connect(self.refresh)
        self.tabs.addTab(w, "Dashboard")

    def build_history(self):
        w = QWidget()
        l = QVBoxLayout(w)
        bar = QHBoxLayout()
        today = QDate.fromString(date_str(), "yyyy-MM-dd")
        self.hist_start = QDateEdit(today)
        self.hist_end = QDateEdit(today)
        for field in (self.hist_start, self.hist_end):
            field.setCalendarPopup(True)
            field.setDisplayFormat("dd MMM yyyy")
        self.hist_all = QCheckBox("All dates")
        self.hist_start.dateChanged.connect(self.history_start_changed)
        self.hist_end.dateChanged.connect(self.history_end_changed)
        self.hist_all.toggled.connect(self.history_range_changed)
        for label, field in (("From", self.hist_start), ("To", self.hist_end)):
            bar.addWidget(QLabel(label))
            bar.addWidget(field)
        bar.addWidget(self.hist_all)
        bar.addStretch()
        clear = QPushButton("Clear History…")
        clear.clicked.connect(self.clear_history)
        bar.addWidget(clear)
        l.addLayout(bar)
        self.history_table = QTableWidget(0, 5)
        self.history_table.setHorizontalHeaderLabels(
            ["Time", "Type", "Title", "Task", "Content"]
        )
        self.history_table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.Stretch
        )
        l.addWidget(self.history_table)
        self.history_table.setWordWrap(True)
        self.history_table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents
        )
        self.tabs.addTab(w, "History")

    def new_task(self):
        d = TaskDialog(self, initial_date=self.view_date.date())
        while d.exec() == QDialog.Accepted:
            v = d.values()
            if not v["name"].strip():
                QMessageBox.warning(self, "Validation", "Task name is required.")
                return
            try:
                self.service.create_task(**v)
                self.refresh()
                return
            except (ValueError, sqlite3.Error) as e:
                QMessageBox.warning(self, "Cannot create task", str(e))

    def edit_task(self, tid):
        task = next((dict(t) for t in self.service.tasks() if t["id"] == tid), None)
        if task is None:
            return
        topics = self.service.topics(tid)
        dialog = TaskDialog(self, task=task, topics=topics)

        def delete():
            if self.remove_task(tid):
                dialog.reject()

        dialog.deleteRequested.connect(delete)
        while dialog.exec() == QDialog.Accepted:
            values = dialog.values()
            # Send changes only; unchanged schedules must not interrupt timers.
            changes = {
                key: value
                for key, value in values.items()
                if key != "topics" and value != task[key]
            }
            old_topics = [
                dict(id=t["id"], scheduled_date=t["scheduled_date"], topic=t["topic"])
                for t in topics
            ]
            if values["topics"] != old_topics:
                changes["topics"] = values["topics"]
            try:
                self.service.update_task(tid, **changes)
                self.refresh()
                return
            except (ValueError, sqlite3.Error) as error:
                QMessageBox.warning(self, "Cannot edit task", str(error))

    def remove_task(self, tid):
        task = next((t for t in self.service.tasks() if t["id"] == tid), None)
        if task is None:
            return False
        answer = QMessageBox.question(
            self,
            "Delete task",
            f"Delete ‘{task['name']}’ from the dashboard and stop all future occurrences?\n\n"
            "This permanently deletes this task, its topics, journals, sessions and breaks. "
            "Other tasks are unaffected. This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return False
        try:
            self.service.delete_task(tid)
            self.refresh()
            return True
        except (ValueError, sqlite3.Error) as error:
            QMessageBox.warning(self, "Cannot delete task", str(error))
            return False

    def open_task_name(self, row, column):
        if column == 0:
            self.edit_task(self.task_table.item(row, 0).data(Qt.UserRole))

    def task_button(self, task):
        actions = QWidget()
        layout = QHBoxLayout(actions)
        layout.setContentsMargins(0, 0, 0, 0)
        if (
            task.get("view_date", date_str()) != date_str()
            or task["status"] == "Completed"
        ):
            label = QLabel(
                "✓ Done"
                if task["status"] == "Completed"
                else "Scheduled"
                if task.get("view_date", "") > date_str()
                else "Past day"
            )
            layout.addWidget(label)
            return actions
        if task["active_session_id"]:
            if task["active_break_id"]:
                resume = QPushButton("Resume")
                resume.clicked.connect(lambda _, tid=task["id"]: self.resume(tid))
                layout.addWidget(resume)
            else:
                pause = QToolButton()
                pause.setText("Break")
                pause.setPopupMode(QToolButton.MenuButtonPopup)
                pause.setToolTip(
                    "Pause the estimated countdown; the work timer keeps running. Use the arrow to mark a distraction."
                )
                pause.clicked.connect(lambda _, tid=task["id"]: self.begin_break(tid))
                menu = QMenu(pause)
                distracted = menu.addAction("Distracted")
                distracted.triggered.connect(
                    lambda _, tid=task["id"]: self.begin_break(
                        tid, reason="distraction"
                    )
                )
                pause.setMenu(menu)
                layout.addWidget(pause)
            complete = QPushButton("Complete")
            complete.clicked.connect(lambda _, tid=task["id"]: self.complete(tid))
            layout.addWidget(complete)
        else:
            button = QPushButton()
            if (
                task["recurrence"] != "none"
                and task["end_date"]
                and task["end_date"] < date_str()
            ):
                button.setText("Ended")
                button.setEnabled(False)
            elif task["active_date"] > date_str():
                button.setText("Scheduled")
                button.setEnabled(False)
            else:
                button.setText("Start")
                button.clicked.connect(lambda _, tid=task["id"]: self.start(tid))
            layout.addWidget(button)
        return actions

    def begin_break(self, tid, reason="break"):
        try:
            self.service.begin_break(tid, reason=reason)
            self.refresh()
        except (ValueError, sqlite3.Error) as e:
            QMessageBox.warning(self, "Cannot start break", str(e))

    def resume(self, tid):
        try:
            self.service.resume_task(tid)
            self.refresh()
        except (ValueError, sqlite3.Error) as e:
            QMessageBox.warning(self, "Cannot resume", str(e))

    def start(self, tid):
        try:
            self.service.start_task(tid)
            self.refresh()
        except (ValueError, sqlite3.Error) as e:
            QMessageBox.warning(self, "Cannot start", str(e))

    def complete(self, tid):
        task = next(
            (row for row in self.service.tasks_for_day() if row["id"] == tid), None
        )
        if task is None:
            QMessageBox.warning(
                self, "Cannot complete", "Task not found. Refresh the dashboard."
            )
            return
        dialog = CompletionDialog(task["name"], self)
        while dialog.exec() == QDialog.Accepted:
            try:
                self.service.complete_task(
                    tid,
                    journal=dialog.content.toPlainText(),
                    expected_date=task["active_date"],
                )
            except (ValueError, sqlite3.Error) as e:
                QMessageBox.warning(self, "Cannot complete", str(e))
                # Keep the journal text available when saving fails.
                continue
            self.refresh()
            return

    def reorder_tasks(self, task_ids):
        selected = self.task_table.currentRow()
        selected_id = (
            self.task_table.item(selected, 0).data(Qt.UserRole)
            if selected >= 0
            else None
        )
        try:
            incomplete = {
                task["id"]
                for task in self.visible_tasks
                if task["status"] != "Completed"
            }
            task_ids = [tid for tid in task_ids if tid in incomplete] + [
                tid for tid in task_ids if tid not in incomplete
            ]
            self.service.reorder_tasks(task_ids)
        except ValueError as e:
            QMessageBox.warning(self, "Cannot reorder", str(e))
        except sqlite3.Error as e:
            QMessageBox.warning(self, "Cannot reorder", str(e))
        self.refresh()
        for row in range(self.task_table.rowCount()):
            if self.task_table.item(row, 0).data(Qt.UserRole) == selected_id:
                self.task_table.selectRow(row)
                break

    def check_rollover(self):
        today = date_str()
        if today == self._rollover_day:
            return
        previous = self._rollover_day
        self.service.rollover()
        self._rollover_day = today
        if self.view_date.date().toString("yyyy-MM-dd") == previous:
            self.view_date.setDate(QDate.fromString(today, "yyyy-MM-dd"))

    def schedule_next_tick(self, candidates=None):
        candidates = (
            self.service.reminder_candidates() if candidates is None else candidates
        )
        now = datetime.fromisoformat(iso())
        midnight = datetime.combine(
            now.date() + timedelta(days=1), datetime.min.time()
        ).astimezone()
        delays = [max(0, midnight.timestamp() - now.timestamp())]
        for task, state in candidates:
            if state["remaining"] <= 0 or not task["active_break_id"]:
                delays.append(max(0, state["remaining"]))
        self.timer.start(max(100, math.ceil(min(delays) * 1000) + 100))

    def check_clock(self):
        if self._closed:
            return
        sample = (datetime.fromisoformat(iso()).timestamp(), time.monotonic())
        wall, monotonic = self._clock_sample
        self._clock_sample = sample
        signature = (
            self.db.conn.total_changes,
            self.db.conn.execute("PRAGMA data_version").fetchone()[0],
            date_str(),
        )
        if (
            abs((sample[0] - wall) - (sample[1] - monotonic)) > 2
            or signature != self._data_signature
        ):
            self.tick()

    def changeEvent(self, event):
        super().changeEvent(event)
        if (
            event.type() == QEvent.ActivationChange
            and self.isActiveWindow()
            and hasattr(self, "_data_signature")
            and not self._closed
        ):
            QTimer.singleShot(0, self.tick)

    def tick(self):
        if self._closed:
            return
        try:
            self.check_rollover()
            candidates = self.service.reminder_candidates()
            reminders = self.service.take_due_reminders(candidates)
            if reminders:
                self.notify_time_up(reminders)
            signature = (
                self.db.conn.total_changes,
                self.db.conn.execute("PRAGMA data_version").fetchone()[0],
                date_str(),
            )
            if signature != self._data_signature:
                self.refresh()
            else:
                self.schedule_next_tick(candidates)
        except sqlite3.Error as e:
            self.timer.stop()
            QMessageBox.warning(self, "Database error", str(e))

    def notify_time_up(self, reminders):
        message = "\n".join(item["message"] for item in reminders)
        self.reminder_banner.setText(message)
        self.reminder_banner.show()
        self.reminder_sound.play()
        QApplication.alert(self)
        if self.tray.isVisible():
            self.tray.showMessage(
                "Task timer", message, QSystemTrayIcon.Information, 10000
            )
        # A visible, non-blocking fallback also works without a system tray.
        if self.time_up_notice is None:
            self.time_up_notice = QMessageBox(self)
            self.time_up_notice.setWindowTitle("Time target reached")
            self.time_up_notice.setIcon(QMessageBox.Information)
            self.time_up_notice.setStandardButtons(QMessageBox.Ok)
            self.time_up_notice.setTextFormat(Qt.PlainText)
            self.time_up_notice.setWindowModality(Qt.NonModal)
        self.time_up_notice.setText(message)
        self.time_up_notice.setInformativeText(
            "Your task is still running. Continue if you like, or complete it when ready."
        )
        self.time_up_notice.show()

    def topic_widget(self, task):
        label = QLabel(task["topic"]["topic"] if task["topic"] else "")
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(True)
        label.setContentsMargins(4, 2, 4, 2)
        label.setToolTip(label.text())
        font = label.font()
        font.setStrikeOut(task["status"] == "Completed")
        label.setFont(font)
        return label

    def refresh_elapsed(self):
        if all(self.task_table.isColumnHidden(i) for i in (2, 3, 4)):
            return
        for i, task in enumerate(self.visible_tasks):
            timing = self.service.timer_state(task)
            self.task_table.item(i, 3).setText(fmt_seconds(timing["day_elapsed"]))
            self.task_table.item(i, 4).setText(
                fmt_seconds(task["total_seconds"] + timing["elapsed"])
            )
            remaining = timing["remaining"]
            text = ""
            if task["view_date"] != date_str():
                text = ""
            elif task["status"] == "Completed":
                text = "Finished"
            elif timing["before_start"]:
                text = f"Starts in {fmt_seconds(timing['before_start'])}"
            elif remaining is not None:
                text = (
                    f"{fmt_seconds(remaining)} left"
                    if remaining > 0
                    else f"Time up (+{fmt_seconds(-remaining)})"
                )
            if task["active_break_id"]:
                text = (
                    "On break" if task["break_reason"] == "break" else "Distracted"
                ) + (f" · {text}" if text else "")
            self.task_table.item(i, 2).setText(text)

    def refresh(self):
        self.check_rollover()
        day = self.view_date.date().toString("yyyy-MM-dd")
        rows = self.service.tasks_for_day(day)
        query = self.search.text().strip().lower()
        if query:
            rows = [
                r
                for r in rows
                if query in r["name"].lower()
                or query in (r["topic"]["topic"].lower() if r["topic"] else "")
            ]
        self.visible_tasks = rows
        self.task_table.setRowCount(0)
        completed = sum(r["status"] == "Completed" for r in rows)
        self.progress.setText(f"{day} · {completed}/{len(rows)} completed")
        for i, task in enumerate(rows):
            self.task_table.insertRow(i)
            values = [task["name"], "", "", "", ""]
            for j, value in enumerate(values):
                item = QTableWidgetItem(value)
                if task["status"] == "Completed":
                    font = item.font()
                    font.setStrikeOut(j == 0)
                    item.setFont(font)
                self.task_table.setItem(i, j, item)
            self.task_table.item(i, 0).setData(Qt.UserRole, task["id"])
            self.task_table.item(i, 0).setToolTip("Double-click to edit or delete")
            self.task_table.setCellWidget(i, 1, self.topic_widget(task))
            self.task_table.setCellWidget(i, 5, self.task_button(task))
        self.task_table.resizeRowsToContents()
        self.refresh_elapsed()
        if self.tabs.currentIndex() == 1:
            self.refresh_history()
        self.schedule_next_tick()
        self._data_signature = (
            self.db.conn.total_changes,
            self.db.conn.execute("PRAGMA data_version").fetchone()[0],
            date_str(),
        )

    def history_start_changed(self):
        if self.hist_start.date() > self.hist_end.date():
            self.hist_end.setDate(self.hist_start.date())
        self.refresh_history()

    def history_end_changed(self):
        if self.hist_end.date() < self.hist_start.date():
            self.hist_start.setDate(self.hist_end.date())
        self.refresh_history()

    def history_range_changed(self, all_dates):
        self.hist_start.setEnabled(not all_dates)
        self.hist_end.setEnabled(not all_dates)
        self.refresh_history()

    def clear_history(self):
        dialog = ClearHistoryDialog(self.hist_start.date(), self)
        dialog.end.setDate(self.hist_end.date())
        dialog.all_dates.setChecked(self.hist_all.isChecked())
        if dialog.exec() != QDialog.Accepted:
            return
        dates = dialog.values()
        period = (
            f"{dates['start_date']} through {dates['end_date']} (inclusive)"
            if dates["start_date"]
            else "ALL dates"
        )
        answer = QMessageBox.question(
            self,
            "Confirm Clear History",
            f"Permanently clear history for {period}?\n\n"
            "This deletes saved journals, recorded sessions and their breaks, and resets completion marks for these dates. "
            "Sessions are selected by their start date, including all their breaks. "
            "Running tasks and all their history are left untouched. "
            "Task definitions, targets and planned topics remain. This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.service.clear_history(**dates)
            self.refresh()
        except (ValueError, sqlite3.Error) as error:
            QMessageBox.warning(self, "Cannot clear history", str(error))

    def refresh_history(self):
        if not hasattr(self, "history_table"):
            return
        signature = (
            self.db.conn.total_changes,
            self.db.conn.execute("PRAGMA data_version").fetchone()[0],
            self.hist_all.isChecked(),
            self.hist_start.date().toString("yyyy-MM-dd"),
            self.hist_end.date().toString("yyyy-MM-dd"),
        )
        if signature == self._history_signature:
            return
        rows = self.service.journal(
            None
            if self.hist_all.isChecked()
            else self.hist_start.date().toString("yyyy-MM-dd"),
            None
            if self.hist_all.isChecked()
            else self.hist_end.date().toString("yyyy-MM-dd"),
        )
        self.history_table.setUpdatesEnabled(False)
        self.history_table.setRowCount(len(rows))
        for i, r in enumerate(reversed(rows)):
            vals = [
                r["timestamp"],
                r["entry_type"],
                r["title"],
                r["task_name"] or "",
                r["content"],
            ]
            for j, v in enumerate(vals):
                self.history_table.setItem(i, j, QTableWidgetItem(str(v)))
        self.history_table.setUpdatesEnabled(True)
        self._history_signature = signature

    def export_json(self):
        dialog = ExportDialog(self.view_date.date(), self)
        if dialog.exec() != QDialog.Accepted:
            return
        dates = dialog.values()
        filename = (
            f"progress_{dates['start_date']}_to_{dates['end_date']}.json"
            if dates["start_date"]
            else "progress_all_dates.json"
        )
        documents = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
        destination = Path(documents) if documents else Path.home()
        path, _ = QFileDialog.getSaveFileName(
            self, "Export", str(destination / filename), "JSON (*.json)"
        )
        if path:
            try:
                self.service.export_json(path, **dates)
            except (OSError, ValueError, sqlite3.Error) as e:
                QMessageBox.warning(self, "Export failed", str(e))
                return
            QMessageBox.information(self, "Exported", path)

    def closeEvent(self, event):
        self._closed = True
        self.timer.stop()
        self.watchdog.stop()
        self.tray.hide()
        self.db.close()
        event.accept()


def run():
    app = QApplication(sys.argv)
    app.setApplicationName("Progress Tracker")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
