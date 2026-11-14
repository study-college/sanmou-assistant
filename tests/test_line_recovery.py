"""拉线订单恢复回归（2026-11-14，交接文档 72 后续）。

背景：process_line 旧版在读取现场前遇到 journal.pending 就直接 continue，
kind='line' 的 prepared/uncertain 订单会被永久锁住；observe_team 的城外到达/征兵
结清也只认 merit/transfer/bandits。这里锁定：
- 每队处理时先用真实现场（observe_team）结清 pending，仍 pending 才跳过；
- line 订单按行军/城外到达/征兵证据结清 accepted，冷却后仍在主城才 rejected；
- 无目标时也必须先恢复 pending；
- 低兵力的 refill 决策不依赖虚构的“页面按钮”状态。
全部使用 fake，不连接设备。
"""
from __future__ import annotations

import inspect
import time

from PySide6.QtWidgets import QApplication

from sanmou_assistant.models import (Observation, Point, Profile, TeamPolicy, line_decision,
                                     line_target)
from sanmou_assistant.worker import Worker


class FakeJournal:
    """只记录 prepared/uncertain 订单的假 journal；finish 后订单即结清。"""

    def __init__(self):
        self.rows = []        # [ident, kind, target, status, created]
        self.finished = []    # (ident, status, note)
        self.phases = {}

    def add(self, kind="line", created=None, status="uncertain"):
        self.rows.append([f"order-{len(self.rows)}", kind, "拉线目标", status,
                          time.time() if created is None else created])

    def pending(self, key):
        return [tuple(row) for row in self.rows if row[3] in ("prepared", "uncertain")]

    def phase(self, key):
        return self.phases.get(key, "")

    def set_phase(self, key, value):
        self.phases[key] = value

    def finish(self, ident, status, note=""):
        for row in self.rows:
            if row[0] == ident:
                row[3] = status
        self.finished.append((ident, status, note))

    def failure_streak(self, key):
        return 0


class FakeRecoveryGame:
    """按预定脚本回答 scan_team 的假游戏；不连接设备。"""

    MARKERS = {"marching": "march", "outside": "stationary", "home": "idle",
               "recruiting": "busy", "unknown": "unknown"}

    def __init__(self, states, *, strength=30000, maximum=33000, wounded=False):
        self.states = list(states)
        self.strength, self.maximum, self.wounded = strength, maximum, wounded
        self.scans = 0
        self.dispatched = []
        self.logs = []
        self.account_checked = True

    def read_all_marker_states(self):
        return {}

    def scan_team(self, policy):
        self.scans += 1
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return Observation(policy.key, state=state,
                           marker_state=self.MARKERS.get(state, "unknown"),
                           strength=self.strength, maximum=self.maximum, wounded=self.wounded,
                           location=(100, 200) if state == "home" else None)

    def log(self, message, level="INFO"):
        self.logs.append((level, message))

    def ensure_sandbox(self):
        pass

    def dispatch(self, policy, obs, point, kind, reuse_target=False):
        self.dispatched.append((kind, point))


def make_worker(tmp_path, *, with_target=True, **policy_kwargs):
    point = Point(name="拉线目标 (800,800)", x=800, y=800, group="默认",
                  actions=["occupy", "march"], expected_name="")
    policy = TeamPolicy(slot=1, role="line", **policy_kwargs)
    profile = Profile(teams=[policy], line_targets=[point] if with_target else [])
    worker = Worker(profile, tmp_path, command="run", dry_run=False)
    worker.journal = FakeJournal()
    return worker, profile, policy


def test_line_pending_marching_evidence_accepted_and_unlocked(tmp_path):
    """行军证据：line 订单按 accepted 结清，同一轮解锁并继续派出，不再永久锁队。"""
    app = QApplication.instance() or QApplication([])
    worker, _profile, policy = make_worker(tmp_path)
    worker.journal.add(kind="line")
    game = FakeRecoveryGame(["marching", "outside"])
    worker.process_line(game)
    assert [status for _ident, status, _note in worker.journal.finished] == ["accepted"]
    assert worker.journal.pending(policy.key) == []
    assert game.dispatched and game.dispatched[0][0] == "line"
    assert "line:main:1" not in worker.retry_at
    worker.deleteLater()
    app.processEvents()


