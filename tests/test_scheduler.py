import copy
import time

import pytest

from sanmou_assistant.models import Observation, Point, Profile, TeamPolicy, decide, bandit_decision, observation_expired


@pytest.fixture
def ready():
    p = Profile(account="测试", main_city=Point(x=100,y=200),targets=[Point(name="战场",x=120,y=200)])
    t = p.teams[0]
    t.merit = True
    o = Observation(t.key,state="home",strength=33000,maximum=33000,stamina=80,supply=100,wounded=False,auto_refill=True)
    return p,t,o


def test_real_strength_threshold_and_injury(ready):
    p,t,o = ready
    o.strength = 29700
    assert decide(p,t,o,1000000).action == "merit"
    o.strength = 29699
    assert decide(p,t,o,1000000).action == "wait"
    o.strength = 33000
    o.wounded = True
    assert "重伤" in decide(p,t,o,1000000).reason
    # 2026-10-31 用户要求：辅助字段缺失不再一律拒绝；未知重伤由现场读取与订单保护兜底。
    o.wounded = None
    assert decide(p,t,o,1000000).action == "merit"


@pytest.mark.parametrize("field",["strength","maximum","wounded","stamina","supply","auto_refill"])
def test_unknown_auxiliary_reading_does_not_block_dispatch(ready,field):
    p,t,o = ready
    setattr(o,field,None)
    assert decide(p,t,o,1000000).action == "merit"


def test_backup_waits_for_full_without_predicted_pool(ready):
    p,t,o = ready
    p.backup_enabled = True
    p.backups = [Point(name="营帐",x=80,y=200)]
    o.strength = 20000
    o.reserve = 100000
    assert decide(p,t,o,1000000).action == "transfer"
    o.state, o.strength = "recruiting",32999
    assert decide(p,t,o,1000000,"backup").action == "wait"
    o.strength = 33000
    assert decide(p,t,o,1000000,"backup").action == "return"
    assert decide(p,t,o,1000000,"").action == "wait"


@pytest.mark.parametrize('home_refill', [None, False, True])
def test_home_refill_does_not_control_backup_recruitment(ready, home_refill):
    p,t,o = ready
    p.backup_enabled = True
    p.backups = [Point(name='补兵城池',x=80,y=200)]
    o.strength, o.auto_refill = 20000, home_refill
    assert decide(p,t,o,1000000).action == 'transfer'


@pytest.mark.parametrize("state",["marching","returning","guarding","unknown","active","recruiting"])
def test_active_or_unlocated_team_is_not_reissued(ready,state):
    p,t,o = ready
    o.state = state
    assert decide(p,t,o,1000000).action == "wait"


def test_stale_identity_food_and_priority(ready):
    p,t,o = ready
    o.observed_at -= 130
    # 观测过期由 Worker 层判断并在读取前处理，decide 不再重复拦截（2026-10-31 简化）。
    assert observation_expired(p,o)
    o.observed_at = time.time()
    o.identity = "当前阵容"
    t.identity = "另一阵容"
    assert decide(p,t,o,1000000).action == "wait"
    t.identity = ""
    p.food_enabled = True
    # 库存读不到不阻止派兵；只按已读到的低库存等待补粮。
    assert decide(p,t,o,None).action == "merit"
    assert decide(p,t,o,499999).action == "wait"
    p.targets.append(Point(name="优先",x=130,y=200,priority=1))
    p.targets[0].priority = 2
    assert decide(p,t,o,1000000).target.name == "优先"


def test_percentage_rounds_up_and_absolute(ready):
    p,t,o = ready
    o.maximum = 33001
    o.strength = 29700
    assert t.threshold(o.maximum) == 29701
    assert decide(p,t,o,1000000).action == "wait"
    t.min_mode,t.minimum = "absolute",25000
    assert decide(p,t,o,1000000).action == "merit"


