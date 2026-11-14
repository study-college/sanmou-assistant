import json
import sqlite3
from types import SimpleNamespace

from PySide6.QtWidgets import QApplication

from sanmou_assistant.accounts import AccountStore
from sanmou_assistant.journal import Journal
from sanmou_assistant.models import Point, Profile
from sanmou_assistant.ui import AccountManagerWindow


def test_legacy_profile_and_journal_are_preserved_and_copied(tmp_path):
    profile = Profile(name="原账号", account="原主公", main_city=Point(x=607, y=291), poll_seconds=51)
    legacy_profile = tmp_path / "active-profile.json"
    legacy_profile.write_text(json.dumps(profile.to_dict(), ensure_ascii=False), encoding="utf-8")
    (tmp_path / "sessions").mkdir()
    journal = Journal(tmp_path / "sessions" / "runtime.sqlite", "legacy-scope")
    intent = journal.prepare("main:1", "merit", "target")
    journal.close()
    original_profile = legacy_profile.read_bytes()
    original_database = (tmp_path / "sessions" / "runtime.sqlite").read_bytes()

    store = AccountStore(tmp_path)
    account = store.accounts[0]
    loaded = account.load_profile()
    assert loaded.account == "原主公" and loaded.poll_seconds == 51
    assert legacy_profile.read_bytes() == original_profile
    assert (tmp_path / "sessions" / "runtime.sqlite").read_bytes() == original_database
    with sqlite3.connect((account.sessions_path / "runtime.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT id,scope,status FROM intents").fetchone()
    assert row == (intent, "legacy-scope", "prepared")

    store.select(account.id)
    reopened = AccountStore(tmp_path)
    assert reopened.selected == account.id
    assert reopened.accounts[0].load_profile().to_dict() == profile.to_dict()


def test_new_account_copies_strategy_but_not_identity_and_saves_independently(tmp_path):
    original = Profile(name="原账号", account="原主公", main_city=Point(x=607, y=291),
                       targets=[Point(name="战场", x=636, y=286)], poll_seconds=51)
    (tmp_path / "active-profile.json").write_text(json.dumps(original.to_dict()), encoding="utf-8")
    store = AccountStore(tmp_path)
    first = store.accounts[0]
    second = store.create_account("新账号", "1", original)

    loaded = second.load_profile()
    assert loaded.account == "" and loaded.main_city is None
    assert loaded.instance == "1" and loaded.targets[0].x == 636
    assert loaded.poll_seconds == 51
    loaded.poll_seconds = 73
    store.save_profile(second.id, loaded)
    assert first.load_profile().poll_seconds == 51
    assert second.load_profile().poll_seconds == 73
    assert AccountStore(tmp_path).selected == second.id


class FakeWorker:
    def __init__(self, profile, folder, command, dry_run):
        self.profile = profile
        self.folder = folder
        self.command = command
        self.dry_run = dry_run
        self.running = False
        self.callbacks = []
        self.event = SimpleNamespace(connect=lambda callback: None)
        self.finished = SimpleNamespace(connect=self.callbacks.append)

    def start(self):
        self.running = True

    def isRunning(self):
        return self.running

    def stop(self):
        self.running = False
        for callback in list(self.callbacks):
            callback()

    def request_refresh(self):
        pass


def manager_with_two_accounts(tmp_path):
    first = Profile(name="账号一", account="主公一", main_city=Point(x=100, y=200), instance="0")
    (tmp_path / "active-profile.json").write_text(json.dumps(first.to_dict()), encoding="utf-8")
    store = AccountStore(tmp_path)
    store.create_account("账号二", "1", first)
    return AccountManagerWindow(tmp_path)


def test_account_switch_keeps_independent_workers_settings_and_events(tmp_path, monkeypatch):
    import sanmou_assistant.ui as module

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(module, "Worker", FakeWorker)
    manager = manager_with_two_accounts(tmp_path)
    first, second = manager.windows
    first.poll.setValue(51)
    assert first.save()
    second.poll.setValue(73)
    assert second.save()
    assert first.config_path != second.config_path
    assert json.loads(first.config_path.read_text(encoding="utf-8"))["poll_seconds"] == 51
    assert json.loads(second.config_path.read_text(encoding="utf-8"))["poll_seconds"] == 73

    first.start("preview")
    first_worker = first.worker
    manager.account_combo.setCurrentIndex(1)
    second.start("preview")
    second_worker = second.worker
    assert first_worker.isRunning() and second_worker.isRunning()
    assert manager.account_combo.itemText(0).endswith("· 运行中")
    assert manager.account_combo.itemText(1).endswith("· 运行中")
    assert first_worker.folder.parent.parent == first.account.root
    assert second_worker.folder.parent.parent == second.account.root
    assert manager.current_window is second and first_worker is first.worker

    second.handle_event("status", "仅账号二的事件")
    assert any("仅账号二" in line for line in second.log_lines)
    assert not any("仅账号二" in line for line in first.log_lines)
    manager.account_combo.setCurrentIndex(0)
    assert first.worker is first_worker and second.worker is second_worker
    assert first_worker.isRunning() and second_worker.isRunning()
    first.stop()
    second.stop()
    manager.close()
    app.processEvents()


def test_same_running_instance_cannot_be_claimed_by_another_account(tmp_path, monkeypatch):
    import sanmou_assistant.ui as module

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(module, "Worker", FakeWorker)
    manager = manager_with_two_accounts(tmp_path)
    first, second = manager.windows
    first.start("preview")
    second.instance.clear()
    second.instance.addItem("同一实例", "0")
    second.start("preview")
    assert first.worker.isRunning()
    assert second.worker is None
    assert "正由账号" in second.status.text()
    first.stop()
    manager.close()
    app.processEvents()


def test_manager_keeps_account_switch_and_save_visible_on_small_desktop(tmp_path, monkeypatch):
    from PySide6.QtCore import QPoint, QRect
    from sanmou_assistant.ui import Window

    app = QApplication.instance() or QApplication([])
    available = SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1024, 640))
    monkeypatch.setattr(AccountManagerWindow, "screen", lambda self: available)
    monkeypatch.setattr(Window, "screen", lambda self: available)
    manager = manager_with_two_accounts(tmp_path)
    manager.show()
    app.processEvents()
    assert manager.width() <= 1024 and manager.height() <= 640
    for index, window in enumerate(manager.windows):
        manager.account_combo.setCurrentIndex(index)
        app.processEvents()
        assert manager.account_combo.isVisible()
        assert window.save_button.mapTo(manager, QPoint(0, window.save_button.height())).y() <= manager.height()
    manager.close()
    app.processEvents()


