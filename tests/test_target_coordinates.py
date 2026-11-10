"""坐标定位：以实际到达的地块坐标为唯一放行条件，不再回读输入框文字。"""
from types import SimpleNamespace

import pytest

from sanmou_assistant.game import Game, GameError
from sanmou_assistant.models import Point


def navigation(frames):
    """frames 形如 'worldmap|'、'sandbox|648,277'；wait_transition 按标记模拟超时。"""
    game = Game.__new__(Game)
    calls = []
    sequence = iter(frames)

    def page(image, verify=True):
        return image.split("|", 1)[0]

    def coords(image):
        part = image.split("|", 1)
        if len(part) < 2 or not part[1]:
            return None
        x, y = part[1].split(",")
        return int(x), int(y)

    def wait_transition(image, expected=(), transitional=()):
        if image.startswith("timeout"):
            raise GameError("页面切换未完成")
        if image.startswith("blocked"):
            raise GameError("游戏提示异地登录或断线，已停止操作")
        return image

    game.d = SimpleNamespace(finish_coordinate_input=lambda: None, zoom_out=lambda: calls.append('zoom'),
        tap=lambda x, y, *args: calls.append((x, y)), text=lambda s: calls.append(s),
        control=SimpleNamespace(sleep=lambda s: None))
    game.v = SimpleNamespace(page=page, map_coordinates=coords,
        find=lambda *args, **kwargs: SimpleNamespace(x=1750, y=998))
    game.ensure_sandbox = lambda: 'worldmap|'
    game.capture = lambda *args: next(sequence)
    game.wait_transition = wait_transition
    game.save_image = lambda *args: None
    game.emit = lambda *args: None
    return game, calls


def test_navigation_passes_without_reading_input_box_at_all():
    g, calls = navigation(['worldmap|', 'worldmap|', 'sandbox|648,277', 'sandbox|648,277'])
    assert g.go_to(Point(x=648, y=277)) == 'sandbox|648,277'
    assert calls.count('648.277') == 1
    assert calls.count((1750, 998)) == 1


def test_wrong_destination_is_retried_once_with_new_input_then_succeeds():
    g, calls = navigation([
        'worldmap|', 'worldmap|', 'sandbox|638,277', 'sandbox|638,277',
        'worldmap|', 'worldmap|', 'sandbox|648,277', 'sandbox|648,277'])
    assert g.go_to(Point(x=648, y=277)) == 'sandbox|648,277'
    assert calls.count('648.277') == 2
    assert calls.count((1750, 998)) == 2


def test_forward_not_taken_is_detected_and_input_replayed():
    g, calls = navigation([
        'worldmap|', 'worldmap|', 'timeout|1', 'worldmap|',
        'worldmap|', 'worldmap|', 'sandbox|648,277', 'sandbox|648,277'])
    assert g.go_to(Point(x=648, y=277)) == 'sandbox|648,277'
    assert calls.count('648.277') == 2


def test_two_wrong_destinations_stop_without_attack():
    g, calls = navigation([
        'worldmap|', 'worldmap|', 'sandbox|638,277', 'sandbox|638,277',
        'worldmap|', 'worldmap|', 'sandbox|637,277', 'sandbox|637,277'])
    with pytest.raises(GameError, match='两次仍未到达目标'):
        g.go_to(Point(x=648, y=277))
    assert calls.count('648.277') == 2


def test_page_change_after_typing_stops_before_forward():
    g, calls = navigation(['worldmap|', 'chat|'])
    with pytest.raises(GameError, match='坐标输入后未确认地图页面'):
        g.go_to(Point(x=648, y=277))
    assert (1750, 998) not in calls


def test_two_unsuccessful_zooms_stop_before_input():
    g, calls = navigation(['sandbox|', 'sandbox|'])
    with pytest.raises(GameError, match='未打开坐标地图'):
        g.go_to(Point(x=648, y=277))
    assert calls == ['zoom', 'zoom']


def test_blocked_after_forward_is_not_retried_as_input_failure():
    g, calls = navigation(['worldmap|', 'worldmap|', 'blocked|', 'blocked|'])
    with pytest.raises(GameError, match='异地登录'):
        g.go_to(Point(x=648, y=277))
    assert calls.count('648.277') == 1
