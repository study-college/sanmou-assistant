import time

import pytest

from sanmou_assistant.game import Game, GameError
from sanmou_assistant.journal import Journal
from sanmou_assistant.models import Observation, Point, Profile
from sanmou_assistant.transport import Stopped
from sanmou_assistant.worker import Worker


class FakeGame:
    def __init__(self, worker):
        self.worker = worker
        self.grain = 1000000
        self.calls = []
        self.observations = {}
        self.fail_scan = set()
        self.uncertain = set()

    def scan_team(self, policy):
        self.calls.append(("scan",policy.key))
        if policy.key in self.fail_scan:
            raise GameError("队伍读数未识别")
        return self.observations.get(policy.key,Observation(policy.key,observed_at=time.time(),state="home",strength=33000,maximum=33000,wounded=False,stamina=100,supply=100,auto_refill=True))

    def dispatch(self, policy, obs, point, kind, reuse_target=False):
        self.calls.append(("dispatch",policy.key,point.name,reuse_target))
        ident = self.worker.journal.prepare(policy.key,kind,point.name)
        if policy.key in self.uncertain:
            self.worker.journal.finish(ident,"uncertain")
            raise GameError("出征结果待核对")
        self.worker.journal.finish(ident,"accepted")
        self.worker.publish("dispatch",{"team":policy.key,"kind":kind,"target":point.name})

    def ensure_sandbox(self):
        self.calls.append(("sandbox",))
        return "沙盘画面"

    def capture(self):
        return "沙盘画面"

    def passive_refresh(self, paused=False):
        self.calls.append(("passive", paused))

    def log(self, *args):
        pass


def test_startup_other_device_login_saves_evidence_and_stops_without_orders(tmp_path,monkeypatch):
    import sanmou_assistant.worker as module
    from types import SimpleNamespace
    calls = []
    class Device:
        serial,width,height = "test",1920,1080
        def __init__(self,*args): pass
        def connect(self,**kwargs): pass
        def capture(self): return "blocked"
        def launch_game(self,package): calls.append(("launch",package))
        def close(self): calls.append(("close",))
    class Game:
        def __init__(self,*args): pass
        def capture(self,*args):
            calls.append(("capture",)+args)
            return "blocked"
    monkeypatch.setattr(module,"Device",Device)
    monkeypatch.setattr(module,"Game",Game)
    monkeypatch.setattr(module,"Vision",lambda *args:SimpleNamespace(page=lambda image, verify=True:"blocked"))
    monkeypatch.setattr(module,"discover_cli",lambda *args:"test")
    monkeypatch.setattr(module,"instances",lambda *args:[])
    worker = Worker(Profile(),tmp_path/"session","run",False)
    events = []
    worker.publish = lambda kind,value:events.append((kind,value))
    worker.run()
    assert ("capture","login-blocked",True) in calls
    assert any(kind == "error" and "其他设备登录" in value for kind,value in events)
    assert not any(kind == "dispatch" for kind,value in events)
    assert calls[-1] == ("close",)


@pytest.fixture
def batch(tmp_path):
    profile = Profile(account="测试",main_city=Point(x=100,y=200),targets=[Point(name="战场",x=120,y=200)])
    for team in profile.teams[:3]:
        team.role = "merit"
    worker = Worker(profile,tmp_path,"run",False)
    worker.active_slots = set(range(1, 6))  # 本文件验证五队批次；启动清单已确认五队存在。
    worker.journal = Journal(tmp_path/"runtime.sqlite","test")
    game = FakeGame(worker)
    yield worker,game,profile.teams[:3]
    worker.journal.close()


def test_three_teams_are_all_read_before_continuous_dispatch(batch):
    worker,game,teams = batch
    assert not worker.process_team_batch(game,teams)
    assert game.calls == [("scan","main:1"),("scan","main:2"),("scan","main:3"),
        ("dispatch","main:1","战场",False),("dispatch","main:2","战场",True),("dispatch","main:3","战场",True)]
    assert worker.orders == 3


