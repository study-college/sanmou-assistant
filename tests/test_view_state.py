"""地图现场（镜头在哪）与“选中目标地块”的契约。

2026-11-18 实机根因：派兵前的读队会点头像把镜头拉到本队，而派兵入口曾无条件
点屏幕正中“重读目标”，结果点到本队/自己城池上，甚至打开「请选择撤退的地点」
这类整屏弹层，退不出来卡死。

规则：
- 镜头是共享可变状态，只有 note_view（坐标导航落地并核对、或重新选中并核对）才能断言；
- 任何移动镜头/改变比例的动作（点头像、缩放、拖图、出城、未知页取消选中）都 drop_view；
- 只有 view_on(point) 为真才允许走“点正中”的快路，否则必须坐标导航。
"""
from types import SimpleNamespace

import pytest

from sanmou_assistant.game import Game, GameError
from sanmou_assistant.models import Observation, Point, Profile, TeamPolicy


def _game(**extra):
    game = Game.__new__(Game)
    game.p = Profile()
    game.emit = lambda *args: None
    game.save_image = lambda *args, **kwargs: None
    game.log = lambda *args, **kwargs: None
    game.control = SimpleNamespace(checkpoint=lambda: None)
    for name, value in extra.items():
        setattr(game, name, value)
    return game


POINT = Point(name="水面", x=715, y=693, actions=["march"])


class TappingDevice:
    def __init__(self):
        self.taps = []

    def tap(self, x, y, *args, **kwargs):
        self.taps.append((x, y))


def _vision(coords, page="sandbox"):
    return SimpleNamespace(page=lambda image, verify=True: page,
                           map_coordinates=lambda image: coords,
                           find=lambda *args, **kwargs: None,
                           joined=lambda *args: "",
                           read=lambda *args, **kwargs: [])


# ---------- 现场状态本身 ----------


def test_view_is_unknown_until_navigation_proves_it():
    game = _game()
    assert game.view_on(POINT) is False
    game.note_view(POINT)
    assert game.view_on(POINT) is True
    assert game.view_on(Point(x=1, y=2)) is False
    game.drop_view()
    assert game.view_on(POINT) is False


def test_failed_navigation_does_not_claim_a_view():
    """坐标两次都没到目标：现场必须保持未知，不能给下一次点击留下“记得”的结论。"""
    game = _game()
    frames = iter(["worldmap|", "worldmap|", "sandbox|638,277", "sandbox|638,277",
                   "worldmap|", "worldmap|", "sandbox|637,277", "sandbox|637,277"])
    game.d = SimpleNamespace(finish_coordinate_input=lambda: None, zoom_out=lambda **k: None,
                             tap=lambda x, y, *a: None, text=lambda s: None,
                             control=SimpleNamespace(sleep=lambda s: None))
    game.v = SimpleNamespace(
        page=lambda image, verify=True: image.split("|", 1)[0],
        map_coordinates=lambda image: (tuple(int(v) for v in image.split("|", 1)[1].split(","))
                                       if "|" in image and image.split("|", 1)[1] else None),
        find=lambda *args, **kwargs: SimpleNamespace(x=1750, y=998))
    game.ensure_sandbox = lambda **kwargs: "worldmap|"
    game.capture = lambda *args: next(frames)
    game.wait_transition = lambda image, expected=(), transitional=(): image
    with pytest.raises(GameError, match="两次仍未到达目标"):
        game.go_to(Point(x=648, y=277))
    assert game.view_point is None


