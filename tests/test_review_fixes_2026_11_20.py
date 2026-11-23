"""2026-11-20 代码评审 P0 + P1 专项回归测试。"""
from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

import sanmou_assistant
from sanmou_assistant.game import (
    Game,
    GameError,
    NavigationRecovered,
    NetworkRecovered,
    RallyJoinRejected,
    RecognitionUnavailable,
)
from sanmou_assistant.journal import Journal
from sanmou_assistant.models import Observation, Point, Profile, TeamPolicy, atomic_json
from sanmou_assistant.team_markers import SLOT_Y
from sanmou_assistant.transport import Control, Device, Stopped
from sanmou_assistant.ui import Window
from sanmou_assistant.vision import Vision
from sanmou_assistant.worker import Worker


def _qt_app():
    return QApplication.instance() or QApplication([])


# ---------- P0-1: 拉线征兵点既有设置不因保存被覆盖 ----------


def test_collect_preserves_existing_line_backup_settings_when_address_unchanged(tmp_path):
    app = _qt_app()
    profile = Profile(
        account="测试",
        main_city=Point(name="主城", x=10, y=20),
        line_targets=[Point(name="测试目标", x=400, y=500, enabled=True)],
        line_backups={
            "main:1": Point(
                name="营帐甲",
                x=300,
                y=200,
                enabled=False,
                priority=7,
                actions=["march"],
                expected_name="旧归属名",
            ),
            "main:2": Point(
                name="营帐乙",
                x=310,
                y=210,
                enabled=True,
                priority=9,
                actions=["march"],
                expected_name="营帐乙",
            ),
        },
    )
    profile.teams[0].role = "line"
    profile.teams[1].role = "line"
    atomic_json(tmp_path / "active-profile.json", profile.to_dict())
    window = Window(tmp_path)
    # main:1 地址不改，main:2 改坐标，验证两者行为分离。
    window.line_backup_edits["main:2"].setText("营帐丙(320,220)")
    collected = window.collect()

    kept = collected.line_backups["main:1"]
    assert (kept.x, kept.y, kept.name) == (300, 200, "营帐甲")
    assert kept.enabled is False
    assert kept.priority == 7
    assert kept.actions == ["march"]
    assert kept.expected_name == "旧归属名"

    changed = collected.line_backups["main:2"]
    assert (changed.x, changed.y, changed.name) == (320, 220, "营帐丙")
    assert changed.enabled is True

    # 留空应删除该队拉线征兵点。
    window.line_backup_edits["main:1"].clear()
    cleared = window.collect()
    assert "main:1" not in cleared.line_backups
    window.close()
    app.processEvents()


# ---------- P0-2: Worker RecognitionUnavailable 恢复块放行 NetworkRecovered / NavigationRecovered ----------


