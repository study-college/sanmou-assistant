"""拉线模块、弹窗层、按队补兵开关：状态机与真帧测试（交接文档 69，2026-11-12）。

拉线目标与武勋目标同一套口径：坐标点 + 目标组 + 顺序（用户 2026-11-13）。
真帧来自 artifacts/teach-20261112 的小型裁切副本 tests/fixtures/teach-2026-11-12/；
该目录是本机真帧，不随公开源码分发，缺失时对应用例自动跳过。
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import cv2
import numpy as np
import pytest

from sanmou_assistant.game import Game
from sanmou_assistant.locations import parse_addresses
from sanmou_assistant.models import (Observation, Point, Profile, TeamPolicy, decide,
                                     line_decision, line_target)
from sanmou_assistant.vision import Vision

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "teach-2026-11-12"


@pytest.fixture(scope="module")
def vision():
    return Vision()


def frame(name):
    path = FIXTURES / name
    if not path.is_file():
        pytest.skip(f"真帧 fixture 未随源码分发：{name}")
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None, name
    return image


class FakeDevice:
    """只记录点击、按顺序返回帧的假设备；不连接游戏。"""

    def __init__(self, frames):
        self.frames = list(frames)
        self.taps = []
        self.control = types.SimpleNamespace(sleep=lambda seconds: None)

    def capture(self, *args, **kwargs):
        return self.frames[0] if len(self.frames) == 1 else self.frames.pop(0)

    def tap(self, *args, **kwargs):
        self.taps.append(args)


def line_setup(x=600, y=700, *, enabled=True, group="默认", **policy_kwargs):
    """构造一个拉线队 + 一个拉线目标点（坐标点 + 目标组，与武勋目标同构）。"""
    point = Point(name=f"目标({x},{y})", x=x, y=y, group=group, priority=1, enabled=enabled,
                  actions=["occupy", "march"])
    policy = TeamPolicy(slot=1, role="line", target_group=group, **policy_kwargs)
    return Profile(teams=[policy], line_targets=[point]), policy


def decision(profile, policy, obs, **kwargs):
    return line_decision(policy, obs, target=line_target(profile, policy), **kwargs)


def moving(**overrides):
    fields = {"marker_state": "stationary", "state": "outside", "strength": 30000, "maximum": 33000}
    fields.update(overrides)
    return Observation("main:1", **fields)


# ---------- 拉线状态机 7 条 ----------

def test_line_1_win_in_place_dispatches_again():
    profile, policy = line_setup()
    assert decision(profile, policy, moving()).action == "line"


def test_line_2_after_defeat_retreat_continues():
    profile, policy = line_setup()
    assert decision(profile, policy, moving(marker_state="idle", state="home")).action == "line"


def test_line_3_quick_refill_when_button_present():
    profile, policy = line_setup()
    low = moving(strength=10000)
    assert decision(profile, policy, low, refill_button=True).action == "refill"


def test_line_4_button_missing_falls_back():
    profile, policy = line_setup()
    low = moving(strength=10000)
    assert decision(profile, policy, low, refill_button=False).action == "recall"
    stop_profile, stop_policy = line_setup(refill_fallback="stop")
    assert decision(stop_profile, stop_policy, low, refill_button=False).action == "wait"


def test_line_5_home_single_read_confirms_full():
    profile, policy = line_setup()
    full = moving(marker_state="idle", state="home", strength=33000)
    assert decision(profile, policy, full).action == "line"


def test_line_6_natural_recruit_reaches_floor_continues():
    profile, policy = line_setup()
    assert decision(profile, policy, moving(marker_state="transfer_stay")).action == "line"


def test_line_7_manual_stop_ends_round():
    profile, policy = line_setup()
    assert decision(profile, policy, moving(), stopped=True).action == "wait"


def test_line_owned_target_keeps_marching_with_warning_reason():
    profile, policy = line_setup()
    result = decision(profile, policy, moving(), target_owned=True)
    assert result.action == "line" and "无战果" in result.reason


def test_line_waits_when_team_state_not_read():
    """读失败（没有角标）不能当成“可以拉”。"""
    profile, policy = line_setup()
    assert decision(profile, policy, Observation("main:1")).action == "wait"
    assert decision(profile, policy, Observation("main:1", marker_state="unknown")).action == "wait"
    assert decision(profile, policy, moving(marker_state="march")).action == "wait"


def test_line_target_uses_shared_address_parsing():
    """拉线目标复用游戏分享地址，不再另造只认 x,y 的格式。"""
    address = parse_addresses("水面(617,759)")[0]
    assert (address.name, address.x, address.y) == ("水面", 617, 759)
    profile, policy = line_setup(617, 759)
    point = line_target(profile, policy)
    assert (point.x, point.y) == (617, 759)
    empty, empty_policy = line_setup(617, 759, enabled=False)
    assert line_target(empty, empty_policy) is None
    assert any("拉线目标" in error for error in empty.validate(active=True))


def test_decide_routes_line_role_to_line_loop():
    _profile, policy = line_setup()
    assert "拉线" in decide(Profile(), policy, Observation("main:1"), None).reason


# ---------- 旧配置迁移 ----------

def test_legacy_team_line_target_migrates_to_line_targets():
    legacy = {"schema": 1, "name": "旧配置", "account": "测试",
              "teams": [{"slot": 1, "role": "line", "line_target": "水面(617,759)"},
                        {"slot": 2, "role": "idle", "line_target": "600,700"}]}
    profile = Profile.from_dict(legacy)
    assert ("水面", 617, 759) in [(p.name, p.x, p.y) for p in profile.line_targets]
    assert not hasattr(profile.teams[0], "line_target")
    assert profile.validate() == []


# ---------- 弹窗层真帧 ----------

def test_enemy_warning_frame_is_new_page_type(vision):
    image = frame("popup-enemy-warning.jpg")
    assert vision.page(image) == "enemy_warning"
    info = vision.enemy_warning(image)
    assert info["continue_point"] and info["avoid_point"]
    assert info["countdown"] == 2
    assert info["checkbox"]        # 「该等级今日不再提示」复选框必须能定位


def test_confirm_frames_are_new_page_type(vision):
    unleveled = frame("popup-confirm-unleveled.jpg")
    assert vision.page(unleveled) == "dispatch_confirm"
    assert vision.confirm_dialog(unleveled)["kind"] == "unleveled"
    unfull = frame("popup-confirm-unfull-skill.jpg")
    assert vision.page(unfull) == "dispatch_confirm"
    assert vision.confirm_dialog(unfull)["kind"] == "unfull_skill"


# ---------- 快速征兵读取与回执 ----------

def test_quick_refill_reads_quota_3828_and_zero(vision):
    before = frame("deployment-refill-3828.jpg")
    assert vision.refill_button(before) is not None
    assert vision.refill_quota(before) == 3828
    assert vision.refill_quota(frame("deployment-refill-zero.jpg")) == 0


def test_quick_refill_receipt_22796_to_26639(vision):
    montage = frame("refill-receipt.jpg")
    text = vision.joined(montage, (0, 0, montage.shape[1], montage.shape[0])).replace(" ", "")
    assert "22796" in text and "26639" in text


def test_no_quick_refill_button_when_block_absent(vision):
    blank = np.full((1080, 1920, 3), 30, np.uint8)
    assert vision.refill_button(blank) is None
    assert vision.refill_quota(blank) is None


# ---------- 按队同步「回城自动补兵」 ----------

def test_auto_refill_frames_judged_both_ways(vision):
    on = frame("team-auto-refill-on.jpg")
    assert vision.checked(on, *vision.refill_checkbox(on)) is True
    off = frame("team-auto-refill-off.jpg")
    assert vision.checked(off, *vision.refill_checkbox(off)) is False


def test_auto_refill_consistent_value_is_not_clicked(vision, tmp_path):
    on = frame("team-auto-refill-on.jpg")
    device = FakeDevice([on])
    policy = TeamPolicy(slot=1, role="line", auto_refill_pending=True)
    game = Game(device, vision, Profile(), None, tmp_path, lambda *a: None)
    assert game.sync_auto_refill(on, policy) is True
    assert policy.auto_refill is True
    assert device.taps == []           # dry-run：一致时不点
    assert policy.auto_refill_pending is None


def test_auto_refill_writes_only_when_different(vision, tmp_path):
    off, on = frame("team-auto-refill-off.jpg"), frame("team-auto-refill-on.jpg")
    device = FakeDevice([on])          # 点击后复读返回“开”
    policy = TeamPolicy(slot=1, role="line", auto_refill_pending=True)
    game = Game(device, vision, Profile(), None, tmp_path, lambda *a: None)
    assert game.sync_auto_refill(off, policy) is True
    assert len(device.taps) == 1
    assert policy.auto_refill is True and policy.auto_refill_pending is None


def test_auto_refill_unknown_frame_is_not_written(vision, tmp_path):
    blank = np.full((1080, 1920, 3), 30, np.uint8)
    device = FakeDevice([blank])
    policy = TeamPolicy(slot=1, role="line", auto_refill_pending=True)
    game = Game(device, vision, Profile(), None, tmp_path, lambda *a: None)
    assert game.sync_auto_refill(blank, policy) is None   # 界面未知：不写、不猜
    assert device.taps == []
    assert policy.auto_refill_pending is True


# ---------- 旧配置兼容 ----------

def test_legacy_profile_without_line_behaves_unchanged():
    example = Path(__file__).resolve().parents[1] / "profiles" / "current-account.example.json"
    profile = Profile.from_dict(json.loads(example.read_text(encoding="utf-8")))
    assert not any(team.role == "line" for team in profile.teams)
    assert profile.validate() == []
