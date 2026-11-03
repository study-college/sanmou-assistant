from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from sanmou_assistant.daily_tasks import quick_visit, visit_offer, visit_balances, reconcile_visit
from sanmou_assistant.game import Game, GameError
from sanmou_assistant.journal import Journal
from sanmou_assistant.models import Profile
from sanmou_assistant.vision import Text, Vision

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT/'artifacts/fix-2026-10-11-dailies/live'


def sample(name):
    image = cv2.imdecode(np.fromfile(LIVE/name,dtype=np.uint8),1)
    assert image is not None
    return image


@pytest.fixture(scope='module')
def vision():
    return Vision()


def test_real_second_cycle_signin_and_progress_checks(vision):
    before = sample('103942_762949-signin.png')
    after = sample('103948_167228-signin-confirm.png')
    assert vision.signin_count(before) == 29
    assert vision.signin_count(after) == 30
    cards = vision.signin_cards(before)
    assert vision.highlighted_signin_card(before,cards[2])
    assert not vision.highlighted_signin_card(before,cards[1])
    assert vision.reward_claimed(after,(958,193,1088,308))
    states = {n:vision.reward_claimed(after,(t.x-65,875,t.x+65,995)) for n,t in vision.signin_milestones(after)}
    assert states == {2:True,7:True,14:True,21:True,28:True,35:False}


def test_real_navigation_works_when_names_cover_activity_text(vision):
    image = sample('103549_451621-sandbox-menu.png')
    activity = vision.navigation_icon(image,'activity')
    monthly = vision.navigation_icon(image,'monthly')
    assert abs(activity[0]-894) < 3 and abs(activity[1]-135) < 3
    assert abs(monthly[0]-779) < 3
    assert vision.navigation_icon(np.zeros_like(image),'activity') is None


def test_real_visit_offers_and_fast_checkbox_do_not_toggle_existing_check(vision):
    game = SimpleNamespace(v=vision)
    free = sample('104248_540694-visit-free-ready.png')
    half = sample('104254_043654-visit-free-result.png')
    full = sample('104658_066871-visit_half-result.png')
    assert visit_offer(game,free)[:2] == ('free',0)
    assert visit_offer(game,half)[:2] == ('half',40)
    assert visit_offer(game,full)[:2] == ('other',1)
    assert visit_balances(game,half) == (17580,22)
    assert visit_balances(game,full) == (17540,22)
    taps = []
    game.d = SimpleNamespace(tap=lambda *args:taps.append(args))
    assert quick_visit(game,free,True) is free
    assert taps == []
    missing = half.copy()
    missing[885:935,490:575] = 0  # 隐去半价标签，40黄金不能冒充“优惠用完”。
    with pytest.raises(GameError):
        visit_offer(game,missing)


def test_pending_half_visit_recovers_from_real_result_without_new_spend(tmp_path,vision):
    path = tmp_path/'runtime.sqlite'
    journal = Journal(path,'A')
    journal.prepare_daily('visit_half',{'offer':'half','cost':40,'gold_before':17580,'tickets_before':22})
    journal.close()
    journal = Journal(path,'A')
    game = SimpleNamespace(v=vision,j=journal,emit=lambda *args:None)
    try:
        reconcile_visit(game,sample('104658_066871-visit_half-result.png'))
        assert journal.daily('visit_half') == ('done',1)
        reconcile_visit(game,sample('104658_066871-visit_half-result.png'))
        assert journal.daily('visit_half') == ('done',1)
    finally:
        journal.close()


@pytest.mark.parametrize('allow_half',[False,True])
def test_free_then_optional_half_flow_ends_without_ticket_spend(tmp_path,vision,allow_half):
    free = sample('104248_540694-visit-free-ready.png')
    half_result = sample('104254_043654-visit-free-result.png')
    half_main = sample('104650_945404-visit-open.png')
    full = sample('104658_066871-visit_half-result.png')
    frames = iter([free,free,half_result]+([half_main,full] if allow_half else []))
    taps = []
    profile = Profile()
    profile.dailies['half_price'] = allow_half
    journal = Journal(tmp_path/'runtime.sqlite','A')
    game = Game(SimpleNamespace(tap=lambda *args:taps.append(args)),vision,profile,journal,tmp_path,lambda *args:None)
    game.account_checked = True
    game.capture = lambda *args:next(frames)
    game.ensure_sandbox = lambda:None
    game.open_visit = lambda:game.capture()
    try:
        game.visit(True)
        assert journal.daily('visit_free') == ('done',1)
        assert journal.daily('visit_half') == (('done',1) if allow_half else ('',0))
        assert journal.daily('visit')[0] == 'done'
        assert sum(760<args[0]<1000 and 880<args[1]<970 for args in taps) == (2 if allow_half else 1)
        assert not any(1200<args[0]<1650 and 870<args[1]<980 for args in taps)
        calls_before = list(taps)
        game.visit(True)
        assert taps == calls_before
    finally:
        journal.close()