def test_diagnostics_include_each_account_profile_session_and_journal(tmp_path):
    import zipfile
    from sanmou_assistant.diagnostics import export_diagnostics

    store = AccountStore(tmp_path)
    second = store.create_account("账号二", "1", store.accounts[0].load_profile())
    folder = second.sessions_path / "20261031-010000"
    folder.mkdir(parents=True)
    (folder / "events.jsonl").write_text(json.dumps({"event": "error", "value": "账号二故障"}), encoding="utf-8")
    Journal(second.sessions_path / "runtime.sqlite", "second-scope").close()
    output = tmp_path / "diagnostics.zip"
    report = export_diagnostics(tmp_path, output, include_private=True)
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert f"accounts/{second.id}/profile.json" in names
        assert f"accounts/{second.id}/sessions/20261031-010000/events.jsonl" in names
        assert f"accounts/{second.id}/journal.json" in names
        assert "账号二故障" in archive.read("report.json").decode("utf-8")
    assert report["account_sessions"][f"accounts/{second.id}/sessions"] == ["20261031-010000"]

    safe_output = tmp_path / "diagnostics-safe.zip"
    safe_report = export_diagnostics(tmp_path, safe_output)
    with zipfile.ZipFile(safe_output) as archive:
        names = archive.namelist()
        assert f"accounts/{second.id}/profile.json" not in names
        assert f"accounts/{second.id}/sessions/20261031-010000/events.jsonl" not in names
        assert f"accounts/{second.id}/journal.json" not in names
        payload = b"".join(archive.read(name) for name in names)
        assert "账号二故障".encode("utf-8") not in payload
        summary = json.loads(archive.read("report.json").decode("utf-8"))
        assert summary["mode"] == "safe"
        assert summary["event_counts"]["error"] == 1
        assert summary["files"][f"accounts/{second.id}/profile.json"] > 0
        assert summary["files"][f"accounts/{second.id}/sessions/runtime.sqlite"] > 0
        assert summary["attention"][-1]["event"] == "error"
    assert safe_report["account_sessions"][f"accounts/{second.id}/sessions"] == ["20261031-010000"]


def test_cli_profile_location_uses_selected_or_explicit_account(tmp_path):
    from sanmou_assistant.__main__ import profile_location

    store = AccountStore(tmp_path)
    first = store.accounts[0]
    second = store.create_account("账号二", "1", first.load_profile())
    store.select(first.id)
    path, sessions = profile_location(tmp_path)
    assert path == first.profile_path and sessions == first.sessions_path
    path, sessions = profile_location(tmp_path, account_id=second.id)
    assert path == second.profile_path and sessions == second.sessions_path
    explicit = tmp_path / "custom.json"
    path, sessions = profile_location(tmp_path, explicit=explicit)
    assert path == explicit and sessions == tmp_path / "sessions"
