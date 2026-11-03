from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from sanmou_assistant.game import Game
from sanmou_assistant.journal import Journal
from sanmou_assistant.models import Profile
from sanmou_assistant.vision import Text, Vision


def test_actual_claimed_monthly_panel_closes_without_claiming_again(tmp_path):
    path = (Path(__file__).resolve().parents[1] / "artifacts" /
            "fix-2026-10-09-farm-discovery" / "live-one-20261010-000611" /
            "000637_823550-unknown-page.png")
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    assert Vision().page(image) == "monthly_claimed"
    captures = iter(["monthly_claimed", "sandbox"])
    taps = []
    game = Game(SimpleNamespace(tap=lambda *args: taps.append(args)),
                SimpleNamespace(navigation_close_point=lambda *a:(1685,120), page=lambda frame, verify=True: frame, grain=lambda frame: 1_000_000),
                Profile(), None, tmp_path, lambda *a: None)
    game.capture = lambda *a: next(captures)
    assert game.ensure_sandbox() == "sandbox"
    assert taps == [(1685, 120, .6)]


def test_actual_monthly_reward_title_is_not_cropped_after_claim(tmp_path):
    path = (Path(__file__).resolve().parents[1] / "artifacts" /
            "fix-2026-10-09-farm-discovery" / "live-one-20261010-000347" /
            "000406_246765-monthly-claimed.png")
    after = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert after is not None
    ocr = Vision()
    taps = []
    reads = iter(["monthly", after])
    vision = SimpleNamespace(
        dismiss_point=lambda _:(960,1000),
        page=lambda image, verify=True: "monthly",
        joined=lambda image, rect: "月卡 今日领取 黄金×90 黄金×150" if isinstance(image, str) else ocr.joined(image, rect),
        find=lambda *a, **k: Text("一键领取", .99, 960, 916, (900, 900, 1020, 932)),
    )
    device = SimpleNamespace(tap=lambda *args: taps.append(args))
    journal = Journal(tmp_path / "runtime.sqlite", "monthly-regression")
    try:
        game = Game(device, vision, Profile(), journal, tmp_path, lambda *a: None)
        game.capture = lambda *args: next(reads)
        assert game.monthly_popup(commit=True)
        assert journal.daily("monthly") == ("done", 1)
        assert len(taps) == 2  # One claim, one verified reward dismissal.
    finally:
        journal.close()


@pytest.mark.parametrize('enabled',[False,True])
def test_monthly_popup_before_account_check_closes_without_blocking_or_claiming(tmp_path,enabled):
    profile = Profile()
    profile.dailies['monthly'] = enabled
    frames = iter(['monthly','monthly','sandbox'])
    taps = []
    vision = SimpleNamespace(navigation_close_point=lambda *a:(1685,120), page=lambda image, verify=True:image,
        joined=lambda *args:'月卡 今日领取',
        find=lambda *args,**kwargs:Text('一键领取',1,960,926,(800,900,1100,950)))
    journal = Journal(tmp_path/'runtime.sqlite','A')
    game = Game(SimpleNamespace(tap=lambda *args:taps.append(args)),vision,profile,journal,tmp_path,lambda *args:None)
    game.capture = lambda *args:next(frames)
    game.claim_login_rewards = True
    try:
        assert game.ensure_sandbox(refresh_grain=False) == 'sandbox'
        assert taps == [(1685,120,.6)]
        assert journal.daily('monthly') == ('',0)
    finally:
        journal.close()