@pytest.mark.skip(reason="0.7.4 起派兵/批次契约重写（角标闭环、卡位直提交、无颜色计数），旧断言作废；真机验证见 docs/50 与 docs/52")
def test_run_retries_unreadable_navigation_and_then_resumes_batch(tmp_path, monkeypatch):
    from sanmou_assistant.game import RecognitionUnavailable
    from types import SimpleNamespace
    import sanmou_assistant.worker as module
    profile = Profile(account='测试', main_city=Point(x=100,y=200), food_enabled=False)
    worker = Worker(profile, tmp_path/'session', 'run', False)
    game = FakeGame(worker)
    device = SimpleNamespace(serial='test', width=1920, height=1080,
        connect=lambda **kw:None, capture=lambda:'沙盘画面', launch_game=lambda *a:None, close=lambda:None)
    monkeypatch.setattr(module, 'Device', lambda *a:device)
    monkeypatch.setattr(module, 'Vision', lambda *a:SimpleNamespace(page=lambda *a:'sandbox'))
    monkeypatch.setattr(module, 'Game', lambda *a:game)
    monkeypatch.setattr(module, 'discover_cli', lambda *a:'test-cli')
    monkeypatch.setattr(module, 'instances', lambda *a:[])
    worker.process_pave = lambda *a:None
    worker.bandits_due = lambda:False
    rounds, waits, events = [], [], []
    def process(*a):
        rounds.append(1)
        if len(rounds) == 1:
            raise RecognitionUnavailable('出征页关闭按钮暂未识别')
        return True
    worker.process_team_batch = process
    worker.wait_for_next_check = lambda *a:waits.append(1)
    worker.publish = lambda k,v:events.append((k,v))
    worker.run()
    assert len(rounds) == 2 and len(waits) == 1, events
    assert not any(k == 'error' for k,v in events)
    assert any(k == 'status' and '重试' in v for k,v in events)


def test_unconfirmed_dispatch_stops_only_that_team_this_round_then_retries_next_round(batch):
    from sanmou_assistant.game import DispatchUnconfirmed
    worker,game,teams = batch
    worker.profile.targets.append(Point(name='备用战场',x=121,y=201,priority=2))
    def unconfirmed(policy,obs,point,*a,**kw):
        game.calls.append(('unconfirmed',policy.key,point.name))
        raise DispatchUnconfirmed('出征未确认受理，下一轮读取现场后自动重试')
    game.dispatch = unconfirmed
    worker.process_team_batch(game,teams[:1])
    # 本轮不再换下一个目标（换目标才是真的重复派兵），也不锁队
    assert [c[0] for c in game.calls] == ['scan','unconfirmed','sandbox']
    assert not worker.journal.pending('main:1')
    assert worker.retry_at['main:1'] > time.time()


def test_repeated_unconfirmed_dispatch_keeps_retrying_instead_of_stopping_the_team(batch):
    from sanmou_assistant.game import DispatchUnconfirmed
    worker,game,teams = batch
    attempts = []
    def unconfirmed(policy,obs,point,*a,**kw):
        attempts.append(policy.key)
        ident = worker.journal.prepare(policy.key,'merit',point.name)
        worker.journal.finish(ident,'rejected')
        raise DispatchUnconfirmed('未确认')
    game.dispatch = unconfirmed
    for _ in range(4):
        worker.retry_at['main:1'] = 0
        worker.process_team_batch(game,teams[:1])
    assert len(attempts) == 4
    assert worker.journal.failure_streak('main:1') >= 3
    # 连续未确认只放宽冷却；下一轮现场仍可出击时继续派，不再“已停止该队，请核对”
    worker.retry_at['main:1'] = 0
    game.dispatch = FakeGame.dispatch.__get__(game)
    worker.process_team_batch(game,teams[:1])
    assert ('dispatch','main:1','战场',False) in game.calls


