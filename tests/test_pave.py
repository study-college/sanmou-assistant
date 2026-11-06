import time

import numpy as np
import pytest

from sanmou_assistant.game import Game, GameError, PaveUnknown
from sanmou_assistant.journal import Journal
from sanmou_assistant.models import (Observation, PaveRoute, Point, Profile,
                                     pave_summary)
from sanmou_assistant.worker import Worker


class FakeGame:
    def __init__(self, worker, observations):
        self.worker = worker
        self.observations = observations
        self.calls = []
        self.account_checked = True
        self.reserved_teams = set()
        self.unknown = False

    def scan_team(self, policy):
        self.calls.append(("scan", policy.key))
        return self.observations[policy.key]

    def pave_dispatch(self, policy, obs, target, ident):
        self.calls.append(("pave", policy.key, tuple(target), ident))
        if self.unknown:
            raise PaveUnknown("测试：提交结果未知")

    def ensure_sandbox(self):
        self.calls.append(("sandbox",))

    def log(self, *args):
        pass


def make_profile(points, team="main:2"):
    profile = Profile(account="测试", main_city=Point(name="主城", x=600, y=280))
    profile.pave_routes = [PaveRoute(name="路线A", team=team, points=points, enabled=True)]
    return profile


def observation(key, state, location, strength=33000, wounded=False):
    return Observation(key, observed_at=time.time(), state=state, strength=strength,
                       maximum=33000, wounded=wounded, location=location)


def make_worker(tmp_path, profile):
    worker = Worker(profile, tmp_path, command="run", dry_run=False)
    worker.journal = Journal(tmp_path / "runtime.sqlite", "scope")
    return worker


def progress(worker, route="路线A"):
    return worker.journal.pave_progress(route)


def seeded(worker, idx=0, status="running", note="", origin="607,291"):
    fingerprint = worker.pave_fingerprint(worker.profile.pave_routes[0])
    worker.journal.set_pave("路线A", idx, status, note, fingerprint=fingerprint, origin=origin)
    return fingerprint


def pave_calls(game):
    return [call for call in game.calls if call[0] == "pave"]


def test_pave_dispatches_first_segment_from_home(tmp_path):
    worker = make_worker(tmp_path, make_profile([[571, 243], [566, 240]]))
    game = FakeGame(worker, {"main:2": observation("main:2", "home", (607, 291))})
    worker.process_pave(game)
    assert len(pave_calls(game)) == 1
    state = progress(worker)
    assert state["idx"] == 0 and state["status"] == "running"
    assert worker.journal.pending("main:2") == []
    assert state["intent"] == ""


def test_unknown_pave_return_to_different_base_recovers_without_skipping_target(tmp_path):
    worker = make_worker(tmp_path, make_profile([[571, 243]]))
    route = worker.profile.pave_routes[0]
    worker.journal.begin_pave(route.team, route.name, 0, "target", "607,291", worker.pave_fingerprint(route))
    worker.journal.db.execute("UPDATE pave SET updated=0")
    worker.journal.db.commit()
    game = FakeGame(worker, {route.team: observation(route.team, "outside", (600, 300))})
    worker.process_pave(game)
    # 停在别处：先把未确认订单结清为"恢复中"，不跳过目标；下一轮再派当前段。
    assert not pave_calls(game)
    worker.process_pave(game)
    assert pave_calls(game)[0][2] == (571, 243)
    assert progress(worker)["idx"] == 0 and progress(worker)["status"] == "running"
    worker.process_pave(game)
    assert pave_calls(game)[0][2] == (571, 243)


def test_edited_completed_route_keeps_team_reserved_before_progress_reset(tmp_path):
    worker = make_worker(tmp_path, make_profile([[571, 243]]))
    seeded(worker, idx=1, status="done")
    worker.profile.pave_routes[0].points = [[566, 240]]
    assert worker.pave_team_keys() == {"main:2"}
    policy = next(t for t in worker.profile.teams if t.key == "main:2")
    game = FakeGame(worker, {policy.key: observation(policy.key, "home", (607, 291))})
    assert worker.team_decision(game, policy, game.observations[policy.key], False).action == "wait"
    worker.process_pave(game)
    assert pave_calls(game)[0][2] == (566, 240)


