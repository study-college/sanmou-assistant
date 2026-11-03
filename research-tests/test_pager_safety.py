from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from sanmou_assistant.game import Game, GameError
from sanmou_assistant.models import Profile, TeamPolicy
from sanmou_assistant.vision import Vision


@pytest.mark.parametrize('initial,readings,expected', [
    ('112', [('112', .99), ('112', .99)], 1),
    ('172.', [('172.', .99), ('.172', .93)], 1),
    ('212', [('212', .99), ('212', .99)], 2),
    ('112', [('112', .99), ('212', .99)], None),
    ('172', [('272', .99), ('272', .99)], None),
    ('112', [('112', .99), ('112', .89)], None),
    ('112', [('112', .99), ('12', .99)], None),
    ('112', [('112', .99), ('', .99)], None),
    ('112', [('1112', .99), ('1112', .99)], None),
])
def test_ambiguous_fast_read_cannot_bypass_tight_confidence_and_agreement(initial, readings, expected):
    v = Vision.__new__(Vision)
    v.checkpoint = lambda: None
    v.joined = lambda image, rect: initial if image.shape[0] == 70 else ''
    calls = []
    from itertools import cycle
    tight = cycle(readings)
    def ocr(crop, **kwargs):
        calls.append(crop.shape)
        # 宽裁剪的低门槛路径不得以两个模糊数字提前返回。
        return ([[initial, .99]], 0) if crop.shape[0] in (34, 23) else ([[*next(tight)]], 0)
    v.ocr = ocr
    assert v.portrait_page(np.zeros((1080, 1920, 3), np.uint8)) == expected
    assert len(calls) == (4 if expected is not None else 8)


@pytest.mark.parametrize('pages,success,swipes', [
    ([1], True, 0),
    ([2, 1], True, 1),
    ([2, None, 1], True, 1),
    ([2, 2, 2], False, 1),
    ([None, None, None], False, 0),
    ([None, 1], True, 0),
])
def test_main_portraits_verifies_actual_first_page_without_blind_repeated_swipes(tmp_path, pages, success, swipes):
    pages = iter(pages)
    inputs, events = [], []
    device = SimpleNamespace(swipe=lambda *a: inputs.append(a), control=SimpleNamespace(sleep=lambda _: None))
    game = Game(device, SimpleNamespace(portrait_page=lambda _, verify=True: next(pages)), Profile(), None, tmp_path, lambda *a: events.append(a))
    game.ensure_sandbox = lambda **_: np.zeros((1080, 1920, 3), np.uint8)
    if success:
        game.main_portraits()
    else:
        with pytest.raises(GameError, match='已重读 3 次'):
            game.main_portraits()
        assert list(tmp_path.rglob('*portrait-page-unreadable.png'))
        assert any(kind == 'vision' and data['stage'] == 'portrait_page' for kind, data in events)
    assert len(inputs) == swipes


def test_today_real_dotted_pager_and_unknown_page_remain_readable():
    root = Path(__file__).resolve().parents[1]
    v = Vision()
    for path in [root/'artifacts/audit-2026-10-18/pager-dots-real.png',
                 root/'data/sessions/20261018-171050/173231_645678-unknown-page.png']:
        image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        assert image is not None
        assert v.portrait_page(image) == 1
        assert v.page(image) == 'sandbox'


def test_real_second_page_pager_moves_with_shorter_portrait_list():
    root = Path(__file__).resolve().parents[1]
    v = Vision()
    image = cv2.imdecode(np.fromfile(root/'artifacts/audit-2026-10-18/second-page-real.png',dtype=np.uint8),cv2.IMREAD_COLOR)
    assert v.portrait_page(image) == 2
    assert v.page(image) == 'sandbox'
    # 第2页页码抹去后不能从三张头像、军力条或地图名字猜2。
    image[571:619,1770:1910] = 0
    assert v.portrait_page(image) is None


def test_real_stationary_merit_team_is_not_marching_because_of_tile_menu(tmp_path):
    root = Path(__file__).resolve().parents[1]
    v = Vision()
    image = cv2.imdecode(np.fromfile(root/'data/sessions/20261018-112345/152357_016680-main_1-located.png',dtype=np.uint8),cv2.IMREAD_COLOR)
    policy = TeamPolicy(slot=1)
    game = Game(None,v,Profile(),SimpleNamespace(phase=lambda _: 'merit'),tmp_path,lambda *a: None)
    assert v.stationary_return_visible(image,1)
    assert game.read_outside_state(image,policy,(636,286)) == 'outside'
