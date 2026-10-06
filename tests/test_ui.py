import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QDate, QTimer, QMimeData, QPointF
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QAbstractButton,
    QToolButton,
    QPushButton,
    QDialog,
    QMessageBox,
)
from app.ui import MainWindow, TaskDialog, CompletionDialog, ExportDialog


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    from datetime import datetime
    from app import timeutil

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    moment = datetime.fromisoformat("2026-10-04T09:00:00").astimezone()
    monkeypatch.setattr(timeutil, "now", lambda: moment)
    win = MainWindow()
    yield win
    win.close()


def test_end_date_is_saved(qapp):
    dialog = TaskDialog()
    assert dialog.values()["end_date"] is None
    dialog.end_enabled.setChecked(True)
    dialog.end.setDate(QDate(2027, 3, 4))
    assert dialog.values()["end_date"] == "2027-03-04"
    dialog.close()


def test_timer_tick_preserves_action_buttons(window):
    tid = window.service.create_task("Task")
    window.service.start_task(tid)
    window.refresh()
    actions = window.task_table.cellWidget(0, 5)
    window.tick()
    assert window.task_table.cellWidget(0, 5) is actions
    buttons = {
        button.text(): button for button in actions.findChildren(QAbstractButton)
    }
    assert set(buttons) == {"Break", "Complete"}
    buttons["Break"].click()
    assert window.service.tasks()[0]["active_session_id"] is not None
    assert window.service.tasks()[0]["active_break_id"] is not None
    assert window.task_table.editTriggers() == QAbstractItemView.NoEditTriggers


def test_future_task_is_disabled(window):
    window.service.create_task("Future", start_date="2099-01-01")
    window.refresh()
    assert window.task_table.rowCount() == 0
    window.view_date.setDate(QDate(2099, 1, 1))
    assert window.task_table.rowCount() == 1
    assert not window.task_table.cellWidget(0, 5).findChildren(QAbstractButton)


def test_tabs_and_priority_are_simplified(window):
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == [
        "Dashboard",
        "History",
    ]
    dialog = TaskDialog()
    assert not hasattr(dialog, "priority")
    assert set(dialog.values()) == {
        "name",
        "recurrence",
        "recurrence_interval",
        "start_date",
        "end_date",
        "expected_minutes",
        "target_type",
        "target_start",
        "target_end",
        "quantity_target",
        "quantity_unit",
        "topics",
    }
    dialog.expected.setValue(45)
    assert dialog.values()["expected_minutes"] == 45
    assert f"Database: {window.db.path.resolve()}" in window.info.toolTip()
    assert (
        "Local-first storage. The SQLite database is the source of truth."
        in window.info.toolTip()
    )
    assert not window.info.icon().isNull()
    assert not hasattr(window, "filter")
    assert "Project" not in [
        window.task_table.horizontalHeaderItem(i).text() for i in range(6)
    ]
    assert any(b.text() == "Export" for b in window.findChildren(QPushButton))


def test_completion_dialog_requires_journal(qapp, monkeypatch):
    dialog = CompletionDialog("Study")
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))
    dialog.accept()
    assert dialog.result() == QDialog.Rejected
    assert warnings
    dialog.content.setPlainText("Learned how signatures work.")
    dialog.accept()
    assert dialog.result() == QDialog.Accepted


def test_complete_prompts_and_saves_linked_journal(window):
    tid = window.service.create_task("Study")
    window.service.start_task(tid)
    window.refresh()

    def fill_dialog():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, CompletionDialog)
        dialog.content.setPlainText("Learned about hashing today.")
        dialog.accept()

    QTimer.singleShot(0, fill_dialog)
    window.complete(tid)
    assert window.service.tasks()[0]["status"] == "Completed"
    journal = [r for r in window.service.journal() if r["entry_type"] == "Journal"]
    assert len(journal) == 1
    assert journal[0]["task_id"] == tid
    assert journal[0]["content"] == "Learned about hashing today."
    window.tabs.setCurrentIndex(1)
    assert "Learned about hashing today." in [
        window.history_table.item(i, 4).text()
        for i in range(window.history_table.rowCount())
    ]


