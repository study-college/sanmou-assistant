"""委托状态的约定：登记、不可派、不读详情、决策等待。

只依赖产品源码，公开仓库可直接运行。
"""

from sanmou_assistant.models import Observation, Point, Profile, decide
from sanmou_assistant.team_states import LEGACY_STATE, STATES, can_dispatch, is_entrusted


def test_entrust_is_registered_and_never_dispatchable():
    # 用户 2026-11-18：角标是浅蓝拳套【委托】，队伍交给城主管理，我方无法派出和管理。
    assert STATES["entrust"] == "委托"
    assert LEGACY_STATE["entrust"] == "marching"
    assert not can_dispatch("entrust")
    assert is_entrusted("entrust") and not is_entrusted("idle")


def test_entrusted_team_never_opens_a_detail_page():
    from sanmou_assistant.worker import needs_detail

    assert not needs_detail({"state": "entrust"}, False)
    assert needs_detail({"state": "entrust"}, True)      # 有待核对订单时仍要看现场


def test_decision_waits_for_an_entrusted_team():
    profile = Profile(account="测试", main_city=Point(x=100, y=200),
                      targets=[Point(name="山道", x=636, y=286)])
    team = profile.teams[0]
    team.role = "merit"
    obs = Observation(team.key, state=LEGACY_STATE["entrust"], strength=33000, maximum=33000,
                      stamina=200, supply=100, wounded=False, marker_state="entrust")
    decision = decide(profile, team, obs, 1000000)
    assert decision.action == "wait"
    assert "委托" in decision.reason
