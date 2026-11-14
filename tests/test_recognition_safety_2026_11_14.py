"""识别失败与提交前安全（2026-11-14）。

- `read_team_marker` / `ensure_hide_on`：停止（Stopped）与断连（TransportUnavailable）
  必须原样传播，不能被 `except Exception` 吞成“读不到/本轮继续”；
- `read_merit`：个人信息页未确认时，恢复途中 ensure_sandbox 抛出的真实错误
  （异地登录/维护/重连作废）原样传播，不降级成“本轮不读武勋”；
- `refill_quota`：读不到返回 None，0 只能来自明确识别到的数字 0，
  OCR 抖动不会触发“额度归零”回城（调用方只把 ==0 判为归零）。

全部为 stub 测试，不依赖真帧、不连接设备。
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from sanmou_assistant.game import Game, GameError, RecognitionUnavailable
from sanmou_assistant.transport import Stopped, TransportUnavailable
from sanmou_assistant.vision import Text, Vision

FRAME = np.zeros((1080, 1920, 3), dtype=np.uint8)


# ---------- read_team_marker：停止/断连不吞 ----------

class _RaisingReader:
    def __init__(self, exc):
        self.exc = exc

    def avatars_visible(self, image):
        return True

    def read_slot(self, image, slot):
        raise self.exc


def marker_game(exc):
    game = Game.__new__(Game)
    game.marker_frame = None
    game.marker_last = {}
    game.active_slots = None
    game._marker_reader = _RaisingReader(exc)
    game.v = SimpleNamespace(frame_clear=lambda img: True,
                             team_wounded=lambda img, slot: False)
    game.d = SimpleNamespace(control=SimpleNamespace(sleep=lambda seconds: None))
    game.capture = lambda *args: FRAME
    game.log = lambda *args, **kwargs: None
    return game


def test_read_team_marker_propagates_stopped():
    with pytest.raises(Stopped):
        marker_game(Stopped()).read_team_marker(FRAME, 1)


def test_read_team_marker_propagates_transport_unavailable():
    with pytest.raises(TransportUnavailable):
        marker_game(TransportUnavailable("adb 断连")).read_team_marker(FRAME, 1)


def test_read_team_marker_still_swallows_plain_recognition_errors():
    # 普通识别异常维持原语义：记日志、返回 None，由调用方按“读不到”处理。
    assert marker_game(ValueError("模板损坏")).read_team_marker(FRAME, 1) is None


# ---------- ensure_hide_on：停止/断连不吞 ----------

def hide_game(exc):
    game = Game.__new__(Game)
    def capture(*args):
        raise exc
    game.capture = capture
    game.log = lambda *args, **kwargs: None
    return game


def test_ensure_hide_on_propagates_stopped():
    with pytest.raises(Stopped):
        hide_game(Stopped()).ensure_hide_on()


def test_ensure_hide_on_propagates_transport_unavailable():
    with pytest.raises(TransportUnavailable):
        hide_game(TransportUnavailable("adb 断连")).ensure_hide_on()


def test_ensure_hide_on_still_propagates_game_error():
    with pytest.raises(RecognitionUnavailable):
        hide_game(RecognitionUnavailable("页面未确认")).ensure_hide_on()


def test_ensure_hide_on_still_swallows_plain_errors():
    assert hide_game(ValueError("识别资源缺失")).ensure_hide_on() is None


# ---------- read_merit：恢复中的真实错误原样传播 ----------

def merit_game(pages, sandbox_results):
    game = Game.__new__(Game)
    game.v = SimpleNamespace(page=lambda img, verify=True: next(pages))
    game.d = SimpleNamespace(tap=lambda *args: None)
    game.capture_clear = lambda *args, **kwargs: FRAME
    game.save_image = lambda *args: None
    calls = []

    def ensure_sandbox(*args, **kwargs):
        result = sandbox_results[min(len(calls), len(sandbox_results) - 1)]
        calls.append(1)
        if isinstance(result, Exception):
            raise result
        return result

    game.ensure_sandbox = ensure_sandbox
    return game


def test_read_merit_propagates_recovery_game_error():
    # 个人信息页没打开、ensure_sandbox 恢复途中读到“服务器维护”：必须原样传播，
    # 不能吞掉后降级成 RecognitionUnavailable 把停止延后一拍。
    game = merit_game(iter(["sandbox", "unknown"]),
                      [FRAME, GameError("游戏服务器维护，已停止任务；维护结束后手动开始")])
    with pytest.raises(GameError, match="维护"):
        game.read_merit()


def test_read_merit_still_downgrades_when_recovery_returns_cleanly():
    game = merit_game(iter(["sandbox", "city"]), [FRAME])
    with pytest.raises(RecognitionUnavailable, match="本轮不读武勋"):
        game.read_merit()


# ---------- refill_quota：读不到是 None，不是 0 ----------

def quota_with(header, spans):
    stub = SimpleNamespace(
        find=lambda image, labels, rect=None, exact=False: header,
        read=lambda image, rect, minimum=.62: spans,
    )
    return Vision.refill_quota(stub, FRAME)


def test_refill_quota_none_when_header_present_but_value_unread():
    # 表头在、数值没读出来（OCR 抖动/数值被遮挡）：None。旧实现在这里返回 0，
    # 会把抖动误判成“额度归零”而触发回城。
    header = Text("消耗辎重", .9, 0, 0, (0, 0, 0, 0))
    assert quota_with(header, []) is None
    assert quota_with(header, [Text("辎重", .6, 0, 0, (0, 0, 0, 0))]) is None


def test_refill_quota_zero_requires_recognised_zero():
    header = Text("消耗辎重", .9, 0, 0, (0, 0, 0, 0))
    assert quota_with(header, [Text("0", .9, 0, 0, (0, 0, 0, 0))]) == 0
    assert quota_with(header, [Text("3828", .9, 0, 0, (0, 0, 0, 0))]) == 3828


def test_refill_quota_none_without_header():
    assert quota_with(None, [Text("0", .9, 0, 0, (0, 0, 0, 0))]) is None