def test_uncertain_half_offer_is_read_without_resubmission(tmp_path,vision):
    half = sample('104254_043654-visit-free-result.png')
    journal = Journal(tmp_path/'runtime.sqlite','A')
    journal.prepare_daily('visit_half',{'cost':40,'gold_before':17580,'tickets_before':22})
    taps = []
    game = Game(SimpleNamespace(tap=lambda *args:taps.append(args)),vision,Profile(),journal,tmp_path,lambda *args:None)
    game.account_checked = True
    game.capture = lambda *args:half
    try:
        with pytest.raises(GameError,match='上次结果未确认'):
            game.visit(True)
        assert taps == []
        assert journal.daily('visit_half')[0] == 'pending'
    finally:
        journal.close()


def test_real_signin_flow_is_durable_and_never_reopens_on_restart(tmp_path,vision):
    before = sample('103942_762949-signin.png')
    after = sample('103948_167228-signin-confirm.png')
    frames = iter([before,sample('103945_085724-signin-result.png'),after])
    taps = []
    journal = Journal(tmp_path/'runtime.sqlite','A')
    game = Game(SimpleNamespace(tap=lambda *args:taps.append(args)),vision,Profile(),journal,tmp_path,lambda *args:None)
    game.account_checked = True
    game.open_activity = lambda:before
    game.capture = lambda *args:next(frames)
    game.ensure_sandbox = lambda:None
    game.signin(True)
    assert journal.daily('signin')[0] == 'done'
    assert journal.daily('signin_day') == ('done',1)
    assert sum(950 < args[0] < 1100 and 200 < args[1] < 330 for args in taps) == 1
    journal.close()
    game.j = Journal(tmp_path/'runtime.sqlite','A')
    calls_before = list(taps)
    game.open_activity = lambda:pytest.fail('已完成签到不应重新进入活动页')
    try:
        game.signin(True)
        assert taps == calls_before
    finally:
        game.j.close()


@pytest.mark.parametrize('count,day',[(27,28),(28,1),(29,2),(55,28),(56,1)])
def test_cycle_boundaries_select_existing_day_cards(tmp_path,count,day):
    card = Text(f'第{day}天',1,1000,160,(925,130,1075,190))
    label = Text('签到有礼',1,195,160,(100,140,290,180))
    milestone = Text('35天',1,1800,1023,(1770,1000,1830,1045))
    frames = iter(['before','reward','after'])
    taps = []
    v = SimpleNamespace(dismiss_point=lambda _: (960,1000), find=lambda *args,**kwargs:label,
        signin_count=lambda image:count if image=='before' else count+1,
        signin_cards=lambda image:{day:card},
        highlighted_signin_card=lambda image,t:image=='before',
        reward_claimed=lambda image,rect:image=='after',
        joined=lambda image,rect:'恭喜获得' if image=='reward' else '',
        signin_milestones=lambda image:[(999,milestone)])
    journal = Journal(tmp_path/'runtime.sqlite','A')
    game = Game(SimpleNamespace(tap=lambda *args:taps.append(args)),v,Profile(),journal,tmp_path,lambda *args:None)
    game.account_checked = True
    game.open_activity = lambda:'before'
    game.capture = lambda *args:next(frames)
    game.ensure_sandbox = lambda:None
    try:
        game.signin(True)
        assert journal.daily_context('signin_day')['day'] == day
        assert journal.daily('signin_day') == ('done',1)
    finally:
        journal.close()


