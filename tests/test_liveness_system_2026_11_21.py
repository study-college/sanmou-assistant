"""2026-11-21 系统性改造回归：让识别小错误在结构上不可能导致停摆。

- 输入净化：选中行金底归一化（full_team_rows 内调用）
- 导航：缺行等距插值补位 + 点击后金底校验（不再单帧判 absent）
- 采集：capture_stable 稳定帧
- 自检：拉线队可派未动作两轮解冷却 + 告警
- 复拉：拉线结束队兵力恢复后自动清结束标记
"""
from __future__ import annotations

import time
import types

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from sanmou_assistant.game import Game
from sanmou_assistant.models import Observation
from sanmou_assistant.vision import Vision
from sanmou_assistant.worker import TeamAbsent

from test_line_loop_2026_11_19 import FakeGame, FakeJournal, make_worker


def row(x, y):
    return types.SimpleNamespace(x=x, y=y)


def test_row_position_prefers_ocr_and_interpolates_missing_slot():
    rows = {1: row(126, 222), 2: row(127, 335), 3: row(128, 448), 5: row(128, 674)}
    # 命中直接用 OCR 位置
    assert Game.full_row_position("team", rows, 2) == (127, 335)
    # 缺 4 行：由相邻行等距插值补出，而不是判队伍不存在
    x, y = Game.full_row_position("team", rows, 4)
    assert x == 128 and 540 <= y <= 580
    # 整页 OCR 失效：页型几何兜底
    assert Game.full_row_position("team", {}, 4) == (126, 561)
    assert Game.full_row_position("overview", {}, 2) == (264, 437)


def make_select_game(image, rows, selected):
    game = Game.__new__(Game)
    game.v = types.SimpleNamespace(
        page=lambda im: "team",
        full_team_rows=lambda im: dict(rows),
    )
    game.d = types.SimpleNamespace(tap=lambda x, y, s: game.taps.append((x, y)))
    game.taps = []
    game.capture_stable = lambda: image
    game.save_image = lambda *args, **kwargs: None
    game.selected_checks = []
    game.selected_full_slot = lambda im, slot, y: selected
    return game


def test_missing_ocr_row_still_clicks_interpolated_position():
    # 真帧金底在第 4 行：即使 OCR 缺 4 行，也按插值位置点击并确认，而不是直接缺席。
    image = np.zeros((1080, 1920, 3), np.uint8)
    rows = {1: row(126, 222), 2: row(127, 335), 3: row(128, 448), 5: row(128, 674)}
    game = make_select_game(image, rows, selected=True)
    game.select_full_team_slot(image, 4)
    assert len(game.taps) == 1
    assert 540 <= game.taps[0][1] <= 580


def test_absent_only_after_three_clicks_without_selection():
    image = np.zeros((1080, 1920, 3), np.uint8)
    game = make_select_game(image, {}, selected=False)
    with pytest.raises(TeamAbsent):
        game.select_full_team_slot(image, 4)
    assert len(game.taps) == 3, "连续 3 次点击都无选中才允许判定未拥有"


def test_capture_stable_rejects_transition_frames():
    game = Game.__new__(Game)
    a = np.zeros((1080, 1920, 3), np.uint8)
    b = np.full((1080, 1920, 3), 255, np.uint8)
    frames = iter([a, b, b, b])
    game.capture = lambda *args, **kwargs: next(frames)
    game.d = types.SimpleNamespace(control=types.SimpleNamespace(sleep=lambda s: None))
    result = game.capture_stable()
    assert result is b, "过渡帧 A 被丢弃，采纳与后帧一致的 B"


def test_line_idle_unlocks_cooldown_after_two_rounds(tmp_path):
    app = QApplication.instance() or QApplication([])
    worker, _profile, policy = make_worker(tmp_path)
    logs = []
    game = types.SimpleNamespace(log=lambda msg, level="INFO": logs.append((level, msg)))
    worker.retry_at[f"line:{policy.key}"] = time.time() + 180
    worker._line_idle(game, policy, "读取未完成")
    assert f"line:{policy.key}" in worker.retry_at, "第一次未动作不干预"
    worker._line_idle(game, policy, "读取未完成")
    assert f"line:{policy.key}" not in worker.retry_at, "连续两轮未动作必须解除冷却"
    assert any(level == "WARN" for level, _ in logs)
    worker._line_alive(policy)
    assert worker.idle_rounds.get(f"line:{policy.key}", 0) == 0
    worker.deleteLater()
    app.processEvents()


def test_finished_line_team_resumes_after_strength_recovers(tmp_path):
    app = QApplication.instance() or QApplication([])
    worker, _profile, policy = make_worker(tmp_path)
    worker.line_finished.add(policy.key)
    game = FakeGame(strength=30000, maximum=30000)
    game.ensure_sandbox = lambda **kwargs: None
    worker.process_line(game)
    assert policy.key not in worker.line_finished, "兵力恢复后必须清结束标记"
    assert any("重新启动拉线" in msg for _level, msg in game.logs)
    worker.deleteLater()
    app.processEvents()


def test_finished_line_team_stays_finished_while_strength_low(tmp_path):
    app = QApplication.instance() or QApplication([])
    worker, _profile, policy = make_worker(tmp_path)
    worker.line_finished.add(policy.key)
    game = FakeGame(strength=10000, maximum=30000)
    game.ensure_sandbox = lambda **kwargs: None
    worker.process_line(game)
    assert policy.key in worker.line_finished, "兵力未恢复继续等待，不盲目复拉"
    worker.deleteLater()
    app.processEvents()
