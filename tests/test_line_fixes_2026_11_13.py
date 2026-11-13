"""交接文档 72 的拉线问题回归（2026-11-13）。

覆盖：3.1 拉线崩溃（Worker 没有设备实例）、3.2 日志重复坐标、
3.3 拉线详情不显示补兵点、3.4 队伍名累积“（未拥有）”、3.5 没有拉线目标时等待并提示。
这些用例不依赖真帧，全部随源码分发。
"""
from __future__ import annotations

import time
import types

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication, QLabel

from sanmou_assistant.game import DispatchUnconfirmed, Game, GameError
from sanmou_assistant.models import (Observation, Point, Profile, TeamPolicy, line_decision,
                                     line_target, point_label, team_label)
from sanmou_assistant.ui import TeamDetailDialog, Window
from sanmou_assistant.vision import Vision
from sanmou_assistant.worker import Worker


# ---------- 3.2 统一地点显示 ----------

def test_point_label_never_repeats_the_same_coordinates():
    assert point_label(Point(name="拉线目标 (617,759)", x=617, y=759)) == "拉线目标 (617,759)"
    assert point_label(Point(name="拉线目标（617，759）", x=617, y=759)) == "拉线目标（617，759）"
    assert point_label(Point(name="水面", x=617, y=759)) == "水面(617,759)"
    assert point_label(Point(name="", x=617, y=759)) == "(617,759)"
    # 名称里是别的坐标：仍然补上真实坐标，不能被误当成“已经有坐标”。
    assert point_label(Point(name="旧点(1,2)", x=617, y=759)) == "旧点(1,2)(617,759)"


def test_line_decision_reason_prints_coordinates_only_once():
    target = Point(name="拉线目标 (617,759)", x=617, y=759, group="默认")
    profile = Profile(teams=[TeamPolicy(slot=1, role="line")], line_targets=[target])
    obs = Observation("main:1", state="outside", marker_state="stationary",
                      strength=33000, maximum=33000, wounded=False)
    reason = line_decision(profile.teams[0], obs, target=line_target(profile, profile.teams[0])).reason
    assert reason.count("617,759") == 1
    assert "拉线 拉线" not in reason


# ---------- 3.1 拉线流程不再崩溃 ----------

class FakeLineGame:
    """只回答拉线循环需要的那几个方法；不连接设备。"""

    def __init__(self, image, marker_state="stationary", fail_dispatch=True, scan_state="outside"):
        self.image = image
        self.marker_state = marker_state
        self.fail_dispatch = fail_dispatch
        self.scan_state = scan_state
        self.hud = {}
        self.logs = []
        self.targets = []
        self.dispatched = []
        self.recalled = []
        self.scanned = 0
        self.sandboxed = 0
        self.account_checked = True
        self.v = types.SimpleNamespace(find=lambda *args, **kwargs: None)

    def read_all_marker_states(self):
        return self.hud

    def scan_team(self, policy):
        # 拉线第一轮完整读数；marker_state 有值，不再走铺路快照兜底。
        self.scanned += 1
        return Observation(policy.key, state=self.scan_state, marker_state=self.marker_state,
                           strength=33000, maximum=33000, wounded=False)

    def log(self, message, level="INFO"):
        self.logs.append((level, message))

    def go_to(self, point):
        self.targets.append(point)
        return self.image

    def capture(self, *args, **kwargs):
        return self.image

    def ensure_sandbox(self):
        self.sandboxed += 1

    def dispatch(self, policy, obs, point, kind, reuse_target=False):
        self.dispatched.append((kind, point))
        if self.fail_dispatch:
            raise GameError("测试停止：不真正派兵")

    def recall(self, policy, obs, minimum=None):
        self.recalled.append((policy.key, obs.location, minimum))


class FakeJournal:
    def pending(self, key):
        return []

    def phase(self, key):
        return ""

    def set_phase(self, key, value):
        pass

    def finish(self, ident, status, note):
        pass

    def failure_streak(self, key):
        return 0


def make_worker(tmp_path, actions=("occupy", "march"), **policy_kwargs):
    point = Point(name="拉线目标 (617,759)", x=617, y=759, group="默认",
                  actions=list(actions), expected_name="")
    policy = TeamPolicy(slot=1, role="line", **policy_kwargs)
    profile = Profile(teams=[policy], line_targets=[point])
    worker = Worker(profile, tmp_path, command="run", dry_run=False)
    worker.journal = FakeJournal()
    return worker, profile, point


