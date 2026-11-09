from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from sanmou_assistant.game import Game, RecognitionUnavailable
from sanmou_assistant.models import Profile, TeamPolicy
from sanmou_assistant.vision import Vision


ROOT = Path(__file__).resolve().parents[1]


def _sample(root, rel):
    """实机样本按 7 天滚动淘汰；文件已过期时跳过，而不是让整包测试报红。"""
    path = root / rel
    if not path.is_file():
        pytest.skip(f"实机样本已按截图策略淘汰：{rel}")
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def test_team_entry_has_no_page_number_dependency():
    # 2026-11-09 根因：队伍入口曾用固定屏幕区域的近白像素猜“第 2 页”。
    # 整个页码概念从队伍读取链路移除，避免地图底色再次决定能否读队。
    assert not hasattr(Vision, "portrait_page")
    assert not hasattr(Vision, "portrait_pager_present")
    assert not hasattr(Game, "main_portraits")


def test_open_team_page_reuses_page_already_open(tmp_path):
    taps = []
    device = SimpleNamespace(tap=lambda *a: taps.append(a),
                             control=SimpleNamespace(sleep=lambda _: None))
    vision = SimpleNamespace(page=lambda image, verify=True: "team")
    game = Game(device, vision, Profile(), None, tmp_path, lambda *a: None)
    game.ensure_sandbox = lambda **_: "frame"
    assert game.open_team_page(1) == "frame"
    assert taps == []


def test_open_team_page_accepts_page_opened_from_avatar_tap(tmp_path):
    taps = []
    device = SimpleNamespace(tap=lambda *a: taps.append(a),
                             control=SimpleNamespace(sleep=lambda _: None))
    vision = SimpleNamespace(page=lambda image, verify=True: image)
    game = Game(device, vision, Profile(), None, tmp_path, lambda *a: None)
    game.ensure_sandbox = lambda **_: "sandbox"
    game.capture = lambda *args: "captured"
    game.wait_transition = lambda image, expected: "team"
    assert game.open_team_page(2) == "team"
    assert taps[0][:2] == (1840, 383)


def test_open_team_page_fails_per_team_without_guessing(tmp_path):
    taps = []
    device = SimpleNamespace(tap=lambda *a: taps.append(a),
                             control=SimpleNamespace(sleep=lambda _: None))
    vision = SimpleNamespace(page=lambda image, verify=True: "sandbox")
    game = Game(device, vision, Profile(), None, tmp_path, lambda *a: None)
    game.ensure_sandbox = lambda **_: "sandbox"
    game.capture = lambda *args: "captured"
    game.wait_transition = lambda image, expected: "sandbox"
    game.save_image = lambda *args: None
    with pytest.raises(RecognitionUnavailable, match="未打开第 2 队"):
        game.open_team_page(2)
    # 最多 3 次进入尝试，每次“第一下 + 仍在沙盘再点一下”；失败只影响本队。
    assert len(taps) == 6


def test_2026_11_09_real_fault_frame_is_sandbox_without_page_number():
    # 事故帧：3 队、无分页条、右下角是浅色地基。旧代码把它当“第 2 页”拒绝三队。
    image = _sample(ROOT, "artifacts/diagnosis-2026-11-09-pager/"
                          "fault-portrait-page-unreadable.png")
    vision = Vision()
    assert vision.page(image) == "sandbox"
    covered = image.copy()
    covered[841:889, 1770:1910] = 0
    assert vision.page(covered) == "sandbox"


def test_real_stationary_merit_team_is_not_marching_because_of_tile_menu(tmp_path):
    root = ROOT
    v = Vision()
    image = _sample(root, 'data/sessions/20261018-112345/152357_016680-main_1-located.png')
    policy = TeamPolicy(slot=1)
    game = Game(None, v, Profile(), SimpleNamespace(phase=lambda _: 'merit'), tmp_path, lambda *a: None)
    assert v.stationary_return_visible(image, 1)
    assert game.read_outside_state(image, policy, (636, 286)) == 'outside'