def test_line_pending_outside_arrival_evidence_accepted(tmp_path):
    """城外到达/征兵证据：line 订单按 accepted 结清并解锁（旧版只认 merit/transfer/bandits）。"""
    app = QApplication.instance() or QApplication([])
    for state in ("outside", "recruiting"):
        worker, _profile, policy = make_worker(tmp_path)
        worker.journal.add(kind="line")
        game = FakeRecoveryGame([state])
        worker.process_line(game)
        assert [status for _ident, status, _note in worker.journal.finished] == ["accepted"], state
        assert worker.journal.pending(policy.key) == [], state
        assert game.dispatched, state
        worker.deleteLater()
        app.processEvents()


def test_line_pending_without_evidence_holds_and_skips(tmp_path):
    """证据不足（状态未识别/兵力没读到）时不结清也不派兵，下一轮再按现场核对。"""
    app = QApplication.instance() or QApplication([])
    worker, _profile, policy = make_worker(tmp_path)
    worker.journal.add(kind="line")
    game = FakeRecoveryGame(["unknown"])
    worker.process_line(game)
    assert game.scans == 1                      # 只做了恢复性读取，没有继续派兵流程
    assert game.dispatched == []
    assert worker.journal.pending(policy.key)   # 仍 pending：本轮跳过但不被遗忘
    blind, _profile2, policy2 = make_worker(tmp_path)
    blind.journal.add(kind="line")
    blind_game = FakeRecoveryGame(["outside"], strength=None)
    blind.process_line(blind_game)
    assert blind_game.dispatched == []
    assert blind.journal.pending(policy2)
    blind.deleteLater()
    worker.deleteLater()
    app.processEvents()


def test_line_pending_recovered_even_without_target(tmp_path):
    """无目标时也必须先结清 pending：恢复在前，“未配置目标”跳过在后。"""
    app = QApplication.instance() or QApplication([])
    worker, _profile, policy = make_worker(tmp_path, with_target=False)
    worker.journal.add(kind="line")
    game = FakeRecoveryGame(["outside"])
    worker.process_line(game)
    assert [status for _ident, status, _note in worker.journal.finished] == ["accepted"]
    assert worker.journal.pending(policy.key) == []
    assert game.dispatched == []                # 没有目标，恢复后仍不派兵
    worker.deleteLater()
    app.processEvents()


def test_line_pending_home_after_cooldown_rejected_then_processes(tmp_path):
    """冷却后仍在主城：按 rejected 结清并允许后续处理（下一轮自动重试）。"""
    app = QApplication.instance() or QApplication([])
    worker, profile, policy = make_worker(tmp_path)
    worker.journal.add(kind="line", created=time.time() - profile.retry_seconds - 10)
    game = FakeRecoveryGame(["home"])
    worker.process_line(game)
    assert [status for _ident, status, _note in worker.journal.finished] == ["rejected"]
    assert worker.journal.pending(policy.key) == []
    assert game.dispatched and game.dispatched[0][0] == "line"   # 结清后同一轮即可重派
    worker.deleteLater()
    app.processEvents()


def test_line_pending_home_within_cooldown_still_holds(tmp_path):
    """冷却时间内仍在主城：不提前 rejected，保持 pending 等下一轮现场。"""
    app = QApplication.instance() or QApplication([])
    worker, profile, policy = make_worker(tmp_path)
    worker.journal.add(kind="line")             # created=now，远小于 retry_seconds
    game = FakeRecoveryGame(["home"])
    worker.process_line(game)
    assert worker.journal.finished == []
    assert worker.journal.pending(policy.key)
    assert game.dispatched == []
    worker.deleteLater()
    app.processEvents()


def test_line_refill_decision_has_no_button_parameter():
    """refill 决策不再接收“页面真实按钮”形参：低兵力且启用快速征兵即去出征页核对。"""
    assert "refill_button" not in inspect.signature(line_decision).parameters
    policy = TeamPolicy(slot=1, role="line")
    target = Point(name="水面", x=800, y=800, group="默认", priority=1)
    low = Observation("main:1", state="outside", marker_state="stationary",
                      strength=10000, maximum=33000, wounded=False)
    result = line_decision(policy, low, target=target)
    assert result.action == "refill"
    assert "去出征页核对" in result.reason      # 不得谎称已看到按钮
    assert "按钮不存在" not in result.reason
    disabled = TeamPolicy(slot=1, role="line", refill_enabled=False)
    assert line_decision(disabled, low, target=target).action == "wait"