@pytest.mark.parametrize("recovery_exc", [NetworkRecovered("网络已重连"), NavigationRecovered("已返回沙盘")])
def test_worker_recognition_recovery_continues_on_network_or_navigation_recovered(tmp_path, monkeypatch, recovery_exc):
    import sanmou_assistant.worker as worker_mod

    profile = Profile(
        name="测试号",
        cli_path="mumu.exe",
        instance="0",
        main_city=Point(name="主城", x=100, y=100),
        teams=[TeamPolicy(slot=1, role="merit")],
        food_enabled=False,
        poll_seconds=1,
    )
    worker = Worker(profile, tmp_path / "session", command="run", dry_run=True)
    events = []
    worker.publish = lambda name, value: events.append((name, value))

    calls = {"ensure": 0, "deselect": 0, "batch": 0}

    class FakeDevice:
        def __init__(self, *args, **kwargs):
            self.info = {}
            self.serial = "127.0.0.1:16384"
            self.width = 1920
            self.height = 1080

        def connect(self, *args, **kwargs):
            return {"serial": self.serial}

        def capture(self, passive=False):
            return np.zeros((10, 10, 3), np.uint8)

        def launch_game(self, package):
            pass

        def close(self):
            pass

    class FakeGame:
        def __init__(self, *args, **kwargs):
            self.grain = 500000
            self.city_visited = False
            self.account_checked = True
            self.j = None
            self.last_image = np.zeros((10, 10, 3), np.uint8)
            self.d = SimpleNamespace(minimize=lambda: None)

        def capture(self, *args, **kwargs):
            return self.last_image

        def log(self, message, level="INFO"):
            events.append(("log", (level, message)))

        def ensure_sandbox(self, refresh_grain=True, recover_navigation=False):
            calls["ensure"] += 1
            if recover_navigation:
                raise recovery_exc
            return self.last_image

        def deselect_unknown_page(self):
            calls["deselect"] += 1
            return False

        def ensure_hide_on(self):
            return "hidden"

        def refresh_resources(self, paused=False):
            pass

    def fake_batch(game, *args, **kwargs):
        calls["batch"] += 1
        if calls["batch"] == 1:
            raise RecognitionUnavailable("页面暂未识别")
        worker.control.stop.set()
        raise Stopped()

    monkeypatch.setattr(worker_mod, "discover_cli", lambda path: "mumu-cli.exe")
    monkeypatch.setattr(worker_mod, "instances", lambda cli: [])
    monkeypatch.setattr(worker_mod, "Device", FakeDevice)
    monkeypatch.setattr(worker_mod, "Vision", lambda control: SimpleNamespace(page=lambda img: "sandbox"))
    monkeypatch.setattr(worker_mod, "Game", FakeGame)
    monkeypatch.setattr(worker, "ensure_home", lambda game: None)
    monkeypatch.setattr(worker, "ensure_roster", lambda game: setattr(worker, "team_slots", [1]))
    monkeypatch.setattr(worker, "check_merit", lambda game, enabled=True: None)
    monkeypatch.setattr(worker, "settle_orphan_pave_orders", lambda game: None)
    monkeypatch.setattr(worker, "process_team_batch", fake_batch)
    monkeypatch.setattr(worker, "wait_for_next_check", lambda game: None)

    worker.run()

    assert calls["batch"] == 2
    assert calls["deselect"] == 0, "NetworkRecovered/NavigationRecovered 不应再误调 deselect_unknown_page"
    assert not any(name == "error" for name, _ in events)
    worker.deleteLater()


def test_worker_deselect_network_recovered_does_not_stop_worker(tmp_path, monkeypatch):
    import sanmou_assistant.worker as worker_mod

    profile = Profile(
        name="测试号",
        cli_path="mumu.exe",
        instance="0",
        main_city=Point(name="主城", x=100, y=100),
        teams=[TeamPolicy(slot=1, role="merit")],
        food_enabled=False,
        poll_seconds=1,
    )
    worker = Worker(profile, tmp_path / "session", command="run", dry_run=True)
    events = []
    worker.publish = lambda name, value: events.append((name, value))
    calls = {"batch": 0, "deselect": 0}

    class FakeDevice:
        def __init__(self, *args, **kwargs):
            self.info = {}
            self.serial = "127.0.0.1:16384"
            self.width = 1920
            self.height = 1080

        def connect(self, *args, **kwargs):
            return {"serial": self.serial}

        def capture(self, passive=False):
            return np.zeros((10, 10, 3), np.uint8)

        def launch_game(self, package):
            pass

        def close(self):
            pass

    class FakeGame:
        def __init__(self, *args, **kwargs):
            self.grain = 500000
            self.city_visited = False
            self.account_checked = True
            self.j = None
            self.last_image = np.zeros((10, 10, 3), np.uint8)
            self.d = SimpleNamespace(minimize=lambda: None)

        def capture(self, *args, **kwargs):
            return self.last_image

        def log(self, message, level="INFO"):
            events.append(("log", (level, message)))

        def ensure_sandbox(self, refresh_grain=True, recover_navigation=False):
            if recover_navigation:
                raise RecognitionUnavailable("仍未读清")
            return self.last_image

        def deselect_unknown_page(self):
            calls["deselect"] += 1
            raise NetworkRecovered("滑动期间重连")

        def ensure_hide_on(self):
            return "hidden"

        def refresh_resources(self, paused=False):
            pass

    def fake_batch(game, *args, **kwargs):
        calls["batch"] += 1
        if calls["batch"] == 1:
            raise RecognitionUnavailable("页面暂未识别")
        worker.control.stop.set()
        raise Stopped()

    monkeypatch.setattr(worker_mod, "discover_cli", lambda path: "mumu-cli.exe")
    monkeypatch.setattr(worker_mod, "instances", lambda cli: [])
    monkeypatch.setattr(worker_mod, "Device", FakeDevice)
    monkeypatch.setattr(worker_mod, "Vision", lambda control: SimpleNamespace(page=lambda img: "sandbox"))
    monkeypatch.setattr(worker_mod, "Game", FakeGame)
    monkeypatch.setattr(worker, "ensure_home", lambda game: None)
    monkeypatch.setattr(worker, "ensure_roster", lambda game: setattr(worker, "team_slots", [1]))
    monkeypatch.setattr(worker, "check_merit", lambda game, enabled=True: None)
    monkeypatch.setattr(worker, "settle_orphan_pave_orders", lambda game: None)
    monkeypatch.setattr(worker, "process_team_batch", fake_batch)
    monkeypatch.setattr(worker, "wait_for_next_check", lambda game: None)

    worker.run()

    assert calls["deselect"] == 1
    assert calls["batch"] == 2
    assert not any(name == "error" for name, _ in events)
    worker.deleteLater()


