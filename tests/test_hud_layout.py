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


def test_layouts_cover_the_measured_building_shift():
    # 用户截图实测：建筑内队伍左移 25px；列表必须包含 0 和 -25，且残差能连成一段。
    assert HUD_OFFSETS[0] == 0
    assert -25 in HUD_OFFSETS
    assert sorted(HUD_OFFSETS) == sorted(set(HUD_OFFSETS))


def test_shifted_badge_in_building_is_read_with_its_own_layout():
    reader = TeamMarkerReader()
    image = make_frame([(1, -25, "busy-1.png")])
    result = reader.read_slot(image, 1)
    assert result["state"] == "busy"
    assert result["hud_dx"] == -25
    assert result["matched_score"] >= .85


def test_same_frame_mixes_shifted_and_normal_rows():
    """一队在建筑里（左移），一队在主城（原位）：各读各的，不能整列一起偏。"""
    reader = TeamMarkerReader()
    image = make_frame([(1, -25, "busy-1.png"), (2, 0, "training-1.png")])
    first = reader.read_slot(image, 1)
    second = reader.read_slot(image, 2)
    assert (first["state"], first["hud_dx"]) == ("busy", -25)
    assert (second["state"], second["hud_dx"]) == ("training", 0)


def test_empty_shifted_row_stays_idle():
    reader = TeamMarkerReader()
    image = make_frame([(1, -25, "busy-1.png")])
    # 第 3 行既没有角标也没有字形：空闲，不能因为布局候选多就误报。
    assert reader.read_slot(image, 3)["state"] == "idle"


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
