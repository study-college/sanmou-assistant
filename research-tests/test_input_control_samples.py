from pathlib import Path

import cv2
import numpy as np
import pytest

from sanmou_assistant.vision import Vision

ROOT = Path(__file__).resolve().parents[1]
ROWS = [
    ("deployment", "core-stability-2026-10-28/close-control-unconfirmed.png"),
    ("overview", "reward-0.4.1/research/sessions/_evidence/actions/20261028-012546/20261028-012709_466218-623e1efa-camp-team-overview.png"),
    ("deployment", "fix-2026-10-11-dailies/live/103353_272515-dispatch-preflight.png"),
    ("monthly", "fix-2026-10-11-dailies/live/104215_110844-monthly-open.png"),
    ("camp_recruit", "reward-0.4.1/research/sessions/_evidence/actions/20261028-012546/20261028-012734_719684-e0c6fd31-camp-recruit-detail.png"),
    ("camp_recruit_list", "reward-0.4.1/research/sessions/_evidence/actions/20261028-012546/20261028-012728_397381-9cbdff20-camp-recruit-panel.png"),
    ("reserve", "fieldwork-2026-10-08/102-live-reserve-pool-panel.png"),
]

def load(name):
    return cv2.imdecode(np.fromfile(ROOT / "artifacts" / name, np.uint8), 1)

@pytest.fixture(scope="module")
def vision():
    return Vision()

@pytest.mark.parametrize("page,name", ROWS)
def test_close_follows_actual_icon_and_refuses_erased_icon(vision, page, name):
    image = load(name)
    point = vision.navigation_close_point(image, page)
    assert point is not None
    x,y = point
    hidden = image.copy()
    hidden[y-45:y+45,x-45:x+45] = 0
    assert vision.navigation_close_point(hidden,page) is None
    shifted = hidden.copy()
    shifted[y-40+10:y+40+10,x-40+12:x+40+12] = image[y-40:y+40,x-40:x+40]
    moved = vision.navigation_close_point(shifted,page)
    assert moved and abs(moved[0]-x-12) <= 2 and abs(moved[1]-y-10) <= 2


def test_menu_dismissal_and_checkbox_require_actual_controls(vision):
    image = load("activity-recovery-2026-10-28/after-activity-back.png")
    assert vision.sandbox_menu_point(image)
    blank = np.zeros_like(image)
    assert vision.sandbox_menu_point(blank) is None
    reward = load("fix-2026-10-11-dailies/live/103945_085724-signin-result.png")
    assert vision.dismiss_point(reward)
    reward[940:] = 0
    assert vision.dismiss_point(reward) is None
    deploy = load("fix-2026-10-11-dailies/live/103353_272515-dispatch-preflight.png")
    assert vision.labeled_checkbox(deploy,["自动回城"],(1230,780,1570,900))
    deploy[800:885,1290:1370] = 0
    assert vision.labeled_checkbox(deploy,["自动回城"],(1230,780,1570,900)) is None


def test_building_drawer_is_recognized_from_its_labels(vision):
    image = load("fieldwork-2026-10-08/24-recruitment-office-location.png")
    assert vision.page(image) == "city_buildings"
    assert vision.city_building_point(load("input-audit-2026-10-28/current-city.png"))
    assert vision.city_building_point(load("fieldwork-2026-10-08/25-recruitment-office-menu.png"))


def test_close_outline_rejects_solid_gold_background(vision):
    image = np.full((1080, 1920, 3), (80, 180, 220), np.uint8)
    assert vision.gold_control(image, 'panel-close-mask.png', (1740,450,1915,645),
                               minimum=.85, outline=True) is None


def test_overflow_stock_is_read_as_one_red_and_white_line(vision):
    image = load('core-stability-2026-10-28/red-stock-split.png')
    assert vision.grain(image) == 6100000


@pytest.mark.parametrize('name,label,checked', [
    ('flow-audit-2026-10-29/transfer-checked.png', '自动征兵', True),
    ('fix-2026-10-11-dailies/live/103353_272515-dispatch-preflight.png', '自动回城', False),
])
def test_checkbox_locates_border_independently_of_tick(vision, name, label, checked):
    image = load(name)
    rect = (1230,780,1570,920)
    point = vision.labeled_checkbox(image, [label], rect)
    assert point is not None
    assert vision.checked(image, *point) is checked
    x,y = point
    hidden = image.copy()
    hidden[y-27:y+27,x-32:x+32] = 0
    assert vision.labeled_checkbox(hidden, [label], rect) is None
    # 方框随页面轻微位移仍跟随实际图像；不能退回硬编码中心。
    shifted = hidden.copy()
    shifted[y-27+5:y+27+5,x-32-6:x+32-6] = image[y-27:y+27,x-32:x+32]
    moved = vision.labeled_checkbox(shifted, [label], rect)
    assert moved and abs(moved[0]-(x-6)) <= 2 and abs(moved[1]-(y+5)) <= 2
    assert vision.checked(shifted, *moved) is checked