def test_cancel_completion_preserves_running_task(window):
    tid = window.service.create_task("Study")
    sid = window.service.start_task(tid)
    window.refresh()
    QTimer.singleShot(0, lambda: QApplication.activeModalWidget().reject())
    window.complete(tid)
    task = window.service.tasks()[0]
    assert task["status"] == "In Progress"
    assert task["active_session_id"] == sid
    assert not [r for r in window.service.journal() if r["entry_type"] == "Journal"]


def test_drag_drop_reorders_whole_tasks_and_actions(window):
    from types import SimpleNamespace
    from unittest.mock import Mock

    ids = [window.service.create_task(name) for name in ["First", "Second", "Third"]]
    window.refresh()
    window.show()
    QApplication.processEvents()
    table = window.task_table
    mime = QMimeData()
    mime.setData(table.MIME_TYPE, str(ids[2]).encode())
    point = table.visualItemRect(table.item(0, 0)).topLeft()
    event = SimpleNamespace(
        source=lambda: table,
        mimeData=lambda: mime,
        position=lambda: QPointF(point),
        setDropAction=Mock(),
        accept=Mock(),
        ignore=Mock(),
    )
    table.dropEvent(event)
    event.accept.assert_called_once()
    assert [r["id"] for r in window.service.tasks()] == [ids[2], ids[0], ids[1]]
    assert table.item(0, 0).text() == "Third"
    # Buttons still act on their own task after moving the row.
    table.cellWidget(0, 5).findChild(QPushButton).click()
    assert window.service.tasks()[0]["status"] == "In Progress"
    assert window.service.tasks()[1]["status"] == "Pending"


def test_drag_to_bottom_with_filter_preserves_hidden_tasks(window):
    ids = [window.service.create_task(name) for name in ["Show A", "Hidden", "Show B"]]
    window.refresh()
    window.search.setText("Show")
    assert window.task_table.move_task(ids[0], 2)
    assert [r["id"] for r in window.service.tasks()] == [ids[2], ids[1], ids[0]]
    window.search.clear()
    assert [window.task_table.item(i, 0).text() for i in range(3)] == [
        "Show B",
        "Hidden",
        "Show A",
    ]


def test_break_and_resume_controls_keep_elapsed_time_running(window, monkeypatch):
    from datetime import datetime
    from app import timeutil

    moment = datetime.fromisoformat("2026-10-04T09:00:00").astimezone()
    monkeypatch.setattr(timeutil, "now", lambda: moment)
    tid = window.service.create_task("Task", expected_minutes=30)
    sid = window.service.start_task(tid)
    window.refresh()
    assert window.service.tasks()[0]["expected_minutes"] == 30
    assert "Target" not in [
        window.task_table.horizontalHeaderItem(i).text() for i in range(6)
    ]
    moment = datetime.fromisoformat("2026-10-04T09:05:00").astimezone()
    window.task_table.cellWidget(0, 5).findChild(QToolButton).click()
    assert window.service.tasks()[0]["active_break_id"] is not None
    moment = datetime.fromisoformat("2026-10-04T09:15:00").astimezone()
    window.tick()
    assert (
        window.service.timer_state(window.service.tasks_for_day()[0])["day_elapsed"]
        == 900
    )
    assert window.task_table.item(0, 4).text() == ""
    buttons = {
        b.text(): b
        for b in window.task_table.cellWidget(0, 5).findChildren(QAbstractButton)
    }
    assert set(buttons) == {"Resume", "Complete"}
    buttons["Resume"].click()
    assert window.service.tasks()[0]["active_break_id"] is None
    assert window.service.tasks()[0]["active_session_id"] == sid
    assert (
        window.service.timer_state(window.service.tasks_for_day()[0])["day_elapsed"]
        == 900
    )