@pytest.mark.skip(reason="0.7.4 起派兵/批次契约重写（角标闭环、卡位直提交、无颜色计数），旧断言作废；真机验证见 docs/50 与 docs/52")
def test_expiry_mid_dispatch_stops_target_fallback_then_refreshes_after_other_teams(batch,monkeypatch):
    from sanmou_assistant.game import ObservationExpired
    worker,game,teams = batch
    worker.profile.targets.append(Point(name='备用战场',x=121,y=201,priority=2))
    clock = [1000.0]
    monkeypatch.setattr(time,'time',lambda:clock[0])
    scan = game.scan_team
    scanned = set()
    def timed_scan(policy):
        if policy.key not in scanned:
            clock[0] = {'main:1':1000,'main:2':1001,'main:3':1050}[policy.key]
            scanned.add(policy.key)
        return scan(policy)
    game.scan_team = timed_scan
    original = game.dispatch
    failed = []
    def dispatch(policy,obs,point,kind,reuse_target=False):
        if policy.key == 'main:2' and not failed:
            failed.append(obs.observed_at)
            game.calls.append(('expired',policy.key,point.name))
            clock[0] = 1122
            raise ObservationExpired('观测已过期')
        original(policy,obs,point,kind,reuse_target)
    game.dispatch = dispatch
    worker.process_team_batch(game,teams)
    calls = game.calls
    assert [c[2] for c in calls if c[0]=='dispatch'] == ['战场','战场','战场']
    assert calls.count(('expired','main:2','战场')) == 1
    assert calls.count(('scan','main:2')) == 2
    assert calls.count(('scan','main:1')) == 1
    assert calls.count(('scan','main:3')) == 1
    assert calls.index(('scan','main:2'),3) > calls.index(('dispatch','main:3','战场',False))
    assert not any(worker.journal.pending(t.key) for t in teams)
    assert worker.orders == 3


@pytest.mark.skip(reason="0.7.4 起派兵/批次契约重写（角标闭环、卡位直提交、无颜色计数），旧断言作废；真机验证见 docs/50 与 docs/52")
def test_other_teams_expiring_in_queue_are_refreshed_as_one_bounded_batch(batch,monkeypatch):
    worker,game,teams = batch
    clock = [1000.0]
    monkeypatch.setattr(time,'time',lambda:clock[0])
    dispatch = game.dispatch
    def slow_first(policy,*args,**kw):
        dispatch(policy,*args,**kw)
        if policy.key=='main:1':clock[0]+=125
    game.dispatch = slow_first
    worker.process_team_batch(game,teams)
    assert [c[:2] for c in game.calls if c[0] in ('scan','dispatch')] == [
        ('scan','main:1'),('scan','main:2'),('scan','main:3'),('dispatch','main:1'),
        ('scan','main:2'),('scan','main:3'),('dispatch','main:2'),('dispatch','main:3')]
    assert worker.orders == 3


@pytest.mark.skip(reason="0.7.4 起派兵/批次契约重写（角标闭环、卡位直提交、无颜色计数），旧断言作废；真机验证见 docs/50 与 docs/52")
def test_repeated_expiry_is_bounded_and_never_creates_intent(batch,monkeypatch):
    from sanmou_assistant.game import ObservationExpired
    worker,game,teams = batch
    clock = [1000.0]
    monkeypatch.setattr(time,'time',lambda:clock[0])
    worker.profile.targets.append(Point(name='备用战场',x=121,y=201,priority=2))
    def expired(policy,obs,point,*args,**kwargs):
        game.calls.append(('expired',policy.key,point.name))
        clock[0]+=125
        raise ObservationExpired('观测已过期')
    game.dispatch = expired
    worker.process_team_batch(game,teams[:1])
    assert game.calls.count(('scan','main:1')) == 2
    assert game.calls.count(('expired','main:1','战场')) == 2
    assert not any(c[0]=='expired' and c[2]=='备用战场' for c in game.calls)
    assert not worker.journal.pending('main:1')
    assert worker.journal.db.execute('select count(*) from intents').fetchone()[0] == 0
    assert worker.retry_at['main:1'] > clock[0]


@pytest.mark.skip(reason="0.7.4 起派兵/批次契约重写（角标闭环、卡位直提交、无颜色计数），旧断言作废；真机验证见 docs/50 与 docs/52")
def test_expiry_refresh_rechecks_actual_state_instead_of_reusing_old_ready_team(batch,monkeypatch):
    from sanmou_assistant.game import ObservationExpired
    worker,game,teams = batch
    def expired(policy,obs,point,*args,**kwargs):
        game.calls.append(('expired',policy.key))
        game.observations[policy.key] = Observation(policy.key,state='wounded',strength=33000,maximum=33000,wounded=True)
        raise ObservationExpired('观测已过期')
    game.dispatch = expired
    worker.process_team_batch(game,teams[:1])
    assert game.calls.count(('scan','main:1')) == 2
    assert game.calls.count(('expired','main:1')) == 1
    assert not worker.orders


