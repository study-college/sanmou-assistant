"""2026-11-20 残留铺路订单锁队回归。

实机：上一轮在“派兵过程中”被停止，铺路的 prepared 订单留在 runtime.sqlite；
随后这条铺路路线被取消分配（team 置空）。铺路模块只处理仍归属某队的路线，
通用观测又明确不结清 pave 订单，于是 main:1 每轮只被完整读一次、从不派兵，
日志表现为“一直反复检查，就是不工作”。

这里覆盖：无主订单按现场结清、仍在使用的路线不受影响、改队/删队/队伍不存在、
读不清时保持待核对，以及“停止键不再把订单留成 prepared”。
"""
from __future__ import annotations

import time

import pytest

from sanmou_assistant.journal import Journal
from sanmou_assistant.models import Observation, PaveRoute, Point, Profile, TeamPolicy
from sanmou_assistant.transport import Stopped
from sanmou_assistant.worker import Worker


class FakeGame:
    def __init__(self, observations=None, stop_on_dispatch=False):
        self.observations = observations or {}
        self.stop_on_dispatch = stop_on_dispatch
        self.scanned = []
        self.dispatched = []
        self.logs = []
        self.account_checked = True
        self.reserved_teams = set()

    def scan_team(self, policy):
        self.scanned.append(policy.key)
        return self.observations[policy.key]

    def pave_dispatch(self, policy, obs, target, ident):
        self.dispatched.append(("pave", policy.key, tuple(target), ident))
        if self.stop_on_dispatch:
            raise Stopped("用户停止")

    def dispatch(self, policy, obs, point, kind):
        self.dispatched.append((kind, policy.key, (point.x, point.y)))

    def ensure_sandbox(self):
        pass

    def log(self, message, level="INFO"):
        self.logs.append((level, message))


def observation(key, state, marker=None, strength=33000, wounded=False):
    return Observation(
        key, observed_at=time.time(), state=state, strength=strength, maximum=33000,
        wounded=wounded, location=(600, 280) if state == "home" else None,
        marker_state=marker, marker_label=marker)


def make_worker(tmp_path, routes, active_slots=None):
    profile = Profile(account="测试", main_city=Point(name="主城", x=600, y=280))
    profile.pave_routes = routes
    worker = Worker(profile, tmp_path, command="run", dry_run=False)
    worker.journal = Journal(tmp_path / "runtime.sqlite", "scope")
    worker.active_slots = set(active_slots or (1, 2, 3, 4, 5))
    return worker


def begin(worker, team="main:1", route=None, idx=0):
    route = route or worker.profile.pave_routes[0]
    target = tuple(route.points[idx][:2])
    return worker.journal.begin_pave(team, route.name, idx, f"攻占 {target}",
                                     "600,280", worker.pave_fingerprint(route))


def intent_status(worker, ident):
    return worker.journal.db.execute(
        "SELECT status FROM intents WHERE id=?", (ident,)).fetchone()[0]


def test_cancelled_route_settles_orphan_order_at_home(tmp_path):
    route = PaveRoute(name="铺路 1", team="", points=[[400, 400]], enabled=False)
    worker = make_worker(tmp_path, [route])
    ident = begin(worker)
    game = FakeGame({"main:1": observation("main:1", "home")})

    worker.settle_orphan_pave_orders(game)

    assert worker.journal.pending("main:1") == []
    assert intent_status(worker, ident) == "rejected"
    progress = worker.journal.pave_progress("铺路 1")
    assert progress["status"] == "idle" and progress["intent"] == ""
    assert game.scanned == ["main:1"], "结清前必须先按现场读一次，不能盲结"
    assert game.dispatched == []
    assert any(level == "WARN" and "残留铺路订单" in message for level, message in game.logs)


def test_cancelled_route_settles_team_already_outside_as_accepted(tmp_path):
    route = PaveRoute(name="铺路 1", team="", points=[[400, 400]], enabled=False)
    worker = make_worker(tmp_path, [route])
    ident = begin(worker)
    game = FakeGame({"main:1": observation("main:1", "outside")})

    worker.settle_orphan_pave_orders(game)

    assert worker.journal.pending("main:1") == []
    assert intent_status(worker, ident) == "accepted"
    assert worker.journal.pave_progress("铺路 1")["status"] == "idle"