def test_distraction_menu_and_completion_during_break(window):
    tid = window.service.create_task("Study")
    window.service.start_task(tid)
    window.refresh()
    break_button = window.task_table.cellWidget(0, 5).findChild(QToolButton)
    break_button.menu().actions()[0].trigger()
    assert window.service.tasks()[0]["break_reason"] == "distraction"
    assert window.task_table.item(0, 2).text() == ""

    def fill_dialog():
        dialog = QApplication.activeModalWidget()
        dialog.content.setPlainText("Finished after an interruption.")
        dialog.accept()

    QTimer.singleShot(0, fill_dialog)
    window.complete(tid)
    assert window.service.tasks()[0]["status"] == "Completed"
    interval = window.db.conn.execute("SELECT * FROM activity_breaks").fetchone()
    assert interval["end_timestamp"] is not None
    assert interval["end_reason"] == "completed"


def test_date_picker_and_completed_bottom_strikethrough(window):
    first = window.service.create_task("First")
    window.service.create_task("Second")
    window.service.complete_task(first, journal="Done")
    window.refresh()
    assert [window.task_table.item(i, 0).text() for i in range(2)] == [
        "Second",
        "First",
    ]
    assert window.task_table.item(1, 0).font().strikeOut()
    assert not window.task_table.cellWidget(1, 5).findChildren(QAbstractButton)
    window.view_date.setDate(QDate(2026, 10, 5))
    assert all(not window.task_table.item(i, 0).font().strikeOut() for i in range(2))
    assert all(
        not window.task_table.cellWidget(i, 5).findChildren(QAbstractButton)
        for i in range(2)
    )
    assert "Status" not in [
        window.task_table.horizontalHeaderItem(i).text() for i in range(6)
    ]


def test_create_day_plan_and_quantity_fields(qapp):
    dialog = TaskDialog(initial_date=QDate(2026, 10, 5))
    dialog.name.setText("ZKP")
    dialog.day_plan.setChecked(True)
    dialog.topic_fields[0].setText("Proof basics")
    dialog.add_day.click()
    dialog.topic_fields[1].setText("Commitments")
    dialog.target.setCurrentIndex(dialog.target.findData("quantity"))
    dialog.quantity.setValue(3)
    dialog.unit.setText("problems")
    values = dialog.values()
    assert values["start_date"] == "2026-10-05"
    assert [t["topic"] for t in values["topics"]] == ["Proof basics", "Commitments"]
    assert values["quantity_target"] == 3
    assert values["expected_minutes"] is None
    assert values["recurrence"] == "daily"
    assert dialog.recurrence.isEnabled()
    dialog.close()


def test_current_topic_is_plain_text_without_checkbox_or_day_number(window):
    from PySide6.QtWidgets import QCheckBox, QLabel

    tid = window.service.create_task("ZKP", topics=["Proof basics", "Commitments"])
    window.refresh()
    label = window.task_table.cellWidget(0, 1)
    assert isinstance(label, QLabel)
    assert label.text() == "Proof basics"
    assert not label.findChildren(QCheckBox)
    assert not label.findChildren(QToolButton)
    window.service.complete_task(tid, journal="Done")
    window.refresh()
    assert window.task_table.cellWidget(0, 1).font().strikeOut()
    assert window.service.topics(tid)[0]["completed_at"]
    window.view_date.setDate(QDate(2026, 10, 5))
    label = window.task_table.cellWidget(0, 1)
    assert label.text() == "Commitments"
    assert not label.font().strikeOut()


def test_timer_notification_does_not_block_or_stop_while_browsing_other_date(
    window, monkeypatch
):
    from datetime import datetime
    from app import timeutil

    tid = window.service.create_task("Timer", expected_minutes=1)
    sid = window.service.start_task(tid)
    window.view_date.setDate(QDate(2026, 10, 5))
    monkeypatch.setattr(
        timeutil,
        "now",
        lambda: datetime.fromisoformat("2026-10-04T09:01:00").astimezone(),
    )
    window.tick()
    assert "Time is up for Timer" in window.reminder_banner.text()
    assert window.service.tasks()[0]["active_session_id"] == sid
    assert window.view_date.date() == QDate(2026, 10, 5)
    assert (
        window.db.conn.execute("SELECT COUNT(*) FROM task_reminders").fetchone()[0] == 1
    )
    window.tick()
    assert (
        window.db.conn.execute("SELECT COUNT(*) FROM task_reminders").fetchone()[0] == 1
    )


