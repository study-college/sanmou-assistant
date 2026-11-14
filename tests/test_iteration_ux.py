"""2026-11-14 体验优化（读取与调度）：

- 个人主页武勋只在本次实际存在刷武勋或拉线队时读取；纯讨贼/铺路/日常不读。
- 低粮门槛只跟随刷武勋：拉线在场也不打开 food_gate（锁定现有口径）。
- 阵容名同一次任务运行只取一次并缓存于 Game 实例：首次读到后保留，
  后续识别失败时回填，动态字段（兵力/体力/补给/重伤）仍每次现场重读。

这些是纯逻辑单测，不连模拟器。
"""
import pathlib
from types import SimpleNamespace

import numpy as np

from sanmou_assistant.game import Game, GameError
from sanmou_assistant.journal import Journal
from sanmou_assistant.models import Observation, Point, Profile, TeamPolicy, decide
from sanmou_assistant.team_states import LEGACY_STATE
from sanmou_assistant.vision import Text
from sanmou_assistant.worker import Worker

IMAGE = np.zeros((1080, 1920, 3), np.uint8)


def make_worker(tmp_path, command="run", dry_run=False):
    return Worker(Profile(), tmp_path, command, dry_run)


def main_team(slot, role):
    return TeamPolicy(slot=slot, role=role)


# ---------------------------------------------------------------- 武勋读取门槛

def test_merit_read_due_only_merit_or_line_present(tmp_path):
    worker = make_worker(tmp_path)
    assert not worker.merit_read_due()          # 默认全队 idle：不读
    cases = {"merit": True, "line": True,
             "bandits": False, "pave": False, "idle": False, "hidden": False}
    for role, expected in cases.items():
        worker.profile.teams = [main_team(1, role)]
        assert worker.merit_read_due() is expected, role
    # 多队组合：任一对 merit/line 在场即读；纯讨贼+铺路不读
    worker.profile.teams = [main_team(1, "bandits"), main_team(2, "pave")]
    assert not worker.merit_read_due()
    worker.profile.teams = [main_team(1, "bandits"), main_team(2, "line")]
    assert worker.merit_read_due()


def test_merit_read_due_filters_absent_slots(tmp_path):
    """按 team_present 过滤：本赛季未拥有的槽位不触发武勋读取。"""
    worker = make_worker(tmp_path)
    worker.active_slots = {2}
    worker.profile.teams = [main_team(1, "merit")]
    assert not worker.merit_read_due()
    worker.profile.teams = [main_team(2, "line")]
    assert worker.merit_read_due()
    # 尚未确认部队列表时（active_slots=None）不因门槛误伤启动流程
    worker.active_slots = None
    assert worker.merit_read_due()


# ------------------------------------------------------------ 低粮门槛只随武勋

def test_food_gate_still_merit_only_line_does_not_open(tmp_path):
    """锁定用户口径：拉线不自动打开低粮门槛，粮食共享库存规则不变。"""
    worker = make_worker(tmp_path)
    worker.profile.food_enabled = True
    line, merit = main_team(1, "line"), main_team(2, "merit")
    assert Worker.has_merit_team([line]) is False
    assert Worker.has_merit_team([merit]) is True
    # 低粮时：只有刷武勋队才置恢复标记；拉线/讨贼队不触发也不沿用标记
    assert worker.food_recovery_state([line], 10, stored=False) is False
    assert worker.food_recovery_state([line], 10, stored=True) is False
    assert worker.food_recovery_state([merit], 10, stored=False) is True
    # grain 未读到时保留已有标记（merit 在场）；没有 merit 队时保持 false
    assert worker.food_recovery_state([merit], None, stored=True) is True
    assert worker.food_recovery_state([line], None, stored=True) is False