def test_process_line_dispatches_without_device_and_backs_off(tmp_path):
    """3.1：拉线循环不再依赖 Worker 不存在的 self.d；派兵失败只退避、不崩。"""
    app = QApplication.instance() or QApplication([])
    worker, _profile, point = make_worker(tmp_path)
    image = np.zeros((1080, 1920, 3), np.uint8)
    game = FakeLineGame(image)
    worker.process_line(game)
    assert game.sandboxed == 1                     # 派兵失败后回沙盘
    assert game.dispatched and game.dispatched[0][0] == "line"
    assert game.dispatched[0][1].name == point.name   # 不改写目标名
    assert "line:main:1" in worker.retry_at
    worker.deleteLater()
    app.processEvents()


def test_process_line_uses_the_target_action_order(tmp_path):
    """用户 2026-11-13：动作顺序由使用者配置，拉线不再写死“攻占/行军”。"""
    app = QApplication.instance() or QApplication([])
    worker, _profile, _point = make_worker(tmp_path, actions=("attack", "occupy", "march"))
    game = FakeLineGame(np.zeros((1080, 1920, 3), np.uint8))
    worker.process_line(game)
    assert game.dispatched, "应当尝试派兵"
    assert game.dispatched[0][1].actions == ["attack", "occupy", "march"]
    worker.deleteLater()
    app.processEvents()


def test_process_line_skips_team_without_enabled_target(tmp_path):
    """3.5：没有启用的拉线目标时只提示，不定位、不派兵。"""
    app = QApplication.instance() or QApplication([])
    worker, profile, _point = make_worker(tmp_path)
    profile.line_targets[0].enabled = False
    game = FakeLineGame(np.zeros((1080, 1920, 3), np.uint8))
    worker.process_line(game)
    assert game.dispatched == [] and game.scanned == 0
    assert not worker.retry_at and not worker.line_snapshot
    assert any("拉线目标" in entry["value"]["message"] for entry in worker.published
               if entry["event"] == "log")
    worker.deleteLater()
    app.processEvents()


def test_process_line_reads_one_hud_frame_for_decision(tmp_path):
    """一轮只截一帧：有缓存和整帧角标时不再为读状态导航到队伍详情。"""
    app = QApplication.instance() or QApplication([])
    worker, _profile, _point = make_worker(tmp_path, minimum=95)
    worker.line_snapshot["main:1"] = (27482, 28200, time.time())
    game = FakeLineGame(np.zeros((1080, 1920, 3), np.uint8))
    game.hud = {1: {"state": "busy", "label": "忙碌", "confidence": .94}}
    worker.process_line(game)
    assert game.scanned == 0, "有整帧角标和兵力缓存时不应再导航读队"
    assert game.dispatched and game.dispatched[0][0] == "line"
    worker.deleteLater()
    app.processEvents()


# ---------- 3.4 队伍名不累积状态后缀 ----------

def test_team_label_strips_every_suffix_variant():
    assert team_label("主城 5 队（未拥有）") == "主城 5 队"
    assert team_label("主城 5 队（未拥有）（未拥有）（未拥有）") == "主城 5 队"
    assert team_label("主城 5 队(未拥有)") == "主城 5 队"
    assert team_label("自定义名字") == "自定义名字"
    assert team_label("") == ""


def test_polluted_team_labels_are_cleaned_when_profile_loads():
    data = {"schema": 1, "name": "测试配置", "account": "", "teams": [
        {"slot": 1, "group": "main", "label": "主城 1 队（未拥有）（未拥有）"},
        {"slot": 5, "group": "main", "label": "主城 5 队（未拥有）（未拥有）（未拥有）（未拥有）"},
        {"slot": 1, "group": "guard", "label": "亲卫 1 队（未拥有）（未拥有）"},
    ]}
    profile = Profile.from_dict(data)
    assert [t.label for t in profile.teams] == ["主城 1 队", "主城 5 队", "亲卫 1 队"]
    assert profile.validate() == []