def test_edit_running_task_preserves_timer_and_updates_topic(window):
    tid = window.service.create_task(
        "ZKP", topics=["Basics", "Proofs"], expected_minutes=30
    )
    sid = window.service.start_task(tid)
    window.service.begin_break(tid)
    original_topic = dict(window.service.topics(tid)[0])

    def fill():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, TaskDialog)
        assert dialog.name.text() == "ZKP"
        assert not dialog.start.isEnabled()
        dialog.name.setText("ZKP study")
        dialog.expected.setValue(45)
        dialog.topic_fields[0].setText("Commitments")
        dialog.accept()

    QTimer.singleShot(0, fill)
    window.edit_task(tid)
    task = window.service.tasks()[0]
    assert task["name"] == "ZKP study"
    assert task["expected_minutes"] == 45
    assert task["active_session_id"] == sid
    assert task["active_break_id"] is not None
    topic = window.service.topics(tid)[0]
    assert topic["id"] == original_topic["id"]
    assert topic["topic"] == "Commitments"


def test_delete_inside_edit_cancel_then_confirm(window, monkeypatch):
    tid = window.service.create_task("Future", start_date="2099-01-01")
    window.view_date.setDate(QDate(2099, 1, 1))
    assert not window.task_table.cellWidget(0, 5).findChildren(QAbstractButton)

    def inspect():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, TaskDialog)
        assert dialog.windowTitle() == "Edit Task"
        delete = dialog.findChild(QPushButton, "delete_task")
        assert not delete.autoDefault()
        assert not delete.isDefault()
        dialog.name.setText("Unsaved name")
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
        delete.click()
        assert window.service.tasks()
        assert dialog.name.text() == "Unsaved name"
        assert dialog.isVisible()
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
        delete.click()

    QTimer.singleShot(0, inspect)
    window.task_table.cellDoubleClicked.emit(0, 0)
    assert not window.service.tasks()
    with pytest.raises(ValueError, match="Task not found"):
        window.service._task(tid)
    assert window.task_table.rowCount() == 0


def test_no_manage_tasks_or_delete_button_on_create(window):
    assert not any(b.text() == "Manage tasks" for b in window.findChildren(QPushButton))
    dialog = TaskDialog()
    assert dialog.findChild(QPushButton, "delete_task") is None
    dialog.close()


def test_export_dialog_validates_range_and_supports_full_backup(qapp, monkeypatch):
    dialog = ExportDialog(QDate(2026, 10, 4))
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))
    assert dialog.values() == {"start_date": "2026-10-04", "end_date": "2026-10-04"}
    dialog.end.setDate(QDate(2026, 10, 3))
    dialog.accept()
    assert dialog.result() == QDialog.Rejected
    assert len(warnings) == 1
    dialog.all_dates.setChecked(True)
    assert not dialog.start.isEnabled()
    assert dialog.values() == {"start_date": None, "end_date": None}
    dialog.accept()
    assert dialog.result() == QDialog.Accepted


def test_export_ui_passes_date_range(window, tmp_path, monkeypatch):
    import json
    from PySide6.QtWidgets import QFileDialog

    window.service.create_task("Today")
    window.service.create_task("Tomorrow", start_date="2026-10-05", recurrence="none")
    output = tmp_path / "range.json"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", lambda *args: (str(output), "JSON")
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)

    def fill():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, ExportDialog)
        dialog.start.setDate(QDate(2026, 10, 5))
        dialog.end.setDate(QDate(2026, 10, 6))
        dialog.accept()

    QTimer.singleShot(0, fill)
    window.export_json()
    payload = json.loads(output.read_text())
    assert payload["export_metadata"]["date_range"] == {
        "from": "2026-10-05",
        "to": "2026-10-06",
    }
    assert {t["name"] for t in payload["tasks"]} == {"Today", "Tomorrow"}


