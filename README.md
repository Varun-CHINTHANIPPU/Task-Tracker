# Tracker Application

A local-first desktop application for recurring tasks, progress and activity tracking,
task-linked journals, historical timelines, and JSON export.

Built with Python, PySide6, and SQLite; tested with pytest.

The database is stored at `$XDG_DATA_HOME/tracker-application/progress_tracker.db`,
or by default at `~/.local/share/tracker-application/progress_tracker.db`.
Updating the application does not replace your database.

## Install or update on Arch Linux

Build the current checkout, including independent elapsed tracking and pausable notification countdowns, permanent deletion, flexible topic dates and custom date ranges in version 0.11.0:

```bash
sudo pacman -S --needed base-devel git
git clone https://github.com/Verlopat/tracker-application.git
cd tracker-application
makepkg -si
```

For an existing clone, close the application and run inside its directory:

```bash
git pull --ff-only
makepkg -sif
```

Run `makepkg` as your normal user. It asks for sudo when installing dependencies
or the resulting package. Launch **Tracker Application** from the desktop menu,
or run `tracker-application`. Uninstall with `sudo pacman -R tracker-application`.

The package depends on `python`, `pyside6`, and `qt6-multimedia`. It installs the
application, launcher, desktop entry, icon, and license. The recipe uses the
checked-out source rather than downloading a possibly different `main` branch.
It stages that source in makepkg's `srcdir` and installs Python bytecode with
paths matching the installed location. Local source edits are included.
Use a complete checkout for this recipe; it is not a standalone AUR PKGBUILD.