# ---------- P0-3: event_signin pending 核对 ----------


def _make_event_game(journal, *, has_button: bool, body_text: str):
    image = object()
    emitted = []
    taps = []
    game = Game.__new__(Game)
    game.j = journal
    game.open_activity = lambda: image
    game.v = SimpleNamespace(
        find=lambda img, labels, *args, **kwargs: (
            SimpleNamespace(x=10, y=20, text="活动签到")
            if "活动签到" in labels
            else (SimpleNamespace(x=50, y=60, text="免费领取") if has_button else None)
        ),
        joined=lambda *args: body_text,
    )
    game.d = SimpleNamespace(tap=lambda x, y, *args: taps.append((x, y)))
    game.capture = lambda *args: image
    game.emit = lambda name, value: emitted.append((name, value))
    game.dismiss_overlay = lambda *args: None
    game.ensure_sandbox = lambda: image
    return game, emitted, taps


def test_event_signin_settles_pending_when_already_claimed(tmp_path):
    journal = Journal(tmp_path / "runtime.sqlite", "scope-a")
    journal.prepare_daily("events", {"activity": "活动签到"})
    game, emitted, taps = _make_event_game(journal, has_button=False, body_text="今日奖励已领取")

    game.event_signin(True)

    assert journal.daily("events")[0] == "done"
    assert len(taps) == 1  # 只点了左侧活动标签，没有点领取按钮
    assert any("已确认领取" in v["result"] for _, v in emitted)
    journal.close()


def test_event_signin_pending_with_button_still_refuses_duplicate_tap(tmp_path):
    journal = Journal(tmp_path / "runtime.sqlite", "scope-a")
    journal.prepare_daily("events", {"activity": "活动签到"})
    game, _emitted, taps = _make_event_game(journal, has_button=True, body_text="免费领取")

    with pytest.raises(GameError, match="未确认"):
        game.event_signin(True)

    assert journal.daily("events")[0] == "pending"
    assert len(taps) == 1
    journal.close()


def test_event_signin_pending_without_button_or_claimed_text_exits_safely(tmp_path):
    journal = Journal(tmp_path / "runtime.sqlite", "scope-a")
    journal.prepare_daily("events", {"activity": "活动签到"})
    game, emitted, taps = _make_event_game(journal, has_button=False, body_text="活动说明")

    game.event_signin(True)

    assert journal.daily("events")[0] == "pending"
    assert len(taps) == 1
    assert any("待核对" in v["result"] for _, v in emitted)
    journal.close()


# ---------- P1-5: export_config 捕获异常不崩溃 ----------


def test_export_config_handles_invalid_input_and_write_error(tmp_path, monkeypatch):
    import sanmou_assistant.ui as ui_mod

    app = _qt_app()
    window = Window(tmp_path)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: warnings.append((title, text)))

    # 1. 非法地址输入触发 ValueError
    window.merit_backup_address.setText("这不是合法坐标")
    window.export_config()
    assert warnings and warnings[-1][0] == "导出失败"

    # 2. 写文件触发 OSError
    window.merit_backup_address.clear()
    monkeypatch.setattr(
        ui_mod.QFileDialog,
        "getSaveFileName",
        lambda *args, **kwargs: (str(tmp_path / "out.json"), "配置 (*.json)"),
    )
    monkeypatch.setattr(ui_mod, "atomic_json", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("磁盘只读")))
    window.export_config()
    assert warnings[-1] == ("导出失败", "磁盘只读")

    window.close()
    app.processEvents()


