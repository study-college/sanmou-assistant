"""坐标战场、输入时序及错误坐标保护的行为回归。"""
from pathlib import Path
from types import SimpleNamespace

import cv2
import pytest

from sanmou_assistant.game import Game, GameError
from sanmou_assistant.models import Point


def navigation(frames, readings, actual=(648,277)):
    game = Game.__new__(Game)
    calls = []
    sequence = iter(frames)
    coords = iter(readings)
    game.d = SimpleNamespace(finish_coordinate_input=lambda:None, zoom_out=lambda:calls.append('zoom'),
        tap=lambda x,y,*args:calls.append((x,y)), text=lambda s:calls.append(s),
        control=SimpleNamespace(sleep=lambda s:None))
    game.v = SimpleNamespace(page=lambda im, verify=True:im.split(':')[0],
        map_input_coordinates=lambda im:next(coords),
        map_coordinates=lambda im:actual,
        find=lambda *args:SimpleNamespace(x=1750,y=998))
    game.ensure_sandbox = lambda:None
    game.capture = lambda *args:next(sequence)
    game.save_image = lambda *args:None
    game.emit = lambda *args:None
    return game,calls


def test_map_zoom_and_input_refresh_are_bounded_without_replaying_text():
    g,calls = navigation(['sandbox','unknown','worldmap','worldmap','worldmap','sandbox','sandbox'],[None,(648,277)])
    assert g.go_to(Point(x=648,y=277)) == 'sandbox'
    assert calls.count('zoom') == 2
    assert calls.count('648.277') == 1
    assert calls.count((1750,998)) == 1


@pytest.mark.parametrize('readings', [[None,None,None],[(638,277)]])
def test_unverified_input_never_goes_forward(readings):
    g,calls = navigation(['worldmap']*4,readings)
    with pytest.raises(GameError,match='地图输入坐标未核对'):
        g.go_to(Point(x=648,y=277))
    assert (1750,998) not in calls
    assert calls.count('648.277') == 1


def test_two_unsuccessful_zooms_stop_before_input():
    g,calls = navigation(['sandbox','sandbox'],[])
    with pytest.raises(GameError,match='未打开坐标地图'):
        g.go_to(Point(x=648,y=277))
    assert calls == ['zoom','zoom']


def test_wrong_detail_coordinate_still_blocks_after_verified_input():
    g,calls = navigation(['worldmap','worldmap','sandbox','sandbox'],[(648,277)],actual=(638,277))
    with pytest.raises(GameError,match='目标详情坐标不同'):
        g.go_to(Point(x=648,y=277))
    assert calls[-1] == (960,539)


@pytest.mark.parametrize('page',['unknown','blocked'])
def test_input_page_change_does_not_send_more_input(page):
    g,calls = navigation(['worldmap',page],[])
    with pytest.raises(GameError):
        g.go_to(Point(x=648,y=277))
    assert (1750,998) not in calls


def test_actual_map_input_frame_and_conflicting_or_partial_digits():
    from sanmou_assistant.vision import Vision
    vision = Vision()
    image = cv2.imread(str(Path(__file__).resolve().parents[1]/'artifacts/fix-2026-10-26-targets/map-input.png'))
    assert vision.map_input_coordinates(image) == (648,277)
    for samples, expected in [(['648.277','638.277'],None),(['玩家648.277','277'],None),(['648.277', '648.277'],(648,277)),(['648277'],None),(['648.277中'],None),([None],None),(['4000.277'],None)]:
        samples = iter(samples)
        vision.read_line = lambda *a,**kw:next(samples)
        assert vision.map_input_coordinates(image) == expected


@pytest.mark.parametrize('filename',['input-clean.png','input-obscured-1.png','input-obscured-2.png'])
def test_input_coordinate_foreground_survives_transparent_map_labels(filename):
    from sanmou_assistant.vision import Vision
    vision = Vision()
    image = cv2.imread(str(Path(__file__).resolve().parents[1]/'artifacts/fix-2026-10-26-coordinate-stale/samples'/filename))
    assert image is not None
    assert vision.map_input_coordinates(image) == (648,277)
    blank = image.copy()
    blank[975:1020,1425:1600] = 0
    assert vision.map_input_coordinates(blank) is None


def test_real_dim_coordinate_stroke_keeps_two_complete_agreeing_readings():
    from sanmou_assistant.vision import Vision
    root = Path(__file__).resolve().parents[1]
    path = next((root/'artifacts/release-0.4.0/probe-data/sessions/_evidence/faults/target3').glob('*coordinate-input-unconfirmed.png'))
    image = cv2.imdecode(__import__('numpy').fromfile(path, dtype='uint8'), cv2.IMREAD_COLOR)
    assert Vision().map_input_coordinates(image) == (599,294)
