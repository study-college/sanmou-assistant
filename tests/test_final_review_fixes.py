"""最终独立复核收尾（2026-11-14）的专项回归。

覆盖 8 项收尾修复中属于本侧的行为：
- 整帧角标优先 read_slots 批量读取、旧 reader 回退逐槽、同帧重伤覆盖保留；
- Stopped / TransportUnavailable 穿过整帧读取必须原样传播；
- _dispatch / line_refill 的 deployment_strength 按真实卡中心（center_x）读数；
- _line_finish 的 remaining 只看实际拥有的拉线队（配置 5 队实际 2 队场景）；
- UI error 分支与 Worker 已发 ERROR 日志去重、无对应日志时仍补记、仍跳日志页；
- AccountStore.get 对不存在账号抛 ValueError（带账号 ID）。

全部 stub，不连接设备、不依赖真帧。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from sanmou_assistant.accounts import AccountStore
from sanmou_assistant.game import Game, LineRefillUnavailable
from sanmou_assistant.models import Observation, Point, Profile, TeamPolicy
from sanmou_assistant.transport import Stopped, TransportUnavailable
from sanmou_assistant.ui import Window
from sanmou_assistant.worker import Worker

FRAME = np.zeros((1080, 1920, 3), dtype=np.uint8)


def marker_game(reader):
    game = Game.__new__(Game)
    game._marker_reader = reader
    game.active_slots = {1, 2}
    game.v = SimpleNamespace(frame_clear=lambda img: True,
                             team_wounded=lambda img, slot: slot == 2)
    game.d = SimpleNamespace(control=SimpleNamespace(sleep=lambda seconds: None))
    game.capture = lambda *args, **kwargs: FRAME
    game.ensure_sandbox = lambda **kwargs: FRAME
    game.log = lambda *args, **kwargs: None
    return game


class BatchReader:
    """新版 team_markers 提供 read_slots(image, slots)；记录调用但不实现 read_slot。"""

    def __init__(self):
        self.calls = []

    @staticmethod
    def avatars_visible(image):
        return True

    def read_slots(self, image, slots):
        self.calls.append((image, tuple(slots)))
        return [{"slot": slot, "state": "idle", "label": "空闲", "confidence": .9}
                for slot in slots]


def test_marker_batch_prefers_read_slots_and_keeps_wounded_overlay():
    reader = BatchReader()
    states = marker_game(reader).read_all_marker_states()
    assert reader.calls == [(FRAME, (1, 2))], "批量可用时应一次读出全部槽位"
    assert states[1]["state"] == "idle"
    # 同帧重伤覆盖保留：第 2 队头像有重伤文字，空闲角标要改成 wounded。
    assert states[2]["state"] == "wounded"


def test_marker_batch_falls_back_to_per_slot_reader():
    class LegacyReader:
        def __init__(self):
            self.slots = []

        @staticmethod
        def avatars_visible(image):
            return True

        def read_slot(self, image, slot):
            self.slots.append(slot)
            return {"slot": slot, "state": "idle", "label": "空闲", "confidence": .9}

    reader = LegacyReader()
    states = marker_game(reader).read_all_marker_states()
    assert reader.slots == [1, 2], "旧 reader 没有 read_slots 时应回退逐槽读取"
    assert set(states) == {1, 2}


class RaisingBatchReader:
    def __init__(self, exc):
        self.exc = exc

    @staticmethod
    def avatars_visible(image):
        return True

    def read_slots(self, image, slots):
        raise self.exc


@pytest.mark.parametrize("exc", [Stopped(), TransportUnavailable("adb 断连")])
def test_marker_batch_propagates_stop_and_transport_errors(exc):
    with pytest.raises(type(exc)):
        marker_game(RaisingBatchReader(exc)).read_all_marker_states()


def test_marker_batch_still_swallows_plain_recognition_errors():
    logs = []
    game = marker_game(RaisingBatchReader(ValueError("模板损坏")))
    game.log = lambda message, level="INFO": logs.append((level, message))
    assert game.read_all_marker_states() == {}
    assert logs and logs[0][0] == "WARN"


# ---------- deployment_strength 按真实卡中心读数 ----------


def refill_game(strength_calls):
    game = Game.__new__(Game)
    game.active_slots = {1, 2}
    game.v = SimpleNamespace(
        refill_quota=lambda img: 3828,
        deployment_strength=lambda img, slot, center_x=None: strength_calls.append(center_x) or 26000,
        refill_button=lambda img: (1700, 900))
    game.d = SimpleNamespace(tap=lambda *args, **kwargs: None)
    game.capture = lambda *args, **kwargs: FRAME
    game.save_image = lambda *args, **kwargs: None
    game.emit = lambda kind, value: None
    game.log = lambda *args, **kwargs: None
    return game


def test_line_refill_reads_strength_at_the_real_card_center():
    calls = []
    game = refill_game(calls)
    after, quota = game.line_refill(FRAME, TeamPolicy(slot=1, role="line"))
    assert quota == 3828 and after is FRAME
    # 两队整组居中：第 1 队卡中心 = 642 + (0 - 0.5) * 250 = 517，不是旧五列的 135。
    assert calls == [517, 517], "补兵前后读数都必须按真实卡中心，少队居中时不能读旧列"


def test_dispatch_refill_check_uses_the_real_card_center(tmp_path):
    calls = []
    game = Game.__new__(Game)
    game.active_slots = {1, 2}
    game.j = SimpleNamespace(pending=lambda key: [])
    game.account_checked = True
    game.emit = lambda kind, value: None
    game.go_to = lambda point: FRAME
    game.pick_action = lambda img, point, kind: ("行军", "march", (1500, 300))
    game.d = SimpleNamespace(tap=lambda *args, **kwargs: None)
    game.capture_clear = lambda *args, **kwargs: FRAME
    game.save_image = lambda *args, **kwargs: None
    game.v = SimpleNamespace(
        page=lambda img, verify=True: "deployment",
        deploy_button_enabled=lambda img: True,
        deployment_strength=lambda img, slot, center_x=None: calls.append(center_x) or 10000,
        refill_button=lambda img: None)
    policy = TeamPolicy(slot=1, role="line")
    obs = Observation("main:1", state="outside", marker_state="stationary",
                      wounded=False, strength=10000, maximum=28200)
    point = Point(name="拉线目标 (800,800)", x=800, y=800, actions=["occupy", "march"])
    with pytest.raises(LineRefillUnavailable):
        game._dispatch(policy, obs, point, "line", reuse_target=False)
    assert calls == [517], "拉线补兵核对必须按真实卡中心读数"


# ---------- _line_finish：未拥有的拉线队不阻止停止 ----------


class FinishJournal:
    def pending(self, key):
        return []

    def phase(self, key):
        return ""

    def set_phase(self, key, value):
        pass

    def finish(self, ident, status, note=""):
        pass

    def failure_streak(self, key):
        return 0


class FinishGame:
    """_line_finish 只需要的假游戏：调动永远成功。"""

    def __init__(self):
        self.dispatched = []
        self.logs = []

    def dispatch(self, policy, obs, point, kind, reuse_target=False):
        self.dispatched.append((policy.key, kind, point.name))

    def log(self, message, level="INFO"):
        self.logs.append((level, message))


def test_line_finish_ignores_teams_the_account_does_not_have(tmp_path):
    """配置 5 支拉线队、实际只拥有 2 支：实际两队都完成后必须停止，
    不能被本赛季不存在的 3～5 队拖成“永不清零”。"""
    point = Point(name="拉线目标 (800,800)", x=800, y=800, group="默认",
                  actions=["occupy", "march"], expected_name="")
    teams = [TeamPolicy(slot=slot, role="line", backup_points=["补给营帐"]) for slot in range(1, 6)]
    profile = Profile(teams=teams, line_targets=[point],
                      backups=[Point(name="补给营帐", x=700, y=700, enabled=True)])
    worker = Worker(profile, tmp_path, command="run", dry_run=False)
    worker.journal = FinishJournal()
    worker.active_slots = {1, 2}
    game = FinishGame()
    obs = Observation("main:1", state="home", location=(100, 200), wounded=False,
                      strength=18000, maximum=28200)
    worker._line_finish(game, teams[0], obs)
    assert not worker.control.stop.is_set(), "实际拥有的第 2 队还没完成，不能停"
    worker._line_finish(game, teams[1], obs)
    assert worker.control.stop.is_set(), "实际两队都完成就应停止，未拥有的队不参与判定"
    assert [key for key, _kind, _name in game.dispatched] == ["main:1", "main:2"]
    worker.control.stop.clear()
    worker.deleteLater()


# ---------- UI error 与 Worker 日志去重 ----------


def test_error_event_deduplicates_against_worker_error_log(tmp_path):
    """Worker 停止时已经发布同一条 ERROR 日志：error 分支只跳日志页、不重复追加。"""
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    window.nav.setCurrentRow(0)
    message = "账号已在其他设备登录，停止操作"
    window.handle_event('log', {'time': '12:00:00', 'level': 'ERROR', 'message': message})
    window.handle_event('error', message)
    errors = [record for record in window.log_records
              if record['level'] == 'ERROR' and record['message'] == message]
    assert len(errors) == 1, "Worker 已发的 ERROR 不应在 error 分支再追加一条"
    assert window.nav.currentRow() == window.log_page_index, "仍必须跳到日志页"
    window.close()
    app.processEvents()


def test_error_event_without_matching_log_is_still_recorded(tmp_path):
    """只有 error 事件、没有对应日志时：仍要补记并跳日志页（保留现有错误留证）。"""
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    window.nav.setCurrentRow(0)
    window.handle_event('error', '连接超时')
    errors = [record for record in window.log_records
              if record['level'] == 'ERROR' and record['message'] == '连接超时']
    assert len(errors) == 1
    assert window.nav.currentRow() == window.log_page_index
    window.close()
    app.processEvents()


def test_error_event_appends_again_when_only_older_same_error_exists(tmp_path):
    """去重只看最近记录：更早的同文 ERROR 不拦新的 error 事件补记。"""
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    message = "连接超时"
    for i in range(10):
        window.append_log({'time': '12:00:00', 'level': 'INFO', 'message': f'填充{i}'})
    window.append_log({'time': '12:00:01', 'level': 'ERROR', 'message': message})
    for i in range(10):
        window.append_log({'time': '12:00:02', 'level': 'INFO', 'message': f'之后{i}'})
    window.handle_event('error', message)
    errors = [record for record in window.log_records
              if record['level'] == 'ERROR' and record['message'] == message]
    assert len(errors) == 2, "最近窗口之外的同文 ERROR 不应去重新错误"
    window.close()
    app.processEvents()


# ---------- AccountStore.get 对不存在账号抛 ValueError ----------


def test_account_store_get_raises_value_error_with_account_id(tmp_path):
    (tmp_path / "active-profile.json").write_text(
        json.dumps(Profile(name="原账号").to_dict()), encoding="utf-8")
    store = AccountStore(tmp_path)
    existing = store.accounts[0].id
    assert store.get(existing).id == existing
    missing = "account-0123456789ab"
    with pytest.raises(ValueError) as raised:
        store.get(missing)
    assert missing in str(raised.value), "错误信息必须带账号 ID"
    with pytest.raises(ValueError):
        store.select(missing)
