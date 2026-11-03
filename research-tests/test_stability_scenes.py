from pathlib import Path

import cv2
import numpy as np
import pytest

from sanmou_assistant.game import Game
from sanmou_assistant.vision import Vision


ROOT = Path(__file__).resolve().parents[1] / 'artifacts/stability-2026-10-28'


@pytest.fixture(scope='module')
def vision():
    return Vision()


def scene(name):
    image = cv2.imdecode(np.fromfile(ROOT/name, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    return image


@pytest.mark.skip(reason="游戏版本升级后该样本帧的部队行位置漂移，需重新采样校准；0.7.4 起派兵不依赖该行")
def test_current_five_team_rows_follow_labels_and_reject_other_selected_slots(vision):
    image = scene('probe-101911/main-5-last.png')
    rows = vision.full_team_rows(image)
    assert set(rows) == {1, 2, 3, 4, 5}
    assert 665 < rows[5].y < 685
    for slot, row in rows.items():
        assert Game.selected_full_slot(image, slot, row.y) is (slot == 5)
    erased = image.copy()
    erased[625:725, 0:280] = 0
    assert vision.full_team_rows(erased) == {}
    obs = vision.full_team(image, 'main:5')
    assert obs.strength == obs.maximum == 33000
    assert obs.wounded is False and obs.auto_refill is True


def test_current_return_controls_do_not_match_marching_or_missing_controls(vision):
    image = scene('current-before.png')
    assert all(vision.stationary_return_visible(image, slot) for slot in (1, 2, 3))
    assert not vision.stationary_return_visible(image, 4)
    assert not vision.stationary_return_visible(image, 5)
    erased = image.copy()
    erased[207:298,1667:1768] = 0
    assert not vision.stationary_return_visible(erased, 1)


def test_current_city_and_crowded_map_remain_distinct(vision):
    city = scene('probe-101244/main-5-last.png')
    assert vision.page(city) == 'city'
    exit_button = vision.find(city, ['出城'], (15,1000,180,1075), exact=True)
    assert exit_button and exit_button.y > 1010
    assert vision.page(scene('current-before.png')) == 'sandbox'


def test_arrived_home_marker_is_distinct_from_stationary_and_marching_teams(vision):
    image = scene('samples/arrived-home.png')
    assert vision.home_team_visible(image, 1)
    assert not any(vision.home_team_visible(image, slot) for slot in (2,3,4,5))
    outside = scene('current-before.png')
    assert not any(vision.home_team_visible(outside, slot) for slot in range(1,6))


def test_returning_white_marker_is_read_as_returning_without_repeating_recall(vision, tmp_path):
    from sanmou_assistant.models import Profile, TeamPolicy
    image = scene('samples/returning-team.png')
    assert vision.returning_team_visible(image, 2)
    assert not any(vision.returning_team_visible(image, slot) for slot in (1,3,4,5))
    game = Game(None, vision, Profile(), None, tmp_path, lambda *a:None)
    assert game.read_outside_state(image, TeamPolicy(slot=2), (627,283)) == 'returning'


def test_active_stop_icon_requires_current_team_marker_and_is_not_idle(vision,tmp_path):
    from sanmou_assistant.models import Profile, TeamPolicy
    folder=Path(__file__).resolve().parents[1]/'artifacts/stability-2026-10-29/samples'
    active=cv2.imdecode(np.fromfile(folder/'active.png',dtype=np.uint8),cv2.IMREAD_COLOR)
    idle=cv2.imdecode(np.fromfile(folder/'stationary.png',dtype=np.uint8),cv2.IMREAD_COLOR)
    assert all(vision.team_task_stop_visible(active,s) for s in (1,2,3))
    assert not any(vision.team_task_stop_visible(idle,s) for s in range(1,6))
    game=Game(None,vision,Profile(),None,tmp_path,lambda *a:None)
    assert game.read_outside_state(active,TeamPolicy(slot=1),(636,286)) == 'active'
    erased=active.copy();erased[207:298,1667:1785]=0
    assert not vision.team_task_stop_visible(erased,1)
    # 地图上的金色文字、残留中止图标均不能单独决定任务状态。
    erased_marker=active.copy();erased_marker[248:290,1840:1900]=0
    assert game.read_outside_state(erased_marker,TeamPolicy(slot=1),(636,286)) == 'unknown'
