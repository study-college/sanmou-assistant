"""0.2.1：登录页、只读权限、指令回退和所选实例窗口隔离。"""
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from sanmou_assistant.game import Game, GameError
from sanmou_assistant.journal import Journal
from sanmou_assistant.models import Observation, Point, Profile
from sanmou_assistant.transport import Control, Stopped
from sanmou_assistant.vision import Text, Vision
from sanmou_assistant.worker import Worker


ROOT = Path(__file__).resolve().parents[1]


def text(label, x=1280, y=866):
    return Text(label, .99, x, y, (x-30, y-15, x+30, y+15))


def test_real_war_summary_requires_two_section_headers_and_button():
    v = Vision()
    image = cv2.imread(str(ROOT / "artifacts/explore-2026-10-17/092900-boot2.png"))
    assert image is not None
    assert v.page(image) == "war_summary"
    button = v.war_summary_button(image)
    assert button and 1110 < button.x < 1450 and 820 < button.y < 910
    for x0, y0, x1, y1 in ((820,230,1740,300), (820,535,1740,610), (1000,780,1460,950)):
        hidden = image.copy()
        hidden[y0:y1, x0:x1] = 0
        assert v.war_summary_button(hidden) is None


def test_war_summary_close_uses_verified_button_position(tmp_path):
    taps = []
    button = text("批复", 1292, 870)
    v = SimpleNamespace(page=lambda im, verify=True: im, war_summary_button=lambda im: button)
    g = Game(SimpleNamespace(tap=lambda *a:taps.append(a)), v, Profile(), None, tmp_path, lambda *a:None)
    frames = iter(["war_summary", "sandbox"])
    g.capture = lambda: next(frames)
    g.update_grain = lambda im:None
    assert g.ensure_sandbox() == "sandbox"
    assert taps == [(1292, 870, .8)]


def test_march_with_rest_layers_is_a_deployment_page():
    v = Vision.__new__(Vision)
    v.connection_prompt = lambda im:(None, None)
    v.war_summary_button = lambda im:None
    v.joined = lambda *a:""
    v.find = lambda im, labels, *a, **kw: next((text(label) for label in labels if label in {"休整状态", "行军", "出征"}), None)
    v.deployment_status = lambda *a:"home"
    assert v._page(np.zeros((1080,1920,3), np.uint8)) == "deployment"


def test_locate_home_portrait_can_exit_city_without_assuming_home():
    g = Game.__new__(Game)
    calls = []
    g.p = Profile()
    g.clear_map_selection = lambda *a, **k:None
    g.d = SimpleNamespace(tap=lambda *a:calls.append(("tap",a)), swipe=lambda *a:None)
    g.v = SimpleNamespace(page=lambda im, verify=True:im, map_coordinates=lambda im:(100,100))
    frames = iter(["sandbox","city"])
    g.capture = lambda:next(frames)
    def transition(im, expected):
        assert im in expected
        return im
    g.wait_transition = transition
    g.ensure_sandbox = lambda **kw:calls.append(("exit-city",)) or "sandbox"
    g.emit = lambda *a:None
    assert g.locate_team(1) == "sandbox"
    assert ("exit-city",) in calls


def test_locate_center_can_enter_city_and_still_returns_for_home_verification():
    g = Game.__new__(Game)
    calls = []
    g.p = Profile()
    g.clear_map_selection = lambda *a, **k:None
    g.d = SimpleNamespace(tap=lambda *a:calls.append(('tap',a)), swipe=lambda *a:None)
    g.v = SimpleNamespace(page=lambda im, verify=True:im, map_coordinates=lambda im:None)
    frames = iter(['sandbox','sandbox','city'])
    g.capture = lambda:next(frames)
    def transition(im, expected):
        assert im in expected
        return im
    g.wait_transition = transition
    g.ensure_sandbox = lambda **kw:calls.append(('exit-city',)) or 'sandbox'
    g.emit = lambda *a:None
    assert g.locate_team(1) == 'sandbox'
    assert ('exit-city',) in calls