def test_route_reassigned_to_other_team_settles_the_original_team(tmp_path):
    route = PaveRoute(name="铺路 1", team="main:2", points=[[400, 400]], enabled=True)
    worker = make_worker(tmp_path, [route])
    ident = begin(worker, team="main:1")
    game = FakeGame({"main:1": observation("main:1", "home")})

    worker.settle_orphan_pave_orders(game)

    assert worker.journal.pending("main:1") == []
    assert intent_status(worker, ident) == "rejected"


def test_route_deleted_from_profile_still_settles(tmp_path):
    worker = make_worker(tmp_path, [PaveRoute(name="铺路 1", team="main:1",
                                              points=[[400, 400]], enabled=True)])
    ident = begin(worker)
    assert worker.journal.route_of_intent(ident) == "铺路 1"
    worker.profile.pave_routes = []
    game = FakeGame({"main:1": observation("main:1", "home")})

    worker.settle_orphan_pave_orders(game)

    assert worker.journal.pending("main:1") == []
    assert intent_status(worker, ident) == "rejected"
    row = worker.journal.db.execute(
        "SELECT status, idx, intent FROM pave WHERE route='铺路 1'").fetchone()
    assert row == ("idle", 0, ""), "删掉路线后遗留的 dispatching 指针也要复位"


def test_absent_team_settles_without_touching_the_game(tmp_path):
    route = PaveRoute(name="铺路 1", team="", points=[[400, 400]], enabled=False)
    worker = make_worker(tmp_path, [route], active_slots=(2, 3))
    ident = begin(worker, team="main:1")
    game = FakeGame({})

    worker.settle_orphan_pave_orders(game)

    assert worker.journal.pending("main:1") == []
    assert intent_status(worker, ident) == "rejected"
    assert game.scanned == []
    assert any("不在本次部队列表" in message for _level, message in game.logs)


def test_orphan_order_is_kept_when_field_state_is_unreadable(tmp_path):
    route = PaveRoute(name="铺路 1", team="", points=[[400, 400]], enabled=False)
    worker = make_worker(tmp_path, [route])
    ident = begin(worker)
    game = FakeGame({"main:1": observation("main:1", "unknown", strength=None)})

    worker.settle_orphan_pave_orders(game)

    assert [row[0] for row in worker.journal.pending("main:1")] == [ident]
    assert "orphan-pave:" + ident in worker.wait_log_at
    assert worker.journal.pave_progress("铺路 1")["status"] == "dispatching"


def test_orphan_order_is_kept_when_home_strength_is_missing(tmp_path):
    route = PaveRoute(name="铺路 1", team="", points=[[400, 400]], enabled=False)
    worker = make_worker(tmp_path, [route])
    ident = begin(worker)
    game = FakeGame({"main:1": observation("main:1", "home", strength=None)})

    worker.settle_orphan_pave_orders(game)

    assert [row[0] for row in worker.journal.pending("main:1")] == [ident]
    assert "orphan-pave:" + ident in worker.wait_log_at
    assert worker.journal.pave_progress("铺路 1")["status"] == "dispatching"


def test_active_route_keeps_its_order_for_the_pave_module(tmp_path):
    route = PaveRoute(name="铺路 1", team="main:1", points=[[400, 400]], enabled=True)
    worker = make_worker(tmp_path, [route])
    ident = begin(worker)
    game = FakeGame({"main:1": observation("main:1", "home")})

    worker.settle_orphan_pave_orders(game)

    assert [row[0] for row in worker.journal.pending("main:1")] == [ident]
    assert game.scanned == [], "仍在使用中的路线应由 process_pave 核对，不能在这里抢读"


def test_stopping_mid_dispatch_leaves_order_as_uncertain(tmp_path):
    route = PaveRoute(name="路线A", team="main:2", points=[[400, 400]], enabled=True)
    worker = make_worker(tmp_path, [route])
    game = FakeGame({"main:2": observation("main:2", "home")}, stop_on_dispatch=True)

    with pytest.raises(Stopped):
        worker.process_pave(game)

    rows = worker.journal.pending("main:2")
    assert rows and rows[0][3] == "uncertain", "停止不能把订单留成没人核对的 prepared"
    assert worker.journal.pave_progress("路线A")["status"] == "dispatching"


