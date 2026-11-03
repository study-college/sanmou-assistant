from pathlib import Path
from types import SimpleNamespace
import os
import subprocess
import time

import numpy as np
import pytest

from sanmou_assistant.evidence import EvidenceLimitError, EvidenceStore, MAX_AGE, unresolved
from sanmou_assistant.game import Game, GameError
from sanmou_assistant.journal import Journal
from sanmou_assistant.models import Profile
from sanmou_assistant.worker import Worker


def test_routine_throttles_disk_only_and_keeps_every_preview(tmp_path):
    events = []
    game = Game(None, None, Profile(), None, tmp_path, lambda *a: events.append(a))
    clock = [time.time()]
    game.evidence_store = EvidenceStore(tmp_path, clock=lambda: clock[0])
    frame = np.zeros((40, 60, 3), dtype=np.uint8)
    for _ in range(4):
        game.save_image(frame, "main:1-home", True)
    assert sum(k == "evidence" for k, _ in events) == 1
    assert sum(k == "preview" for k, _ in events) == 4
    clock[0] += 600
    game.save_image(frame, "main:1-home", True)
    assert sum(k == "evidence" for k, _ in events) == 2


def test_budget_spans_sessions_and_never_touches_legacy_or_faults(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    legacy = sessions / "20261015-legacy.png"
    legacy.write_bytes(b"legacy")
    limits = {"routine": 8, "actions": 8, "faults": 8}
    first = EvidenceStore(sessions / "20261022-1", limits=limits)
    ordinary = first.write("main_1-home", b"123456")
    fault = first.write("unknown-page", b"fault")
    second = EvidenceStore(sessions / "20261022-2", limits=limits)
    fresh = second.write("main_2-home", b"abcdef")
    assert not ordinary.exists() and fresh.read_bytes() == b"abcdef"
    assert legacy.read_bytes() == b"legacy" and fault.read_bytes() == b"fault"


@pytest.mark.parametrize("pending_kind", ["order", "daily"])
def test_pending_in_another_scope_does_not_pin_screenshots(tmp_path, pending_kind):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    journal = Journal(sessions / "runtime.sqlite", "another-account")
    if pending_kind == "order":
        journal.prepare("main:1", "merit", "target")
    else:
        journal.prepare_daily("visit", {"before": 100})
    store = EvidenceStore(sessions / "new", lambda: unresolved(journal.db),
                          limits={"routine": 8, "actions": 8, "faults": 8})
    first = store.write("dispatch-preflight", b"123456")
    latest = store.write("dispatch-result", b"abcdef")
    assert not first.exists() and latest.read_bytes() == b"abcdef"
    assert unresolved(journal.db)
    journal.close()


def test_fault_rollover_and_oversize_frame_do_not_raise(tmp_path):
    store = EvidenceStore(tmp_path, limits={"faults": 8})
    first = store.write("unknown-page", b"123456")
    latest = store.write("unknown-page", b"abcdef")
    assert not first.exists() and latest.exists()
    assert store.write("unknown-page", b"a" * 9) is None
    assert latest.exists()


def test_capture_write_failure_keeps_preview_and_continues(tmp_path, monkeypatch):
    events = []
    game = Game(None, None, Profile(), None, tmp_path, lambda *a: events.append(a))
    def fail(*a):
        raise OSError("disk full")
    monkeypatch.setattr(EvidenceStore, "write", fail)
    game.save_image(np.zeros((40, 60, 3), dtype=np.uint8), "dispatch-result", True)
    assert any(k == "preview" for k, v in events)
    assert any(k == "log" and v["level"] == "WARN" for k, v in events)


def test_age_pruning_includes_faults(tmp_path):
    store = EvidenceStore(tmp_path / "sessions/20261022")
    ordinary = store.write("main_1-located", b"routine")
    fault = store.write("portrait-page-unreadable", b"fault")
    old = time.time() - MAX_AGE - 10
    for path in (ordinary, fault):
        os.utime(path, (old, old))
    store.prune("routine")
    store.prune("faults")
    assert not ordinary.exists() and not fault.exists()


def test_missing_journal_does_not_prevent_action_rollover(tmp_path):
    game = Game(None, None, Profile(), None, tmp_path, lambda *a: None)
    frame = np.zeros((40, 60, 3), dtype=np.uint8)
    game.save_image(frame, "dispatch-preflight")
    first = game.evidence_store.files("actions")[0][2]
    game.evidence_store.limits["actions"] = first.stat().st_size + 1
    game.save_image(frame, "dispatch-result")
    assert not first.exists()


def test_maintenance_with_missing_database_prunes_old_action_evidence(tmp_path, monkeypatch):
    from sanmou_assistant.evidence import main
    store = EvidenceStore(tmp_path / "sessions/20261022")
    action = store.write("dispatch-preflight", b"keep without database")
    old = time.time() - MAX_AGE - 10
    os.utime(action, (old, old))
    monkeypatch.setattr("sys.argv", ["evidence", "--data-dir", str(tmp_path), "--prune"])
    main()
    assert not action.exists()


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction boundary")
def test_pruning_refuses_nested_junction_without_following_it(tmp_path):
    store = EvidenceStore(tmp_path / "sessions/20261022", limits={"routine": 1, "actions": 1, "faults": 1})
    base = store.root / "routine"
    base.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.png"
    sentinel.write_bytes(b"do not delete")
    link = base / "foreign"
    done = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(outside)],
                          capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    assert done.returncode == 0, done.stderr
    try:
        with pytest.raises(EvidenceLimitError, match="联接"):
            store.prune("routine")
        assert sentinel.read_bytes() == b"do not delete"
    finally:
        os.rmdir(link)  # Junction only; no recursive deletion.


def test_unknown_page_still_saves_frame_and_stops_with_no_game_input(tmp_path):
    frame = np.zeros((40, 60, 3), dtype=np.uint8)
    taps = []
    device = SimpleNamespace(capture=lambda: frame, control=SimpleNamespace(sleep=lambda *_: None),
                             tap=lambda *a: taps.append(a))
    game = Game(device, SimpleNamespace(page=lambda _, verify=True: "unknown"), Profile(), None, tmp_path, lambda *a: None)
    with pytest.raises(GameError, match="未识别"):
        game.ensure_sandbox()
    assert not taps and list(tmp_path.rglob("*unknown-page.png"))
