from pathlib import Path
from xml.sax.saxutils import escape
from .timeutil import fmt_seconds


def build_report(service, start, end, path):
    if start > end:
        raise ValueError("Report start date cannot be after end date")
    # Optional legacy PDF API; the dashboard never imports ReportLab.
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate,
        Paragraph,
        Spacer,
        Table,
        TableStyle,
    )

    summary = service.summary(start, end)
    rows = service.journal(start, end)
    c = service.db.conn
    tasks = c.execute(
        """
        SELECT t.name, p.name project_name,
               SUM(CASE WHEN j.entry_type='Completed' THEN 1 ELSE 0 END) completed,
               SUM(CASE WHEN j.entry_type='Missed' THEN 1 ELSE 0 END) missed,
               COALESCE((SELECT SUM(s.duration_seconds) FROM activity_sessions s
                         WHERE s.task_id=t.id AND s.date BETWEEN ? AND ?),0) seconds
        FROM tasks t LEFT JOIN projects p ON p.id=t.project_id
        LEFT JOIN journal_entries j ON j.task_id=t.id AND j.event_date BETWEEN ? AND ?
        GROUP BY t.id ORDER BY seconds DESC, t.name
    """,
        (start, end, start, end),
    ).fetchall()

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
    )
    styles = getSampleStyleSheet()
    story = []
    story += [
        Paragraph("Progress Tracker Report", styles["Title"]),
        Paragraph(f"{start} to {end}", styles["Heading2"]),
        Spacer(1, 8),
    ]
    overview = [
        ["Metric", "Value"],
        ["Completed", str(summary["completed"])],
        ["Missed", str(summary["missed"])],
        ["Activity sessions", str(summary["sessions"])],
        ["Elapsed time (including breaks)", fmt_seconds(summary["seconds"])],
        ["Journal notes", str(summary["notes"])],
    ]
    t = Table(overview, colWidths=[75 * mm, 75 * mm])
    t.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("PADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story += [
        t,
        Spacer(1, 12),
        Paragraph("Task / project activity", styles["Heading2"]),
    ]
    data = [
        ["Task", "Project", "Completed", "Missed", "Elapsed time (including breaks)"]
    ]
    for r in tasks:
        data.append(
            [
                Paragraph(escape(r["name"]), styles["BodyText"]),
                Paragraph(escape(r["project_name"] or ""), styles["BodyText"]),
                str(r["completed"] or 0),
                str(r["missed"] or 0),
                fmt_seconds(r["seconds"]),
            ]
        )
    t = Table(
        data,
        colWidths=[50 * mm, 35 * mm, 22 * mm, 18 * mm, 35 * mm],
        repeatRows=1,
        splitInRow=1,
    )
    t.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story += [t, Spacer(1, 12), Paragraph("Chronological journal", styles["Heading2"])]
    for r in reversed(rows):
        label = f"{r['timestamp']} — {r['entry_type']} — {r['title']}"
        story.append(Paragraph(escape(label), styles["Heading4"]))
        if r["content"]:
            story.append(
                Paragraph(
                    escape(r["content"]).replace("\n", "<br/>"), styles["BodyText"]
                )
            )
    doc.build(story)
