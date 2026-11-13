"""角标“无角标＝空闲”的防抖（2026-11-13 实机：1 队在目标点被读成空闲）。

空闲在游戏里是“没有角标”，但到点/开战的一瞬角标也会短暂消失；
上一状态还是行军/调动/撤退/战斗/忙碌时，单帧“无角标”不能当回主城。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np

from sanmou_assistant.game import Game


class FakeReader:
    def __init__(self, states):
        self.states = list(states)
        self.calls = 0

    def avatars_visible(self, image):
        return True

    def read_slot(self, image, slot):
        self.calls += 1
        state = self.states[min(self.calls - 1, len(self.states) - 1)]
        return {"slot": slot, "state": state, "label": state,
                "reason": "test", "confidence": 0.9}


def make_game(states):
    game = Game.__new__(Game)
    game.marker_last = {1: ("march", time.time() - 60)}
    game.marker_frame = None
    reader = FakeReader(states)
    frames = [np.zeros((1080, 1920, 3), np.uint8), np.ones((1080, 1920, 3), np.uint8)]
    game.marker_reader = lambda: reader
    game.v = SimpleNamespace(frame_clear=lambda image, verify=True: True,
                             team_wounded=lambda image, slot: False)
    game.d = SimpleNamespace(control=SimpleNamespace(sleep=lambda seconds: None))
    game.capture = lambda *args: frames.pop(0)
    game.log = lambda message, level="INFO": None
    return game, reader


def test_idle_after_moving_is_rechecked_once():
    game, reader = make_game(["idle", "march"])
    marker = game.read_team_marker(np.zeros((1080, 1920, 3), np.uint8), 1)
    assert marker["state"] == "march"
    assert reader.calls == 2
    assert game.marker_last[1][0] == "march"


def test_two_consecutive_idle_reads_are_accepted():
    game, reader = make_game(["idle", "idle"])
    marker = game.read_team_marker(np.zeros((1080, 1920, 3), np.uint8), 1)
    assert marker["state"] == "idle"
    assert reader.calls == 2
    assert game.marker_last[1][0] == "idle"
