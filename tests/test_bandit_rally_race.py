"""集结满员竞态：点击参与/选队后可能已满员，奖励次数没有减少。

实机确认（20261030-124037 / 20261030-130210）：
- 提交后游戏关闭整个流程直接回城务页，成功没有“加入成功”文字残留；
- 奖励次数在集结出发时才扣减，提交后立刻读次数无法区分成败，只能作为已消耗证据；
- 加入成功后目标可继续显示为“已参加”且没有加号，目标消失本身不能证明成功。

因此必须按当前页面（提示浮层 + 目标集的已参加/可加入状态）识别结果，
并在仍有其他可加入集结时有限重试；绝不把失败当成功，也不重复消耗次数。
"""
from types import SimpleNamespace

import numpy as np
import pytest

from sanmou_assistant.game import Game, GameError, RallyJoinRejected
from sanmou_assistant.journal import Journal
from sanmou_assistant.models import Observation, Point, Profile


class Frame(np.ndarray):
    """带页型/文字脚本的画面帧；出征页自带金色出征按钮区域。"""

    def __new__(cls, page, joined=None, rallies=None, **vision):
        arr = np.zeros((1080, 1920, 3), np.uint8)
        obj = arr.view(cls)
        obj.page = page
        obj.joined_text = joined or {}
        obj.rallies = rallies
        obj.vision = vision
        if page == "deployment":
            obj[947:1020, 1580:1860] = (30, 200, 200)
        return obj


class FakeVision:
    def __init__(self, availability):
        self.availability = availability

    def page(self, image, verify=True):
        return image.page

    def joined(self, image, rect=(0, 0, 1920, 1080)):
        return image.joined_text.get(tuple(rect), "")

    def bandit_availability(self, image, detail=False):
        key = "detail" if detail else "list"
        return tuple(self.availability.get(key, ("unknown", None)))

    def bandit_rallies(self, image):
        return list(getattr(image, "rallies", None) or [])

    def find(self, image, labels, rect=(0, 0, 1920, 1080), exact=False):
        wanted = image.vision.get("buttons", {})
        for label in labels:
            if label in wanted:
                return SimpleNamespace(x=wanted[label][0], y=wanted[label][1])
        return None

    def deployment_status(self, image, slot, center_x=None):
        return image.vision.get("status", "home")

    def deployment_strength(self, image, slot):
        return image.vision.get("strength", 33000)


@pytest.fixture
def joiner(tmp_path):
    profile = Profile(account="测试", main_city=Point(x=100, y=200))
    journal = Journal(tmp_path / "runtime.sqlite", "test")
    events = []

    def build(frames, availability):
        vision = FakeVision(availability)
        queue = list(frames)
        game = Game(None, vision, profile, journal, tmp_path,
                    lambda kind, value: events.append((kind, value)))
        game.account_checked = True
        game.d = SimpleNamespace(tap=lambda *args, **kwargs: None, control=SimpleNamespace(sleep=lambda s: None))
        game.capture = lambda name="", publish=False: queue.pop(0)
        return game

    policy = profile.teams[4]
    policy.role = "bandits"
    obs = Observation(policy.key, state="home", strength=33000, maximum=33000,
                      stamina=200, supply=100, wounded=False, auto_refill=True)
    rally = {"text": "20级贼寇 神兵丨东风神 集结中..00:14:53", "join": (1290, 375)}
    yield build, policy, obs, rally, journal, events
    journal.close()


def note_of(journal, policy):
    row = journal.db.execute(
        "SELECT note FROM intents WHERE scope=? AND team=? ORDER BY created DESC LIMIT 1",
        (journal.scope, policy.key)).fetchone()
    return row[0] if row else ""


def status_of(journal, policy):
    return journal.recent_orders(policy.key)[0][3]


def detail_frame():
    return Frame("rally",
                 joined={(115, 210, 800, 285): "已集结玩家 00:08:41"},
                 buttons={"参与集结": (960, 890)})