The old `dist/` package was removed because it does not contain these fixes.
Fresh packages are available as artifacts of successful
[build-arch-package workflow runs](https://github.com/Verlopat/tracker-application/actions/workflows/arch-package.yml).
After downloading and unzipping an artifact, install from its directory:

```bash
zstd -t tracker-application-0.11.0-2-any.pkg.tar.zst
sudo pacman -U tracker-application-0.11.0-2-any.pkg.tar.zst
```

## Run from source

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

You can also run `python -m app` from the repository root.

## Dashboard and dates

The dashboard opens on **today**. Use **Previous day**, **Next day**, **Today**, or
the calendar date picker to view any date. Only tasks and topics assigned to the
chosen day appear. Future/past dates are planning/history views; start, break,
resume and completion operate on today. Unfinished one-off tasks from earlier
dates also appear today, including sessions or breaks that continue past midnight.
Late completion marks the original topic complete and records the actual completion
date; the task is no longer carried forward afterward. Creating a task while
viewing another date uses that date as its initial start date.

Completed tasks are crossed out at the bottom of the same list. There is no
Status column, status selector, or separate Completed tab. Drag rows to save
your preferred order; completed tasks stay below unfinished tasks. Hidden tasks
keep their positions when you reorder a filtered list.

The only tabs are **Dashboard** and **History**. Hover over the small information
icon next to the date/search controls to see the database location and the
local-storage note. There is no separate Settings, Journal, or Reports tab.

## Creating tasks and day topics

The task form keeps **name**, **recurrence**, **start date**, **end date**, and a
**target**. Description, project, tags, notes, and priority inputs are removed;
older stored values remain in the database and full export.

Targets:

- **Expected minutes:** a work-duration countdown that pauses during breaks. Leaving it at
  “Not specified” gives elapsed tracking without a deadline.
- **Start / end time:** a clock-time window such as 09:00–10:00 on the scheduled
  day. End time must be later than start time on that same day.
- **Quantity:** a number and unit, such as 3 problems. You decide when it is done.
- **No target:** elapsed tracking and manual completion.

The optional **Set end date** control opens a calendar near the current date;
selecting it automatically chooses **custom**. Custom means every date from
**Start date** through **End date**, including both dates. It has no interval input.
Choosing **custom** directly enables the end date. Unchecking the end date returns
to **daily**; choosing daily, weekly, monthly or none removes the custom range.
Leave the end date unchecked for an open-ended schedule.

Check **Add dated topics** to add topic rows. Each row has its own date picker,
text field and **Remove** button. **+ Add topic** chooses the next unused scheduled
date and becomes unavailable when the end date leaves no further dates. Selecting
topics does not change the recurrence. Weekly/monthly plans work too. Existing
interval plans from older versions retain their dates and appear as **existing
interval schedule** when edited; new custom tasks use a simple date range.

Topic dates must fit the selected recurrence and task dates, with no duplicate
dates. Invalid rows show an inline explanation and disable saving. If an end date
is shortened past an existing topic, move or remove that topic before saving.
Changing a topic date never silently shifts another topic. Existing topic text,
dates and rows can be edited or removed. Removing a row preserves task journals;
moving a completed topic clears its topic completion mark at the new date.

A task appears on every date in its recurrence through its end date, even when
no topic is assigned. For example, a daily task from 5–7 October with topics only
on 5 and 6 October still appears on 7 October with an empty Topic cell.

The dashboard shows the selected date's topic directly as text, without a
separate checkbox or numbered day label. Completing the task completes its topic
and strikes through both. Double-click the task name to change or append
topics. Target values remain saved in the edit form and JSON exports; the dashboard
omits the Target, Timer, Elapsed and Total time columns. Timing continues in the
background, including during breaks. Only the estimated countdown for the
notification pauses during breaks and resumes with **Resume**. Session durations
and exact break intervals remain in JSON exports.

## Timing, breaks, and completion

- **Start** starts elapsed tracking. While running, use **Break** or **Complete**.
- **Break** records the beginning of an interval; its arrow menu also offers
  **Distracted**. Both pause only the estimated countdown used by the buzzer.
  **Resume** closes the interval and continues that countdown from where it paused.
  The work timer, elapsed time and total time keep counting throughout breaks
  and distractions, including after reopening the app.
  Clock-time targets also move later by the break time after their scheduled start.
- Reaching a duration or clock-time target plays a short two-tone chime and
  displays a non-blocking notification window plus a dashboard message. A desktop
  notification is also used when a system tray is available. A system beep is
  used if audio playback is unavailable. The reminder never stops or completes your task. Reminders fire once
  per task occurrence and remain suppressed after reopening. A new recurring
  occurrence gets a new reminder; a one-off does not reset at midnight. Both
  duration and clock-time reminders require a running task.
  For a one-hour target, enter **60 expected minutes** and click **Start**.
  A ten-minute break means the reminder arrives after 70 minutes of wall time,
  when you have actually worked for 60 minutes.
  The application must be running to deliver notifications; audio follows your
  system volume/mute settings. The Arch package installs Qt Multimedia for sound.
- **Complete** opens the journal for that task. **Save journal and complete**
  saves the journal, completion, any open break, and session end atomically.
  **Cancel** leaves the task and timer unchanged. Quantity targets also require
  this manual completion step.
- Closing the window does not stop an active session or break. Reopening
  restores them. The existing recurring-day rollover rule still closes an
  overdue recurring session (including its break) at local midnight and marks
  an unfinished occurrence missed. One-off sessions continue until ended and
  remain actionable on today's dashboard. Completed occurrences cannot restart;
  the next recurring occurrence can start normally.
- Daily, weekly, monthly, custom date ranges and one-off schedules are supported. Monthly schedules
  preserve the original day: January 31 → February 28/29 → March 31. End dates
  are inclusive. Every scheduled date counts whether or not a topic is assigned.

## Edit or delete a task

**Double-click a task name** on the dashboard to open its edit form directly.
A single click selects the row for dragging. To edit future or past tasks,
choose their date on the dashboard. The small **Delete** button is inside the
edit form, beside Save changes and Cancel.

Edit the name, recurrence, dates, target or topic rows and save your changes.
Edits apply to the task's recurring schedule. Existing journals are preserved.
While a timer is running, complete the task before changing its schedule; its
name, target and topic text can still be edited.

**Delete** asks for confirmation, then permanently removes only that task and all
of its journals, sessions, breaks, topics and reminder records. There is no
Deleted tasks list or restore action. Other tasks and their records are untouched.
Deleted tasks are absent from both date-range exports and full exports.

**Upgrade note:** versions before 0.8.0 used `archived: 1` for deleted tasks.
During the run-once migration from an unversioned database, those previously deleted tasks and their related
records are permanently removed. Tasks that were not deleted are preserved.

## History and clearing records

The History tab supports inclusive **From / To** dates or **All dates**.
Choose **Clear History…** to select a custom date range or **All history**, then
confirm permanent deletion. Canceling either dialog leaves records unchanged.

Clearing removes journals, recorded sessions (selected by their start date), all
breaks belonging to those sessions, and reminder records in the selected period.
It resets completion marks for those dates and updates elapsed totals. Task
definitions, recurrence, targets and planned topic text remain saved. Future task activity is still recorded normally.
Cleared data is also absent from subsequent JSON exports.

Running tasks are skipped automatically, including tasks currently on a break.
Their timers, breaks, journals, previous sessions, topic completion and reminder
records stay unchanged, so elapsed totals continue without resetting. Records
for the other tasks in the selected period are cleared. There is no need to
stop a running task first. Export first if you want a backup; clearing
cannot be undone.

## JSON export

Choose **Export** on the dashboard, select inclusive **From / To** dates,
then choose where to save the file. Both dates initially match the dashboard's
selected date. Choose **All dates (full backup)** to export everything instead.
The dashboard search does not restrict exports.

Date-range exports include journals by event date, topics by scheduled date,
and reminders by event date. Sessions are selected by their local **start date**
and include all associated breaks, even if a session continues past midnight.
Durations remain the complete recorded duration; they are not clipped to the
range. Related task definitions use their current settings. Deleted tasks are
absent because their records have been permanently removed. Range metadata is
saved in `export_metadata.date_range`; it is null for a full export.

Schema `1.3` keeps the top-level `projects`, `tasks`, `journal_entries`, and
`activity_sessions` collections and adds `activity_breaks`, `task_topics`, and
`task_reminders`. Each task also contains its own `journal_entries`, `breaks`,
and `topics`; each session contains its `breaks`. Nested arrays are views of the
same records, not additional database entries.

Each break includes `task_id`, `session_id`, `reason` (`break` or `distraction`),
`start_timestamp`, `end_timestamp`, `duration_seconds`, and `end_reason` (for
example, `resumed` or `completed`). An ongoing break has a null end and duration,
so the export does not invent an end time. Session `duration_seconds` retains
elapsed time, including breaks; `work_duration_seconds` records active time
excluding breaks and distractions for the estimated countdown. Both session durations are null until the session ends. The export
metadata explains this distinction. Recorded breaks are subtracted only for
the notification countdown; elapsed totals and original session records stay intact.
Day topics include their number, scheduled date, topic, and completion timestamp.
Target settings, reminder delivery timestamps, manual order, and dated journals
are retained too. New custom ranges export as `recurrence: "date_range"`.
The `recurrence_interval` field is retained for older interval plans; it is 1 for
new tasks. Existing non-deleted task data is preserved.

## Multimedia startup messages

`Using Qt multimedia with FFmpeg version ...` identifies the audio backend and
is informational. A missing `libvdpau_nvidia.so` message comes from probing an
optional NVIDIA video-acceleration backend, which the notification chime does not
need. The app now disables FFmpeg video hardware probing by default before
initializing sound, while retaining the same chime and system-beep fallback.
Explicit `QT_FFMPEG_DECODING_HW_DEVICE_TYPES` or
`QT_FFMPEG_ENCODING_HW_DEVICE_TYPES` environment overrides are respected.

## Internal reliability and performance

- New and edited recurring schedules align to the first current/future occurrence.
  Creating a task with a past start date does not invent missed history. Extending
  an expired end date does not backfill dates outside its previous schedule.
- Database migrations use `PRAGMA user_version` and atomic, ordered steps. The
  legacy deletion conversion runs once. The archive API has been removed. Old
  `status` and `archived` columns remain for file compatibility; task status is
  derived from sessions and journals, and legacy status values are never updated
  or used to decide the current status.
- A single-shot timer schedules the next reminder or midnight boundary. Hidden
  timer columns are not calculated; dashboard reads batch topics and completion
  events. A lightweight 30-second check detects clock/suspend changes and external
  database writes without taking a write lock. Returning to the window also checks
  deadlines. History loads on demand and is reused while its data/range is unchanged.
- JSON exports default to the user's Documents directory. They write and flush a
  temporary file beside the destination, then atomically replace it. A failed write
  leaves the previous export intact.
- `summary()["seconds"]` means elapsed time including breaks, matching the work
  timer. The legacy PDF API uses that same label. PDF generation is optional:
  install `reportlab` separately if calling `app.reports.build_report` from a script.
  There is no PDF/report tab and ReportLab is not required to launch the tracker.

## Tests

```bash
QT_QPA_PLATFORM=offscreen python -m pytest -q
```

The suite covers calendar dates, targets and reminders, day topics, recurrence,
end dates, breaks, rollover, transaction rollback, existing database upgrades,
JSON/PDF export, desktop interactions, and package staging.
GitHub Actions runs the suite on Python 3.12–3.14 and also builds, installs, and
smoke-tests the Arch package on pushes to `main`, version tags, pull requests,
and manual package workflow runs.
