"""右侧队伍 HUD 的两套布局（2026-11-13 用户截图确认）。

选中建筑物后，驻扎在该建筑里的队伍整行左移 ~25px（头像/血条/角标一起），
不在该建筑的队伍仍在原位。旧读取只按固定 x=1840±10 找角标，建筑内队伍被读成
“未识别”。这里锁定：同一帧里“左移的角标”和“原位的角标”都要读对。
"""
from __future__ import annotations

import types
from pathlib import Path

import cv2
import numpy as np

from sanmou_assistant.game import Game
from sanmou_assistant.team_markers import (BADGE_DY, BADGE_X, HUD_OFFSETS, SLOT_Y,
                                           TeamMarkerReader)

ASSETS = Path(__file__).resolve().parents[1] / "sanmou_assistant" / "assets" / "team-markers"


def template(name):
    image = cv2.imdecode(np.fromfile(ASSETS / name, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None and image.shape[:2] == (58, 58)
    return image


def make_frame(pastes):
    """pastes: [(slot, dx, template_name)]；同时画血条，模拟真实 HUD。"""
    image = np.full((1080, 1920, 3), 30, np.uint8)
    for slot, dx, name in pastes:
        y = SLOT_Y[slot - 1] + BADGE_DY
        image[y:y + 58, BADGE_X + dx:BADGE_X + dx + 58] = template(name)
    for slot, dx, _name in pastes:
        y = SLOT_Y[slot - 1]
        cv2.rectangle(image, (1800 + dx, y + 51), (1884 + dx, y + 56), (60, 220, 60), -1)
    return image


def unknown_frame(dx, plaque=True):
    image = np.full((1080, 1920, 3), 40 if plaque else 80, np.uint8)
    for y in SLOT_Y:
        cv2.rectangle(image, (1780, y + 51), (1900, y + 56), (60, 220, 60), -1)
    y = SLOT_Y[3]
    background = (40, 40, 40) if plaque else (80, 80, 80)
    cv2.rectangle(image, (1780, y + 51), (1900, y + 56), background, -1)
    cv2.rectangle(image, (1800 + dx, y + 51), (1884 + dx, y + 56), (60, 220, 60), -1)
    cx, cy = 1869 + dx, y + BADGE_DY + 29
    if plaque:
        diamond = np.array([[cx + 29, cy], [cx, cy - 29], [cx - 29, cy], [cx, cy + 29]], np.int32)
        cv2.fillPoly(image, [diamond], (20, 20, 20))
    else:
        cv2.circle(image, (cx, cy), 15, (20, 20, 20), -1)
    cv2.circle(image, (cx, cy), 11, (255, 255, 255), -1)
    return image


def test_layouts_cover_the_measured_building_shift():
    # 建筑内整行左移 25px；识别允许测量值附近的小残差，不依赖三套角标窗口竞分。
    assert HUD_OFFSETS[0] == 0
    assert -25 in HUD_OFFSETS
    assert sorted(HUD_OFFSETS) == sorted(set(HUD_OFFSETS))


def test_shifted_badge_in_building_is_read_with_its_own_layout():
    reader = TeamMarkerReader()
    image = make_frame([(1, -25, "busy-1.png")])
    result = reader.read_slot(image, 1)
    assert result["state"] == "busy"
    assert abs(result["hud_dx"] + 25) <= 1
    assert result["matched_score"] >= .85


def test_same_frame_mixes_shifted_and_normal_rows():
    """一队在建筑里（左移），一队在主城（原位）：各读各的，不能整列一起偏。"""
    reader = TeamMarkerReader()
    image = make_frame([(1, -25, "busy-1.png"), (2, 0, "training-1.png")])
    first = reader.read_slot(image, 1)
    second = reader.read_slot(image, 2)
    assert first["state"] == "busy" and abs(first["hud_dx"] + 25) <= 1
    assert second["state"] == "training" and abs(second["hud_dx"]) <= 1


def test_empty_shifted_row_stays_idle():
    reader = TeamMarkerReader()
    image = make_frame([(1, -25, "busy-1.png")])
    y = SLOT_Y[2]
    cv2.rectangle(image, (1800, y + 51), (1884, y + 56), (60, 220, 60), -1)
    assert reader.read_slot(image, 3)["state"] == "idle"


def test_missing_team_row_never_reads_idle():
    reader = TeamMarkerReader()
    image = make_frame([(1, -25, "busy-1.png")])
    assert reader.read_slot(image, 3)["state"] == "unknown"


def test_hud_position_measurement_allows_vertical_bar_offset():
    reader = TeamMarkerReader()
    image = make_frame([(1, 0, "busy-1.png")])
    y = SLOT_Y[0]
    cv2.rectangle(image, (1800, y + 51), (1884, y + 56), (30, 30, 30), -1)
    cv2.rectangle(image, (1800, y + 61), (1884, y + 66), (60, 220, 60), -1)
    result = reader.read_slot(image, 1)
    assert result["state"] == "busy" and abs(result["hud_dx"]) <= 1


def test_unknown_shifted_badge_never_falls_back_to_idle():
    reader = TeamMarkerReader()
    result = reader.read_slot(unknown_frame(-25), 4)
    assert result["state"] == "unknown"
    assert result["reason"] == "角标未识别"


def test_shifted_unclassified_bright_shape_stays_unknown():
    reader = TeamMarkerReader()
    assert reader.read_slot(unknown_frame(-25, plaque=False), 4)["state"] == "unknown"


def test_avatars_visible_covers_the_shifted_hud():
    reader = TeamMarkerReader()
    assert reader.avatars_visible(make_frame([(1, -25, "busy-1.png")]))
    assert not reader.avatars_visible(np.zeros((1080, 1920, 3), np.uint8))


class _StubReader:
    def avatars_visible(self, image):
        return True

    def read_slot(self, image, slot, offset=None):
        return {"slot": slot, "state": "busy", "label": "忙碌", "confidence": .9,
                "reason": "模板匹配"}


def test_marker_frame_is_kept_for_same_frame_wounded_check():
    """重伤要和角标同帧：read_team_marker 记录它实际使用的那一帧。"""
    game = Game.__new__(Game)
    image = np.zeros((1080, 1920, 3), np.uint8)
    game._marker_reader = _StubReader()
    game.active_slots = None
    game.v = types.SimpleNamespace(frame_clear=lambda img: True,
                                   team_wounded=lambda img, slot: False)
    game.log = lambda *args: None
    assert game.read_team_marker(image, 1)["state"] == "busy"
    assert game.marker_frame is image
