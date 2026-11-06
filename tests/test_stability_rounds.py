"""稳定性回归：指令顺序、未确认后的换指令、以及“可派必派”的自检。

用户 2026-11-03 的底线是“队伍不能闲死”。这些用例只验证三件事：
1. 站在目标点上时，候选指令仍然是配置里那一串（不再被脚本裁剪）；
2. 未确认受理时，只有确认队伍还停在原地才按顺序换下一条，忙起来就本轮停手；
3. 可派却连续没动作，第二轮必须解除冷却、下一轮强制重试。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from sanmou_assistant.game import DispatchUnconfirmed, Game
from sanmou_assistant.journal import Journal
from sanmou_assistant.models import Decision, Observation, Point, Profile
from sanmou_assistant.worker import Worker


def _game(tmp_path):
    profile = Profile(account="测试", main_city=Point(x=100, y=200))
    profile.teams[0].role = "merit"
    journal = Journal(tmp_path / "runtime.sqlite", "scope")
    return Game(None, SimpleNamespace(), profile, journal, tmp_path, lambda *a: None), profile, journal


def _obs():
    return Observation("main:1", state="outside", strength=33000, maximum=33000,
                       stamina=200, supply=100, wounded=False, location=(636, 286),
                       marker_state="stationary", marker_label="停留", marker_confidence=0.96)


def test_unconfirmed_keeps_the_configured_order_and_tries_the_next_action(tmp_path):
    game, profile, journal = _game(tmp_path)
    point = Point(name="山道", x=636, y=286, actions=["attack", "guard", "occupy", "march"])
    seen, observations = [], []

    def fake(policy, obs, target, kind, reuse_target=False):
        seen.append(list(target.actions)[0])
        observations.append(obs.marker_state)
        # 队伍还停在原地：这条没成，允许换下一条
        raise DispatchUnconfirmed("出征未确认受理（结果页 sandbox / ）", action=target.actions[0],
                                  busy=False, marker="stationary", confidence=0.95,
                                  page="sandbox", status="")

    game._dispatch = fake
    with pytest.raises(DispatchUnconfirmed) as excinfo:
        game.dispatch(profile.teams[0], _obs(), point, "merit")
    # 顺序完全按配置走，一条失败就下一条，直到用完
    assert seen == ["attack", "guard", "occupy", "march"]
    assert excinfo.value.action == "march"


def test_unconfirmed_stops_immediately_when_the_team_is_already_busy(tmp_path):
    game, profile, journal = _game(tmp_path)
    point = Point(name="山道", x=636, y=286, actions=["attack", "guard", "occupy", "march"])
    seen = []

    def fake(policy, obs, target, kind, reuse_target=False):
        seen.append(list(target.actions)[0])
        raise DispatchUnconfirmed("出征未确认受理", action=target.actions[0], busy=True,
                                  marker="fighting", confidence=0.93, page="deployment", status="fighting")

    game._dispatch = fake
    with pytest.raises(DispatchUnconfirmed):
        game.dispatch(profile.teams[0], _obs(), point, "merit")
    assert seen == ["attack"]          # 已确认在战斗，绝不补发第二条


def test_unconfirmed_refreshes_the_marker_before_the_next_action(tmp_path):
    """换下一条之前要用提交后读到的真实角标，避免拿旧观测做受理对比。"""
    game, profile, journal = _game(tmp_path)
    point = Point(name="山道", x=636, y=286, actions=["attack", "guard"])
    seen = []

    def fake(policy, obs, target, kind, reuse_target=False):
        seen.append((target.actions[0], obs.marker_state, round(obs.marker_confidence or 0, 2)))
        raise DispatchUnconfirmed("未确认", action=target.actions[0], busy=False,
                                  marker="transfer_stay", confidence=0.88, page="sandbox", status="")

    game._dispatch = fake
    with pytest.raises(DispatchUnconfirmed):
        game.dispatch(profile.teams[0], _obs(), point, "merit")
    assert seen[0] == ("attack", "stationary", 0.96)
    assert seen[1] == ("guard", "transfer_stay", 0.88)


def test_dispatch_survives_an_unconfirmed_frame_and_tries_next_action(tmp_path):
    """回归：实机 17:26 曾因把截图 numpy 数组当布尔判断（late or after）直接崩溃。

    这条用例走真实 `_dispatch` 的“未确认”收尾，确保它只用图像对象、不参与真假判断。
    """
    import numpy as np

    game, profile, journal = _game(tmp_path)
    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    game.account_checked = True
    game.d = SimpleNamespace(tap=lambda *a, **k: None, control=SimpleNamespace(sleep=lambda s: None))
    game.v = SimpleNamespace(page=lambda image: "deployment",
                             deployment_status=lambda image, slot: "unknown",
                             deploy_button_enabled=lambda image: True,
                             deployment_hint=lambda image: "",
                             joined=lambda image, rect: "")
    game.go_to = lambda point: frame
    game.reopen_target = lambda point: frame
    game.capture = lambda *a, **k: frame
    game.capture_clear = lambda *a, **k: frame
    game.save_image = lambda *a, **k: None
    game.log = lambda *a, **k: None
    game.emit = lambda *a, **k: None
    game.read_team_marker = lambda image, slot: {"state": "stationary", "confidence": 0.95}
    picked = []
    names = {"attack": "攻城", "guard": "驻守", "march": "行军"}

    def pick(image, point, kind):
        picked.append(list(point.actions))
        action = point.actions[0]
        return names[action], action, (1, 1)

    game.pick_action = pick
    point = Point(name="山道", x=636, y=286, actions=["attack", "guard", "march"])

    with pytest.raises(DispatchUnconfirmed) as excinfo:
        game.dispatch(profile.teams[0], _obs(), point, "merit")
    # 每条都试过：没有因为 numpy 布尔歧义崩掉，也没有停在第一条
    assert picked == [["attack", "guard", "march"], ["guard", "march"], ["march"]]
    assert excinfo.value.action == "march"
    assert excinfo.value.busy is False


def test_unconfirmed_cooldown_never_exceeds_one_minute(tmp_path):
    worker = Worker(Profile(retry_seconds=180), tmp_path)
    worker.journal = SimpleNamespace(failure_streak=lambda key: 9)
    assert worker.unconfirmed_delay("main:1") == 60
    worker.journal = SimpleNamespace(failure_streak=lambda key: 0)
    assert worker.unconfirmed_delay("main:1") == 30


def test_self_check_releases_a_dispatchable_team_after_two_idle_rounds(tmp_path):
    worker = Worker(Profile(), tmp_path)
    worker.command, worker.dry_run = "run", False
    worker.profile.teams[0].role = "merit"
    events, logs = [], []
    worker.publish = lambda kind, value: events.append((kind, value))
    game = SimpleNamespace(log=lambda message, level="INFO": logs.append((level, message)))
    obs = Observation("main:1", state="outside", strength=33000, maximum=33000,
                      stamina=200, supply=100, marker_state="stationary", marker_label="停留")
    plans = [(worker.profile.teams[0], obs, Decision("merit", "当前队伍可尝试出击"))]
    worker.retry_at["main:1"] = time.time() + 180

    worker.round_self_check(game, plans, set())
    assert worker.retry_at["main:1"] > time.time()     # 第一轮只记账，不动冷却
    worker.round_self_check(game, plans, set())
    assert "main:1" not in worker.retry_at             # 第二轮解除冷却，下一轮强制重试
    assert any(kind == "diagnostic" and value["stage"] == "liveness" for kind, value in events)
    assert any("可派但连续 2 轮未动作" in message for _level, message in logs)
    assert any(kind == "log" and "可派未动作" in value["message"] for kind, value in events)


def test_self_check_does_not_warn_for_a_team_that_did_dispatch(tmp_path):
    worker = Worker(Profile(), tmp_path)
    worker.command, worker.dry_run = "run", False
    worker.profile.teams[0].role = "merit"
    events = []
    worker.publish = lambda kind, value: events.append((kind, value))
    game = SimpleNamespace(log=lambda *a, **k: events.append(("game-log", a)))
    obs = Observation("main:1", state="outside", strength=33000, maximum=33000,
                      stamina=200, supply=100, marker_state="stationary", marker_label="停留")
    plans = [(worker.profile.teams[0], obs, Decision("merit", "当前队伍可尝试出击"))]
    worker.round_self_check(game, plans, {"main:1"})
    assert not any(kind == "game-log" for kind, _value in events)
    assert worker.idle_rounds.get("main:1") is None


def test_self_check_ignores_busy_teams(tmp_path):
    worker = Worker(Profile(), tmp_path)
    worker.command, worker.dry_run = "run", False
    worker.profile.teams[0].role = "merit"
    events = []
    worker.publish = lambda kind, value: events.append((kind, value))
    game = SimpleNamespace(log=lambda *a, **k: events.append(("game-log", a)))
    obs = Observation("main:1", state="guarding", marker_state="guard", marker_label="驻守")
    plans = [(worker.profile.teams[0], obs, Decision(reason="角标显示驻守，本轮不派兵"))]
    worker.round_self_check(game, plans, set())
    worker.round_self_check(game, plans, set())
    assert not any(kind == "game-log" for kind, _value in events)
