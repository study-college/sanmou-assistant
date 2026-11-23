"""Tests for local ControlServer HTTP API, ControlClient, ctl CLI, and diagnostics privacy."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from sanmou_assistant.accounts import AccountStore
from sanmou_assistant.control_server import (
    ControlClient,
    ControlServer,
    control_file_path,
    run_ctl,
)
from sanmou_assistant.controller import HeadlessHubBackend, RuntimeHub
from sanmou_assistant.diagnostics import export_diagnostics
from sanmou_assistant.models import Point, Profile
from sanmou_assistant.target_ops import merge_live_target_fields


class _FakeWorker(QObject):
    """Lightweight fake Worker matching Worker's public thread/signal surface."""

    event = Signal(str, object)
    finished = Signal()

    def __init__(self, profile: Profile, folder: Path, command="run", dry_run=False, max_orders=0, duration=0):
        super().__init__()
        self.profile = profile
        self.folder = folder
        self.command = command
        self.dry_run = dry_run
        self.max_orders = max_orders
        self.duration = duration
        self._running = False
        self.paused_flag = False
        self.woken_count = 0
        self.refresh_count = 0
        self.queued_updates = []

    def isRunning(self) -> bool:
        return self._running

    def start(self) -> None:
        self._running = True
        self.event.emit("status", "运行中")
        self.event.emit("connection", {"instance": self.profile.instance, "serial": "127.0.0.1:16384", "resolution": "1920×1080"})
        self.event.emit("grain", 1250000)
        self.event.emit("observation", {"key": "main:1", "state": "home", "strength": 30000, "maximum": 30000, "marker_state": "idle"})

    def stop(self) -> None:
        if self._running:
            self._running = False
            self.event.emit("status", "已停止新动作；游戏中的行军/征兵继续")
            self.finished.emit()

    def wait(self, _ms: int = 0) -> bool:
        return True

    def pause(self, value: bool) -> None:
        self.paused_flag = bool(value)
        if not value:
            self.wake()

    def wake(self) -> None:
        self.woken_count += 1

    def request_refresh(self) -> None:
        self.refresh_count += 1

    def queue_target_update(self, profile: Profile, changed=None, source="ui"):
        merged, detected = merge_live_target_fields(self.profile, profile, active=(self.command == "run" and not self.dry_run))
        self.profile = merged
        self.queued_updates.append((merged, set(changed or detected), source))
        self.wake()
        self.event.emit("log", {"time": "09:30:00", "level": "INFO", "message": f"【热更新/{source}】测试生效"})
        return merged, set(changed or detected)


def _setup_store(tmp_path: Path) -> tuple[AccountStore, str]:
    store = AccountStore(tmp_path)
    aid = store.selected
    p = store.get(aid).load_profile()
    p.account = "测试角色"
    p.main_city = Point(name="测试主城", x=100, y=100, expected_name="测试角色")
    p.teams[0].role = "line"
    p.teams[1].role = "merit"
    p.line_targets = [Point(name="初始拉线", x=200, y=200, actions=["occupy", "march"], priority=1, enabled=True)]
    p.targets = [Point(name="初始武勋", x=250, y=250, actions=["attack", "occupy", "guard"], priority=1, enabled=True)]
    p.normalize()
    store.save_profile(aid, p)
    return store, aid


