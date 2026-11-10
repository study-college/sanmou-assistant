"""补给提醒弹窗：勾选“今日登录不再提示”并确认（2026-11-10 实机帧）。"""
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from sanmou_assistant.game import Game
from sanmou_assistant.models import TeamPolicy
from sanmou_assistant.vision import Vision

ROOT = Path(__file__).resolve().parents[1]
FRAME = ROOT / "artifacts" / "fix-2026-11-10-supply" / "supply-prompt-80.png"


def load_frame():
    return cv2.imdecode(np.fromfile(str(FRAME), dtype=np.uint8), cv2.IMREAD_COLOR)


def make_game(image):
    game = Game.__new__(Game)
    game.v = Vision()
    taps, events, logs = [], [], []
    game.d = SimpleNamespace(tap=lambda x, y, *args: taps.append((x, y)))
    game.capture = lambda *args, **kwargs: image
    game.save_image = lambda *args, **kwargs: None
    game.emit = lambda kind, value: events.append((kind, value))
    game.log = lambda message, level="INFO": logs.append((level, message))
    return game, taps, events, logs


def test_supply_prompt_checks_daily_silence_and_confirms():
    image = load_frame()
    game, taps, events, logs = make_game(image)
    assert game.handle_supply_prompt(image, TeamPolicy(slot=1), "(40,465)") is True
    assert len(events) == 1 and events[0][0] == "supply_prompt"
    assert "补给少于80" in events[0][1]["text"]
    # 第一下点复选框，第二下点“确定”；核对的是“处理了这件事”，不依赖勾选后的下一帧。
    assert len(taps) == 2
    assert 790 <= taps[0][0] <= 900 and 590 <= taps[0][1] <= 670
    assert 1080 <= taps[1][0] <= 1280 and 640 <= taps[1][1] <= 740
    assert any("补给提醒" in message for _, message in logs)


def test_non_supply_page_is_not_treated_as_prompt():
    game, taps, events, logs = make_game(np.zeros((1080, 1920, 3), dtype=np.uint8))
    assert game.handle_supply_prompt(np.zeros((1080, 1920, 3), dtype=np.uint8),
                                     TeamPolicy(slot=1), "(40,465)") is False
    assert taps == [] and events == [] and logs == []