# ---------- P1-6: 讨贼列表去倒计时去重与重试上限 ----------


def test_bandits_deduplicates_countdown_changes_and_caps_retries():
    game = Game.__new__(Game)
    game.p = Profile(bandit_allow_assist=False, teams=[TeamPolicy(slot=1, role="bandits", minimum=50)])
    game.account_checked = True
    game.grain = 500000
    game.j = SimpleNamespace(
        bandits_exhausted=lambda: False,
        observe_bandit_availability=lambda *args: None,
        record_daily=lambda *args, **kwargs: None,
        pending=lambda key: [],
        failure_streak=lambda key: 0,
    )
    countdown = iter(["04:59", "04:58", "04:57"])
    join_calls = []
    emitted = []
    game.open_bandit_list = lambda: object()
    game.ensure_sandbox = lambda: object()
    game.log = lambda *args: None
    game.emit = lambda name, value: emitted.append((name, value))
    obs = Observation("main:1", state="home", location=(10, 10), strength=20000, maximum=20000, stamina=100, supply=100, wounded=False)
    game.scan_team = lambda policy: obs
    game.v = SimpleNamespace(
        joined=lambda *args: "讨贼集结列表",
        bandit_availability=lambda img: ("reward", 2),
        bandit_rallies=lambda img: [{"text": f"测试盟友 讨贼 集结中 {next(countdown, '04:00')}", "join": (100, 200)}],
    )

    def reject(policy, obs, rally, allow_assist=False):
        join_calls.append(rally["text"])
        raise RallyJoinRejected("集结已满员")

    game.join_bandits = reject

    game.bandits(commit=True)

    assert len(join_calls) == 1, "倒计时变化不应把同一集结重复加入候选"
    assert any(name == "daily" and "无其他可加入集结" in v["result"] for name, v in emitted)


def test_bandits_retry_loop_has_upper_bound():
    game = Game.__new__(Game)
    game.p = Profile(bandit_allow_assist=False, teams=[TeamPolicy(slot=1, role="bandits", minimum=50)])
    game.account_checked = True
    game.grain = 500000
    game.j = SimpleNamespace(
        bandits_exhausted=lambda: False,
        observe_bandit_availability=lambda *args: None,
        record_daily=lambda *args, **kwargs: None,
        pending=lambda key: [],
        failure_streak=lambda key: 0,
    )
    counter = {"n": 0}
    game.open_bandit_list = lambda: object()
    game.ensure_sandbox = lambda: object()
    game.log = lambda *args: None
    game.emit = lambda *args: None
    obs = Observation("main:1", state="home", location=(10, 10), strength=20000, maximum=20000, stamina=100, supply=100, wounded=False)
    game.scan_team = lambda policy: obs

    def next_rally(img):
        counter["n"] += 1
        return [{"text": f"盟友{counter['n']} 集结中 04:59", "join": (100, 200)}]

    game.v = SimpleNamespace(
        joined=lambda *args: "讨贼集结列表",
        bandit_availability=lambda img: ("reward", 2),
        bandit_rallies=next_rally,
    )
    game.join_bandits = lambda *args, **kwargs: (_ for _ in ()).throw(RallyJoinRejected("满员"))

    game.bandits(commit=True)

    assert counter["n"] <= 11  # 1 次初始检查 + 最多 10 次尝试


# ---------- P1-7: _line_finish 调动失败不永久结束该队 ----------