def deployment_frame():
    return Frame("deployment",
                 joined={(750, 0, 1190, 180): "讨伐贼寇", (1700, 835, 1900, 900): "00:00:09"},
                 buttons={"出征": (1700, 980)}, status="home", strength=33000)


def affairs_frame(toast_text=""):
    # 实机确认：提交后游戏关闭整个流程回城务页；toast_text 模拟提示浮层残留。
    return Frame("affairs", joined={(300, 240, 1650, 900): toast_text,
                                    (470, 240, 1510, 895): toast_text})


def join_sequence(toast_text="", result_text=""):
    # 详情 → 出征页 → 选队核对 → 提示浮层 → 稳定页
    return [detail_frame(), deployment_frame(), deployment_frame(),
            affairs_frame(toast_text), affairs_frame(result_text)]


def test_post_submit_rally_full_failure_text_rejected_and_retriable(joiner):
    build, policy, obs, rally, journal, events = joiner
    # 满员：失败提示在浮层帧，目标集已不可加入（无空槽），列表可读 → 未提交未消耗。
    game = build(join_sequence(toast_text="集结已满员"), {"detail": ("reward", 2), "list": ("reward", 2)})
    game.open_bandit_list = lambda: Frame("war", rallies=[])
    with pytest.raises(RallyJoinRejected, match="未消耗"):
        game.join_bandits(policy, obs, rally)
    assert status_of(journal, policy) == "rejected"
    assert "未消耗" in note_of(journal, policy)


def test_post_submit_count_decreased_is_accepted(joiner):
    build, policy, obs, rally, journal, events = joiner
    # 集结在复核期间恰好出发：真实次数已扣减（2→1）是已消耗的正向证据。
    game = build(join_sequence(), {"detail": ("reward", 2), "list": ("reward", 1)})
    game.open_bandit_list = lambda: Frame("war", rallies=[])
    game.join_bandits(policy, obs, rally)
    assert status_of(journal, policy) == "accepted"


def test_post_submit_list_unreadable_stays_uncertain(joiner):
    build, policy, obs, rally, journal, events = joiner
    # 无成功文字且列表读不出：保持待核对锁定，安全路径不猜。
    game = build(join_sequence(), {"detail": ("reward", 2)})

    def broken():
        raise GameError("讨贼列表未识别")

    game.open_bandit_list = lambda: broken()
    with pytest.raises(GameError, match="待核对"):
        game.join_bandits(policy, obs, rally)
    assert status_of(journal, policy) == "uncertain"


def test_post_submit_target_still_open_is_rejected(joiner):
    build, policy, obs, rally, journal, events = joiner
    # 提交后目标集仍带空槽列出：本次提交没有注册（游戏静默拒绝），确定未消耗，换其他集结。
    game = build(join_sequence(), {"detail": ("reward", 2), "list": ("reward", 2)})
    game.open_bandit_list = lambda: Frame("war", rallies=[dict(rally)])
    with pytest.raises(RallyJoinRejected, match="未生效"):
        game.join_bandits(policy, obs, rally)
    assert status_of(journal, policy) == "rejected"


def test_post_submit_target_gone_from_list_stays_uncertain(joiner):
    build, policy, obs, rally, journal, events = joiner
    other = {"text": "20级贼寇 测试玩家 集结中..00:27:18", "join": (1290, 590)}
    game = build(join_sequence(), {"detail": ("reward", 1), "list": ("reward", 1)})
    game.open_bandit_list = lambda: Frame("war", rallies=[other])
    with pytest.raises(GameError, match="待核对"):
        game.join_bandits(policy, obs, rally)
    assert status_of(journal, policy) == "uncertain"
    assert not any(kind == "dispatch" for kind, value in events)