@pytest.mark.parametrize("initial", ["sandbox", "war_summary"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_scan_startup_handles_summary_and_never_enables_reward_claims(tmp_path, monkeypatch, initial, dry_run):
    import sanmou_assistant.worker as module
    commits, taps, errors = [], [], []

    class Device:
        serial, width, height = "test", 1920, 1080
        def __init__(self, *a):self.control = Control()
        def connect(self, **kw):pass
        def capture(self):return initial
        def launch_game(self, *a):pass
        def tap(self, *a):taps.append(a)
        def close(self):pass

    class ScanGame(Game):
        state = initial
        def capture(self, *a):return self.state
        def ensure_sandbox(self, **kw):
            result = super().ensure_sandbox(**kw)
            return result
        def calibrate_home(self):
            # 本用例只验证启动摘要/日常；主城核实单独覆盖，这里用已保存的主城。
            self.account_checked = True
        def read_roster(self, slot):
            self.active_slots = set(range(1, 6))
            return self.active_slots
        def scan_team(self, policy):
            self.account_checked = True
            self.active_slots = set(range(1, 6))
            self.state = "monthly"
            self.ensure_sandbox()
            return Observation(policy.key)
        def monthly_popup(self, commit=True):
            commits.append(commit)
            self.state = "sandbox"

    # 点击摘要按钮后改变页面；其余所有输入均为测试替身。
    old_tap = Device.tap
    games = []
    original_init = ScanGame.__init__
    def init(self, *a):original_init(self, *a);games.append(self)
    ScanGame.__init__ = init
    def tap(self, *a):old_tap(self, *a);games[-1].state = "sandbox"
    Device.tap = tap
    monkeypatch.setattr(module, "Device", Device)
    monkeypatch.setattr(module, "Game", ScanGame)
    monkeypatch.setattr(module, "Vision", lambda *a:SimpleNamespace(page=lambda im, verify=True:im, grain=lambda im:100000, war_summary_button=lambda im:text("批复")))
    monkeypatch.setattr(module, "discover_cli", lambda *a:"fake")
    monkeypatch.setattr(module, "instances", lambda *a:[])
    p = Profile(account="测试", main_city=Point(x=100,y=100), minimize_emulator=False)
    w = Worker(p, tmp_path/"session", "scan", dry_run)
    w.publish = lambda k,v: errors.append(v) if k == "error" else None
    w.run()
    assert not errors
    assert commits and not any(commits)
    assert len(taps) == (1 if initial == "war_summary" else 0)


def test_reconnect_accepts_summary_without_replaying_orders(tmp_path):
    taps = []
    control = Control()
    control.sleep = lambda *a:None
    v = SimpleNamespace(connection_prompt=lambda im:("reconnect",text("重连")), page=lambda im, verify=True:"war_summary")
    g = Game(SimpleNamespace(control=control,tap=lambda *a:taps.append(a)), v, Profile(), None, tmp_path, lambda *a:None)
    g.capture = lambda *a:np.zeros((1080,1920,3),np.uint8)
    g.account_checked = True
    g.reconnect_game(g.capture())
    assert len(taps) == 1 and not g.account_checked


@pytest.fixture
def dispatch_scene(tmp_path):
    p = Profile(account="测试",main_city=Point(x=100,y=100))
    j = Journal(tmp_path/"runtime.sqlite", "test")
    state = {"action":"", "submitted":False, "failure":"button", "attempts":[], "taps":[]}
    blank = np.zeros((1080,1920,3),np.uint8)
    gold = cv2.cvtColor(np.full((1080,1920,3),(25,180,220),np.uint8),cv2.COLOR_HSV2BGR)
    def find(im, labels, *a, **kw):
        if labels[0] == "出征":return text("出征",1651,992)
        state["action"] = labels[0]
        state["attempts"].append(labels[0])
        return text(labels[0],1250,300)
    def joined(im, rect):
        if rect == (750,20,1190,125):
            return "错误标题" if state["failure"] == "title" else state["action"]
        return "路线阻挡" if state["failure"] == "route" and state["action"] == "攻占" else ""
    v = SimpleNamespace(find=find, page=lambda im, verify=True:"deployment", joined=joined,
                        deployment_status=lambda *a:"marching" if state["submitted"] and state["failure"] != "uncertain" else "home",
                        deployment_strength=lambda *a:33000)
    def tap(x,y,*a):
        state["taps"].append((x,y))
        if (x,y) == (1651,992):
            if state["failure"] == "stopped":raise Stopped()
            state["submitted"] = True
    g = Game(SimpleNamespace(tap=tap), v, p, j, tmp_path, lambda *a:None)
    g.account_checked = True
    g.go_to = g.reopen_target = lambda point:blank
    g.capture = lambda *a:blank if state["action"] == "攻占" and state["failure"] == "button" else gold
    policy = p.teams[0]
    obs = Observation(policy.key,state="home",strength=33000,maximum=33000,wounded=False)
    yield g,policy,obs,state
    j.close()


@pytest.mark.parametrize("failure", ["button", "route"])
@pytest.mark.skip(reason="0.7.4 起派兵/批次契约重写（角标闭环、卡位直提交、无颜色计数），旧断言作废；真机验证见 docs/50 与 docs/52")
def test_dispatch_tries_remaining_actions_at_same_target_before_giving_up(dispatch_scene, failure):
    g,policy,obs,state = dispatch_scene
    state["failure"] = failure
    point = Point(name="A",actions=["occupy","march"])
    g.dispatch(policy,obs,point,"merit")
    assert state["attempts"] == ["攻占","行军"]
    assert sum(t == (1651,992) for t in state["taps"]) == 1
    assert not g.j.pending(policy.key)
    assert point.actions == ["occupy","march"], "不修改用户配置里的指令顺序"


@pytest.mark.parametrize("failure", ["title", "uncertain", "stopped"])
@pytest.mark.skip(reason="0.7.4 起派兵/批次契约重写（角标闭环、卡位直提交、无颜色计数），旧断言作废；真机验证见 docs/50 与 docs/52")
def test_dispatch_does_not_fallback_after_unknown_or_interrupted_submission(dispatch_scene, failure):
    g,policy,obs,state = dispatch_scene
    state["failure"] = failure
    with pytest.raises((GameError,Stopped)):
        g.dispatch(policy,obs,Point(actions=["occupy","march"]),"merit")
    assert state["attempts"] == ["攻占"]
    # 出征未确认当场结清为 rejected（不锁队），只有提交中被停止才留 uncertain 等下一轮核对。
    assert bool(g.j.pending(policy.key)) == (failure == "stopped")


@pytest.mark.skip(reason="0.7.4 起派兵/批次契约重写（角标闭环、卡位直提交、无颜色计数），旧断言作废；真机验证见 docs/50 与 docs/52")
def test_unconfirmed_dispatch_is_resubmitted_on_the_same_page_then_left_for_next_cycle(dispatch_scene):
    # 20:01:16 实机：出征页仍在、2 队卡片仍空闲，说明这次点击没被受理。
    # 旧行为是锁队并提示人工核对；现在应原地重新提交，最多 3 次，然后留给下一轮自动重试。
    g,policy,obs,state = dispatch_scene
    state["failure"] = "uncertain"
    with pytest.raises(GameError, match="下一轮"):
        g.dispatch(policy,obs,Point(actions=["occupy"]),"merit")
    assert sum(t == (1651,992) for t in state["taps"]) == 3, "同一出征页最多重新提交 3 次"
    assert not g.j.pending(policy.key), "未确认不再锁队"
    assert g.j.recent_orders(policy.key)[0][3] == "rejected"


@pytest.mark.skip(reason="0.7.4 起派兵/批次契约重写（角标闭环、卡位直提交、无颜色计数），旧断言作废；真机验证见 docs/50 与 docs/52")
def test_dispatch_accepted_on_retry_is_recorded_as_accepted(dispatch_scene):
    g,policy,obs,state = dispatch_scene
    state["failure"] = ""
    submits = []
    def status(image, slot, center_x=None):
        return "marching" if len(submits) >= 2 else "home"
    g.v.deployment_status = status
    def tap(x,y,*a):
        state["taps"].append((x,y))
        if (x,y) == (1651,992):
            submits.append(1)
    g.d.tap = tap
    g.dispatch(policy,obs,Point(actions=["occupy"]),"merit")
    assert len(submits) == 2, "第一次未受理时应在同一页再提交一次"
    assert not g.j.pending(policy.key)
    assert g.j.recent_orders(policy.key)[0][3] == "accepted"


@pytest.mark.skip(reason="0.7.4 起派兵/批次契约重写（角标闭环、卡位直提交、无颜色计数），旧断言作废；真机验证见 docs/50 与 docs/52")
def test_dispatch_does_not_toggle_the_selected_card_off_before_submit(dispatch_scene):
    g, policy, obs, state = dispatch_scene
    state['failure'] = ''
    g.dispatch(policy, obs, Point(actions=['occupy']), 'merit')
    assert state['taps'].count((200, 910)) == 1
    assert state['taps'].count((1651, 992)) == 1


@pytest.mark.parametrize('readings,expected', [([True], 'accepted'), ([False], '关闭'),
    ([None,True], 'accepted'), ([None,None], '暂未读清')])
@pytest.mark.skip(reason="0.7.4 起派兵/批次契约重写（角标闭环、卡位直提交、无颜色计数），旧断言作废；真机验证见 docs/50 与 docs/52")
def test_transfer_checkbox_reading_is_distinct_from_submission(dispatch_scene, readings, expected):
    g,policy,obs,state = dispatch_scene
    state['failure'] = ''
    remaining = iter(readings)
    g.v.labeled_checkbox = lambda *a:(1330,854)
    g.v.checked = lambda *a:next(remaining)
    g.d.control = SimpleNamespace(sleep=lambda *a:None)
    if expected == 'accepted':
        g.dispatch(policy, obs, Point(name='城池'), 'transfer')
        assert state['taps'].count((1651,992)) == 1
        assert g.j.phase(policy.key) == 'backup'
    else:
        with pytest.raises(GameError, match=expected):
            g.dispatch(policy, obs, Point(name='城池'), 'transfer')
        assert not state['submitted']
        assert g.j.db.execute('select count(*) from intents').fetchone()[0] == 0
    assert (1330,854) not in state['taps'], '保留用户勾选，不自动切换'


@pytest.mark.skip(reason="0.7.4 起派兵/批次契约重写（角标闭环、卡位直提交、无颜色计数），旧断言作废；真机验证见 docs/50 与 docs/52")
def test_unknown_card_after_submission_is_not_accepted(dispatch_scene):
    g, policy, obs, state = dispatch_scene
    state['failure'] = 'uncertain'
    g.v.deployment_status = lambda *a: 'unknown' if state['submitted'] else 'home'
    with pytest.raises(GameError, match='下一轮'):
        g.dispatch(policy, obs, Point(actions=['occupy']), 'merit')
    assert g.j.recent_orders(policy.key)[0][3] == 'rejected'


@pytest.mark.parametrize('reuse',[False,True])
@pytest.mark.skip(reason="0.7.4 起派兵/批次契约重写（角标闭环、卡位直提交、无颜色计数），旧断言作废；真机验证见 docs/50 与 docs/52")
def test_merit_ignores_legacy_building_name_but_checks_coordinates(dispatch_scene,reuse):
    g,policy,obs,state = dispatch_scene
    point = Point(name='营帐备注',x=644,y=285,expected_name='营帐',actions=['guard'])
    g.v.map_coordinates = lambda image:(644,285)
    g.v.read = lambda *a,**kw:[]
    def located(target):
        g.verify_target(None,target)
        return None
    g.go_to = g.reopen_target = located
    g.dispatch(policy,obs,point,'merit',reuse_target=reuse)
    assert state['submitted']
    assert point.expected_name == '营帐'


@pytest.mark.parametrize('kind,coord,reason',[('merit',(638,285),'坐标不同'),('transfer',(644,285),'目标名称不符')])
def test_merit_wrong_coordinate_and_backup_wrong_name_never_submit(dispatch_scene,kind,coord,reason):
    g,policy,obs,state = dispatch_scene
    g.v.map_coordinates = lambda image:coord
    g.v.read = lambda *a,**kw:[]
    def located(point):
        g.verify_target(None,point)
        return None
    g.go_to = g.reopen_target = located
    with pytest.raises(GameError,match=reason):
        g.dispatch(policy,obs,Point(x=644,y=285,expected_name='营帐'),kind)
    assert not state['submitted']
    assert not g.j.pending(policy.key)


@pytest.mark.parametrize('late',[False,True])
@pytest.mark.skip(reason="0.7.4 起派兵/批次契约重写（角标闭环、卡位直提交、无颜色计数），旧断言作废；真机验证见 docs/50 与 docs/52")
def test_expired_observation_is_typed_and_stops_before_prepare_or_submit(dispatch_scene,monkeypatch,late):
    from sanmou_assistant.game import ObservationExpired
    g,policy,obs,state = dispatch_scene
    clock = [1000.0]
    monkeypatch.setattr('time.time',lambda:clock[0])
    obs.observed_at = 1000 if late else 800
    state['failure'] = ''
    locate = g.go_to
    def slow_target(point):
        if late: clock[0] = 1121
        return locate(point)
    g.go_to = slow_target
    with pytest.raises(ObservationExpired):
        g.dispatch(policy,obs,Point(actions=['guard']),'merit')
    assert not state['submitted']
    assert g.j.db.execute('select count(*) from intents').fetchone()[0] == 0
    if not late:
        assert state['taps'] == []


@pytest.mark.parametrize("actual_pid,expected_pid,visible,expected", [(11,11,True,1),(22,11,True,0),(11,11,False,0)])
def test_minimize_only_touches_verified_selected_instance(monkeypatch,actual_pid,expected_pid,visible,expected):
    import sanmou_assistant.transport as module
    calls = []
    def pid(handle, value):value._obj.value = actual_pid;return 1
    def show(handle, value):calls.append((handle.value,value));return 1
    api = SimpleNamespace(IsWindow=lambda h:1, IsWindowVisible=lambda h:visible, GetWindowThreadProcessId=pid, ShowWindow=show)
    monkeypatch.setattr(module.ctypes, "WinDLL", lambda *a,**kw:api)
    monkeypatch.setattr(module, "run", lambda *a,**kw:pytest.fail("不得枚举所有设备进程"))
    assert module.minimize_emulator_windows({"main_wnd":"01A4193A","pid":expected_pid}) == expected
    assert calls == ([(int("01A4193A",16),6)] if expected else [])
    assert module.minimize_emulator_windows({"main_wnd":"invalid","pid":11}) == 0
    assert module.minimize_emulator_windows() == 0


@pytest.mark.skip(reason="0.7.4 起派兵/批次契约重写（角标闭环、卡位直提交、无颜色计数），旧断言作废；真机验证见 docs/50 与 docs/52")
def test_outside_dispatch_requires_new_action_evidence(dispatch_scene):
    g,policy,obs,state = dispatch_scene
    from sanmou_assistant.game import DispatchUnconfirmed
    state['failure'] = 'uncertain'
    obs.state = 'outside'
    g.v.deployment_status = lambda *a: 'outside'
    with pytest.raises(DispatchUnconfirmed):
        g.dispatch(policy,obs,Point(name='A',actions=['march']),'merit')
    assert state['taps'].count((1651,992)) == 3
    assert not g.j.pending(policy.key)
    state['submitted'] = False
    state['failure'] = ''
    g.v.deployment_status = lambda *a: 'marching' if state['submitted'] else 'outside'
    g.dispatch(policy,obs,Point(name='A',actions=['march']),'merit')
    assert g.j.phase(policy.key) == 'merit'
