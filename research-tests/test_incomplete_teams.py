from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from sanmou_assistant.game import Game, GameError, TeamAbsent
from sanmou_assistant.models import Observation, Point, Profile, TeamPolicy
from sanmou_assistant.vision import Vision
from sanmou_assistant.worker import Worker


ROOT = Path(__file__).resolve().parents[1]
IMAGE = np.full((1080, 1920, 3), 90, np.uint8)


def sample(rel):
    path = ROOT / rel
    if not path.is_file():
        pytest.skip(f"实机样本已按截图策略淘汰：{rel}")
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    return image


def two_team_profile():
    return Profile(
        name="测试配置",
        account="测试账号",
        main_city=Point(name="我的主城", x=600, y=300),
        teams=[TeamPolicy(slot=1, role="merit"), TeamPolicy(slot=2, role="merit")],
        targets=[Point(name="目标", x=636, y=286, group="默认", enabled=True)],
    )


def game_with(tmp_path, vision=None, profile=None):
    device = SimpleNamespace(control=SimpleNamespace(sleep=lambda _: None))
    game = Game(device, vision or SimpleNamespace(), profile or two_team_profile(), None, tmp_path,
                lambda *args: None)
    return game


def test_profile_with_only_two_teams_is_valid():
    # 旧版本把五队/亲卫当成前置条件；配置本身只写两队也必须能开始执行。
    assert two_team_profile().validate(active=True) == []


def test_read_roster_needs_no_page_number(tmp_path):
    # 2026-11-09 根因：进入部队页只点头像；队伍数量/页码不参与。
    vision = SimpleNamespace(page=lambda _: 'overview',
                             full_team_rows=lambda _: {1: object(), 2: object()})
    game = game_with(tmp_path, vision)
    game.open_team_page = lambda slot: IMAGE
    game.close_panel = lambda image: None
    game.ensure_sandbox = lambda **_: IMAGE
    assert game.read_roster(2) == {1, 2}


def test_roster_comes_from_team_rows_and_is_kept_fixed(tmp_path):
    events = []
    vision = SimpleNamespace(page=lambda _: 'overview', full_team_rows=lambda _: {1: object(), 2: object()})
    game = game_with(tmp_path, vision)
    game.emit = lambda kind, value: events.append((kind, value))
    assert game.note_team_roster(IMAGE) is True
    assert game.active_slots == {1, 2}
    assert ("team_presence", {"slots": [1, 2]}) in events

    # 运行期间不重新判断队伍；后面再读到三行也不改变本次启动的集合。
    vision.full_team_rows = lambda _: {1: object(), 2: object(), 3: object()}
    assert game.note_team_roster(IMAGE) is True
    assert game.active_slots == {1, 2}


def test_non_contiguous_team_rows_are_not_accepted(tmp_path):
    # 行号缺口说明 OCR 漏了一行，不能拿 {1,3} 当“当前只有两队”。
    vision = SimpleNamespace(page=lambda _: 'overview', full_team_rows=lambda _: {1: object(), 3: object()})
    game = game_with(tmp_path, vision)
    assert game.note_team_roster(IMAGE) is False
    assert game.active_slots is None


def test_scan_team_skips_slots_outside_roster(tmp_path):
    game = game_with(tmp_path, SimpleNamespace())
    game.active_slots = {1, 2}
    with pytest.raises(TeamAbsent):
        game.scan_team(TeamPolicy(slot=3))


def test_marker_batch_reads_only_roster_slots(tmp_path):
    class Reader:
        def __init__(self):
            self.slots = []

        @staticmethod
        def avatars_visible(_):
            return True

        def read_slot(self, image, slot):
            self.slots.append(slot)
            return {"slot": slot, "state": "idle", "label": "空闲", "reason": "无角标",
                    "confidence": .9, "plaque": 0, "glyph": None, "matched": None,
                    "matched_label": None, "matched_score": None, "second": None,
                    "second_score": None, "shift": None}

    vision = SimpleNamespace(frame_clear=lambda _: True, team_wounded=lambda *_: False)
    game = game_with(tmp_path, vision)
    game.active_slots = {1, 2}
    game._marker_reader = Reader()
    game.capture = lambda *args, **kwargs: IMAGE
    states = game.read_all_marker_states()
    assert set(states) == {1, 2}
    assert game._marker_reader.slots == [1, 2]