def test_double_click_task_name_opens_edit_directly(window):
    from PySide6.QtTest import QTest
    from PySide6.QtCore import Qt

    window.service.create_task("Original", expected_minutes=30)
    window.refresh()
    window.show()
    QApplication.processEvents()
    rect = window.task_table.visualItemRect(window.task_table.item(0, 0))
    QTest.mouseClick(window.task_table.viewport(), Qt.LeftButton, pos=rect.center())
    assert QApplication.activeModalWidget() is None

    def edit():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, TaskDialog)
        assert dialog.windowTitle() == "Edit Task"
        dialog.name.setText("Edited directly")
        dialog.recurrence.setCurrentText("weekly")
        dialog.accept()

    QTimer.singleShot(0, edit)
    QTest.mouseDClick(window.task_table.viewport(), Qt.LeftButton, pos=rect.center())
    assert window.task_table.item(0, 0).text() == "Edited directly"
    assert window.service.tasks()[0]["recurrence"] == "weekly"


def test_topic_form_uses_editable_dates(qapp):
    dialog = TaskDialog(initial_date=QDate(2026, 10, 4))
    dialog.name.setText("Study")
    dialog.day_plan.setChecked(True)
    dialog.topic_fields[0].setText("First")
    dialog.add_day.click()
    dialog.topic_fields[1].setText("Second")
    assert [d.date() for d in dialog.topic_dates] == [
        QDate(2026, 10, 4),
        QDate(2026, 10, 5),
    ]
    dialog.topic_dates[1].setDate(QDate(2026, 10, 9))
    assert dialog.values()["topics"][1]["scheduled_date"] == "2026-10-09"
    assert dialog.save.isEnabled()
    dialog.close()


def test_history_custom_range_and_all_dates(window):
    window.service.add_journal(
        title="Past", content="Past note", timestamp="2026-10-03T09:00:00"
    )
    window.service.add_journal(
        title="Today", content="Today's note", timestamp="2026-10-04T09:00:00"
    )
    window.refresh_history()
    assert window.history_table.rowCount() == 1
    window.hist_start.setDate(QDate(2026, 10, 3))
    assert window.history_table.rowCount() == 2
    window.hist_end.setDate(QDate(2026, 10, 3))
    assert window.history_table.rowCount() == 1
    assert window.history_table.item(0, 2).text() == "Past"
    window.hist_all.setChecked(True)
    assert window.history_table.rowCount() == 2
    assert not window.hist_start.isEnabled()


def test_clear_history_ui_cancel_then_confirm_date_range(window, monkeypatch):
    from app.ui import ClearHistoryDialog

    window.service.add_journal(
        title="Past", content="Keep", timestamp="2026-10-03T09:00:00"
    )
    window.service.add_journal(
        title="Today", content="Delete", timestamp="2026-10-04T09:00:00"
    )

    def choose():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, ClearHistoryDialog)
        assert dialog.values() == {"start_date": "2026-10-04", "end_date": "2026-10-04"}
        dialog.accept()

    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
    QTimer.singleShot(0, choose)
    window.clear_history()
    assert len(window.service.journal()) == 2
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    QTimer.singleShot(0, choose)
    window.clear_history()
    assert [r["content"] for r in window.service.journal()] == ["Keep"]
    assert window.history_table.rowCount() == 0


def test_clear_all_history_keeps_running_task_and_clears_others(window, monkeypatch):
    from app.ui import ClearHistoryDialog

    tid = window.service.create_task("Running")
    sid = window.service.start_task(tid)
    other = window.service.create_task("Finished")
    window.service.complete_task(other, journal="Delete finished history")
    warnings = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))

    def choose_all():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, ClearHistoryDialog)
        dialog.all_dates.setChecked(True)
        assert dialog.values() == {"start_date": None, "end_date": None}
        dialog.accept()

    QTimer.singleShot(0, choose_all)
    window.clear_history()
    assert not warnings
    assert {r["task_id"] for r in window.service.journal()} == {tid}
    assert window.service.tasks()[0]["active_session_id"] == sid
    window.service.complete_task(tid, journal="Finished")
    QTimer.singleShot(0, choose_all)
    window.clear_history()
    assert not window.service.journal()
    assert window.task_table.rowCount() == 2