def test_decide_line_role_never_blocked_by_food_gate(tmp_path):
    """line 角色的 decide 早退到拉线循环，food_gate 对它没有任何作用点。"""
    worker = make_worker(tmp_path)
    profile = worker.profile
    profile.food_enabled = True
    line, merit = main_team(1, "line"), main_team(2, "merit")
    obs = Observation(line.key, state="home", strength=33000, maximum=33000)
    decision = decide(profile, line, obs, grain=0, food_gate=True)
    assert decision.action == "wait" and "拉线任务" in decision.reason
    # 刷武勋队则照旧被低粮拦住（food_gate 由 worker 按 merit_active 写入）
    obs = Observation(merit.key, state="home", strength=33000, maximum=33000)
    blocked = decide(profile, merit, obs, grain=0, food_gate=True)
    assert blocked.action == "wait" and "等待补粮" in blocked.reason
    released = decide(profile, merit, obs, grain=0, food_gate=False)
    assert "等待补粮" not in released.reason


# ---------------------------------------------------------------- 阵容名缓存

class FakeFullTeamVision:
    """scripted: {key: [Observation, ...]} 每次调用弹出下一个；弹完返回空 identity 新观测。"""

    def __init__(self, scripted=None):
        self.scripted = {key: list(values) for key, values in (scripted or {}).items()}
        self.calls = []

    def full_team(self, image, key):
        self.calls.append(key)
        queue = self.scripted.get(key)
        if queue:
            return queue.pop(0)
        return Observation(key)


class FakeOverviewVision:
    """heroes_queue: 每次对英雄区的 read 弹出一组 Text；其余区域返回空。"""

    HEROES_RECT = (477, 374)

    def __init__(self, heroes_queue=None):
        self.heroes_queue = [list(v) for v in (heroes_queue or [])]

    def read(self, image, rect):
        if rect[:2] == self.HEROES_RECT and self.heroes_queue:
            return self.heroes_queue.pop(0)
        return []


def make_game(vision):
    game = Game(SimpleNamespace(tap=lambda *a: None), vision, Profile(),
                SimpleNamespace(), pathlib.Path("."), lambda *a: None)
    game.save_image = lambda *a, **k: None
    game.sync_auto_refill = lambda *a, **k: None
    game.close_panel = lambda *a, **k: None
    game.select_full_team_slot = lambda image, slot: image
    return game


def test_identity_first_read_kept_and_dynamic_fields_stay_fresh():
    """首次读取缓存阵容；后续读取识别不出名字时回填 identity，
    但兵力等动态字段必须反映本次读数（没有就用空值，绝不用缓存凑数）。"""
    key = "main:1"
    vision = FakeFullTeamVision({key: [
        Observation(key, identity="皇甫嵩/袁术/朱儽", strength=30000, maximum=33000),
        Observation(key, identity="", strength=25000, maximum=33000),
        Observation(key, identity="", strength=None),
    ]})
    game = make_game(vision)
    first = game._full_team(IMAGE, key)
    assert first.identity == "皇甫嵩/袁术/朱儽" and first.strength == 30000
    second = game._full_team(IMAGE, key)
    assert second.identity == "皇甫嵩/袁术/朱儽"   # 没丢
    assert second.strength == 25000                # 动态字段用本次读数
    third = game._full_team(IMAGE, key)
    assert third.identity == "皇甫嵩/袁术/朱儽"
    assert third.strength is None                  # 没读到就保持未知，不拿旧数
    # 阵容真实变化时（读得出新名字），新值更新缓存
    vision.scripted[key] = [Observation(key, identity="新阵容/袁术/朱儽")]
    assert game._full_team(IMAGE, key).identity == "新阵容/袁术/朱儽"


def test_identity_cache_not_shared_across_teams():
    vision = FakeFullTeamVision({
        "main:1": [Observation("main:1", identity="甲/乙/丙")],
        "main:2": [Observation("main:2", identity="丁/戊/己")],
    })
    game = make_game(vision)
    assert game._full_team(IMAGE, "main:1").identity == "甲/乙/丙"
    assert game._full_team(IMAGE, "main:2").identity == "丁/戊/己"
    # 两队后续都识别不出名字，各自回填各自的，互不串
    assert game._full_team(IMAGE, "main:1").identity == "甲/乙/丙"
    assert game._full_team(IMAGE, "main:2").identity == "丁/戊/己"