def test_control_server_auth_and_full_lifecycle(tmp_path):
    app = QApplication.instance() or QApplication([])
    _store, aid = _setup_store(tmp_path)
    backend = HeadlessHubBackend(tmp_path, worker_factory=_FakeWorker)
    hub = RuntimeHub(backend, mode="headless", parent=app)
    server = ControlServer(hub, tmp_path, mode="headless")
    info = server.start()

    try:
        assert control_file_path(tmp_path).is_file()
        assert info["host"] == "127.0.0.1"
        assert info["port"] > 0

        # 未携带 Token 的受保护接口必须返回 401
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/v1/accounts", method="GET")
        try:
            opener.open(req, timeout=2)
            assert False, "expected 401"
        except urllib.error.HTTPError as exc:
            assert exc.code == 401

        # 诊断包（含完整模式）绝不能泄漏 control-server.json 里的本机 Token
        zip_path = tmp_path / "diag.zip"
        export_diagnostics(tmp_path, zip_path, include_private=True)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            assert not any("control-server" in n for n in names)
            for name in names:
                assert info["token"].encode("utf-8") not in zf.read(name)

        client = ControlClient(tmp_path)
        assert client.discover() is not None

        # 启动（在后台线程发请求，同时主线程处理 Qt 信号事件）
        def call_in_thread(fn):
            box = []
            done = threading.Event()

            def runner():
                try:
                    box.append((True, fn()))
                except BaseException as exc:
                    box.append((False, exc))
                finally:
                    done.set()

            t = threading.Thread(target=runner, daemon=True)
            t.start()
            deadline = time.monotonic() + 5
            while not done.is_set() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            t.join(timeout=1)
            assert done.is_set(), "HTTP call timed out waiting for Qt main thread"
            ok, val = box[0]
            if not ok:
                raise val
            return val

        started = call_in_thread(lambda: client.start(aid, command="run", execute=True))
        assert started["started"] is True

        st = call_in_thread(lambda: client.status(aid))
        assert st["running"] is True
        assert st["grain"] == 1250000
        assert st["teams"][0]["observation"]["strength"] == 30000

        # 运行中热切换拉线目标
        switched = call_in_thread(lambda: client.switch_target(
            account_id=aid, mode="line", address="新焦点(410,520)", actions="驻守,攻城,行军"))
        assert switched["live_applied"] is True
        assert switched["targets"]["line"]["primary"]["x"] == 410
        worker = backend.get_worker(aid)
        assert worker is not None and len(worker.queued_updates) == 1
        assert worker.woken_count >= 1
        # 磁盘 profile.json 同步更新
        persisted = AccountStore(tmp_path).get(aid).load_profile()
        assert persisted.line_targets[0].x == 410

        # 热更新拉线征兵点
        backup_res = call_in_thread(lambda: client.set_backup(
            account_id=aid, mode="line", team_key="main:1", address="前线营帐(420,530)"))
        assert backup_res["live_applied"] is True
        assert backup_res["targets"]["line"]["backups"]["main:1"]["x"] == 420

        # 暂停 / 恢复 / 唤醒 / 刷新 / 日志 / 停止
        assert call_in_thread(lambda: client.pause(aid))["paused"] is True
        assert worker.paused_flag is True
        assert call_in_thread(lambda: client.resume(aid))["paused"] is False
        assert worker.paused_flag is False
        assert call_in_thread(lambda: client.wake(aid))["woken"] is True
        assert call_in_thread(lambda: client.refresh(aid))["refresh_requested"] is True
        assert worker.refresh_count == 1
        logs_res = call_in_thread(lambda: client.logs(aid, limit=20))
        assert any("热更新" in item["message"] for item in logs_res["logs"])
        stop_res = call_in_thread(lambda: client.stop(aid))
        assert stop_res["stop_requested"] is True
    finally:
        server.stop()

    assert not control_file_path(tmp_path).exists()


def test_ctl_cli_offline_fallback_and_error_reporting(tmp_path, capsys):
    _store, aid = _setup_store(tmp_path)

    # 离线列账号（支持 --data-dir 在子命令前或后）
    rc = run_ctl(["--data-dir", str(tmp_path), "accounts"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["data"]["source"] == "offline"
    assert out["data"]["accounts"][0]["account_id"] == aid

    # 离线热换命令自动降级写盘
    rc = run_ctl([
        "switch-target", "--data-dir", str(tmp_path), "--account-id", aid,
        "--mode", "line", "--address", "离线新目标(512,612)", "--actions", "guard,attack,march",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["data"]["live_applied"] is False
    assert out["data"]["source"] == "offline"
    assert AccountStore(tmp_path).get(aid).load_profile().line_targets[0].x == 512

    # 离线设置备用征兵点
    rc = run_ctl([
        "set-backup", "--data-dir", str(tmp_path), "--mode", "merit",
        "--address", "离线营帐(333,444)", "--enable",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    reloaded = AccountStore(tmp_path).get(aid).load_profile()
    assert reloaded.backup_enabled is True and reloaded.merit_backup.x == 333

    # 离线调用 status 返回清晰的结构化错误码
    rc = run_ctl(["--data-dir", str(tmp_path), "status"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["code"] == "server_not_running"