def test_generic_observation_does_not_settle_pave_or_assign_merit_phase(tmp_path):
    worker = make_worker(tmp_path, make_profile([[571, 243]]))
    ident = worker.journal.begin_pave("main:2", "路线A", 0, "攻占 (571, 243)",
                                     "607,291", worker.pave_fingerprint(worker.profile.pave_routes[0]))
    game = FakeGame(worker, {"main:2": observation("main:2", "marching", None)})
    policy = next(t for t in worker.profile.teams if t.key == "main:2")
    worker.observe_team(game, policy)
    assert worker.journal.pending(policy.key)[0][0] == ident
    assert worker.journal.phase(policy.key) != "merit"
    worker.process_pave(game)
    assert worker.journal.pending(policy.key) == []
    assert progress(worker)["status"] == "running"


def test_pave_first_segment_can_start_from_a_standing_team(tmp_path):
    worker = make_worker(tmp_path, make_profile([[571, 243]]))
    seeded(worker, idx=0, status="idle", origin="")
    game = FakeGame(worker, {"main:2": observation("main:2", "outside", (580, 250))})
    worker.process_pave(game)
    assert len(pave_calls(game)) == 1


def test_pave_waits_while_team_is_marching(tmp_path):
    worker = make_worker(tmp_path, make_profile([[571, 243], [566, 240]]))
    seeded(worker, note="(571,243)")
    game = FakeGame(worker, {"main:2": observation("main:2", "marching", None)})
    worker.process_pave(game)
    assert not pave_calls(game)


def test_pave_advances_after_arrival_and_dispatches_next(tmp_path):
    worker = make_worker(tmp_path, make_profile([[571, 243], [566, 240]]))
    seeded(worker, note="(571,243)")
    game = FakeGame(worker, {"main:2": observation("main:2", "outside", (571, 243))})
    worker.process_pave(game)
    assert len(pave_calls(game)) == 1
    state = progress(worker)
    assert state["idx"] == 1 and state["status"] == "running" and state["note"] == "(566,240)"


def test_pave_marks_done_after_last_point(tmp_path):
    worker = make_worker(tmp_path, make_profile([[571, 243]]))
    seeded(worker, note="(571,243)")
    game = FakeGame(worker, {"main:2": observation("main:2", "outside", (571, 243))})
    worker.process_pave(game)
    state = progress(worker)
    assert state["idx"] == 1 and state["status"] == "done"
    assert not pave_calls(game)


def test_pave_resumes_when_team_returns_home_mid_route(tmp_path):
    worker = make_worker(tmp_path, make_profile([[571, 243], [566, 240]]))
    seeded(worker, note="(571,243)")
    game = FakeGame(worker, {"main:2": observation("main:2", "home", (607, 291))})
    worker.process_pave(game)
    # 铺路就是多阶段：队伍停下来了（哪怕在家）就恢复当前段，不再用"回城后继续"开关挡着。
    assert pave_calls(game)[0][2] == (571, 243)


def test_pave_resume_after_wounding_retries_unfinished_segment(tmp_path):
    profile=make_profile([[571,243],[566,240]])
    profile.pave_routes[0].resume_after_return=True
    worker=make_worker(tmp_path,profile)
    seeded(worker)
    game=FakeGame(worker,{"main:2":observation("main:2","wounded",(607,291),wounded=True)})
    worker.process_pave(game)
    assert progress(worker)["idx"]==0 and progress(worker)["status"]=="recovering"
    assert not pave_calls(game)
    game.observations["main:2"]=observation("main:2","home",(607,291),strength=12000)
    worker.process_pave(game)
    assert pave_calls(game)[0][2]==(571,243)
    assert progress(worker)["status"]=="running" and progress(worker)["idx"]==0


def test_pave_completed_segment_is_not_lost_when_next_starts_from_home(tmp_path):
    worker=make_worker(tmp_path,make_profile([[571,243],[566,240]]))
    seeded(worker,idx=1,status="idle")
    game=FakeGame(worker,{"main:2":observation("main:2","home",(607,291))})
    worker.process_pave(game)
    assert pave_calls(game)[0][2]==(566,240)
    assert progress(worker)["idx"]==1


def test_pave_night_or_long_occupation_never_expires_an_active_segment(tmp_path):
    worker=make_worker(tmp_path,make_profile([[571,243]]))
    seeded(worker)
    worker.journal.db.execute("UPDATE pave SET updated=?",(time.time()-7200,));worker.journal.db.commit()
    game=FakeGame(worker,{"main:2":observation("main:2","active",(570,242))})
    worker.process_pave(game)
    assert not pave_calls(game) and progress(worker)["status"]=="running"
    assert progress(worker)["idx"]==0


