import json


def test_json_export_contains_underlying_records(service, tmp_path):
    tid = service.create_task("Export test")
    service.start_task(tid)
    service.complete_task(tid)
    p = tmp_path / "export.json"
    service.export_json(p)
    data = json.loads(p.read_text())
    assert data["tasks"][0]["id"] == tid
    assert len(data["activity_sessions"]) == 1
    assert len(data["journal_entries"]) >= 2
