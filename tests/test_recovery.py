from datetime import datetime
import threading
import time
import os

import pytest

from sanmou_assistant.journal import Journal, cycle_key
from sanmou_assistant.transport import Control, Stopped, InstanceLease


def test_crash_recovery_blocks_duplicate_and_isolates_account(tmp_path):
    db = tmp_path/"runtime.sqlite"
    j = Journal(db,"A")
    ident = j.prepare("main:1","attack","目标")
    j.close()
    j = Journal(db,"A")
    assert j.pending("main:1")[0][0] == ident
    with pytest.raises(RuntimeError):
        j.prepare("main:1","attack","目标")
    j.finish(ident,"uncertain")
    assert j.pending("main:1")
    j.finish(ident,"reviewed","主城核对")
    assert not j.pending("main:1")
    j.set_phase("main:1","backup")
    j.close()
    j = Journal(db,"A")
    assert j.phase("main:1") == "backup"
    other = Journal(db,"B")
    assert not other.pending("main:1") and not other.phase("main:1")
    j.close()
    other.close()


def test_refresh_cycle_midnight_vs_seven():
    early = datetime(2026,10,9,6,59)
    late = datetime(2026,10,9,7,0)
    assert cycle_key("monthly",early) == "2026-10-09"
    assert cycle_key("farming",early) == "2026-10-08"
    assert cycle_key("bandits",late) == "2026-10-09"


def test_daily_pending_and_budget_survive_restart(tmp_path):
    db = tmp_path/"runtime.sqlite"
    j = Journal(db,"A")
    j.record_daily("farming","done",1)
    j.record_daily("farming","pending")
    j.close()
    j = Journal(db,"A")
    assert j.daily("farming") == ("pending",1)
    j.close()


def test_farming_counter_evidence_is_durable_scoped_and_counts_once(tmp_path):
    path = tmp_path/'runtime.sqlite'
    j = Journal(path,'A')
    j.record_daily('farming','done',1)
    j.prepare_farming(18,150000,180000)
    j.close()
    j = Journal(path,'A')
    other = Journal(path,'B')
    assert j.daily('farming') == ('pending',1)
    assert j.farming_attempt()['remaining_before'] == 18
    assert other.farming_attempt() is None
    with pytest.raises(RuntimeError):
        j.prepare_farming(18,150000,180000)
    assert not j.confirm_farming(18,330000)
    assert j.confirm_farming(17,120000)
    assert j.daily('farming') == ('done',2)
    assert not j.confirm_farming(17,120000)
    assert j.daily('farming') == ('done',2)
    j.close()
    other.close()


def test_retries_are_bounded_until_real_review(tmp_path):
    j = Journal(tmp_path/"runtime.sqlite","A")
    for _ in range(3):
        ident = j.prepare("main:1","merit","战场")
        j.finish(ident,"rejected","已核对未在行动")
    assert j.failure_streak("main:1") == 3
    j.review_team("main:1")
    assert j.failure_streak("main:1") == 0
    j.close()


def test_game_farm_exhaustion_is_scoped_durable_and_refreshes_at_seven(tmp_path,monkeypatch):
    import sanmou_assistant.journal as module
    clock=[datetime(2026,10,11,23,59)]
    original=module.cycle_key
    monkeypatch.setattr(module,'cycle_key',lambda task:original(task,clock[0]))
    path=tmp_path/'runtime.sqlite'
    j=Journal(path,'A')
    j.observe_farming_remaining(0,module.cycle_key('farming'))
    j.close()
    j=Journal(path,'A')
    other=Journal(path,'B')
    assert j.farming_exhausted() and not other.farming_exhausted()
    clock[0]=datetime(2026,10,12,6,59)
    assert j.farming_exhausted()
    clock[0]=datetime(2026,10,12,7,0)
    assert not j.farming_exhausted()
    assert j.daily('farming')==('',0)
    j.close()
    other.close()


def test_upgrade_uses_confirmed_zero_result_without_mutating_old_attempt(tmp_path):
    j=Journal(tmp_path/'runtime.sqlite','A')
    j.prepare_farming(1,150000,180000)
    assert j.confirm_farming(0,100000)
    old=j.farming_attempt()
    assert j.farming_exhausted()
    assert j.farming_attempt()==old
    assert j.daily_context('farming_remaining',cycle_key('farming')) is None
    j.close()


def test_food_recovery_mode_survives_restart_without_leaking_to_other_account(tmp_path):
    db = tmp_path/"runtime.sqlite"
    j = Journal(db,"A")
    j.set_flag("food_recovery",True)
    j.close()
    j = Journal(db,"A")
    other = Journal(db,"B")
    assert j.flag("food_recovery") and not other.flag("food_recovery")
    j.set_flag("food_recovery",False)
    assert not j.flag("food_recovery")
    j.close()
    other.close()


def test_stop_interrupts_pause_and_long_wait():
    c = Control()
    c.pause.set()
    done = threading.Event()
    def work():
        try:
            c.sleep(30)
        except Stopped:
            done.set()
    thread = threading.Thread(target=work)
    thread.start()
    time.sleep(.05)
    c.stop.set()
    thread.join(1)
    assert done.is_set() and not thread.is_alive()


@pytest.mark.skipif(os.name != "nt", reason="Windows 实例互斥")
def test_instance_input_has_one_owner_even_across_workers(tmp_path):
    owner = InstanceLease(str(tmp_path/"mumu-cli.exe"), "90")
    with pytest.raises(RuntimeError):
        InstanceLease(str(tmp_path/"mumu-cli.exe"), "90")
    other = InstanceLease(str(tmp_path/"mumu-cli.exe"), "91")
    owner.close()
    replacement = InstanceLease(str(tmp_path/"mumu-cli.exe"), "90")
    replacement.close()
    other.close()


@pytest.mark.parametrize("following",["sandbox","blocked","unknown"])
def test_navigation_waits_without_clicking_unrecognized_or_blocked_frames(following):
    from sanmou_assistant.game import Game,GameError
    from types import SimpleNamespace
    waits = []
    game = Game.__new__(Game)
    game.v = SimpleNamespace(page=lambda image, verify=True:image)
    game.d = SimpleNamespace(control=SimpleNamespace(sleep=waits.append))
    game.emit = lambda *args:None
    game.capture = lambda *args:following
    evidence = []
    game.save_image = lambda image,*args:evidence.append(image)
    if following == "sandbox":
        assert game.wait_transition("unknown") == "sandbox"
    else:
        with pytest.raises(GameError,match="异地登录|切换未完成"):
            game.wait_transition("unknown")
    assert 1 <= len(waits) <= 4
    assert evidence == ([] if following == "sandbox" else [following])