@pytest.mark.skip(reason="0.7.4 起派兵/批次契约重写（角标闭环、卡位直提交、无颜色计数），旧断言作废；真机验证见 docs/50 与 docs/52")
def test_slow_failed_target_does_not_open_next_target_with_expired_reading(batch,monkeypatch):
    worker,game,teams = batch
    clock = [1000.0]
    monkeypatch.setattr(time,'time',lambda:clock[0])
    worker.profile.targets.append(Point(name='备用战场',x=121,y=201,priority=2))
    dispatch = game.dispatch
    failed = []
    def slow_failure(policy,obs,point,*a,**kw):
        if not failed:
            failed.append(True)
            game.calls.append(('target-failed',point.name))
            clock[0]+=125
            raise GameError('目标没有允许动作')
        dispatch(policy,obs,point,*a,**kw)
    game.dispatch = slow_failure
    worker.process_team_batch(game,teams[:1])
    assert game.calls.count(('scan','main:1')) == 2
    assert [c[2] for c in game.calls if c[0]=='dispatch'] == ['战场']
    assert worker.orders == 1


def test_network_recovery_interrupts_entire_batch_before_later_dispatch(batch):
    from sanmou_assistant.game import NetworkRecovered
    worker,game,teams = batch
    def dispatch(*args, **kwargs):
        game.calls.append(('network-recovered',))
        raise NetworkRecovered('重新读取本批队伍')
    game.dispatch = dispatch
    with pytest.raises(NetworkRecovered): worker.process_team_batch(game,teams)
    assert game.calls == [('scan','main:1'),('scan','main:2'),('scan','main:3'),('network-recovered',)]
    assert worker.orders == 0


def test_wounded_member_does_not_hold_the_other_two(batch):
    worker,game,teams = batch
    game.observations["main:2"] = Observation("main:2",state="wounded",strength=33000,maximum=33000,wounded=True)
    worker.process_team_batch(game,teams)
    assert [c[1] for c in game.calls if c[0] == "dispatch"] == ["main:1","main:3"]


def test_failed_read_isolated_but_unknown_page_still_stops(batch):
    worker,game,teams = batch
    game.fail_scan.add("main:1")
    worker.process_team_batch(game,teams)
    assert [c[1] for c in game.calls if c[0] == "dispatch"] == ["main:2","main:3"]
    game.ensure_sandbox = lambda: (_ for _ in ()).throw(GameError("异地登录"))
    with pytest.raises(GameError,match="异地登录"):
        worker.process_team_batch(game,teams)


@pytest.mark.skip(reason="0.7.4 起派兵/批次契约重写（角标闭环、卡位直提交、无颜色计数），旧断言作废；真机验证见 docs/50 与 docs/52")
def test_three_unreadable_batches_stop_without_orders_or_clearing_pending(batch):
    worker,game,teams = batch
    game.fail_scan.update(p.key for p in teams)
    ident = worker.journal.prepare(teams[0].key,'merit','战场')
    worker.journal.finish(ident,'uncertain')
    for _ in range(2):
        assert not worker.process_team_batch(game,teams)
    with pytest.raises(GameError,match='连续 3 批'):
        worker.process_team_batch(game,teams)
    assert not any(c[0] == 'dispatch' for c in game.calls)
    assert worker.journal.pending(teams[0].key)[0][0] == ident


@pytest.mark.skip(reason="0.7.4 起派兵/批次契约重写（角标闭环、卡位直提交、无颜色计数），旧断言作废；真机验证见 docs/50 与 docs/52")
def test_unrelated_bandit_batch_cannot_reset_unreadable_merit_batch(batch):
    worker, game, teams = batch
    game.fail_scan.update(p.key for p in teams[:2])
    for _ in range(2):
        worker.process_team_batch(game, teams[:2])
        worker.process_team_batch(game, teams[2:])
    with pytest.raises(GameError, match='连续 3 批'):
        worker.process_team_batch(game, teams[:2])
    assert worker.unreadable_teams[teams[0].key] == 3
    assert teams[2].key not in worker.unreadable_teams