def test_settings_page_does_not_save_unowned_suffix_into_names(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    window.apply_team_presence([1, 2])
    assert "未拥有" in window.team_table.item(2, 1).text()   # 界面上仍要提示
    first = window.collect()
    assert "未拥有" not in first.teams[2].label
    assert first.teams[2].label == "主城 3 队"
    # 反复同步/保存也不累积。
    window.sync_team_row(2)
    window.sync_team_row(5)
    second = window.collect()
    assert second.teams[2].label == "主城 3 队"
    assert "未拥有" not in second.teams[5].label
    window.close()
    app.processEvents()


# ---------- 3.3 拉线详情不显示补兵点 ----------

def dialog_profile():
    return Profile(
        teams=[TeamPolicy(slot=1, role="line"), TeamPolicy(slot=2, role="merit", backup_points=["营帐A"])],
        line_targets=[Point(name="水面", x=617, y=759, group="拉线组", priority=1),
                      Point(name="备用", x=1, y=2, group="默认", priority=2)],
        targets=[Point(name="战场", x=640, y=286, group="战场组", priority=1)],
        backups=[Point(name="营帐A", x=300, y=200)])


def test_line_team_detail_shows_backup_points_and_uses_line_groups():
    app = QApplication.instance() or QApplication([])
    profile = dialog_profile()
    profile.teams[0].backup_points = ["营帐A"]
    dialog = TeamDetailDialog(profile.teams[0], profile)
    assert dialog.line_role
    assert dialog.points is not None and dialog.points.count() == 1
    groups = [dialog.group.itemText(i) for i in range(dialog.group.count())]
    assert "拉线组" in groups and "默认" in groups            # 目标组取自拉线目标
    assert "战场组" not in groups
    assert dialog.values()[1] == ["营帐A"]                     # 拉线的营帐（补兵点）顺序
    labels = [w.text() for w in dialog.findChildren(QLabel)]
    assert any("拉线补不动时" in text for text in labels)
    assert any("补兵点 / 营帐" in text for text in labels)
    dialog.close()
    app.processEvents()


def test_merit_team_detail_keeps_backup_points_without_line_fallback():
    app = QApplication.instance() or QApplication([])
    profile = dialog_profile()
    dialog = TeamDetailDialog(profile.teams[1], profile)
    assert not dialog.line_role
    assert dialog.points.count() == 1
    groups = [dialog.group.itemText(i) for i in range(dialog.group.count())]
    assert "战场组" in groups and "拉线组" not in groups
    labels = [w.text() for w in dialog.findChildren(QLabel)]
    assert any("备用征兵地点" in text for text in labels)
    assert not any("拉线补不动时" in text for text in labels)
    assert dialog.values() == ("默认", ["营帐A"], "recall")
    dialog.close()
    app.processEvents()


def test_line_team_summary_shows_its_real_target(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    team = window.profile.teams[0]
    team.role, team.target_group = "line", "默认"
    window.profile.line_targets = [Point(name="水面", x=617, y=759, group="默认", priority=1)]
    summary = window.team_detail_summary(team)
    assert "水面(617,759)" in summary
    assert "补兵点" in summary          # 拉线的营帐（补兵点）也显示在行内摘要
    window.close()
    app.processEvents()


# ---------- 实机额外发现：提交前卡片已经重伤/撤退 ----------
# 2026-11-13 实机：观测“停留”后走到目标，卡片已经是“撤退 00:01:22 / 重伤 04:25”，
# 旧代码仍连点 3 次出征。这里锁定“提交前核卡片状态”的行为。

def test_deployment_card_status_recognises_retreat():
    vision = Vision()
    image = np.zeros((1080, 1920, 3), np.uint8)
    vision.joined = lambda *args, **kwargs: "撤退|00:01:22"
    assert vision.deployment_status(image, 1, 517) == "retreat"
    vision.joined = lambda *args, **kwargs: "重伤|04:25"
    assert vision.deployment_status(image, 1, 517) == "wounded"
    vision.joined = lambda *args, **kwargs: "停留"
    assert vision.deployment_status(image, 1, 517) == "outside"
    vision.joined = lambda *args, **kwargs: "空闲"
    assert vision.deployment_status(image, 1, 517) == "home"


class StubVision:
    """只回答提交前核对用得到的几个方法。"""

    def __init__(self, status):
        self.status = status

    def page(self, image):
        return "deployment"

    def deploy_button_enabled(self, image):
        return True

    def deployment_status(self, image, slot, center_x=None):
        return self.status

    def find(self, *args, **kwargs):
        return None

    def joined(self, image, box):
        return ""


class StubDevice:
    def __init__(self, image):
        self.image = image
        self.taps = []

    def capture(self, *args, **kwargs):
        return self.image

    def tap(self, *args, **kwargs):
        self.taps.append(args)


def stub_game(tmp_path, status):
    image = np.zeros((1080, 1920, 3), np.uint8)
    game = Game.__new__(Game)
    game.d, game.v = StubDevice(image), StubVision(status)
    game.p = Profile()
    game.j = types.SimpleNamespace(pending=lambda key: [], prepare=lambda *args: "intent")
    game.folder = tmp_path
    game.emit = lambda *args: None
    game.account_checked = True
    game.save_image = lambda *args, **kwargs: None
    game.go_to = lambda point: image
    game.reopen_target = lambda point: image
    game.capture_clear = lambda *args, **kwargs: image
    game.pick_action = lambda img, point, kind: ("行军", "march", (1500, 300))
    return game


def test_dispatch_card_block_reads_only_explicit_busy_states():
    game = stub_game(None, "outside")
    image = np.zeros((10, 10, 3), np.uint8)
    assert game.dispatch_card_block(image, TeamPolicy(slot=1)) == ""
    for status, label in (("retreat", "撤退"), ("wounded", "重伤"), ("fighting", "战斗"),
                          ("marching", "行军"), ("garrison", ""), ("unknown", "")):
        game.v.status = status
        assert game.dispatch_card_block(image, TeamPolicy(slot=1)) == label


def test_unknown_card_is_allowed_when_recruiting_is_the_only_possibility():
    """指令里没有攻占时，忙碌只可能是征兵：卡片读不出也放行；有攻占则要求读清。"""
    game = stub_game(None, "unknown")
    image = np.zeros((10, 10, 3), np.uint8)
    policy = TeamPolicy(slot=1, role="line")
    assert game.dispatch_card_block(image, policy, assume_recruiting=True) == ""
    assert game.dispatch_card_block(image, policy, require_known=True) == "未识别"


def test_line_and_transfer_may_pass_a_recruiting_card():
    """拉线到线就拉 / 回城后调动营帐：这两种流程允许“征兵”，占领等仍不提交。"""
    game = stub_game(None, "recruiting")
    image = np.zeros((10, 10, 3), np.uint8)
    policy = TeamPolicy(slot=1, role="line")
    assert game.dispatch_card_block(image, policy) == "征兵"
    assert game.dispatch_card_block(image, policy, allow=("recruiting",)) == ""
    game.v.status = "occupying"
    assert game.dispatch_card_block(image, policy, allow=("recruiting",)) == "占领"


# ---------- 用户 2026-11-13 MVP：忙到线就拉、回城转营帐 ----------

def line_busy_profile(minimum=95):
    point = Point(name="水面", x=617, y=759, group="默认", priority=1, actions=["march"])
    policy = TeamPolicy(slot=1, role="line", minimum=minimum)
    return Profile(teams=[policy], line_targets=[point]), policy


def test_line_busy_always_probes_the_deployment_page():
    """忙（占领/征兵）也要去出征页核对，不能干等；低于线也先去看有没有一键补兵。"""
    profile, policy = line_busy_profile()
    target = line_target(profile, policy)
    above = Observation("main:1", state="active", marker_state="busy",
                        strength=27482, maximum=28200, wounded=False)
    below = Observation("main:1", state="active", marker_state="busy",
                        strength=7778, maximum=28200, wounded=False)
    assert line_decision(policy, above, target=target).action == "line"
    assert line_decision(policy, below, target=target).action == "line"


def test_line_fallback_recalls_and_waits_for_backup(tmp_path):
    worker, profile, _point = make_worker(tmp_path, minimum=95)
    policy = profile.teams[0]
    obs = Observation("main:1", location=(608, 708), wounded=False,
                      strength=10000, maximum=28200)
    game = FakeLineGame(np.zeros((1080, 1920, 3), np.uint8))
    worker._line_fallback(game, policy, obs)
    assert game.recalled and game.recalled[0][0] == "main:1"
    assert policy.key in worker.line_backup
    worker.deleteLater()


def test_line_backup_transfers_to_the_configured_point(tmp_path):
    worker, profile, _point = make_worker(tmp_path)
    policy = profile.teams[0]
    profile.backups = [Point(name="伏波营", x=592, y=655, enabled=True)]
    worker.line_backup.add(policy.key)
    game = FakeLineGame(np.zeros((1080, 1920, 3), np.uint8), fail_dispatch=False)
    obs = Observation("main:1", state="home", location=(331, 562), wounded=False,
                      strength=18000, maximum=28200)
    worker._line_transfer_backup(game, policy, obs)
    assert game.dispatched and game.dispatched[0][0] == "transfer"
    assert game.dispatched[0][1].name == "伏波营"
    assert policy.key not in worker.line_backup
    worker.deleteLater()


def test_process_line_at_home_with_backup_transfers_first(tmp_path):
    """用户 2026-11-13：有补兵点的拉线队，在主城（含自然征兵）就先转补兵点再拉。"""
    worker, profile, _point = make_worker(tmp_path)
    policy = profile.teams[0]
    profile.backups = [Point(name="营帐B", x=660, y=753, enabled=True)]
    game = FakeLineGame(np.zeros((1080, 1920, 3), np.uint8),
                        marker_state="idle", scan_state="home", fail_dispatch=False)
    worker.process_line(game)
    assert game.dispatched and game.dispatched[0][0] == "transfer"
    assert game.dispatched[0][1].name == "营帐B"
    worker.deleteLater()


def test_process_line_after_recall_transfers_home_to_backup(tmp_path):
    worker, profile, _point = make_worker(tmp_path)
    policy = profile.teams[0]
    profile.backups = [Point(name="伏波营", x=592, y=655, enabled=True)]
    worker.line_backup.add(policy.key)
    game = FakeLineGame(np.zeros((1080, 1920, 3), np.uint8),
                        marker_state="idle", scan_state="home", fail_dispatch=False)
    worker.process_line(game)
    assert game.dispatched and game.dispatched[0][0] == "transfer"
    assert game.dispatched[0][1].name == "伏波营"
    assert policy.key not in worker.line_backup
    worker.deleteLater()


def test_dispatch_does_not_submit_when_card_already_retreat_or_wounded(tmp_path):
    for status in ("retreat", "wounded"):
        game = stub_game(tmp_path, status)
        policy = TeamPolicy(slot=1, role="line")
        obs = Observation("main:1", marker_state="stationary", state="outside", wounded=False)
        point = Point(name="拉线目标 (617,759)", x=617, y=759, actions=["occupy", "march"])
        with pytest.raises(DispatchUnconfirmed) as raised:
            game._dispatch(policy, obs, point, "line", reuse_target=True)
        assert raised.value.busy is True
        # 只点了地块指令和队伍卡，没有按“出征”。
        assert game.d.taps and all(tap[:2] != Game.SUBMIT_POINT for tap in game.d.taps)


def test_unknown_page_swipe_clears_selection_and_returns_to_sandbox():
    """2026-11-13 实机：攻城特效+建筑详情被判未知；左右滑动取消选中后恢复沙盘。"""
    game = Game.__new__(Game)
    swipes = []
    game.d = types.SimpleNamespace(swipe=lambda *args, **kwargs: swipes.append(args),
                                   control=types.SimpleNamespace(sleep=lambda seconds: None))
    game.v = types.SimpleNamespace(page=lambda image: "sandbox" if len(swipes) >= 2 else "unknown")
    game.capture = lambda *args, **kwargs: "frame"
    assert game.deselect_unknown_page() is True
    assert len(swipes) == 2
    game.v.page = lambda image: "unknown"
    assert game.deselect_unknown_page() is False


def test_line_refill_emits_event_instead_of_calling_missing_publish():
    """2026-11-13 实机：点完一键补兵后 line_refill 调用了不存在的 self.publish 而崩。"""
    image = np.zeros((1080, 1920, 3), np.uint8)
    events = []
    game = Game.__new__(Game)
    game.v = types.SimpleNamespace(refill_quota=lambda img: 3828,
                                   deployment_strength=lambda img, slot: 26000,
                                   refill_button=lambda img: (1700, 900))
    game.d = types.SimpleNamespace(tap=lambda *args, **kwargs: None)
    game.capture = lambda *args, **kwargs: image
    game.save_image = lambda *args, **kwargs: None
    game.emit = lambda kind, value: events.append((kind, value))
    game.log = lambda *args, **kwargs: None
    after, quota = game.line_refill(image, TeamPolicy(slot=1, role="line"))
    assert quota == 3828 and after is image
    refills = [value for kind, value in events if kind == "refill"]
    assert refills and refills[0]["quota"] == 3828