def test_one_hour_alarm_with_hidden_timers_keeps_running(window, monkeypatch):
    from datetime import datetime
    from app import timeutil
    from PySide6.QtCore import Qt

    tid = window.service.create_task("One hour", expected_minutes=60)
    sid = window.service.start_task(tid)
    monkeypatch.setattr(
        timeutil,
        "now",
        lambda: datetime.fromisoformat("2026-10-04T09:15:00").astimezone(),
    )
    window.service.begin_break(tid)
    monkeypatch.setattr(
        timeutil,
        "now",
        lambda: datetime.fromisoformat("2026-10-04T09:25:00").astimezone(),
    )
    window.service.resume_task(tid)
    window.refresh()
    assert [
        window.task_table.horizontalHeaderItem(i).text()
        for i in range(window.task_table.columnCount())
        if not window.task_table.isColumnHidden(i)
    ] == ["Task", "Topic", "Actions"]
    sounds = []
    monkeypatch.setattr(window.reminder_sound, "play", lambda: sounds.append(True))
    monkeypatch.setattr(
        timeutil,
        "now",
        lambda: datetime.fromisoformat("2026-10-04T09:59:59").astimezone(),
    )
    window.tick()
    assert not sounds
    monkeypatch.setattr(
        timeutil,
        "now",
        lambda: datetime.fromisoformat("2026-10-04T10:00:00").astimezone(),
    )
    window.tick()
    assert not sounds
    monkeypatch.setattr(
        timeutil,
        "now",
        lambda: datetime.fromisoformat("2026-10-04T10:10:00").astimezone(),
    )
    window.tick()
    assert sounds == [True]
    assert window.time_up_notice.isVisible()
    assert window.time_up_notice.windowModality() == Qt.NonModal
    assert "One hour" in window.time_up_notice.text()
    assert (
        window.service.timer_state(window.service.tasks_for_day()[0])["day_elapsed"]
        == 4200
    )
    assert window.service._task(tid)["active_session_id"] == sid
    assert window.service.tasks()[0]["active_break_id"] is None
    window.time_up_notice.accept()
    window.tick()
    assert sounds == [True]
    assert not window.time_up_notice.isVisible()


def test_deleted_tasks_are_not_available_in_a_recovery_list(window):
    assert not hasattr(window, "deleted_tasks")
    assert not any(
        b.text() == "Deleted tasks…" for b in window.findChildren(QPushButton)
    )


def test_alarm_falls_back_to_system_beep(window, monkeypatch):
    calls = []
    monkeypatch.setattr(QApplication, "beep", lambda: calls.append(True))
    window.reminder_sound.effect = None
    window.reminder_sound.play()
    assert calls == [True]


def test_topic_end_limit_add_remove_and_inline_validation(qapp):
    dialog = TaskDialog(initial_date=QDate(2026, 10, 5))
    dialog.name.setText("Study")
    dialog.end_enabled.setChecked(True)
    dialog.end.setDate(QDate(2026, 10, 7))
    dialog.day_plan.setChecked(True)
    dialog.topic_fields[0].setText("First")
    dialog.add_day.click()
    dialog.topic_fields[1].setText("Second")
    dialog.add_day.click()
    dialog.topic_fields[2].setText("Third")
    assert not dialog.add_day.isEnabled()
    dialog.add_day.click()
    assert len(dialog.topic_fields) == 3
    assert dialog.topic_dates[-1].maximumDate() == QDate(2026, 10, 7)
    dialog.topic_rows[-1].findChild(QPushButton).click()
    assert len(dialog.topic_fields) == 2
    assert dialog.add_day.isEnabled()
    assert dialog.save.isEnabled()
    dialog.end.setDate(QDate(2026, 10, 5))
    assert not dialog.save.isEnabled()
    assert "start/end dates" in dialog.validation.text()
    dialog.topic_rows[-1].findChild(QPushButton).click()
    assert dialog.save.isEnabled()
    dialog.close()


