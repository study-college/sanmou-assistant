"""2026-11-19 拉线死循环回归：旧兵力缓存 + 目标格点不中。

实机：队伍回城补满后缓存仍是受伤时的 13591/30000，拉线一直报“低于出击下限”；
同时目标 空地(740,683) 被太虚民港(741,683) 盖住，脚本每 3 分钟重试一次、永不结束。
"""
from __future__ import annotations

import time
import types

import numpy as np
from PySide6.QtWidgets import QApplication

from sanmou_assistant.game import TargetNotSelectable
from sanmou_assistant.models import Observation, Point, Profile, TeamPolicy
from sanmou_assistant.worker import Worker


class FakeJournal:
    def pending(self, key):
        return []

    def phase(self, key):
        return ""

    def set_phase(self, key, value):
        pass

    def finish(self, ident, status, note):
        pass

    def failure_streak(self, key):
        return 0


class FakeGame:
    def __init__(self, strength=30000, maximum=30000, marker_state="stationary", exc=None):
        self.strength, self.maximum = strength, maximum
        self.marker_state = marker_state
        self.exc = exc
        self.scanned = 0
        self.dispatched = []
        self.sandboxed = 0
        self.logs = []
        self.account_checked = True
        self.hud = {}
        self.v = types.SimpleNamespace(find=lambda *args, **kwargs: None)

    def read_all_marker_states(self):
        return self.hud

    def scan_team(self, policy):
        self.scanned += 1
        return Observation(policy.key, state="outside", marker_state=self.marker_state,
                           strength=self.strength, maximum=self.maximum, wounded=False)

    def scan_pave_team(self, policy):
        return Observation(policy.key, state="outside", marker_state=self.marker_state,
                           wounded=False, note="铺路快照")

    def capture(self, *args, **kwargs):
        return np.zeros((1080, 1920, 3), np.uint8)

    def ensure_sandbox(self):
        self.sandboxed += 1

    def log(self, message, level="INFO"):
        self.logs.append((level, message))

    def dispatch(self, policy, obs, point, kind):
        self.dispatched.append((kind, point))
        if self.exc is not None:
            raise self.exc


def make_worker(tmp_path, **policy_kwargs):
    point = Point(name="太虚民港", x=741, y=683, actions=["guard", "march"])
    policy = TeamPolicy(slot=1, role="line", **policy_kwargs)
    profile = Profile(teams=[policy], line_targets=[point])
    worker = Worker(profile, tmp_path, command="run", dry_run=False)
    worker.journal = FakeJournal()
    return worker, profile, policy


def test_stale_line_strength_is_refreshed_before_deciding(tmp_path):
    app = QApplication.instance() or QApplication([])
    worker, _profile, policy = make_worker(tmp_path)
    game = FakeGame(strength=30000, maximum=30000)
    worker.line_snapshot[policy.key] = (13591, 30000,
                                        time.time() - worker.LINE_STRENGTH_TTL - 10)
    obs = worker.observe_line_team(game, policy)
    assert game.scanned == 1, "缓存过期必须完整读一次，不能继续拿旧兵力做决策"
    assert obs.strength == 30000 and obs.maximum == 30000
    assert worker.line_snapshot[policy.key][0] == 30000
    worker.deleteLater()
    app.processEvents()


def test_forced_line_read_ignores_fresh_cache(tmp_path):
    app = QApplication.instance() or QApplication([])
    worker, _profile, policy = make_worker(tmp_path)
    game = FakeGame(strength=30000, maximum=30000)
    worker.line_snapshot[policy.key] = (13591, 30000, time.time())
    obs = worker.observe_line_team(game, policy, force=True)
    assert game.scanned == 1
    assert obs.strength == 30000
    worker.deleteLater()
    app.processEvents()


def test_line_busy_with_backup_reads_once_not_every_poll(tmp_path):
    """忙碌可能持续很久：有补兵点时只在进入忙碌/缓存过期时完整读，不能每 15 秒导航一次。"""
    app = QApplication.instance() or QApplication([])
    worker, profile, policy = make_worker(tmp_path)
    profile.line_backups[policy.key] = Point(name="太虚", x=755, y=644,
                                             actions=["attack", "occupy", "guard"])
    game = FakeGame(strength=30000, maximum=30000)
    game.hud = {1: {"state": "busy", "label": "忙碌", "confidence": .94}}
    worker.line_snapshot[policy.key] = (30000, 30000, time.time())
    worker.line_last_marker[policy.key] = "busy"
    worker.process_line(game)
    assert game.scanned == 0, "持续忙碌且缓存新鲜时不应每轮都导航读队"
    # 缓存过期后允许再完整读一次。
    worker.line_snapshot[policy.key] = (30000, 30000,
                                        time.time() - worker.LINE_STRENGTH_TTL - 1)
    worker.retry_at.clear()
    worker.process_line(game)
    assert game.scanned == 1
    worker.deleteLater()
    app.processEvents()


def test_unselectable_line_target_backs_off_then_finishes_team(tmp_path):
    """2026-11-21 行为更新：连续点不中不再结束队伍（结束/复拉来回跳无意义），
    改为该目标拉黑 30 分钟并换下一个目标；单目标时冷却到期自动重试，不停摆。"""
    app = QApplication.instance() or QApplication([])
    worker, _profile, policy = make_worker(tmp_path)
    game = FakeGame(exc=TargetNotSelectable("坐标定位两次仍未到达目标：目标坐标 (741, 683)，要求 (740, 683)",
                                            requested=(740, 683), selected=(741, 683)))
    for _ in range(Worker.LINE_TARGET_MAX_FAILURES):
        worker.retry_at.clear()
        worker.line_target_blocked.clear()  # 模拟冷却到期，集中验证计数与拉黑升级
        worker.process_line(game)
    assert policy.key not in worker.line_finished, "不再结束队伍"
    assert worker.line_failures[policy.key]["count"] >= Worker.LINE_TARGET_MAX_FAILURES
    # 达上限后目标被拉黑 1800 秒，期间不再尝试该坐标
    assert worker.line_target_blocked.get((policy.key, 741, 683), 0) > time.time() + 1700
    assert any("点不中" in message or "冷却" in message for _level, message in game.logs)
    worker.deleteLater()
    app.processEvents()


def test_line_target_failure_counter_resets_after_success(tmp_path):
    app = QApplication.instance() or QApplication([])
    worker, _profile, policy = make_worker(tmp_path)
    game = FakeGame(exc=TargetNotSelectable("目标点不中", requested=(740, 683), selected=(741, 683)))
    worker.process_line(game)
    assert worker.line_failures[policy.key]["count"] == 1
    game.exc = None
    worker.retry_at.clear()
    worker.line_target_blocked.clear()  # 冷却到期后才允许再次尝试并成功
    worker.process_line(game)
    assert policy.key not in worker.line_failures
    assert policy.key not in worker.line_finished
    worker.deleteLater()
    app.processEvents()
