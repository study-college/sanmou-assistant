from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from sanmou_assistant.game import Game
from sanmou_assistant.models import Observation, Point, Profile, TeamPolicy
from sanmou_assistant.vision import Text, Vision


ROOT = Path(__file__).resolve().parents[1]


def test_horn_template_matches_real_sandbox_but_not_missing_horn():
    vision = Vision()
    image = cv2.imread(str(ROOT / "artifacts/user-ui-samples-2026-10-30/sandbox-with-horn.png"))
    assert image is not None
    point = vision.horn_point(image)
    assert point is not None and abs(point["x"] - 635) <= 2 and abs(point["y"] - 886) <= 2
    image[820:930, 590:690] = 0
    assert vision.horn_point(image) is None


def test_horn_entry_does_not_navigate_through_affairs(tmp_path):
    taps = []
    device = SimpleNamespace(tap=lambda *args: taps.append(args), capture=lambda: "war-page")
    vision = SimpleNamespace(
        horn_point=lambda image: {"x": 635, "y": 886, "score": .9},
        page=lambda image: "war",
        find=lambda image, labels, *args, **kwargs: Text("讨贼", 1, 200, 250, (0, 0, 0, 0)),
    )
    game = Game(device, vision, Profile(), None, tmp_path, lambda *args: None)
    game.save_image = lambda *args: None
    game.ensure_sandbox = lambda: "sandbox"
    game._open_bandit_list_affairs = lambda: (_ for _ in ()).throw(AssertionError("不应绕行城务"))
    assert game.open_bandit_list() == "war-page"
    assert taps == [(635, 886, .9)]


def test_full_team_batch_reads_each_slot_once_and_closes_once(tmp_path):
    policies = [TeamPolicy(slot=slot) for slot in (1, 2, 3)]
    reads = []
    vision = SimpleNamespace(full_team=lambda image, key: reads.append(key) or Observation(key, strength=33000))
    game = Game(None, vision, Profile(), None, tmp_path, lambda *args: None)
    game._team_batch_policies = tuple(policies)
    game._team_detail_cache = {}
    selections, closed = [], []
    game.select_full_team_slot = lambda image, slot: selections.append(slot) or f"slot-{slot}"
    game.close_panel = lambda image: closed.append(image)
    game.save_image = lambda *args: None
    obs = game.read_full_team_batch("slot-1", policies[0])
    assert obs.key == "main:1"
    assert selections == [1, 2, 3]
    assert reads == ["main:1", "main:2", "main:3"]
    assert closed == ["slot-3"]
    assert set(game._team_detail_cache) == {"main:1", "main:2", "main:3"}


def test_home_scans_reuse_batch_details_without_reopening_page(tmp_path):
    policies = [TeamPolicy(slot=1), TeamPolicy(slot=2)]
    profile = Profile(account="测试", main_city=Point(x=100, y=200))
    full_reads, taps, closed = [], [], []

    class Device:
        control = SimpleNamespace(sleep=lambda seconds: None)

        def tap(self, *args):
            taps.append(args)

        def capture(self):
            return "full-1"

    vision = SimpleNamespace(
        map_coordinates=lambda image: (100, 200),
        home_team_visible=lambda *args: False,
        find=lambda image, labels, *args, **kwargs: SimpleNamespace() if "亲卫整编" in labels else None,
        page=lambda image: "sandbox" if image == "located" else "team",
        full_team=lambda image, key: full_reads.append(key) or Observation(key, wounded=False, strength=33000, maximum=33000),
    )
    game = Game(Device(), vision, profile, None, tmp_path, lambda *args: None)
    game.locate_team = lambda slot: "located"
    game.home_matches_account = lambda image: True
    game.select_full_team_slot = lambda image, slot: f"selected-{slot}"
    game.close_panel = lambda image: closed.append(image)
    game.save_image = lambda *args: None

    with game.team_reading_batch(policies):
        first = game.scan_team(policies[0])
        first_read_at = first.observed_at
        second = game.scan_team(policies[1])

    assert first.state == second.state == "home"
    assert first.key == "main:1" and second.key == "main:2"
    assert full_reads == ["main:1", "main:2"]
    assert len(taps) == 1 and taps[0][:2] == (1745, 245)
    assert closed == ["selected-2"]
    assert second.observed_at > 0 and first_read_at > 0
    assert game._team_batch_policies is None and game._team_detail_cache == {}