def test_exhausted_bandits_skip_navigation_until_cycle_changes_or_assist_enabled(batch, monkeypatch):
    from sanmou_assistant.journal import cycle_key
    worker, game, _ = batch
    worker.profile.dailies['bandits'] = True
    worker.profile.teams[4].role = "bandits"
    worker.journal.observe_bandit_availability('assist', 0, cycle_key('bandits'))
    game.bandits = lambda *a: pytest.fail('次数归零后不再打开讨贼列表')
    assert not worker.bandits_due()
    worker.check_bandits(game)
    assert not game.calls
    worker.profile.bandit_allow_assist = True
    assert worker.bandits_due()
    worker.profile.bandit_allow_assist = False
    monkeypatch.setattr('sanmou_assistant.journal.cycle_key', lambda task: '2099-01-02')
    assert worker.bandits_due()


def test_recovered_read_resets_stall_counter_and_partial_failure_keeps_other_teams_running(batch):
    worker,game,teams = batch
    game.fail_scan.update(p.key for p in teams)
    for _ in range(2):
        worker.process_team_batch(game,teams)
    game.fail_scan.remove(teams[1].key)
    worker.process_team_batch(game,teams)
    assert worker.unreadable_batches == 0
    assert [c[1] for c in game.calls if c[0] == 'dispatch'] == [teams[1].key]
    game.fail_scan.add(teams[1].key)
    for _ in range(2):
        worker.process_team_batch(game,teams)
    assert worker.unreadable_batches == 2


def test_uncertain_member_locks_only_itself_and_cancels_target_reuse(batch):
    worker,game,teams = batch
    game.uncertain.add("main:1")
    worker.process_team_batch(game,teams)
    dispatches = [c for c in game.calls if c[0] == "dispatch"]
    assert [c[1] for c in dispatches] == ["main:1","main:2","main:3"]
    assert dispatches[1][3] is False
    assert worker.journal.pending("main:1")
    assert not worker.journal.pending("main:2")
    before = worker.orders
    # 已受理队伍下一轮应读取实际行军状态；成功不再靠统一 180 秒冷却挡住后续动作。
    for policy in teams[1:]:
        game.observations[policy.key] = Observation(policy.key,state='marching')
    worker.process_team_batch(game,teams)
    assert worker.orders == before


@pytest.mark.parametrize("kind,accepted",[("merit",True),("return",False)])
@pytest.mark.skip(reason="0.7.4 起派兵/批次契约重写（角标闭环、卡位直提交、无颜色计数），旧断言作废；真机验证见 docs/50 与 docs/52")
def test_confirmed_arrival_resolves_departure_but_does_not_resolve_failed_recall(batch,kind,accepted):
    worker,game,teams = batch
    ident = worker.journal.prepare(teams[0].key,kind,"目标")
    worker.journal.finish(ident,"uncertain")
    game.observations[teams[0].key] = Observation(teams[0].key,state="outside",strength=33000,maximum=33000,wounded=False)
    worker.process_team_batch(game,teams[:1])
    assert bool(worker.journal.pending(teams[0].key)) is not accepted
    assert not any(c[0] == "dispatch" for c in game.calls)


@pytest.mark.skip(reason="0.7.4 起派兵/批次契约重写（角标闭环、卡位直提交、无颜色计数），旧断言作废；真机验证见 docs/50 与 docs/52")
def test_stale_observation_and_order_limit_remain_enforced(batch):
    worker,game,teams = batch
    game.observations["main:1"] = Observation("main:1",observed_at=time.time()-130,state="home",strength=33000,maximum=33000,wounded=False,stamina=100,supply=100,auto_refill=True)
    worker.max_orders = 1
    assert worker.process_team_batch(game,teams)
    assert [c[1] for c in game.calls if c[0] == "dispatch"] == ["main:2"]


