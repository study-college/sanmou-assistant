"""2026-11-21 拉线多目标退让 + 签到幂等重试回归。"""
from __future__ import annotations

import time
import types

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from sanmou_assistant.daily_tasks import signin
from sanmou_assistant.game import GameError
from sanmou_assistant.journal import Journal, cycle_key
from sanmou_assistant.models import Point

from test_line_loop_2026_11_19 import FakeGame, make_worker


class FallbackGame(FakeGame):
    """首选目标(741,683)不可用，其余目标正常；记录每次 dispatch 尝试。"""
    unavailable_message = "出征按钮不可用：被攻击的建筑物没有连接地"

    def __init__(self, *args, fail_all=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_all = fail_all
        self.primary_down = True

    def dispatch(self, policy, obs, point, kind):
        if self.fail_all or (self.primary_down and point.x == 741):
            raise GameError(self.unavailable_message)
        self.dispatched.append((kind, point))


def make_two_target_worker(tmp_path):
    worker, profile, policy = make_worker(tmp_path)
    profile.line_targets.append(Point(name="营帐", x=707, y=644, priority=2,
                                      actions=["guard", "march"], enabled=True))
    return worker, profile, policy


def test_unavailable_primary_target_falls_back_to_second(tmp_path):
    app = QApplication.instance() or QApplication([])
    worker, _profile, policy = make_two_target_worker(tmp_path)
    game = FallbackGame(strength=30000, maximum=30000)
    worker.process_line(game)
    # 首选被冷却，第二目标成功派出
    assert worker.line_target_blocked.get((policy.key, 741, 683), 0) > time.time()
    assert game.dispatched and game.dispatched[-1][1].x == 707
    assert policy.key not in worker.line_finished
    assert any("试下一个" in msg for _lv, msg in game.logs)
    worker.deleteLater()
    app.processEvents()


def test_all_targets_blocked_waits_and_recovers_next_round(tmp_path):
    app = QApplication.instance() or QApplication([])
    worker, _profile, policy = make_two_target_worker(tmp_path)
    game = FallbackGame(strength=30000, maximum=30000, fail_all=True)
    worker.process_line(game)
    assert not game.dispatched, "全部不可用本轮不派"
    assert len(worker.line_target_blocked) == 2
    # 冷却到期后自动从头重试并派出（首选目标已恢复可用）
    worker.line_target_blocked.clear()
    game.fail_all = False
    game.primary_down = False
    worker.retry_at.clear()
    worker.process_line(game)
    assert game.dispatched and game.dispatched[-1][1].x == 741
    worker.deleteLater()
    app.processEvents()


def test_unconfirmed_dispatch_does_not_switch_target(tmp_path):
    from sanmou_assistant.game import DispatchUnconfirmed
    app = QApplication.instance() or QApplication([])
    worker, _profile, policy = make_two_target_worker(tmp_path)
    game = FakeGame(strength=30000, maximum=30000,
                    exc=DispatchUnconfirmed("提交后未确认受理"))
    worker.process_line(game)
    # 命令可能已提交：直接冷却等待核对，不拿第二目标再发一次
    assert len(game.dispatched) == 1 and game.dispatched[0][1].x == 741
    assert not worker.line_target_blocked
    worker.deleteLater()
    app.processEvents()


# ---------------- 签到幂等重试 ----------------

class SigninGame:
    def __init__(self, journal, count, claimed):
        self.j = journal
        self.account_checked = True
        self.count = count
        self.claimed = claimed
        self.taps = []
        self.results = []
        self.d = types.SimpleNamespace(tap=lambda x, y, s: self.taps.append((x, y)),
                                       swipe=lambda *a: None)
        self.v = types.SimpleNamespace(
            find=lambda *a, **k: types.SimpleNamespace(x=100, y=200),
            signin_count=lambda im: self.count,
            signin_cards=lambda im: {i: types.SimpleNamespace(x=1000, y=400) for i in (11, 12)},
            reward_claimed=lambda im, rect: self.claimed,
            signin_milestones=lambda im: [(99, types.SimpleNamespace(x=500, y=900))],
            joined=lambda im, rect: "",
        )

    def open_activity(self):
        return np.zeros((1080, 1920, 3), np.uint8)

    def capture(self, *a, **k):
        return np.zeros((1080, 1920, 3), np.uint8)

    def save_image(self, *a, **k):
        pass

    def ensure_sandbox(self, **k):
        pass

    def emit(self, kind, value):
        self.results.append(value)


def test_signin_pending_not_applied_is_retried(tmp_path):
    """提交未生效（累计天数没动）：撤销待核对并当场重新领取，不再锁死全天。"""
    journal = Journal(tmp_path / "r.sqlite", "test")
    journal.prepare_daily("signin_day", {"count": 10, "day": 11})
    game = SigninGame(journal, count=10, claimed=False)
    # 点击日卡后：累计+1、绿勾出现（模拟本次领取生效）；页签点击(y≈200)不改变状态
    def tap(x, y, s):
        game.taps.append((x, y))
        if y > 300:
            game.count = 11
            game.claimed = True
    game.d.tap = tap
    signin(game)
    assert journal.daily("signin_day")[0] == "done"
    assert journal.daily("signin")[0] == "done"
    assert any("已领取" in str(r.get("result", "")) for r in game.results)


def test_signin_pending_applied_and_claimed_settles_without_new_click(tmp_path):
    """提交已生效且绿勾确认：结清，当天不再点日卡。"""
    journal = Journal(tmp_path / "r.sqlite", "test")
    journal.prepare_daily("signin_day", {"count": 10, "day": 11})
    game = SigninGame(journal, count=11, claimed=True)
    signin(game)
    assert journal.daily("signin_day")[0] == "done"
    assert journal.daily("signin")[0] == "done"
    # 只有打开页签的一次点击，没有日卡/里程碑提交点击
    assert len(game.taps) == 1


def test_signin_gives_up_today_after_three_blind_retries(tmp_path):
    """生效但绿勾持续读不出：幂等重试最多 3 次，之后今日跳过、明日再来。"""
    journal = Journal(tmp_path / "r.sqlite", "test")
    journal.prepare_daily("signin_day", {"count": 10, "day": 11, "retries": 3})
    game = SigninGame(journal, count=11, claimed=False)
    with pytest.raises(GameError, match="今日跳过"):
        signin(game)