def test_identity_cache_not_shared_across_game_instances():
    """缓存挂在 Game 实例上：新实例（新任务运行）不沿用旧阵容。"""
    vision = FakeFullTeamVision({"main:1": [Observation("main:1", identity="甲/乙/丙")]})
    first_game = make_game(vision)
    assert first_game._full_team(IMAGE, "main:1").identity == "甲/乙/丙"
    second_game = make_game(vision)   # 同 vision、新实例
    assert second_game._full_team(IMAGE, "main:1").identity == ""


def test_identity_all_empty_read_never_caches():
    """首次就读不到名字：不缓存空值，之后读到了照常更新。"""
    key = "main:1"
    vision = FakeFullTeamVision()
    game = make_game(vision)
    assert game._full_team(IMAGE, key).identity == ""
    vision.scripted[key] = [Observation(key, identity="甲/乙/丙")]
    assert game._full_team(IMAGE, key).identity == "甲/乙/丙"


HEROES_FIRST = [
    Text("10皇甫嵩", .9, 500, 520, (0, 0, 0, 0)),
    Text("20袁术", .9, 800, 522, (0, 0, 0, 0)),
    Text("30朱儽", .9, 1100, 519, (0, 0, 0, 0)),
]
HEROES_CHANGED = [
    Text("10曹操", .9, 500, 520, (0, 0, 0, 0)),
    Text("20袁术", .9, 800, 522, (0, 0, 0, 0)),
    Text("30朱儽", .9, 1100, 519, (0, 0, 0, 0)),
]


def test_read_overview_identity_backfilled_and_updated():
    vision = FakeOverviewVision([HEROES_FIRST, [], HEROES_CHANGED])
    game = make_game(vision)
    first = game.read_overview(IMAGE, "main:1")
    assert first.identity == "皇甫嵩/袁术/朱儽"
    assert first.strength is None      # 英雄区没有数值跨度，动态字段照常为空
    second = game.read_overview(IMAGE, "main:1")
    assert second.identity == "皇甫嵩/袁术/朱儽"   # 名称没读到也不丢
    third = game.read_overview(IMAGE, "main:1")
    assert third.identity == "曹操/袁术/朱儽"       # 读得到新名字就更新


def test_read_full_team_batch_uses_identity_cache():
    """批路径 read_full_team_batch 与单队路径同一套缓存语义。"""
    p1, p2 = main_team(1, "merit"), main_team(2, "bandits")
    vision = FakeFullTeamVision({
        p1.key: [Observation(p1.key, identity="甲/乙/丙", strength=30000, maximum=33000)],
        p2.key: [Observation(p2.key, identity="丁/戊/己", strength=9000, maximum=30000)],
    })
    game = make_game(vision)
    with game.team_reading_batch([p1, p2]):
        first = game.read_full_team_batch(IMAGE, p1)
        assert first.identity == "甲/乙/丙" and first.strength == 30000
    with game.team_reading_batch([p1, p2]):
        second = game.read_full_team_batch(IMAGE, p1)
        assert second.identity == "甲/乙/丙"        # 批路径回填，UI 阵容不丢
        assert second.strength is None             # 动态字段不用缓存凑数
    # 批内另一队读到的是自己的阵容，互不串
    with game.team_reading_batch([p1, p2]):
        other = game.read_full_team_batch(IMAGE, p2)
        assert other.identity == "丁/戊/己"