def test_topic_recurrence_not_forced_and_custom_date_range(qapp):
    dialog = TaskDialog(initial_date=QDate(2026, 10, 5))
    dialog.name.setText("Weekly")
    dialog.recurrence.setCurrentText("weekly")
    dialog.day_plan.setChecked(True)
    dialog.topic_fields[0].setText("First")
    assert dialog.recurrence.currentText() == "weekly"
    dialog.add_day.click()
    dialog.topic_fields[1].setText("Second")
    assert dialog.topic_dates[1].date() == QDate(2026, 10, 12)
    dialog.recurrence.setCurrentText("custom")
    assert dialog.end_enabled.isChecked()
    assert not hasattr(dialog, "interval")
    dialog.end.setDate(QDate(2026, 10, 7))
    assert not dialog.save.isEnabled()
    assert "start/end dates" in dialog.validation.text()
    dialog.topic_dates[1].setDate(QDate(2026, 10, 7))
    assert dialog.save.isEnabled()
    assert dialog.values()["recurrence"] == "date_range"
    assert dialog.values()["recurrence_interval"] == 1
    dialog.topic_dates[1].setDate(QDate(2026, 10, 5))
    assert "different date" in dialog.validation.text()
    assert not dialog.save.isEnabled()
    dialog.close()


def test_end_calendar_opens_in_current_month_and_blank_topic_tracks_new_start(window):
    dialog = TaskDialog()
    assert dialog.end.date() == QDate(2026, 10, 4)
    dialog.end_enabled.setChecked(True)
    calendar = dialog.end.calendarWidget()
    assert (calendar.yearShown(), calendar.monthShown()) == (2026, 10)
    dialog.start.setDate(QDate(2026, 11, 10))
    dialog.day_plan.setChecked(True)
    assert dialog.topic_dates[0].date() == QDate(2026, 11, 10)
    assert dialog.end.date() >= dialog.start.date()
    dialog.close()


def test_edit_remove_existing_topic_and_change_date_saves(window):
    tid = window.service.create_task("Study", topics=["First", "Second", "Extra"])
    ids = [t["id"] for t in window.service.topics(tid)]

    def fill():
        dialog = QApplication.activeModalWidget()
        assert dialog.start.isEnabled()
        assert dialog.recurrence.isEnabled()
        dialog.topic_rows[-1].findChild(QPushButton).click()
        dialog.topic_dates[1].setDate(QDate(2026, 10, 8))
        assert dialog.save.isEnabled()
        dialog.accept()

    QTimer.singleShot(0, fill)
    window.edit_task(tid)
    topics = window.service.topics(tid)
    assert [t["id"] for t in topics] == ids[:2]
    assert topics[1]["scheduled_date"] == "2026-10-08"


def test_end_date_selects_custom_range_and_round_trips(window):
    dialog = TaskDialog(initial_date=QDate(2026, 10, 5))
    dialog.name.setText("Range")
    dialog.recurrence.setCurrentText("weekly")
    dialog.end_enabled.setChecked(True)
    dialog.end.setDate(QDate(2026, 10, 7))
    assert dialog.recurrence.currentText() == "custom"
    tid = window.service.create_task(**dialog.values())
    for day in ("2026-10-05", "2026-10-06", "2026-10-07"):
        assert [t["id"] for t in window.service.tasks_for_day(day)] == [tid]
    assert window.service.tasks_for_day("2026-10-08") == []
    edit = TaskDialog(task=window.service._task(tid))
    assert edit.recurrence.currentText() == "custom"
    assert edit.end_enabled.isChecked()
    assert edit.values() == dialog.values()
    edit.end_enabled.setChecked(False)
    assert edit.recurrence.currentText() == "daily"
    assert edit.values()["end_date"] is None
    edit.recurrence.setCurrentText("custom")
    assert edit.end_enabled.isChecked()
    edit.recurrence.setCurrentText("monthly")
    assert not edit.end_enabled.isChecked()
    assert edit.values()["end_date"] is None
    dialog.close()
    edit.close()