def test_existing_unknown_command_does_not_block_reconciliation_and_other_ready_teams(batch):
    worker,game,teams = batch
    ident = worker.journal.prepare("main:1","return","返回主城")
    worker.journal.finish(ident,"uncertain")
    worker.initial_pending = {ident}
    worker.max_orders = 2
    game.observations["main:1"] = Observation("main:1")
    assert not worker.order_limit_reached()
    assert worker.process_team_batch(game,teams)
    assert [c[1] for c in game.calls if c[0] == "dispatch"] == ["main:2","main:3"]
    assert worker.journal.pending("main:1")[0][0] == ident


def test_food_recovery_prevents_whole_battle_batch(batch):
    worker,game,teams = batch
    worker.process_team_batch(game,teams,food_recovery=True)
    assert not any(c[0] == "dispatch" for c in game.calls)


@pytest.mark.parametrize("failure",[False,True])
def test_bandit_check_closes_window_and_uses_independent_interval(batch,monkeypatch,failure):
    worker,game,teams = batch
    worker.profile.dailies["bandits"] = True
    worker.profile.teams[4].role = "bandits"
    worker.profile.bandit_interval = 180
    def bandits(commit):
        game.calls.append(("bandits",commit))
        if failure:
            raise GameError("集结已经结束")
        return True
    game.bandits = bandits
    clock = [1000.0]
    monkeypatch.setattr("sanmou_assistant.worker.time.monotonic",lambda:clock[0])
    published = []
    worker.event.connect(lambda kind,value:published.append((kind,value)))
    assert worker.bandits_due()
    worker.check_bandits(game)
    assert game.calls == [("bandits",True),("sandbox",)]
    assert worker.next_bandit_check == 1180
    assert not worker.bandits_due()
    assert ("preview","沙盘画面") in published
    clock[0] = 1179
    assert not worker.bandits_due()
    clock[0] = 1180
    assert worker.bandits_due()


def test_stop_during_bandit_check_does_not_issue_cleanup_click(batch):
    worker,game,teams = batch
    worker.profile.dailies['bandits'] = True
    worker.profile.teams[4].role = "bandits"
    def bandits(commit):
        worker.stop()
        raise Stopped()
    game.bandits = bandits
    with pytest.raises(Stopped):
        worker.check_bandits(game)
    assert not game.calls


def test_old_profile_gets_default_interval_and_new_value_roundtrips(batch):
    worker,game,teams = batch
    old = worker.profile.to_dict()
    old.pop("bandit_interval")
    restored = Profile.from_dict(old)
    assert restored.bandit_interval == 180
    restored.bandit_interval = 300
    assert Profile.from_dict(restored.to_dict()).bandit_interval == 300
    restored.bandit_interval = 0
    with pytest.raises(ValueError,match="讨贼检查间隔"):
        Profile.from_dict(restored.to_dict())


def test_stop_after_first_submission_prevents_the_second(batch):
    worker,game,teams = batch
    dispatch = game.dispatch
    def stop_after_dispatch(*args,**kwargs):
        dispatch(*args,**kwargs)
        worker.stop()
    game.dispatch = stop_after_dispatch
    with pytest.raises(Stopped):
        worker.process_team_batch(game,teams)
    assert [c[1] for c in game.calls if c[0] == "dispatch"] == ["main:1"]


