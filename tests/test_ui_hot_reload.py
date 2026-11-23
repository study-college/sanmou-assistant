"""Offscreen UI tests for live target switching, partial locking, and GUI<->Agent sync."""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from sanmou_assistant.accounts import AccountStore
from sanmou_assistant.control_server import ControlClient
from sanmou_assistant.models import Point
from sanmou_assistant.target_ops import merge_live_target_fields
from sanmou_assistant.ui import AccountManagerWindow, QuickTargetDialog


class _StubRunWorker(QObject):
    event = Signal(str, object)
    finished = Signal()

    def __init__(self, profile, folder, command="run", dry_run=False, max_orders=0, duration=0):
        super().__init__()
        self.profile = profile
        self.folder = folder
        self.command = command
        self.dry_run = dry_run
        self._running = False
        self.paused = False
        self.updates = []

    def isRunning(self) -> bool:
        return self._running

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        if self._running:
            self._running = False
            self.finished.emit()

    def pause(self, value: bool) -> None:
        self.paused = bool(value)

    def wake(self) -> None:
        pass

    def queue_target_update(self, profile, changed=None, source="ui"):
        merged, detected = merge_live_target_fields(self.profile, profile, active=(self.command == "run" and not self.dry_run))
        self.profile = merged
        self.updates.append((merged, set(changed or detected), source))
        return merged, set(changed or detected)


def _prepare_account(tmp_path):
    store = AccountStore(tmp_path)
    aid = store.selected
    p = store.get(aid).load_profile()
    p.account = "桌面角色"
    p.main_city = Point(name="桌面主城", x=100, y=100, expected_name="桌面角色")
    p.teams[0].role = "line"
    p.teams[1].role = "merit"
    p.line_targets = [Point(name="旧拉线", x=200, y=200, actions=["occupy", "march"], priority=1, enabled=True)]
    p.targets = [Point(name="旧武勋", x=250, y=250, actions=["attack", "occupy", "guard"], priority=1, enabled=True)]
    p.normalize()
    store.save_profile(aid, p)
    return aid


def test_ui_live_run_unlocks_only_merit_and_line_tabs_and_hot_applies(tmp_path, monkeypatch):
    import sanmou_assistant.ui as ui_module

    app = QApplication.instance() or QApplication([])
    aid = _prepare_account(tmp_path)
    monkeypatch.setattr(ui_module, "Worker", _StubRunWorker)

    manager = AccountManagerWindow(tmp_path, enable_control_server=False)
    window = manager.current_window
    try:
        window.start("run")
        app.processEvents()

        # 运行中：02 部队任务、04 屯田日常、05 连接设置以及 03 里的铺路 Tab 锁定；
        # 03 里的武勋目标、拉线目标 Tab 与热应用按钮保持可用。
        assert not window.stack.widget(1).isEnabled()
        assert window.stack.widget(2).isEnabled()
        assert window.merit_target_tab.isEnabled()
        assert window.line_target_tab.isEnabled()
        assert not window.pave_tab.isEnabled()
        assert not window.stack.widget(3).isEnabled()
        assert not window.stack.widget(4).isEnabled()
        assert window.save_button.isEnabled()
        assert window.save_button.text() == "热应用目标设置"
        assert window.pause_button.isEnabled()

        # 通过快捷切换方法在运行中换坐标
        ok = window.apply_quick_target_switch(
            mode="line",
            address="热点目标(410,520)",
            actions=["guard", "attack", "march"],
            keep_others=False,
        )
        assert ok is True
        assert window.line_table.cellWidget(0, 2).value() == 410
        assert window.line_table.cellWidget(0, 3).value() == 520
        assert "热点目标(410,520)" in window.targets_banner.text()
        assert len(window.worker.updates) == 1
        assert window.worker.updates[0][0].line_targets[0].x == 410

        # 通过 03 目标页表格直接改 X 坐标并点击「热应用目标设置」
        window.line_table.cellWidget(0, 2).setValue(411)
        assert window.save() is True
        assert len(window.worker.updates) == 2
        assert window.worker.updates[-1][0].line_targets[0].x == 411
        assert AccountStore(tmp_path).get(aid).load_profile().line_targets[0].x == 411

        # 暂停与继续切换
        window.toggle_pause()
        assert window.paused is True and window.worker.paused is True
        assert window.pause_button.text() == "继续任务"
        window.toggle_pause()
        assert window.paused is False and window.worker.paused is False

        # 停止后恢复所有页签和保存按钮
        window.stop()
        app.processEvents()
        assert window.save_button.text() == "保存设置"
        assert window.pave_tab.isEnabled()
        assert not window.pause_button.isEnabled()
    finally:
        manager.close()
        app.processEvents()


def test_gui_and_agent_control_server_stay_in_sync_without_overwrite(tmp_path, monkeypatch):
    import sanmou_assistant.ui as ui_module

    app = QApplication.instance() or QApplication([])
    aid = _prepare_account(tmp_path)
    monkeypatch.setattr(ui_module, "Worker", _StubRunWorker)

    manager = AccountManagerWindow(tmp_path, enable_control_server=True)
    window = manager.current_window
    try:
        assert manager.control_server is not None
        window.start("run")
        app.processEvents()

        client = ControlClient(tmp_path)
        result_box = []
        done = threading.Event()

        def agent_call():
            try:
                res = client.switch_target(
                    account_id=aid,
                    mode="line",
                    address="代理新目标(888,999)",
                    actions=["guard", "attack", "march"],
                )
                result_box.append((True, res))
            except BaseException as exc:
                result_box.append((False, exc))
            finally:
                done.set()

        t = threading.Thread(target=agent_call, daemon=True)
        t.start()
        deadline = time.monotonic() + 5
        while not done.is_set() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        t.join(timeout=1)
        assert done.is_set()
        ok, res = result_box[0]
        assert ok, res
        assert res["live_applied"] is True

        # GUI 控件与总览摘要条已实时同步 Agent 改的新坐标
        assert window.line_table.cellWidget(0, 2).value() == 888
        assert window.line_table.cellWidget(0, 3).value() == 999
        assert "代理新目标(888,999)" in window.targets_banner.text()

        # 停止后在 GUI 点普通保存：绝不会把 Agent 改的新坐标覆盖回旧的 (200,200)
        window.stop()
        app.processEvents()
        assert window.save() is True
        reloaded = AccountStore(tmp_path).get(aid).load_profile()
        assert reloaded.line_targets[0].x == 888 and reloaded.line_targets[0].y == 999
    finally:
        if window.worker and window.worker.isRunning():
            window.stop()
            app.processEvents()
        manager.close()
        app.processEvents()


def test_quick_target_dialog_validates_and_returns_values(tmp_path):
    app = QApplication.instance() or QApplication([])
    aid = _prepare_account(tmp_path)
    profile = AccountStore(tmp_path).get(aid).load_profile()
    dialog = QuickTargetDialog(profile, running=True)
    dialog.address_edit.setText("涪县(300,300)")
    dialog.set_actions(["guard", "attack", "march"])
    dialog._validate_and_accept()
    vals = dialog.values()
    assert vals == {
        "mode": "line",
        "address": "涪县(300,300)",
        "actions": ["guard", "attack", "march"],
        "keep_others": False,
    }
    dialog.close()
    app.processEvents()