def test_worker_ensure_roster_reads_once_and_filters_to_existing_teams(tmp_path):
    profile = Profile(
        name="测试配置",
        account="测试账号",
        main_city=Point(name="我的主城", x=600, y=300),
        teams=[TeamPolicy(slot=slot, role="merit") for slot in range(1, 6)]
        + [TeamPolicy(slot=slot, group="guard") for slot in range(1, 4)],
    )
    worker = Worker(profile, tmp_path, "scan", dry_run=True)
    calls = []

    def read_roster(slot):
        calls.append(slot)
        game.active_slots = {1, 2}

    game = SimpleNamespace(active_slots=None, read_roster=read_roster, log=lambda *args: None,
                           ensure_sandbox=lambda **_: None)
    assert worker.ensure_roster(game) == {1, 2}
    # 优先用非第 1 队做只读探针，避免点正在集结的第 1 队。
    assert calls == [2]
    assert worker.team_present("main:1")
    assert worker.team_present("main:2")
    assert not worker.team_present("main:3")
    assert [t.key for t in worker.active_policies(profile.teams)] == ["main:1", "main:2"]


def test_worker_roster_probe_falls_back_to_team_one_when_team_two_absent(tmp_path):
    profile = Profile(
        name="测试配置",
        account="测试账号",
        main_city=Point(name="我的主城", x=600, y=300),
        teams=[TeamPolicy(slot=1, role="merit"), TeamPolicy(slot=2, role="merit")],
    )
    worker = Worker(profile, tmp_path, "scan", dry_run=True)
    calls = []

    def read_roster(slot):
        calls.append(slot)
        if slot == 2:
            raise TeamAbsent("第 2 队尚未拥有")
        game.active_slots = {1}

    game = SimpleNamespace(active_slots=None, read_roster=read_roster, log=lambda *args: None,
                           ensure_sandbox=lambda **_: None)
    assert worker.ensure_roster(game) == {1}
    assert calls == [2, 1]


def test_worker_ensure_roster_fails_clearly_when_rows_never_appear(tmp_path):
    worker = Worker(two_team_profile(), tmp_path, "scan", dry_run=True)
    worker.control.sleep = lambda _: None
    game = SimpleNamespace(active_slots=None, read_roster=lambda slot: None,
                           log=lambda *args: None, ensure_sandbox=lambda **_: None)
    with pytest.raises(GameError, match="部队列表"):
        worker.ensure_roster(game)


def test_real_two_team_sandbox_is_readable_without_team_page_number():
    # 2026-11-08 赛季初实机帧：右侧只有两个头像、没有分页条。
    image = sample("artifacts/incomplete-teams-2026-11-08/current2.png")
    assert Vision().page(image) == "sandbox"


def test_real_team_list_rows_work_for_two_three_four_five_teams(tmp_path):
    # 旧赛季部队总览的五队行，按行截断模拟本次只有 2/3/4/5 队。
    image = sample("artifacts/incomplete-teams-2026-11-08/overview-five-teams.png")
    vision = Vision()
    for cut, expected in ((500, {1, 2}), (600, {1, 2, 3}), (705, {1, 2, 3, 4}), (None, {1, 2, 3, 4, 5})):
        frame = image.copy()
        if cut:
            frame[cut:810, 0:430] = 0
        assert set(vision.full_team_rows(frame)) == expected
        game = game_with(tmp_path, vision)
        assert game.note_team_roster(frame) is True
        assert game.active_slots == expected


def test_deployment_cards_are_centered_by_actual_team_count(tmp_path):
    # 2026-11-08 实机：两队卡片中心 517/767，不是固定五列的 135/385。
    game = game_with(tmp_path)
    cases = [({1}, [642]), ({1, 2}, [517, 767]), ({1, 2, 3}, [392, 642, 892]),
             ({1, 2, 3, 4}, [267, 517, 767, 1017]),
             (set(range(1, 6)), [142, 392, 642, 892, 1142])]
    for slots, expected in cases:
        game.active_slots = slots
        assert [game.card_center(slot) for slot in sorted(slots)] == expected


def test_real_two_team_deployment_status_uses_centered_cards():
    image = sample("artifacts/card-layout-2026-11-08/deployment-two-teams.png")
    vision = Vision()
    # 第一队正在集结，第二队空闲；按居中卡片位置读到的状态必须和画面一致。
    assert vision.deployment_status(image, 1, 517) == "outside"
    assert vision.deployment_status(image, 2, 767) == "home"


