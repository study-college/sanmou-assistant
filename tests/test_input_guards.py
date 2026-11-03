from types import SimpleNamespace
from collections import OrderedDict

import numpy as np
import pytest

from sanmou_assistant.game import Game, GameError, RecognitionUnavailable
from sanmou_assistant.models import Profile
from sanmou_assistant.transport import Control, Device
from sanmou_assistant.vision import Vision


@pytest.mark.parametrize("state,backs", [("mInputShown=true",1),("mInputShown=false",0),("unrecognized state",0)])
def test_keyboard_return_requires_android_visible_keyboard(state, backs):
    device = Device("C:/MuMu/mumu-cli.exe", "0", Control())
    inputs = []
    device.shell = lambda *a: inputs.append(a) or state
    device.control.sleep = lambda _:None
    assert device.finish_coordinate_input() == bool(backs)
    assert sum(a == ("input","keyevent","66") for a in inputs) == backs
    assert not any(a == ("input","keyevent","4") for a in inputs)


def test_unknown_page_and_missing_control_cannot_close_or_dismiss(tmp_path):
    inputs = []
    v = SimpleNamespace(page=lambda _:"unknown", navigation_close_point=lambda *a:None, dismiss_point=lambda _:None)
    game = Game(SimpleNamespace(tap=lambda *a:inputs.append(a)), v, Profile(), None, tmp_path, lambda *a:None)
    game.save_image = lambda *a:None
    for action in (game.close_panel,game.dismiss_overlay):
        with pytest.raises(RecognitionUnavailable):
            action(np.zeros((1080,1920,3),np.uint8))
    assert inputs == []


def test_monthly_does_not_fall_back_to_currency_position(tmp_path):
    inputs = []
    v = SimpleNamespace(navigation_icon=lambda *a:None, navigation_toggle=lambda *a:None, find=lambda *a,**kw:None)
    game = Game(SimpleNamespace(tap=lambda *a:inputs.append(a)), v, Profile(), SimpleNamespace(daily=lambda _:("",0)), tmp_path, lambda *a:None)
    game.account_checked = True
    game.ensure_sandbox = lambda:np.zeros((1080,1920,3),np.uint8)
    with pytest.raises(GameError,match="不点击顶栏货币"):
        game.monthly()
    assert inputs == []


def test_visit_entry_uses_live_label_and_missing_label_does_not_click(tmp_path):
    inputs = []
    label = SimpleNamespace(x=1792,y=1016)
    v = SimpleNamespace(find=lambda *a,**kw:label, page=lambda _:"visit")
    game = Game(SimpleNamespace(tap=lambda *a:inputs.append(a)), v, Profile(), None, tmp_path, lambda *a:None)
    game.ensure_sandbox = lambda:"sandbox"
    game.capture = lambda *a:"visit"
    assert game.open_visit() == "visit"
    assert inputs == [(1792,1016,.8)]
    v.find = lambda *a,**kw:None
    with pytest.raises(GameError,match="入口未识别"):
        game.open_visit()
    assert len(inputs) == 1


def test_cached_text_still_enables_page_fallback():
    vision = Vision.__new__(Vision)
    vision.checkpoint = lambda:None
    vision.cache = OrderedDict()
    calls = []
    def ocr(*a,**kw):
        calls.append(1)
        return [([[0,0],[20,0],[20,20],[0,20]],"标题",.99)], None
    vision.ocr = ocr
    image = np.zeros((40,40,3),np.uint8)
    assert vision.read(image,(0,0,40,40))
    vision.saw_text = False
    assert vision.read(image,(0,0,40,40))
    assert vision.saw_text and len(calls)==1