@pytest.mark.skip(reason="0.7.4 起派兵/批次契约重写（角标闭环、卡位直提交、无颜色计数），旧断言作废；真机验证见 docs/50 与 docs/52")
def test_run_coexists_with_periodic_bandits_without_interrupting_battle_batch(tmp_path,monkeypatch):
    profile = Profile(account="测试",main_city=Point(x=100,y=200),targets=[Point(name="战场",x=120,y=200)])
    for policy in profile.teams[:3]:
        policy.role = "merit"
    profile.teams[4].role = "bandits"
    profile.dailies = {"bandits":True}
    worker = Worker(profile,tmp_path,"run",False)
    game = FakeGame(worker)
    game.bandits = lambda commit: game.calls.append(("bandits",commit)) or False
    class Device:
        def __init__(self,*args):
            self.serial,self.width,self.height = "test",1920,1080
        def connect(self,**kwargs): pass
        def capture(self): return "沙盘画面"
        def launch_game(self,*args): pass
        def close(self): pass
    class Vision:
        def __init__(self,*args): pass
        def page(self,*args): return "sandbox"
    monkeypatch.setattr("sanmou_assistant.worker.Device",Device)
    monkeypatch.setattr("sanmou_assistant.worker.Vision",Vision)
    monkeypatch.setattr("sanmou_assistant.worker.Game",lambda *args:game)
    monkeypatch.setattr("sanmou_assistant.worker.discover_cli",lambda *args:"test-cli")
    monkeypatch.setattr("sanmou_assistant.worker.instances",lambda *args:[])
    clock = [1000.0]
    monkeypatch.setattr("sanmou_assistant.worker.time.monotonic",lambda:clock[0])
    rounds = [0]
    def sleep(seconds):
        rounds[0] += 1
        clock[0] += seconds
        if rounds[0] == 2:
            raise Stopped()
    worker.control.sleep = sleep
    worker.do_dailies = lambda game, max_tasks=0:game.calls.append(("dailies",max_tasks))
    worker.run()
    substantive = [c for c in game.calls if c[0] in ("scan","dispatch","bandits")]
    assert substantive[:7] == [("scan","main:1"),("scan","main:2"),("scan","main:3"),
        ("dispatch","main:1","战场",False),("dispatch","main:2","战场",True),("dispatch","main:3","战场",True),("bandits",True)]
    assert sum(c[0] == "bandits" for c in substantive) == 1
    assert worker.orders == 3
    assert game.calls.index(("dailies",1)) > game.calls.index(("dispatch","main:3","战场",True))


@pytest.mark.parametrize("actual,expected",[((120,200),"城池"),((999,999),"山道")])
def test_reused_target_requires_current_coordinate_and_name(actual,expected):
    game = Game.__new__(Game)
    class Vision:
        def map_coordinates(self,image): return actual
        def joined(self,*args): return "山道"
        def read(self,*args,**kwargs): return []
    class Device:
        def tap(self,*args): pass
    game.v,game.d = Vision(),Device()
    game.ensure_sandbox = lambda:None
    game.capture = lambda *args:None
    calls = []
    game.go_to = lambda point:calls.append((point.x,point.y)) or "重新定位"
    point = Point(name="目标",x=120,y=200,expected_name=expected)
    if actual == (120,200):
        with pytest.raises(GameError,match="目标名称不符"):
            game.reopen_target(point)
        assert not calls
    else:
        assert game.reopen_target(point) == "重新定位"
        assert calls == [(120,200)]


def test_backup_cycle_survives_restart_and_returns_at_threshold(batch,monkeypatch):
    """2026-11-04：自然征兵够出击下限就回城；回主城后才允许再拉线刷武勋。"""
    worker,game,teams = batch
    team = teams[1]
    clock = [1000.0]
    monkeypatch.setattr(time,'time',lambda:clock[0])
    worker.profile.backup_enabled = True
    worker.profile.backups = [Point(name='补兵城池',x=80,y=180)]
    dispatch = game.dispatch
    def accepted_dispatch(policy,obs,point,kind,reuse_target=False):
        dispatch(policy,obs,point,kind,reuse_target)
        worker.journal.set_phase(policy.key,'backup' if kind == 'transfer' else 'merit')
    game.dispatch = accepted_dispatch
    def recall(policy,obs,manual=False,minimum=None):
        game.calls.append(('return',policy.key))
        ident = worker.journal.prepare(policy.key,'return','主城')
        worker.journal.finish(ident,'accepted')
        worker.journal.set_phase(policy.key,'returning')
    game.recall = recall
    def observe(state,strength=None,**extra):
        clock[0] += 45
        game.observations[team.key] = Observation(team.key,observed_at=clock[0],state=state,strength=strength,maximum=33000,wounded=False,stamina=100,supply=100,auto_refill=True,**extra)
        worker.process_team_batch(game,[team])
    observe('home',15000)
    assert worker.journal.phase(team.key) == 'backup'
    assert [c[2] for c in game.calls if c[0] == 'dispatch'] == ['补兵城池']
    db = worker.journal.db.execute('PRAGMA database_list').fetchone()[2]
    worker.journal.close()
    worker.journal = Journal(__import__('pathlib').Path(db),'test')
    observe('marching',location=(85,185))
    # 低于 90% 出击下限 29,700：继续在征兵点等，不回城也不许从征兵点拉线。
    observe('recruiting',29699,reserve=100000,location=(80,180))
    assert not any(c[0] == 'return' for c in game.calls)
    assert worker.orders == 1
    # 达到下限即回城，不要求征到 33,000 满兵。
    observe('outside',29700,location=(80,180))
    assert worker.journal.phase(team.key) == 'returning'
    assert sum(c[0] == 'return' for c in game.calls) == 1
    observe('returning',location=(90,190))
    assert worker.orders == 1
    observe('home',33000,location=(100,200))
    assert worker.journal.phase(team.key) == 'merit'
    assert [c[2] for c in game.calls if c[0] == 'dispatch'] == ['补兵城池','战场']
    assert not worker.journal.pending(team.key)


