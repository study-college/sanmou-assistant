from types import SimpleNamespace

import numpy as np
import pytest

from sanmou_assistant.journal import Journal
from sanmou_assistant.rewards import collect


def fake_game(tmp_path):
    game=SimpleNamespace(account_checked=True,has_chest=True,has_reward=True,taps=[],events=[])
    game.j=Journal(tmp_path/'runtime.sqlite','account')
    game.ensure_sandbox=lambda:'before'
    game.capture=lambda label,*a:'after' if label.endswith('result') else 'before'
    game.v=SimpleNamespace(reward_chest=lambda image,task:{'x':1600,'y':980} if game.has_chest and image=='before' else None,
                           joined=lambda *a:'恭喜获得' if game.has_reward else '',page=lambda image:'unknown')
    game.d=SimpleNamespace(tap=lambda *a:game.taps.append(a))
    game.emit=lambda *a:game.events.append(a)
    return game


@pytest.mark.parametrize('task',['drill','alliance_rewards'])
def test_new_chest_can_be_claimed_twice_in_one_day(tmp_path,task):
    game=fake_game(tmp_path)
    assert collect(game,task) and collect(game,task)
    assert len(game.taps)==2
    assert game.j.db.execute("SELECT count(*) FROM intents WHERE status='accepted'").fetchone()[0]==2
    game.j.close()


def test_no_chest_and_readonly_never_click_or_write_intent(tmp_path):
    game=fake_game(tmp_path);game.has_chest=False
    assert collect(game,'drill') is False
    game.has_chest=True
    assert collect(game,'drill',False) is False
    assert not game.taps and game.j.pending('reward:drill')==[]
    assert game.j.db.execute('SELECT count(*) FROM intents').fetchone()[0]==0
    game.j.close()


def test_unknown_reward_claim_survives_restart_without_another_click(tmp_path):
    game=fake_game(tmp_path);game.has_reward=False
    assert collect(game,'drill')
    assert len(game.taps)==1 and game.j.pending('reward:drill')
    game.j.close();game.j=Journal(tmp_path/'runtime.sqlite','account')
    assert collect(game,'drill') is False and len(game.taps)==1
    game.has_chest=False
    collect(game,'drill')
    assert not game.j.pending('reward:drill')
    game.j.close()


def test_repeated_absence_does_not_spam_status(tmp_path):
    game=fake_game(tmp_path);game.has_chest=False
    for _ in range(5):collect(game,'alliance_rewards')
    assert len(game.events)==1
    game.j.close()


def test_reward_detector_requires_icon_near_matching_navigation():
    import cv2
    from pathlib import Path
    from sanmou_assistant.vision import Vision,Text
    v=Vision.__new__(Vision)
    p=Path(__file__).resolve().parents[1]/'sanmou_assistant/assets/reward-chest-live.png'
    template=cv2.imdecode(np.fromfile(p,np.uint8),cv2.IMREAD_COLOR)
    image=np.zeros((1080,1920,3),np.uint8)
    h,w=template.shape[:2];image[960:960+h,1610:1610+w]=template
    v.reward_templates=[cv2.cvtColor(template,cv2.COLOR_BGR2GRAY)]
    v.find=lambda *a,**kw:Text('征战',1,1580,1010,(1540,990,1620,1030))
    assert v.reward_chest(image,'drill')
    v.find=lambda *a,**kw:Text('同盟',1,1120,1010,(1080,990,1160,1030))
    assert v.reward_chest(image,'alliance_rewards') is None
    image[:]=0;cv2.circle(image,(1180,978),15,(0,220,255),-1)
    assert v.reward_chest(image,'alliance_rewards') is None


def test_worker_does_not_skip_recurring_chest_because_old_daily_is_done(tmp_path):
    from sanmou_assistant.worker import Worker
    from sanmou_assistant.models import Profile
    profile=Profile(dailies={'drill':True,'monthly':True})
    worker=Worker(profile,tmp_path,'run',False)
    worker.journal=Journal(tmp_path/'runtime.sqlite','account')
    worker.journal.record_daily('drill','done')
    calls=[]
    game=SimpleNamespace(drill=lambda commit:calls.append('drill') or False,
        monthly=lambda commit:calls.append('monthly'),signin=lambda *a:None,
        event_signin=lambda *a:None,visit=lambda *a:None)
    worker.do_dailies(game,max_tasks=1)
    assert calls==['drill','monthly']
    worker.journal.close()