def test_read_overview_batch_uses_identity_cache():
    """批路径 read_overview_batch 与单队 read_overview 同一套缓存语义。"""
    p1, p2 = main_team(1, "merit"), main_team(2, "line")
    # 每批每队从队列弹一组英雄区跨度：批1 p1=原阵容 p2=新阵容；批2 p1=新阵容 p2=空；批3 全空。
    vision = FakeOverviewVision([HEROES_FIRST, HEROES_CHANGED, HEROES_CHANGED, []])
    game = make_game(vision)
    with game.team_reading_batch([p1, p2]):
        first = game.read_overview_batch(IMAGE, p1)
        assert first.identity == "皇甫嵩/袁术/朱儽"
    with game.team_reading_batch([p1, p2]):
        second = game.read_overview_batch(IMAGE, p1)
        assert second.identity == "曹操/袁术/朱儽"   # 批路径同样接受更新的阵容名
    with game.team_reading_batch([p1, p2]):
        third = game.read_overview_batch(IMAGE, p1)
        assert third.identity == "曹操/袁术/朱儽"   # 识别失败时回填最近一次


# ------------------------------------------------------------ 同帧角标即时更新

class MarkerBatchGame:
    """带整帧角标的假 Game：read_all_marker_states 返回固定角标；scan_team 记录顺序并发布详情帧。"""

    def __init__(self, worker, markers):
        self.worker = worker
        self.markers = markers              # {slot: {"state","label","confidence"}}
        self.scans = []
        self.dispatched = []
        self.sandboxes = 0
        self.account_checked = True
        self.grain = None
        self.identities = {}                # key -> Game 阵容缓存（remembered_identity）
        self.fail_keys = set()
        self.clock = [1000.0]
        self.order = None                   # 测试注入：与 publish 共用一条事件序

    def read_all_marker_states(self):
        return self.markers

    def remembered_identity(self, key):
        return self.identities.get(key, "")

    def scan_team(self, policy):
        self.scans.append(policy.key)
        if self.order is not None:
            self.order.append(("scan", policy.key))
        self.clock[0] += 10
        if policy.key in self.fail_keys:
            raise GameError("队伍读数未识别")
        obs = Observation(policy.key, state="home", strength=33000, maximum=33000,
                          wounded=False, stamina=100, supply=100, auto_refill=True,
                          identity=self.identities.get(policy.key, ""),
                          marker_state="idle", marker_label="空闲", marker_confidence=.9,
                          observed_at=self.clock[0], marker_at=self.clock[0])
        # 真 Game.scan_team 读完详情自己整帧发布；这里照做以便断言发布顺序。
        self.worker.publish("observation", obs.__dict__)
        return obs

    def dispatch(self, policy, obs, point, kind, reuse_target=False):
        ident = self.worker.journal.prepare(policy.key, kind, point.name)
        self.worker.journal.finish(ident, "accepted")
        self.dispatched.append(policy.key)

    def ensure_sandbox(self):
        self.sandboxes += 1
        return "沙盘"

    def log(self, *args, **kwargs):
        pass


def make_marker_worker(tmp_path, slots=(1, 2, 3)):
    profile = Profile(account="测试", targets=[Point(name="战场", x=10, y=20)])
    profile.teams = [main_team(slot, "merit") for slot in slots]
    worker = Worker(profile, tmp_path, "run", False)
    worker.journal = Journal(tmp_path / "runtime.sqlite", "test")
    worker.active_slots = set(slots)
    return worker, profile.teams


def collect(worker):
    published, order = [], []
    def publish(kind, value):
        published.append((kind, value))
        order.append((kind, value))
    worker.publish = publish
    return published, order