def test_profile_migration_and_validation(ready):
    p,t,o = ready
    restored = Profile.from_dict(p.to_dict())
    assert restored.to_dict() == p.to_dict()
    assert restored.validate(active=True) == []
    bad = p.to_dict()
    bad["targets"][0]["x"] = -1
    with pytest.raises(ValueError):
        Profile.from_dict(bad)
    bad = p.to_dict()
    bad["teams"].append(copy.deepcopy(bad["teams"][0]))
    with pytest.raises(ValueError):
        Profile.from_dict(bad)
    assert "已" not in " ".join(Profile().validate())
    restored.teams[5].merit = True
    assert any("亲卫" in e for e in restored.validate(active=True))


def test_bandit_policy_uses_its_own_real_strength_limits(ready):
    p,t,o = ready
    t.merit,t.bandits = False,True
    assert decide(p,t,o,1000000).action == "bandits"
    o.strength = 26342
    assert bandit_decision(p,t,o,1000000).action == "wait"
    t.min_mode,t.minimum = "absolute",26000
    assert bandit_decision(p,t,o,1000000).action == "bandits"
    o.wounded = None
    # 未知重伤不再一律拒绝，由现场读取与订单核对兜底（2026-10-31 规则）。
    assert bandit_decision(p,t,o,1000000).action == "bandits"
    o.wounded = False
    o.state = "marching"
    assert bandit_decision(p,t,o,1000000).action == "wait"


def test_marker_state_decides_whether_a_team_can_be_dispatched(ready):
    p,t,o = ready
    # 忙碌（占领/建筑内征兵等）不能派
    o.marker_state, o.state = "busy", "active"
    assert decide(p,t,o,1000000).action == "wait"
    assert "忙碌" in decide(p,t,o,1000000).reason
    # 停留（城外）、空闲（主城）、调动停留（营帐/城池）都可以派
    for marker, legacy in (("stationary", "outside"), ("idle", "home"), ("transfer_stay", "outside")):
        o.marker_state, o.state = marker, legacy
        assert decide(p,t,o,1000000).action == "merit"
    # 未识别不猜，等下一次读取
    o.marker_state, o.state = "unknown", "unknown"
    assert decide(p,t,o,1000000).action == "wait"
    assert "未识别" in decide(p,t,o,1000000).reason
    # 行军、驻守、战斗、排队、撤退都不派
    for marker, legacy in (("march", "marching"), ("guard", "guarding"),
                           ("fighting", "fighting"), ("queued_battle", "fighting"),
                           ("retreat", "returning"), ("transfer", "marching")):
        o.marker_state, o.state = marker, legacy
        assert decide(p,t,o,1000000).action == "wait"


def test_bandit_dispatch_accepts_outside_and_building_stays(ready):
    p,t,o = ready
    t.merit,t.bandits = False,True
    o.state = "outside"
    o.marker_state = "stationary"
    assert bandit_decision(p,t,o,1000000).action == "bandits"
    o.marker_state = "transfer_stay"
    assert bandit_decision(p,t,o,1000000).action == "bandits"
    o.marker_state = "busy"
    assert bandit_decision(p,t,o,1000000).action == "wait"
    o.marker_state = "unknown"
    assert bandit_decision(p,t,o,1000000).action == "wait"



def test_outside_idle_dispatch_and_backup_ownership(ready):
    p,t,o = ready
    o.state, o.auto_refill = 'outside', None
    assert decide(p,t,o,1000000).action == 'merit'
    assert decide(p,t,o,1000000,'backup').action == 'return'
    o.strength = 10000
    p.backup_enabled = True
    p.backups = [Point(name='备用点',x=1,y=2)]
    assert decide(p,t,o,1000000,'backup').action == 'wait'
    assert decide(p,t,o,1000000).action == 'transfer'
    o.wounded = True
    assert decide(p,t,o,1000000).action == 'wait'
    o.wounded = False
    assert decide(p,t,o,None).action == 'wait'