def test_new_season_home_is_recognized_without_guard_menu(tmp_path):
    # 2026-11-08 新赛季主城菜单改成行军/驻守/外观，不再有“亲卫整编”。
    image = sample("artifacts/card-layout-2026-11-08/home-new-season.png")
    vision = Vision()
    profile = Profile(account="测试主公", main_city=Point(name="我的主城", x=17, y=199))
    game = Game(SimpleNamespace(control=SimpleNamespace(sleep=lambda _: None)), vision, profile,
                None, tmp_path, lambda *args: None)
    assert vision.map_coordinates(image) == (17, 199)
    assert game.home_matches_account(image)
    assert game.main_city_selected(image)
    game.check_account(image)
    assert game.account_checked


def test_single_action_text_misread_still_uses_configured_action(tmp_path):
    # 2026-11-08 实机：新赛季“行军”按钮被 OCR 读成“研究”，但点击位置正确。
    class VisionStub:
        @staticmethod
        def action_buttons(_image):
            return {}

        @staticmethod
        def find(_image, _labels, _rect, exact=True):
            return SimpleNamespace(x=1238, y=463, text="研究")

    game = game_with(tmp_path, VisionStub())
    point = Point(name="空地", x=18, y=204, actions=["march"])
    assert game.pick_action(IMAGE, point, "merit") == ("行军", "march", (1238, 463))


def test_pick_action_retries_action_icons_once_before_ocr(tmp_path):
    # 菜单动画让首帧图标没命中时，先重截一帧再试，不立刻退回 OCR。
    class VisionStub:
        calls = 0

        @classmethod
        def action_buttons(cls, _image):
            cls.calls += 1
            return {} if cls.calls == 1 else {"march": (1238, 463, .99)}

        @staticmethod
        def frame_clear(_image):
            return True

        @staticmethod
        def find(*_args, **_kwargs):
            raise AssertionError("图标重试命中后不应退回 OCR")

    device = SimpleNamespace(control=SimpleNamespace(sleep=lambda _: None),
                             capture=lambda: IMAGE)
    game = Game(device, VisionStub(), two_team_profile(), None, tmp_path, lambda *args: None)
    point = Point(name="空地", x=18, y=204, actions=["march"])
    assert game.pick_action(IMAGE, point, "merit") == ("行军", "march", (1238, 463))
    assert VisionStub.calls == 2


def test_worker_ensure_home_calibrates_once_per_start(tmp_path):
    worker = Worker(two_team_profile(), tmp_path, "scan", dry_run=True)
    calls = []

    def calibrate_home():
        calls.append(1)
        worker.profile.account = "测试账号"
        worker.profile.main_city = Point(name="我的主城", x=17, y=199)

    game = SimpleNamespace(calibrate_home=calibrate_home, ensure_sandbox=lambda **_: None)
    assert worker.ensure_home(game).x == 17
    assert worker.home_checked is True
    # 同一任务内不重复核实。
    assert worker.ensure_home(game).x == 17
    assert calls == [1]


def test_worker_ensure_home_stops_after_three_failures(tmp_path):
    worker = Worker(two_team_profile(), tmp_path, "scan", dry_run=True)
    worker.control.sleep = lambda _: None
    calls = []

    def calibrate_home():
        calls.append(1)
        raise GameError("主城没读到")

    game = SimpleNamespace(calibrate_home=calibrate_home, ensure_sandbox=lambda **_: None)
    with pytest.raises(GameError, match="主城坐标"):
        worker.ensure_home(game)
    assert len(calls) == 3
    assert worker.home_checked is False


def test_food_recovery_only_follows_actual_merit_teams(tmp_path):
    # 用户 2026-11-09：没有刷武勋队时粮食多少都无所谓，不屯田、不拦其他工作。
    profile = Profile(food_enabled=True, food_low=500000, food_recover=1000000)
    worker = Worker(profile, tmp_path, "scan", dry_run=True)
    merit = TeamPolicy(slot=1, role="merit")
    bandits = TeamPolicy(slot=2, role="bandits")
    assert worker.food_recovery_state([bandits], 1, True) is False
    assert worker.food_recovery_state([merit], 1, False) is True
    assert worker.food_recovery_state([merit], 1000000, True) is False
    assert worker.food_recovery_state([merit], None, True) is True
    assert worker.food_recovery_state([merit], None, False) is False




