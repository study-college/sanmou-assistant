"""拉线模块、弹窗层、按队补兵开关：状态机与真帧测试（交接文档 69，2026-11-12）。

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
from sanmou_assistant.models import (Observation, Profile, TeamPolicy, decide, line_decision,
                                     line_target, parse_target)
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


def line_team(target="600,700", **kwargs):
    return TeamPolicy(slot=1, role="line", line_target=target, **kwargs)


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


# ---------- 拉线状态机 7 条 ----------

def test_line_1_win_in_place_dispatches_again():
    obs = Observation("main:1", marker_state="stationary", state="outside", strength=30000, maximum=33000)
    assert line_decision(line_team(), obs).action == "line"


def test_line_2_after_defeat_retreat_continues():
    obs = Observation("main:1", marker_state="idle", state="home", strength=30000, maximum=33000)
    assert line_decision(line_team(), obs).action == "line"


def test_line_3_quick_refill_when_button_present():
    obs = Observation("main:1", marker_state="stationary", state="outside", strength=10000, maximum=33000)
    assert line_decision(line_team(), obs, refill_button=True).action == "refill"


def test_line_4_button_missing_falls_back():
    obs = Observation("main:1", marker_state="stationary", state="outside", strength=10000, maximum=33000)
    assert line_decision(line_team(), obs, refill_button=False).action == "recall"
    assert line_decision(line_team(refill_fallback="stop"), obs, refill_button=False).action == "wait"


def test_line_5_home_single_read_confirms_full():
    obs = Observation("main:1", marker_state="idle", state="home", strength=33000, maximum=33000)
    assert line_decision(line_team(), obs).action == "line"


def test_line_6_natural_recruit_reaches_floor_continues():
    obs = Observation("main:1", marker_state="transfer_stay", state="outside", strength=30000, maximum=33000)
    assert line_decision(line_team(), obs).action == "line"


def test_line_7_manual_stop_ends_round():
    obs = Observation("main:1", marker_state="stationary", state="outside", strength=30000, maximum=33000)
    assert line_decision(line_team(), obs, stopped=True).action == "wait"


def test_line_owned_target_keeps_marching_with_warning_reason():
    obs = Observation("main:1", marker_state="stationary", state="outside", strength=30000, maximum=33000)
    decision = line_decision(line_team(), obs, target_owned=True)
    assert decision.action == "line" and "无战果" in decision.reason


def test_line_coordinate_parsing_and_validation():
    assert parse_target("602,707") == (602, 707)
    assert parse_target("602，707") == (602, 707)
    assert parse_target("abc") is None
    bad = TeamPolicy(slot=1, role="line", line_target="")
    assert any("拉线" in error for error in Profile(teams=[bad]).validate())
    assert line_target(TeamPolicy(slot=1, role="line", line_target="602,707")) == (602, 707)


def test_decide_routes_line_role_to_line_loop():
    decision = decide(Profile(), line_team(), Observation("main:1"), None)
    assert "拉线" in decision.reason


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
    # 区块整个消失时读不到按钮与数值：走兜底，不能当“点了没反应”重试。
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
    policy = TeamPolicy(slot=1, role="line", line_target="600,700", auto_refill_pending=True)
    game = Game(device, vision, Profile(), None, tmp_path, lambda *a: None)
    assert game.sync_auto_refill(on, policy) is True
    assert policy.auto_refill is True
    assert device.taps == []           # dry-run：一致时不点
    assert policy.auto_refill_pending is None


def test_auto_refill_writes_only_when_different(vision, tmp_path):
    off, on = frame("team-auto-refill-off.jpg"), frame("team-auto-refill-on.jpg")
    device = FakeDevice([on])          # 点击后复读返回“开”
    policy = TeamPolicy(slot=1, role="line", line_target="600,700", auto_refill_pending=True)
    game = Game(device, vision, Profile(), None, tmp_path, lambda *a: None)
    assert game.sync_auto_refill(off, policy) is True
    assert len(device.taps) == 1
    assert policy.auto_refill is True and policy.auto_refill_pending is None


def test_auto_refill_unknown_frame_is_not_written(vision, tmp_path):
    blank = np.full((1080, 1920, 3), 30, np.uint8)
    device = FakeDevice([blank])
    policy = TeamPolicy(slot=1, role="line", line_target="600,700", auto_refill_pending=True)
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