def test_pave_only_pending_does_not_trigger_a_full_team_read_every_poll(tmp_path):
    profile = Profile(account="测试", main_city=Point(name="主城", x=600, y=280))
    profile.teams = [TeamPolicy(slot=1, role="line")]
    profile.line_targets = [Point(name="目标", x=727, y=634, actions=["guard"])]
    worker = Worker(profile, tmp_path, command="run", dry_run=False)
    worker.active_slots = {1}

    class PendingJournal:
        def pending(self, key):
            return [("ident", "pave", "攻占 (400, 400)", "prepared", time.time())]

        def phase(self, key):
            return "pave"

        def set_phase(self, key, value):
            pass

        def finish(self, ident, status, note):
            raise AssertionError("通用观测不允许结清铺路订单")

        def failure_streak(self, key):
            return 0

    worker.journal = PendingJournal()
    game = FakeGame({})

    worker.process_line(game)

    assert game.scanned == [], "铺路订单由铺路模块核对，拉线不应每轮空读一遍队伍"
    assert "line-pending:main:1" in worker.wait_log_at


def test_settled_orphan_order_releases_the_line_team(tmp_path):
    route = PaveRoute(name="铺路 1", team="", points=[[400, 400]], enabled=False)
    worker = make_worker(tmp_path, [route])
    begin(worker)
    target = Point(name="目标", x=727, y=634, actions=["guard"])
    worker.profile.teams = [TeamPolicy(slot=1, role="line")]
    worker.profile.line_targets = [target]
    game = FakeGame({"main:1": observation("main:1", "home", marker="idle")})

    worker.settle_orphan_pave_orders(game)
    assert worker.journal.pending("main:1") == []

    worker.process_line(game)

    assert [call[0] for call in game.dispatched] == ["line"]
    assert game.dispatched[0][2] == (727, 634)


def test_edited_route_settles_pending_order_then_dispatches_the_new_route(tmp_path):
    route = PaveRoute(name="路线A", team="main:2", points=[[400, 400]], enabled=True)
    worker = make_worker(tmp_path, [route])
    ident = begin(worker, team="main:2", route=route)
    worker.journal.db.execute("UPDATE pave SET fingerprint='deadbeef'")
    worker.journal.db.commit()
    route.points = [[300, 300]]
    game = FakeGame({"main:2": observation("main:2", "home")})

    worker.process_pave(game)

    assert worker.journal.pending("main:2") == []
    assert intent_status(worker, ident) == "rejected"
    progress = worker.journal.pave_progress("路线A")
    assert progress["fingerprint"] == worker.pave_fingerprint(route)
    assert ("pave", "main:2", (300, 300)) == game.dispatched[0][:3], \
        "旧订单结清后要在同一轮按新路线继续，不能继续停在旧段"


def test_edited_route_keeps_pausing_when_the_field_is_unreadable(tmp_path):
    route = PaveRoute(name="路线A", team="main:2", points=[[400, 400]], enabled=True)
    worker = make_worker(tmp_path, [route])
    ident = begin(worker, team="main:2", route=route)
    worker.journal.db.execute("UPDATE pave SET fingerprint='deadbeef'")
    worker.journal.db.commit()
    route.points = [[300, 300]]
    game = FakeGame({"main:2": observation("main:2", "unknown", strength=None)})

    worker.process_pave(game)

    assert [row[0] for row in worker.journal.pending("main:2")] == [ident]
    assert game.dispatched == []
    assert any("保持暂停" in message for _level, message in game.logs)


def test_route_taken_over_after_reassign_waits_for_the_orphan_sweep(tmp_path):
    old_route = PaveRoute(name="路线A", team="main:1", points=[[400, 400]], enabled=True)
    worker = make_worker(tmp_path, [old_route])
    ident = begin(worker, team="main:1", route=old_route)
    # 路线改给 2 队，点数也变了；旧订单仍指向 1 队。
    old_route.team = "main:2"
    worker.journal.db.execute("UPDATE pave SET fingerprint='deadbeef'")
    worker.journal.db.commit()
    old_route.points = [[300, 300]]
    game = FakeGame({"main:1": observation("main:1", "home"),
                     "main:2": observation("main:2", "home")})

    worker.process_pave(game)
    assert [row[0] for row in worker.journal.pending("main:1")] == [ident]
    assert game.dispatched == [], "旧订单属于 1 队，不能用 2 队的现场替它结清"

    worker.settle_orphan_pave_orders(game)
    assert worker.journal.pending("main:1") == []

    worker.process_pave(game)
    assert game.dispatched[0][:3] == ("pave", "main:2", (300, 300))