def test_marker_frame_pre_published_before_any_detail_scan(tmp_path):
    """整帧角标成功后：全部有角标的队先预发布（同帧时间、角标、缓存阵容），
    再逐队读详情；预发布只进 UI，详情完成后整帧替换；预发布绝不进 decide。"""
    worker, teams = make_marker_worker(tmp_path)
    markers = {1: {"state": "busy", "label": "忙碌", "confidence": .9},
               2: {"state": "idle", "label": "空闲", "confidence": .95},
               3: {"state": "guard", "label": "驻守", "confidence": .9}}
    game = MarkerBatchGame(worker, markers)
    game.identities["main:2"] = "缓存/阵容/丙"
    published, order = collect(worker)
    game.order = order
    try:
        worker.process_team_batch(game, teams)
        # 前三条事件就是三支队的同帧预发布，先于任何详情扫描
        pre = order[:3]
        assert [v["key"] for _k, v in pre] == ["main:1", "main:2", "main:3"]
        for (kind, value), slot in zip(pre, (1, 2, 3)):
            assert kind == "observation"
            assert value["marker_state"] == markers[slot]["state"]
            assert value["marker_label"] == markers[slot]["label"]
            assert value["marker_confidence"] == markers[slot]["confidence"]
            assert value["observed_at"] == value["marker_at"]   # 同帧时间
            assert value["wounded"] is None
        assert pre[0][1]["state"] == LEGACY_STATE["busy"]
        assert pre[2][1]["state"] == LEGACY_STATE["guard"]
        assert pre[0][1]["identity"] == ""                    # 没读过缓存就是空
        assert pre[1][1]["identity"] == "缓存/阵容/丙"        # 缓存阵容不回丢
        assert order.index(("scan", "main:2")) >= 3           # 预发布全部先于首次详情
        # busy/驻守不进详情，只有 idle 队读了详情
        assert game.scans == ["main:2"]
        # 预发布每队只此一次；详情队整帧替换（发布两次）
        obs_counts = {key: sum(1 for k, v in published if k == "observation" and v["key"] == key)
                      for key in ("main:1", "main:2", "main:3")}
        assert obs_counts == {"main:1": 1, "main:2": 2, "main:3": 1}
        detail = [v for k, v in published if k == "observation" and v["key"] == "main:2"][-1]
        assert detail["strength"] == 33000                    # 详情字段来自详情帧
        assert detail["observed_at"] == 1010.0                # 详情用自己的详情帧时间
        assert detail["observed_at"] != pre[1][1]["observed_at"]
        # decide 只消费 read_batch 的观测：三队各一条决策，且只有详情队出击
        decisions = {v["team"]: v for k, v in published if k == "decision"}
        assert sorted(decisions) == ["main:1", "main:2", "main:3"]
        assert decisions["main:1"]["action"] == "wait"
        assert decisions["main:3"]["action"] == "wait"
        assert decisions["main:2"]["action"] == "merit"
        assert game.dispatched == ["main:2"]
    finally:
        worker.journal.close()


def test_recheck_due_team_computed_once_and_reads_detail(tmp_path):
    """needs_scan 只算一次：到点复核队若被算第二次会因记账间隔被判成免读，
    所以"确实走了详情"本身就证明没有重复计算；强制复核日志照常。"""
    import time as time_module
    worker, teams = make_marker_worker(tmp_path, slots=(1,))
    markers = {1: {"state": "busy", "label": "忙碌", "confidence": .9}}
    game = MarkerBatchGame(worker, markers)
    worker.started_at = time_module.time() - 3600
    worker.last_progress["main:1"] = time_module.time() - 21 * 60
    published, order = collect(worker)
    game.order = order
    try:
        worker.process_team_batch(game, teams)
        assert game.scans == ["main:1"]                       # 到点复核确实读了详情
        assert worker.last_recheck["main:1"]                  # 记账只发生一次
        assert any(k == "log" and "详情：兵力 33000/33000" in v["message"]
                   for k, v in published)                   # 强制复核日志照常
        assert any(k == "observation" and v["key"] == "main:1" and v["strength"] == 33000
                   for k, v in published)
        # 预发布仍然先于详情
        assert order[0][0] == "observation" and order[0][1]["key"] == "main:1"
        assert order.index(("scan", "main:1")) >= 1
    finally:
        worker.journal.close()