def test_post_submit_target_marked_joined_is_accepted(joiner):
    build, policy, obs, rally, journal, events = joiner
    joined = dict(rally, join=None, joined=True)
    game = build(join_sequence(), {"detail": ("reward", 1), "list": ("reward", 1)})
    game.open_bandit_list = lambda: Frame("war", rallies=[joined])
    game.join_bandits(policy, obs, rally)
    assert status_of(journal, policy) == "accepted"
    assert any(kind == "dispatch" and value.get("state") == "confirmed" for kind, value in events)


def test_pre_submit_full_rally_rejected_without_order(joiner):
    build, policy, obs, rally, journal, events = joiner
    # 点“参与集结”后游戏弹了满员提示（非详情/出征页）：没写订单，可直接换其他集结。
    dialog = Frame("unknown", joined={(300, 240, 1650, 900): "集结已满员"})
    game = build([detail_frame(), dialog], {"detail": ("reward", 2)})
    with pytest.raises(RallyJoinRejected, match="未提交"):
        game.join_bandits(policy, obs, rally)
    assert journal.pending(policy.key) == []


def test_join_success_text_is_accepted(joiner):
    build, policy, obs, rally, journal, events = joiner
    game = build(join_sequence(toast_text="加入成功"), {"detail": ("reward", 2), "list": ("reward", 1)})
    game.open_bandit_list = lambda: Frame("war")
    game.join_bandits(policy, obs, rally)
    assert status_of(journal, policy) == "accepted"
    assert any(kind == "dispatch" for kind, _ in events)


def bandits_game(build, availability, rallies):
    game = build([], availability)
    game.scan_team = lambda p: Observation(p.key, state="home", strength=33000, maximum=33000,
                                           stamina=200, supply=100, wounded=False, auto_refill=True)
    game.open_bandit_list = lambda: Frame("war")
    game.ensure_sandbox = lambda *args, **kwargs: Frame("sandbox")
    game.v.bandit_rallies = lambda image: list(rallies)
    return game


def test_bandits_tries_next_rally_after_rejection(joiner):
    build, policy, obs, rally, journal, events = joiner
    first = {"text": "20级贼寇A", "join": (1290, 375)}
    second = {"text": "20级贼寇B", "join": (1290, 590)}
    game = bandits_game(build, {"list": ("reward", 2)}, [first, second])
    attempted = []

    def join(policy, obs, rally, **kwargs):
        attempted.append(rally["text"])
        if len(attempted) == 1:
            raise RallyJoinRejected("集结已满或不可加入，未提交出征")

    game.join_bandits = join
    assert game.bandits(True) is True
    assert attempted == ["20级贼寇A", "20级贼寇B"]


def test_bandits_stops_retrying_after_all_rallies_rejected(joiner):
    build, policy, obs, rally, journal, events = joiner
    first = {"text": "20级贼寇A", "join": (1290, 375)}
    game = bandits_game(build, {"list": ("reward", 2)}, [first])
    attempted = []

    def join(policy, obs, rally, **kwargs):
        attempted.append(rally["text"])
        raise RallyJoinRejected("集结已满或不可加入，未提交出征")

    game.join_bandits = join
    assert game.bandits(True) is True
    assert attempted == ["20级贼寇A"]
    assert any(kind == "daily" and "无其他可加入集结" in str(value) for kind, value in events)


def test_bandits_single_rally_success_needs_one_attempt(joiner):
    build, policy, obs, rally, journal, events = joiner
    first = {"text": "20级贼寇A", "join": (1290, 375)}
    game = bandits_game(build, {"list": ("reward", 2)}, [first])
    attempted = []
    game.join_bandits = lambda *args, **kwargs: attempted.append(args[2]["text"])
    assert game.bandits(True) is True
    assert attempted == ["20级贼寇A"]


def test_bandits_joined_row_is_not_another_join_opportunity(joiner):
    build, policy, obs, rally, journal, events = joiner
    joined = {"text": "20级贼寇A 已参加", "join": None, "joined": True}
    game = bandits_game(build, {"list": ("reward", 1)}, [joined])
    game.join_bandits = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("已参加不能重复加入"))
    assert game.bandits(True) is False