def test_pave_no_longer_checks_target_ownership_before_repeating(tmp_path):
    # 旧逻辑会导航核对目标归属（pave_point_completed）；0.7.4 只看角标状态，停在别处就恢复当前段。
    profile=make_profile([[571,243],[566,240]])
    worker=make_worker(tmp_path,profile);seeded(worker)
    game=FakeGame(worker,{"main:2":observation("main:2","home",(607,291))})
    worker.process_pave(game)
    assert pave_calls(game)[0][2]==(571,243) and progress(worker)["idx"]==0


def test_pave_next_segment_can_start_from_another_confirmed_standing_location(tmp_path):
    worker = make_worker(tmp_path, make_profile([[571, 243], [566, 240]]))
    seeded(worker, idx=1, status="idle")
    game = FakeGame(worker, {"main:2": observation("main:2", "outside", (600, 300))})
    worker.process_pave(game)
    assert pave_calls(game)[0][2] == (566, 240)


def test_pave_unknown_submit_is_not_replayed_and_keeps_team_reserved(tmp_path):
    worker = make_worker(tmp_path, make_profile([[571, 243]]))
    game = FakeGame(worker, {"main:2": observation("main:2", "home", (607, 291))})
    game.unknown = True
    worker.process_pave(game)
    state = progress(worker)
    assert state["status"] == "dispatching" and state["intent"]
    assert worker.journal.pending("main:2")
    assert worker.pave_team_keys() == {"main:2"}
    worker.retry_at.clear()
    game.calls.clear()
    worker.process_pave(game)
    assert not pave_calls(game)


def test_pave_recovers_at_target_after_restart(tmp_path):
    worker = make_worker(tmp_path, make_profile([[571, 243]]))
    fingerprint = worker.pave_fingerprint(worker.profile.pave_routes[0])
    ident = worker.journal.begin_pave("main:2", "路线A", 0, "攻占 (571, 243)", "607,291", fingerprint)
    game = FakeGame(worker, {"main:2": observation("main:2", "outside", (571, 243))})
    worker.process_pave(game)
    state = progress(worker)
    assert state["idx"] == 1 and state["status"] == "done" and state["intent"] == ""
    assert worker.journal.pending("main:2") == []
    assert worker.journal.db.execute("SELECT status FROM intents WHERE id=?", (ident,)).fetchone()[0] == "accepted"


def test_pave_origin_unchanged_after_cooldown_is_rejected(tmp_path):
    worker = make_worker(tmp_path, make_profile([[571, 243]]))
    fingerprint = worker.pave_fingerprint(worker.profile.pave_routes[0])
    ident = worker.journal.begin_pave("main:2", "路线A", 0, "攻占 (571, 243)", "607,291", fingerprint)
    worker.journal.db.execute("UPDATE pave SET updated=? WHERE route=?", (time.time() - 400, "路线A"))
    worker.journal.db.commit()
    game = FakeGame(worker, {"main:2": observation("main:2", "outside", (607, 291))})
    worker.process_pave(game)
    state = progress(worker)
    assert state["status"] == "idle" and state["intent"] == ""
    assert worker.journal.pending("main:2") == []
    assert worker.journal.db.execute("SELECT status FROM intents WHERE id=?", (ident,)).fetchone()[0] == "rejected"


def test_pave_team_reserved_while_blocked_and_done_releases(tmp_path):
    worker = make_worker(tmp_path, make_profile([[571, 243]]))
    assert worker.pave_team_keys() == {"main:2"}
    worker.journal.set_pave("路线A", 0, "blocked", "队伍回城")
    assert worker.pave_team_keys() == {"main:2"}
    worker.journal.set_pave("路线A", 1, "done", "")
    assert worker.pave_team_keys() == set()


def test_pave_route_change_resets_progress(tmp_path):
    worker = make_worker(tmp_path, make_profile([[571, 243], [566, 240]]))
    seeded(worker, idx=1, status="done", note="")
    worker.profile.pave_routes[0].points = [[500, 200]]
    game = FakeGame(worker, {"main:2": observation("main:2", "home", (607, 291))})
    worker.process_pave(game)
    state = progress(worker)
    assert state["idx"] == 0 and state["status"] == "running"
    assert [call[2] for call in pave_calls(game)] == [(500, 200)]