def test_observe_team_error_fallback_keeps_marker_hint_and_identity(tmp_path):
    """详情读取失败时，回退观测并入整帧角标与缓存阵容，不把界面抹空；
    订单结清逻辑不变（未知状态不碰待核对订单）。"""
    worker, teams = make_marker_worker(tmp_path, slots=(2,))
    policy = teams[0]
    game = MarkerBatchGame(worker, {})
    game.fail_keys.add(policy.key)
    game.identities[policy.key] = "缓存/阵容/丙"
    marker = {"state": "busy", "label": "忙碌", "confidence": .9}
    ident = worker.journal.prepare(policy.key, "merit", "战场")
    worker.journal.finish(ident, "uncertain")
    published, _order = collect(worker)
    try:
        obs = worker.observe_team(game, policy, marker_hint=marker)
        assert obs is not None and obs.state == "unknown"
        assert worker.journal.pending(policy.key), "未知状态不得结清待核对订单"
        fallback = [v for k, v in published if k == "observation" and v["key"] == policy.key][-1]
        assert fallback["marker_state"] == "busy"
        assert fallback["marker_label"] == "忙碌"
        assert fallback["marker_confidence"] == .9
        assert fallback["marker_at"]
        assert fallback["identity"] == "缓存/阵容/丙"
        assert "队伍读数未识别" in fallback["note"]
        assert game.sandboxes == 1
    finally:
        worker.journal.close()


def test_marker_only_publish_keeps_cached_identity_across_rounds(tmp_path):
    """第一轮详情读过阵容，第二轮这队变忙碌只走角标：预发布仍带缓存阵容。"""
    worker, teams = make_marker_worker(tmp_path, slots=(1,))
    game = MarkerBatchGame(worker, {1: {"state": "idle", "label": "空闲", "confidence": .9}})
    game.identities["main:1"] = "甲/乙/丙"
    published, _order = collect(worker)
    try:
        worker.process_team_batch(game, teams)
        round1 = [v for k, v in published if k == "observation" and v["key"] == "main:1"]
        assert round1 and round1[-1]["identity"] == "甲/乙/丙"
        # 第二轮：角标变忙碌 → 免详情，但预发布仍从 Game 缓存回填阵容
        game.markers = {1: {"state": "busy", "label": "忙碌", "confidence": .9}}
        worker.process_team_batch(game, teams)
        assert game.scans == ["main:1"]                       # 只有第一轮读了详情
        round2 = [v for k, v in published if k == "observation" and v["key"] == "main:1"]
        assert len(round2) == 3                               # 一轮预发布 + 一轮预发布 + 一轮详情
        assert round2[-1]["identity"] == "甲/乙/丙"
        assert round2[-1]["marker_state"] == "busy"
        assert round2[-1]["strength"] is None                 # 没读详情就没有动态字段
    finally:
        worker.journal.close()


def test_marker_prepublish_covers_pave_line_and_idle_without_changing_batch_decisions(tmp_path):
    """预发布覆盖本批之外的铺路/拉线/空闲队，但它们不进入武勋批次的决策。"""
    worker, teams = make_marker_worker(tmp_path, slots=(1,))
    pave, line, idle = main_team(2, "pave"), main_team(3, "line"), main_team(4, "idle")
    worker.profile.teams = teams + [pave, line, idle]
    worker.active_slots = {1, 2, 3, 4}
    markers = {1: {"state": "idle", "label": "空闲", "confidence": .9},
               2: {"state": "march", "label": "行军", "confidence": .9},
               3: {"state": "busy", "label": "忙碌", "confidence": .9},
               4: {"state": "stationary", "label": "停留", "confidence": .9}}
    game = MarkerBatchGame(worker, markers)
    published, order = collect(worker)
    game.order = order
    try:
        worker.process_team_batch(game, teams)
        first_four = order[:4]
        assert [value["key"] for kind, value in first_four] == ["main:1", "main:2", "main:3", "main:4"]
        assert all(kind == "observation" for kind, _value in first_four)
        decisions = [value["team"] for kind, value in published if kind == "decision"]
        assert decisions == ["main:1"]
        assert game.scans == ["main:1"]
    finally:
        worker.journal.close()
