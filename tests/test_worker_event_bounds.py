import json

from sanmou_assistant.models import Profile
from sanmou_assistant.worker import Worker


def test_worker_memory_event_summary_is_bounded_but_disk_history_is_complete(tmp_path):
    worker = Worker(Profile(), tmp_path, command="run", dry_run=False)
    for index in range(worker.EVENT_HISTORY_LIMIT + 100):
        worker.publish("status", f"事件 {index}")
    worker.publish("preview", object())

    assert len(worker.published) == worker.EVENT_HISTORY_LIMIT
    assert worker.published[0]["value"] == "事件 100"
    assert worker.published[-1]["value"] == f"事件 {worker.EVENT_HISTORY_LIMIT + 99}"
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == worker.EVENT_HISTORY_LIMIT + 100
    assert json.loads(lines[0])["value"] == "事件 0"
    assert all(record["event"] != "preview" for record in worker.published)
    worker.deleteLater()