@pytest.mark.parametrize('pending,claimed',[(False,False),(True,False),(True,True)])
def test_progress_rewards_have_separate_durable_claims(tmp_path,pending,claimed):
    card = Text('第7天',1,1000,160,(925,130,1075,190))
    next_card = Text('第8天',1,1150,160,(1075,130,1225,190))
    label = Text('签到有礼',1,195,160,(100,140,290,180))
    milestone = Text('35天',1,1800,1023,(1770,1000,1830,1045))
    frames = iter(['before','reward','after'])
    taps = []
    v = SimpleNamespace(dismiss_point=lambda _: (960,1000), find=lambda *args,**kwargs:label,signin_count=lambda image:35,
        signin_cards=lambda image:{7:card,8:next_card},highlighted_signin_card=lambda *args:False,
        reward_claimed=lambda image,rect:rect[1]<875 or claimed or image=='after',
        joined=lambda image,rect:'恭喜获得' if image=='reward' else '',
        signin_milestones=lambda image:[(35,milestone)])
    journal = Journal(tmp_path/'runtime.sqlite','A')
    if pending:
        journal.prepare_daily('signin_milestone_35',{'threshold':35,'count':35})
    game = Game(SimpleNamespace(tap=lambda *args:taps.append(args)),v,Profile(),journal,tmp_path,lambda *args:None)
    game.account_checked = True
    game.open_activity = lambda:'before'
    game.capture = lambda *args:next(frames)
    game.ensure_sandbox = lambda:None
    try:
        if pending and not claimed:
            with pytest.raises(GameError,match='待核对'):
                game.signin(True)
            assert not any(args[0]==1800 for args in taps)
            assert journal.daily('signin')[0] != 'done'
        else:
            game.signin(True)
            assert journal.daily('signin_milestone_35')[0] == 'done'
            assert sum(args[0]==1800 for args in taps) == (0 if claimed else 1)
            assert journal.daily('signin')[0] == 'done'
    finally:
        journal.close()


@pytest.mark.parametrize('previous_day',[False,True])
def test_day_28_pending_is_found_below_fold_without_skipping_next_cycle(tmp_path,monkeypatch,previous_day):
    import sanmou_assistant.journal as module
    journal = Journal(tmp_path/'runtime.sqlite','A')
    if previous_day:
        with monkeypatch.context() as patch:
            patch.setattr(module,'cycle_key',lambda task:'2000-01-01')
            submitted = journal.prepare_daily('signin_day',{'count':27,'day':28})
    else:
        submitted = journal.prepare_daily('signin_day',{'count':27,'day':28})
    first = Text('第1天',1,865,160,(790,130,940,190))
    last = Text('第28天',1,1810,560,(1735,530,1885,590))
    label = Text('签到有礼',1,195,160,(100,140,290,180))
    future = Text('999天',1,1800,1023,(1770,1000,1830,1045))
    frames = iter(['top','bottom']+(['fresh','reward','after'] if previous_day else []))
    taps,swipes = [],[]
    v = SimpleNamespace(dismiss_point=lambda _: (960,1000), find=lambda *args,**kwargs:label,
        signin_count=lambda image:29 if image=='after' else 28,
        signin_cards=lambda image:{28:last} if image=='bottom' else {1:first},
        highlighted_signin_card=lambda image,t:previous_day and image in ('top','fresh'),
        reward_claimed=lambda image,rect:image in ('bottom','after'),
        joined=lambda image,rect:'恭喜获得' if image=='reward' else '',
        signin_milestones=lambda image:[(999,future)])
    game = Game(SimpleNamespace(tap=lambda *args:taps.append(args),swipe=lambda *args:swipes.append(args)),v,Profile(),journal,tmp_path,lambda *args:None)
    game.account_checked = True
    game.open_activity = lambda:'top'
    game.capture = lambda *args:next(frames)
    game.ensure_sandbox = lambda:None
    try:
        game.signin(True)
        assert journal.daily('signin_day',submitted) == ('done',1)
        assert journal.daily('signin')[0] == 'done'
        assert sum(args[0]==1810 for args in taps) == 0
        assert sum(args[0]==865 for args in taps) == (1 if previous_day else 0)
        assert len(swipes) == (2 if previous_day else 1)
        if previous_day:
            assert journal.daily_context('signin_day')['day'] == 1
    finally:
        journal.close()