def test_unassigned_route_never_enters_pave_batch(tmp_path):
    """回归：没有人选中的路线只是草稿，不参与铺路，也不拦开始执行。"""
    profile = make_profile([[571, 243]])
    profile.pave_routes[0].team = ""
    worker = make_worker(tmp_path, profile)
    assert worker.pave_team_keys() == set()
    assert profile.validate() == []


def test_pave_route_validation():
    profile = Profile(account="x", main_city=Point(name="主城", x=1, y=1))
    profile.pave_routes = [PaveRoute(name="A", team="main:9", points=[[1, 2]])]
    assert any("队伍不存在" in error for error in profile.validate())
    profile.pave_routes = [PaveRoute(name="A", team="main:2", points=[[4000, 2]])]
    assert any("无效坐标" in error for error in profile.validate())
    profile.pave_routes = [PaveRoute(name="A", team="main:2", points=[])]
    assert any("坐标" in error for error in profile.validate())
    profile.pave_routes = [PaveRoute(name="A", team="main:2", points=[[1, 2]]),
                           PaveRoute(name="A", team="main:3", points=[[3, 4]])]
    assert any("名称重复" in error for error in profile.validate())
    profile.pave_routes = [PaveRoute(name="A", points=[[1, 2]], enabled=True),
                           PaveRoute(name="B", points=[[3, 4]], enabled=True)]
    profile.teams[1].role, profile.teams[1].route = "pave", "A"
    profile.teams[2].role, profile.teams[2].route = "pave", "A"
    profile.normalize()
    assert any("同一条铺路路线" in error for error in profile.validate())


def test_pave_route_roundtrip_and_labels():
    profile = Profile(account="x", main_city=Point(name="主城", x=1, y=1))
    profile.pave_routes = [PaveRoute(name="A", team="main:2",
                                     points=[[1, 2, "空地"], [3, 4]], enabled=True)]
    restored = Profile.from_dict(profile.to_dict())
    assert restored.pave_routes[0].points == [[1, 2, "空地"], [3, 4]]
    assert pave_summary(restored.pave_routes[0].points) == "空地(1,2) → (3,4)"


class StubControl:
    def sleep(self, seconds):
        return None


class StubDevice:
    def __init__(self):
        self.taps = []
        self.control = StubControl()

    def tap(self, x, y, delay=0):
        self.taps.append((x, y))


class StubText:
    x, y = 1000, 400


class MarkerVision:
    """最小 Vision 替身：出征页/卡片状态可空，动作图标与按钮状态给固定值。"""

    def __init__(self, page_after="deployment", status_after="outside"):
        self.page_after = page_after
        self.status_after = status_after
        self.status_calls = 0

    def action_buttons(self, image):
        return {}

    def frame_clear(self, image):
        return True

    def deploy_button_enabled(self, image):
        return True

    def deployment_hint(self, image):
        return ""

    def page(self, image, verify=True):
        return self.page_after

    def find(self, image, labels, rect=None, exact=False):
        return StubText()

    def joined(self, image, rect):
        if rect and rect[0] == 750 and rect[1] < 30:
            return "攻占"
        return ""

    def labeled_checkbox(self, image, labels, rect):
        return (1335,842)

    def checked(self, image, x, y):
        return False

    def deployment_status(self, image, slot):
        self.status_calls += 1
        return "outside" if self.status_calls <= 2 else self.status_after

    def deployment_strength(self, image, slot):
        return 33000


def stub_game(tmp_path, vision, image):
    game = Game.__new__(Game)
    game.p = Profile(account="x", main_city=Point(name="主城", x=1, y=1))
    game.j = Journal(tmp_path / "runtime.sqlite", "scope")
    game.v, game.d = vision, StubDevice()
    game.account_checked = True
    game.emit = lambda *args: None
    game.log = lambda *args: None
    game.capture = lambda *args: image
    game.save_image = lambda *args: None
    game.go_to = lambda point: image
    return game


def test_pave_dispatch_requires_new_evidence_after_submit(tmp_path):
    image = np.zeros((1080, 1920, 3), np.uint8)
    image[947:1020, 1580:1860] = (0, 200, 255)  # 出征按钮金色
    vision = MarkerVision(page_after="deployment", status_after="outside")
    game = stub_game(tmp_path, vision, image)
    policy = next(t for t in game.p.teams if t.key == "main:2")
    obs = observation("main:2", "outside", (571, 243))
    ident = game.j.begin_pave("main:2", "路线A", 0, "攻占 (571, 243)", "607,291", "fp")
    try:
        game.pave_dispatch(policy, obs, (571, 243), ident)
    except PaveUnknown:
        pass
    else:
        raise AssertionError("停留状态原样返回不能算铺路出发成功")


