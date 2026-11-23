"""2026-11-19 隐藏面板全勾选：七项复选框识别与逐项勾选。"""
from __future__ import annotations

import types

import cv2
import numpy as np

from sanmou_assistant.game import Game
from sanmou_assistant.vision import Text, Vision

LABELS = ["隐藏部队模型", "隐藏部队名字", "隐藏行军线", "隐藏建筑名字",
          "隐藏战术指令", "隐藏同盟计划", "最低视角时自动触发设置"]


def test_hide_option_checked_reads_large_green_check():
    grey = np.full((100, 100, 3), 46, np.uint8)
    assert Vision.hide_option_checked(grey, 50, 50) is False
    checked = grey.copy()
    cv2.rectangle(checked, (35, 45), (65, 55), (87, 213, 131), -1)  # BGR 绿勾
    assert Vision.hide_option_checked(checked, 50, 50) is True
    black = np.zeros((100, 100, 3), np.uint8)
    assert Vision.hide_option_checked(black, 50, 50) is None


def test_hide_panel_visible_uses_anchor_label():
    image = np.zeros((1080, 1920, 3), np.uint8)
    stub = types.SimpleNamespace(
        find=lambda _image, labels, rect=None, exact=False: Text("隐藏同盟计划", .99, 1493, 712, (0, 0, 0, 0)))
    assert Vision.hide_panel_visible(stub, image) is True
    stub.find = lambda *args, **kwargs: None
    assert Vision.hide_panel_visible(stub, image) is False


def test_hide_option_rows_includes_two_line_last_row():
    game = Game.__new__(Game)
    y_by_label = {label: 312 + index * 80 for index, label in enumerate(LABELS[:6])}
    game.v = types.SimpleNamespace(
        hide_panel_visible=lambda image: True,
        find=lambda _image, labels, rect=None, exact=False: (
            Text(labels[0], .99, 1493, y_by_label[labels[0]], (0, 0, 0, 0)) if labels[0] in y_by_label else None),
        joined=lambda _image, rect: "最低视角时自动触发设置",
    )
    rows = game._hide_option_rows(np.zeros((1080, 1920, 3), np.uint8))
    assert rows is not None and len(rows) == 7
    assert rows[0][1] == (game.HIDE_OPTION_X, 312)
    assert rows[-1] == (LABELS[-1], (game.HIDE_OPTION_X, game.HIDE_OPTION_LAST_Y))


def test_ensure_hide_options_taps_only_unchecked_items():
    game = Game.__new__(Game)
    y_by_label = {label: 312 + index * 80 for index, label in enumerate(LABELS[:6])}
    states = {312: True, 392: True, 472: False, 552: True, 632: True, 712: True, 818: True}
    taps = []
    logs = []

    def state_for(x, y, image=None):
        # 第一次读到第三项未勾选；点击后同一坐标变为已勾选。
        return True if (x, y) in taps else states[y]

    game.v = types.SimpleNamespace(
        hide_panel_visible=lambda image: True,
        find=lambda _image, labels, rect=None, exact=False: (
            Text(labels[0], .99, 1493, y_by_label[labels[0]], (0, 0, 0, 0)) if labels[0] in y_by_label else None),
        hide_option_checked=lambda image, x, y: state_for(x, y),
        joined=lambda _image, rect: "最低视角时自动触发设置",
    )
    game.d = types.SimpleNamespace(tap=lambda x, y, delay=0: taps.append((round(x), round(y))),
                                   control=types.SimpleNamespace(sleep=lambda _s: None))
    game.capture = lambda *args: np.zeros((1080, 1920, 3), np.uint8)
    game.log = lambda message, level="INFO": logs.append((level, message))
    game._ensure_hide_options(np.zeros((1080, 1920, 3), np.uint8))
    assert taps == [(game.HIDE_OPTION_X, 472)]
    assert any("已勾选" in message for _level, message in logs)
    assert any("全部勾选" in message for _level, message in logs)