def test_camera_moves_invalidate_the_view():
    """点头像定位队伍、缩放、拖图、出城这类动作之后，必须承认“不知道镜头在哪”。"""
    from sanmou_assistant.game import TeamAbsent

    # 定位队伍（会在外面点头像）——这里用“队不在列表”提前退出，验证入口先作废现场。
    game = _game(active_slots={1})
    game.ensure_sandbox = lambda **kwargs: "sandbox"
    game.note_view(POINT)
    with pytest.raises(TeamAbsent):
        game.locate_team(2)
    assert game.view_point is None

    # 视野缩放
    game = _game()
    game.note_view(POINT)
    game.d = SimpleNamespace(long_press=lambda *a: None)
    game.capture = lambda *args: "frame"
    game.v = SimpleNamespace(map_zoom_point=lambda image, zoom_in=True: (10, 10))
    assert game.map_zoom(True) is True
    assert game.view_point is None

    # 未知页面的往返拖动
    game = _game()
    game.note_view(POINT)
    game.d = SimpleNamespace(swipe=lambda *a: None, control=SimpleNamespace(sleep=lambda s: None))
    game.capture = lambda *args: "frame"
    game.v = SimpleNamespace(page=lambda image, verify=True: "sandbox")
    assert game.deselect_unknown_page() is True
    assert game.view_point is None

    # 回城 + 点主城中心
    game = _game()
    game.note_view(POINT)
    game.d = SimpleNamespace(tap=lambda *a: None)
    game.capture = lambda *args: "frame"
    game.ensure_sandbox = lambda **kwargs: "sandbox"
    game.v = SimpleNamespace(map_coordinates=lambda image: None,
                             read=lambda *args, **kwargs: [])
    with pytest.raises(GameError):
        game.calibrate_home()
    assert game.view_point is None


# ---------- 派兵入口：快路必须凭现场 ----------


def test_reopen_without_view_navigates_instead_of_tapping_the_center():
    """本轮读队之后（镜头在本队身上），必须走坐标导航，绝不能点屏幕正中。"""
    game = _game()
    device = TappingDevice()
    game.d = device
    calls = []
    game.ensure_sandbox = lambda **kwargs: "sandbox"
    game.go_to = lambda point: calls.append((point.x, point.y)) or "定位结果"
    game.capture = lambda *args: "frame"
    game.v = _vision((715, 693))
    assert game.reopen_target(POINT) == "定位结果"
    assert calls == [(715, 693)]
    assert device.taps == [], "镜头不在目标上时不允许点屏幕正中"


def test_reopen_with_view_only_taps_the_center_and_verifies():
    game = _game()
    device = TappingDevice()
    game.d = device
    game.ensure_sandbox = lambda **kwargs: "sandbox"
    game.capture = lambda *args: "frame"
    game.v = _vision((715, 693))
    game.go_to = lambda point: pytest.fail("镜头已经在目标上，不该再导航")
    game.note_view(POINT)
    assert game.reopen_target(POINT) == "frame"
    assert device.taps == [(960, 539)]
    assert game.view_on(POINT) is True


def test_reopen_drops_the_view_when_the_center_is_not_the_target():
    """快路也要核对：点完读到的坐标不对，就作废现场重新导航，不能接着提交。"""
    game = _game()
    device = TappingDevice()
    game.d = device
    game.ensure_sandbox = lambda **kwargs: "sandbox"
    game.capture = lambda *args: "frame"
    game.v = _vision((755, 644))
    calls = []
    game.go_to = lambda point: calls.append((point.x, point.y)) or "重新定位"
    game.note_view(POINT)
    assert game.reopen_target(POINT) == "重新定位"
    assert device.taps == [(960, 539)]
    assert calls == [(715, 693)]
    assert game.view_point is None


def test_failed_dispatch_invalidates_the_view():
    """派兵失败/异常后屏幕上可能已经不是目标详情：下一次必须重新导航。"""
    game = _game()
    game.j = SimpleNamespace(pending=lambda key: [])
    game.note_view(POINT)

    def boom(*args):
        raise GameError("出征页没打开")

    game._dispatch = boom
    policy = TeamPolicy(slot=1, role="line")
    with pytest.raises(GameError):
        game.dispatch(policy, Observation("main:1", state="outside"), POINT, "line")
    assert game.view_point is None


def test_read_team_then_dispatch_never_blind_taps(tmp_path):
    """回归 2026-11-18：读队（镜头被拉到本队）之后派兵，只能导航，不能点正中。"""
    from sanmou_assistant.game import TeamAbsent

    game = _game(active_slots={1})
    device = TappingDevice()
    game.d = device
    game.ensure_sandbox = lambda **kwargs: "sandbox"
    game.capture = lambda *args: "frame"
    game.v = _vision(None)
    game.go_to = lambda point: "sandbox"
    game.note_view(POINT)          # 上一单派完时镜头确实还在目标上
    with pytest.raises(TeamAbsent):
        game.locate_team(2)        # 本轮读队：镜头被拉走
    assert game.view_point is None
    game.reopen_target(POINT)      # 再派兵
    assert (960, 539) not in device.taps