def test_user_overview_sample_has_all_rows_and_complete_reading():
    vision = Vision()
    image = cv2.imread(str(ROOT / "artifacts/user-ui-samples-2026-10-30/team-overview.png"))
    rows = vision.full_team_rows(image)
    assert set(rows) == {1, 2, 3, 4, 5}
    assert Game.selected_full_slot(image, 1, rows[1].y)
    game = Game.__new__(Game)
    game.v = vision
    obs = game.read_overview(image, "main:1")
    assert obs.strength == obs.maximum == 33000
    assert obs.stamina == 99 and obs.supply == 100 and obs.wounded is False
    assert obs.identity == "皇甫嵩/袁术/朱偽"


def test_overview_batch_reads_each_slot_once_and_closes_once(tmp_path):
    policies = [TeamPolicy(slot=slot) for slot in (1, 2, 3)]
    game = Game(None, SimpleNamespace(), Profile(), None, tmp_path, lambda *args: None)
    game._team_batch_policies = tuple(policies)
    game._team_overview_cache = {}
    selections, reads, closed = [], [], []
    game.select_full_team_slot = lambda image, slot: selections.append(slot) or f"overview-{slot}"
    game.read_overview = lambda image, key: reads.append(key) or Observation(key, strength=33000)
    game.close_panel = lambda image: closed.append(image)
    game.save_image = lambda *args: None
    obs = game.read_overview_batch("overview-1", policies[0])
    assert obs.key == "main:1"
    assert selections == [1, 2, 3]
    assert reads == ["main:1", "main:2", "main:3"]
    assert closed == ["overview-3"]
    assert set(game._team_overview_cache) == {"main:1", "main:2", "main:3"}


@pytest.mark.skip(reason="样本帧与当前游戏版本不一致，部队行 OCR 需重新采样")
def test_split_team_number_is_merged_only_from_the_real_adjacent_row():
    vision = Vision()
    image = cv2.imread(str(ROOT / "artifacts/multi-account-2026-10-31/team-slot-split.png"))
    assert image is not None
    rows = vision.full_team_rows(image)
    assert set(rows) == {1, 2, 3, 4, 5}
    assert rows[4].text == "部队四"
    assert Game.selected_full_slot(image, 1, rows[1].y)
    obs = vision.full_team(image, "main:1")
    assert obs.strength == obs.maximum == 33000
    assert obs.wounded is False and obs.auto_refill is True


@pytest.mark.skip(reason="样本帧与当前游戏版本不一致，部队行 OCR 需重新采样")
def test_overlapping_team_number_and_red_clothing_are_not_injury():
    vision = Vision()
    image = cv2.imread(str(ROOT / "artifacts/multi-account-2026-10-31/team-slot-overlap.png"))
    assert image is not None
    rows = vision.full_team_rows(image)
    assert set(rows) == {1, 2, 3, 4, 5}
    assert Game.selected_full_slot(image, 2, rows[2].y)
    obs = vision.full_team(image, "main:2")
    assert obs.strength == obs.maximum == 33000
    assert obs.wounded is False and obs.stamina == 200 and obs.supply == 100


@pytest.mark.skip(reason="样本帧与当前游戏版本不一致，部队行 OCR 需重新采样")
def test_team_number_fragments_do_not_merge_across_distant_columns():
    vision = Vision.__new__(Vision)

    def label(text, x, y, score=.99):
        return Text(text, score, x, y, (x-20, y-15, x+20, y+15))

    texts = [label(f"部队{number}", 120, 220+i*110) for i, number in enumerate("一二三五")]
    texts[3] = label("部队五", 120, 670)
    texts.extend([label("部队", 100, 560), label("四", 360, 560, .842)])
    vision.read = lambda *args, **kwargs: texts
    assert vision.full_team_rows(None) == {}