def test_pave_unknown_auto_return_never_toggles_checkbox(tmp_path):
    image = np.zeros((1080,1920,3),np.uint8)
    vision = MarkerVision()
    vision.checked = lambda *a:None
    game = stub_game(tmp_path,vision,image)
    policy = game.p.teams[0]
    obs = observation(policy.key,"outside",(571,243))
    ident = game.j.begin_pave(policy.key,"路线A",0,"攻占 (571,243)","607,291","fp")
    try:
        with pytest.raises(GameError,match="状态未识别"):
            game.pave_dispatch(policy,obs,(571,243),ident)
        assert (1335,842) not in game.d.taps
        assert game.j.pending(policy.key)[0][0] == ident
    finally:
        game.j.close()


def test_bandits_skip_teams_reserved_for_pave(tmp_path):
    """复核问题 3 回归：讨贼不得抢占仍有未完成路线的队伍。"""
    from types import SimpleNamespace

    profile = Profile(account="x", main_city=Point("主城", 1, 1))
    profile.pave_routes = [PaveRoute("A", "main:2", [[20, 20]], True)]
    profile.food_enabled = False
    journal = Journal(tmp_path / "runtime.sqlite", "scope")
    worker = Worker(profile, tmp_path, command="run", dry_run=False)
    worker.journal = journal
    team = next(t for t in profile.teams if t.key == "main:2")
    team.role = "bandits"
    home = Observation("main:2", observed_at=time.time(), state="home", strength=33000,
                       maximum=33000, wounded=False, stamina=200, supply=100,
                       location=(607, 291))
    joined, scanned = [], []
    game = SimpleNamespace(
        account_checked=True, j=journal, p=profile, grain=1000000,
        v=SimpleNamespace(joined=lambda *a: "",
                          bandit_rallies=lambda *a: [{"id": "r", "text": "贼寇", "join": (1, 2)}],
                          bandit_availability=lambda *a: ("reward", 1)),
        open_bandit_list=lambda: None, ensure_sandbox=lambda: None,
        emit=lambda *a: None,
        scan_team=lambda policy: (scanned.append(policy.key), home)[1],
        join_bandits=lambda policy, *a, **kw: joined.append(policy.key),
        reserved_teams=worker.pave_team_keys(),
    )
    Game.bandits(game, True)
    assert game.reserved_teams == {"main:2"}
    assert scanned == [] and joined == []
    # 同一条件下去掉铺路归属，讨贼会选中该队；证明上面的断言确实拦截了抢队。
    game.reserved_teams = set()
    Game.bandits(game, True)
    assert joined == ["main:2"]


def test_pave_dispatch_accepts_when_badge_state_changes(tmp_path):
    image = np.zeros((1080, 1920, 3), np.uint8)
    image[947:1020, 1580:1860] = (0, 200, 255)
    vision = MarkerVision(page_after="deployment", status_after="marching")
    game = stub_game(tmp_path, vision, image)
    policy = next(t for t in game.p.teams if t.key == "main:2")
    obs = observation("main:2", "outside", (571, 243))
    obs.marker_state = "stationary"
    states = iter([{"state": "stationary"}, {"state": "march"}])
    game.read_team_marker = lambda img, slot: next(states)
    ident = game.j.begin_pave("main:2", "路线A", 0, "攻占 (571, 243)", "607,291", "fp")
    game.pave_dispatch(policy, obs, (571, 243), ident)
    # 五张卡实测间距 250：第 2 队点 385；提交按钮固定在 1717,984。
    assert (385, 880) in game.d.taps
    assert (1717, 984) in game.d.taps


def test_low_food_still_settles_completed_pave_without_new_segment(tmp_path):
    worker = make_worker(tmp_path, make_profile([[571,243],[566,240]]))
    seeded(worker, idx=0, status='running')
    game = FakeGame(worker, {'main:2':observation('main:2','outside',(571,243))})
    worker.process_pave(game, food_recovery=True)
    assert progress(worker)['idx'] == 1 and not pave_calls(game)
    assert worker.pave_team_keys() == {'main:2'}