def test_line_finish_does_not_mark_finished_when_transfer_fails(tmp_path):
    policy = TeamPolicy(slot=1, role="line")
    profile = Profile(
        teams=[policy],
        line_targets=[Point(name="目标", x=500, y=500, enabled=True)],
        line_backups={"main:1": Point(name="补兵营帐", x=600, y=600, enabled=True)},
        main_city=Point(name="主城", x=100, y=100),
    )
    worker = Worker(profile, tmp_path, command="run", dry_run=False)
    worker.line_snapshot["main:1"] = (100.0, Observation("main:1", state="home"))
    logs = []
    game = SimpleNamespace(
        dispatch=lambda *args, **kwargs: (_ for _ in ()).throw(GameError("临时点不中")),
        log=lambda message, level="INFO": logs.append((level, message)),
    )
    obs = Observation("main:1", state="home", location=(100, 100), strength=15000, maximum=30000)

    worker._line_finish(game, policy, obs)

    assert "main:1" not in worker.line_finished
    assert "main:1" not in worker.line_snapshot
    assert any("下轮重试" in msg for _, msg in logs)

    # NetworkRecovered 必须向上传播，不能被当作普通 GameError 吞掉。
    game_net = SimpleNamespace(
        dispatch=lambda *args, **kwargs: (_ for _ in ()).throw(NetworkRecovered("重连")),
        log=lambda *args: None,
    )
    with pytest.raises(NetworkRecovered):
        worker._line_finish(game_net, policy, obs)
    worker.deleteLater()


# ---------- P1-8: Control.sleep(passive=True) 与 capture(passive=True) 不在暂停时死等 ----------


def test_control_sleep_passive_does_not_block_while_paused():
    control = Control()
    control.pause.set()
    # passive=True 应迅速完成而不因 pause 死等。
    control.sleep(0.05, passive=True)

    control.stop.set()
    with pytest.raises(Stopped):
        control.sleep(0.5, passive=True)


def test_device_capture_passive_retry_does_not_block_while_paused(monkeypatch):
    import sanmou_assistant.transport as transport_mod

    control = Control()
    control.pause.set()
    device = Device("mumu.exe", "0", control=control)
    device.adb = "adb.exe"
    device.serial = "127.0.0.1:16384"

    ok, encoded = cv2.imencode(".png", np.zeros((1080, 1920, 3), np.uint8))
    assert ok
    png_bytes = encoded.tobytes()

    sleep_calls = []
    monkeypatch.setattr(control, "sleep", lambda seconds, passive=False: sleep_calls.append((seconds, passive)))

    seq = iter([
        RuntimeError("device offline"),  # 首次 capture 失败
        "",  # attempt 0 disconnect
        "",  # attempt 0 connect
        "offline",  # attempt 0 get-state 失败 → 触发 attempt 1 的 sleep
        "",  # attempt 1 disconnect
        "",  # attempt 1 connect
        "device",  # attempt 1 get-state 成功
        png_bytes,  # attempt 1 capture 成功
    ])

    def fake_run(args, timeout=20, binary=False, control=None):
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(transport_mod, "run", fake_run)
    frame = device.capture(passive=True)

    assert frame.shape == (1080, 1920, 3)
    assert sleep_calls == [(1.0, True)]


# ---------- P1-9: home_team_visible 适配 HUD_OFFSETS ----------


def test_home_team_visible_supports_hud_offsets():
    vision = Vision.__new__(Vision)
    assets = Path(sanmou_assistant.__file__).resolve().parent / "assets"
    vision.home_marker = cv2.imdecode(np.fromfile(assets / "home-team-marker-mask.png", np.uint8), cv2.IMREAD_GRAYSCALE)
    assert vision.home_marker is not None
    mh, mw = vision.home_marker.shape

    for dx in (0, -25):
        canvas = np.zeros((1080, 1920, 3), np.uint8)
        y0 = SLOT_Y[0] - 40 + 10
        x0 = 1700 + dx + 10
        # 填充符合金色 HSV (10..43, 35..255, 110..255) 的 BGR 颜色 (0, 200, 255)
        patch = canvas[y0 : y0 + mh, x0 : x0 + mw]
        patch[vision.home_marker > 127] = (0, 200, 255)
        assert vision.home_team_visible(canvas, 1) is True
        assert vision.home_team_visible(canvas, 2) is False


# ---------- P1-10: pyproject.toml 版本与 package-data 一致 ----------


def test_pyproject_version_and_package_data_match_package():
    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == sanmou_assistant.__version__
    package_data = data["tool"]["setuptools"]["package-data"]["sanmou_assistant"]
    assert "assets/app.ico" in package_data