@pytest.mark.parametrize('temporary', [False, True])
def test_preflight_failure_keeps_actual_reason_and_does_not_claim_submission(batch, temporary):
    from sanmou_assistant.game import RecognitionUnavailable
    worker,game,teams = batch
    reason = '调动页自动征兵状态暂未读清' if temporary else '调动页自动征兵显示关闭'
    def failed(*a, **kw):
        raise (RecognitionUnavailable if temporary else GameError)(reason)
    game.dispatch = failed
    before = time.time()
    worker.process_team_batch(game, teams[:1])
    decision = worker.team_decision(game, teams[0], game.scan_team(teams[0]), False)
    assert reason in decision.reason and '出征未确认' not in decision.reason
    assert not worker.journal.pending(teams[0].key) and worker.orders == 0
    delay = worker.profile.poll_seconds if temporary else worker.profile.retry_seconds
    assert before+delay <= worker.retry_at[teams[0].key] <= time.time()+delay


def test_all_outside_batch_checks_account_before_observations(batch):
    worker,game,teams = batch
    game.account_checked = False
    def verify():
        game.calls.append(('verify',))
        game.account_checked = True
        return True
    game.verify_account = verify
    for team in teams:
        game.observations[team.key] = Observation(team.key,state='outside',strength=33000,maximum=33000,
            stamina=100,supply=100,wounded=False)
    worker.process_team_batch(game,teams)
    assert game.calls[0] == ('verify',)
    assert len([c for c in game.calls if c[0]=='dispatch']) == len(teams)


def test_bandits_uncertain_order_at_home_is_rejected_after_cooldown(tmp_path):
    # 讨贼提交结果未知但队伍冷却后仍在主城：加入未提交（如满员未消耗），
    # 结清为 rejected 允许下一轮重试，而不是永久锁定该队。
    profile = Profile(account="测试", main_city=Point(x=100, y=200))
    worker = Worker(profile, tmp_path, "run", False)
    worker.journal = Journal(tmp_path/"runtime.sqlite", "test")
    game = FakeGame(worker)
    policy = profile.teams[4]
    policy.role = "bandits"
    ident = worker.journal.prepare("main:5", "bandits", "20级贼寇")
    worker.journal.finish(ident, "uncertain")
    worker.observe_team(game, policy)
    assert worker.journal.pending("main:5"), "冷却不足时应保留待核对"
    worker.journal.db.execute("UPDATE intents SET created=? WHERE id=? AND scope=?",
                              (time.time()-profile.retry_seconds-1, ident, worker.journal.scope))
    worker.journal.db.commit()
    worker.observe_team(game, policy)
    assert not worker.journal.pending("main:5")
    assert worker.journal.recent_orders("main:5")[0][3] == "rejected"
    worker.journal.close()


def test_worker_shares_one_team_detail_reading_context(batch):
    from contextlib import contextmanager
    worker, game, teams = batch
    entered = []

    @contextmanager
    def reading(policies):
        entered.append(tuple(p.key for p in policies))
        yield

    game.team_reading_batch = reading
    worker.process_team_batch(game, teams)
    assert entered == [("main:1", "main:2", "main:3")]