def test_existing_interval_plan_is_preserved_when_editing_name(window):
    tid = window.service.create_task(
        "Existing",
        recurrence="custom",
        recurrence_interval=3,
        end_date="2026-10-12",
        topics=["One", "Two"],
    )
    dialog = TaskDialog(
        task=window.service._task(tid), topics=window.service.topics(tid)
    )
    assert dialog.recurrence.currentText() == "existing interval schedule"
    dialog.name.setText("Renamed")
    assert dialog.save.isEnabled()
    window.service.update_task(tid, **dialog.values())
    task = window.service._task(tid)
    assert (task["recurrence"], task["recurrence_interval"], task["end_date"]) == (
        "custom",
        3,
        "2026-10-12",
    )
    assert not window.service.tasks_for_day("2026-10-05")
    assert window.service.tasks_for_day("2026-10-07")[0]["name"] == "Renamed"
    dialog.close()


def test_overdue_oneoff_has_resume_and_complete_controls_today(window, monkeypatch):
    from datetime import datetime
    from app import timeutil

    tid = window.service.create_task("Overnight", recurrence="none", topics=["Topic"])
    window.service.start_task(tid)
    window.service.begin_break(tid)
    window.refresh()
    monkeypatch.setattr(
        timeutil,
        "now",
        lambda: datetime.fromisoformat("2026-10-05T00:10:00").astimezone(),
    )
    window.tick()
    assert window.view_date.date() == QDate(2026, 10, 5)
    buttons = {
        b.text(): b
        for b in window.task_table.cellWidget(0, 5).findChildren(QAbstractButton)
    }
    assert set(buttons) == {"Resume", "Complete"}
    buttons["Resume"].click()
    assert window.service.tasks()[0]["active_break_id"] is None

    def fill():
        dialog = QApplication.activeModalWidget()
        dialog.content.setPlainText("Finished late")
        dialog.accept()

    QTimer.singleShot(0, fill)
    window.complete(tid)
    assert window.service.topics(tid)[0]["completed_at"]
    assert window.service._task(tid)["active_session_id"] is None


def test_deadline_timer_rearms_after_break_without_hidden_column_queries(
    window, monkeypatch
):
    tid = window.service.create_task("Target", expected_minutes=10)
    window.start(tid)
    assert window.timer.isSingleShot()
    assert 600000 <= window.timer.interval() <= 601000
    window.begin_break(tid)
    assert window.timer.interval() > 600000  # Only the midnight deadline remains.
    window.resume(tid)
    assert 600000 <= window.timer.interval() <= 601000

    def unexpected(*args):
        raise AssertionError("Hidden columns must not calculate timers")

    monkeypatch.setattr(window.service, "timer_state", unexpected)
    window.refresh_elapsed()
    assert all(window.task_table.item(0, i).text() == "" for i in (2, 3, 4))


def test_idle_tick_does_not_rollover_write_or_rebuild_history(window, monkeypatch):
    def unexpected(*args):
        raise AssertionError("No idle rollover or hidden History rebuild")

    monkeypatch.setattr(window.service, "rollover", unexpected)
    monkeypatch.setattr(window, "refresh_history", unexpected)
    statements = []
    window.db.conn.set_trace_callback(statements.append)
    try:
        window.tick()
        window.refresh()
        assert not any(
            s.upper().startswith(("BEGIN", "UPDATE", "INSERT")) for s in statements
        )
    finally:
        window.db.conn.set_trace_callback(None)


def test_history_is_loaded_on_demand_and_cached(window, monkeypatch):
    window.service.add_journal("Note", "Read me")
    window.refresh()
    assert window.history_table.rowCount() == 0
    window.tabs.setCurrentIndex(1)
    assert window.history_table.rowCount() == 1

    def unexpected(*args):
        raise AssertionError("Unchanged History must not be queried again")

    monkeypatch.setattr(window.service, "journal", unexpected)
    window.refresh()
    window.refresh_history()


def test_export_defaults_to_documents(window, tmp_path, monkeypatch):
    from PySide6.QtCore import QStandardPaths
    from PySide6.QtWidgets import QFileDialog

    documents = tmp_path / "Documents"
    monkeypatch.setattr(
        QStandardPaths, "writableLocation", lambda location: str(documents)
    )
    suggested = []
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda parent, title, path, filters: (suggested.append(path) or "", ""),
    )
    QTimer.singleShot(0, lambda: QApplication.activeModalWidget().accept())
    window.export_json()
    assert suggested == [str(documents / "progress_2026-10-04_to_2026-10-04.json")]
