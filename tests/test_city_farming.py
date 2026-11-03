from types import SimpleNamespace

import pytest

from sanmou_assistant.game import Game, GameError, FarmingUnavailable, NetworkRecovered
from sanmou_assistant.journal import Journal
from sanmou_assistant.models import Profile


@pytest.fixture
def farm(tmp_path):
    events, taps = [], []
    class Vision:
        remaining = 20
        current = 150000
        after = 310000  # 收益18万，同时补兵耗粮2万，库存净增16万。
        accepted = True
        is_farm = True
        def page(self, image, verify=True): return 'city'
        def find(self,image,labels,rect,exact=False):
            if labels == ['参与']:
                return SimpleNamespace(text='参与',x=1125,y=891)
            if labels == ['农田']:
                return SimpleNamespace(text='12级农田') if self.is_farm else None
            return SimpleNamespace(text=f'屯田（余{self.remaining}）',x=1201,y=516)
        def read(self,*args):
            return [SimpleNamespace(text='12级农田',x=960,y=450),
                    SimpleNamespace(text='预计收入18万',x=960,y=575),
                    SimpleNamespace(text='屯田',x=960,y=700)]
        def grain(self,image): return self.current
    vision = Vision()
    def tap(x,y,delay=.35):
        taps.append((x,y))
        if (x,y) == (960,700) and vision.accepted:
            vision.remaining -= 1
            vision.current = vision.after
    device = SimpleNamespace(tap=tap,back=lambda:None,control=SimpleNamespace(sleep=lambda seconds:None))
    journal = Journal(tmp_path/'runtime.sqlite','A')
    game = Game(device,vision,Profile(food_enabled=True,farming_budget=5),journal,tmp_path/'proof',lambda *e:events.append(e))
    game.grain = 150000
    game.open_affairs = lambda:'affairs'
    game.capture = lambda *args:'city'
    def checked_farm(inside=False):
        if not vision.is_farm:
            raise FarmingUnavailable('未确认农田，暂停新屯粮并继续部队监控')
        return 'city', SimpleNamespace(text=f'屯田（余{vision.remaining}）',x=1201,y=516), vision.remaining
    game.city_farm = checked_farm
    def sandbox():
        game.grain = vision.current
        return 'sandbox'
    game.ensure_sandbox = sandbox
    yield game, journal, vision, taps, events
    game.j.close()


@pytest.mark.parametrize('after',[310000,100000])
def test_city_farming_confirms_used_count_despite_concurrent_grain_consumption(farm,after):
    game,journal,vision,taps,events = farm
    vision.after = after
    game.farming()
    assert taps.count((960,700)) == 1
    assert journal.daily('farming') == ('done',1)
    assert journal.farming_attempt()['remaining_after'] == 19
    assert journal.farming_attempt()['grain_after'] == after
    assert game.grain == after
    assert any(kind == 'farming_check' and value['confirmed'] for kind,value in events)


def test_unchanged_count_stays_pending_and_rechecks_without_resubmitting_after_restart(farm,tmp_path):
    game,journal,vision,taps,events = farm
    vision.accepted = False
    game.farming()
    assert journal.daily('farming') == ('pending',0)
    game.farming()
    assert taps.count((960,700)) == 1
    journal.close()
    game.j = Journal(tmp_path/'runtime.sqlite','A')
    vision.remaining = 19  # 延迟的游戏结果已经到达。
    vision.current = game.grain = 2000000
    game.farming()  # 即便库存已经恢复，也先核对待完成记录。
    assert game.j.daily('farming') == ('done',1)
    assert taps.count((960,700)) == 1
    game.farming()
    assert game.j.daily('farming') == ('done',1)
    assert taps.count((960,700)) == 1


def test_farming_preflight_failure_returns_to_monitoring_without_submission(farm):
    game,journal,vision,taps,events = farm
    vision.read = lambda *a:[]
    game.farming()
    assert (960,700) not in taps
    assert journal.daily('farming') == ('',0)
    assert game.farming_retry_at > 0
    assert any(kind == 'daily' and '未确认屯田收益' in value.get('result','') for kind,value in events)


@pytest.mark.parametrize('error', [RuntimeError('设备错误'), NetworkRecovered('网络已恢复')])
def test_farming_does_not_swallow_transport_or_recovery(farm, error):
    game, *_ = farm
    def fail(*a, **kw):
        raise error
    game._farming = fail
    with pytest.raises(type(error)):
        game.farming()


def test_city_farming_uses_saved_budget_and_never_exceeds_it(farm):
    game,journal,vision,taps,events = farm
    for _ in range(7):
        game.farming(force=True)
    assert journal.daily('farming') == ('done',5)
    assert taps.count((960,700)) == 5
    assert any('预算已用完' in value.get('result','') for kind,value in events if kind == 'daily')


@pytest.mark.parametrize('bad_remaining',[20,18,21])
def test_ambiguous_counter_change_does_not_confirm_or_allow_duplicate(farm,bad_remaining):
    game,journal,vision,taps,events = farm
    journal.prepare_farming(20,150000,180000)
    vision.remaining = bad_remaining
    game.farming()
    assert journal.daily('farming') == ('pending',0)
    assert (960,700) not in taps


def test_non_farm_or_missing_game_uses_are_not_spent(farm):
    game,journal,vision,taps,events = farm
    vision.is_farm = False
    game.farming()
    assert (960,700) not in taps
    assert journal.daily('farming') == ('',0)
    assert any('未确认农田' in value.get('result','') for kind,value in events if kind == 'daily')
    game.farming_retry_at = 0
    vision.is_farm,vision.remaining = True,0
    game.farming()
    assert (960,700) not in taps
    assert journal.daily('farming') == ('',0)


def test_preview_and_legacy_pending_leave_budget_and_game_untouched(farm):
    game,journal,vision,taps,events = farm
    game.farming(commit=False)
    assert journal.daily('farming') == ('',0)
    assert (960,700) not in taps
    journal.record_daily('farming','pending')
    game.farming()
    assert journal.daily('farming') == ('pending',0)
    assert (960,700) not in taps
    assert any('旧屯田记录' in value.get('result','') for kind,value in events if kind == 'daily')


def test_last_game_use_stops_navigation_across_restart_below_local_budget(farm,tmp_path):
    game,journal,vision,taps,events = farm
    vision.remaining = 1
    game.farming(force=True)
    assert journal.daily('farming') == ('done',1)
    assert journal.farming_exhausted()
    journal.close()
    game.j = Journal(tmp_path/'runtime.sqlite','A')
    game.city_farm = lambda **k: pytest.fail('已确认余0，不应再次进入农田')
    for _ in range(3):
        game.farming(force=True)
    assert taps.count((960,700)) == 1
    assert game.j.daily('farming') == ('done',1)


def test_first_observed_zero_is_durable_without_spending_or_budget_change(farm,tmp_path):
    game,journal,vision,taps,events = farm
    vision.remaining = 0
    game.farming()
    assert journal.farming_exhausted()
    journal.close()
    game.j = Journal(tmp_path/'runtime.sqlite','A')
    game.city_farm = lambda **k: pytest.fail('余0跨重启必须跳过')
    game.farming()
    assert game.j.daily('farming') == ('',0)
    assert not taps


def test_pending_attempt_is_checked_before_cached_zero_and_never_resubmitted(farm):
    from sanmou_assistant.journal import cycle_key
    game,journal,vision,taps,events = farm
    journal.prepare_farming(1,150000,180000)
    journal.observe_farming_remaining(0,cycle_key('farming'))
    vision.remaining = 0
    game.farming()
    assert journal.daily('farming') == ('done',1)
    assert not taps


def test_refresh_during_preflight_cancels_submission_and_does_not_cache_old_zero(farm,monkeypatch):
    import sanmou_assistant.journal as journal_module
    import sanmou_assistant.game as game_module
    game,journal,vision,taps,events = farm
    cycle = ['2026-10-11']
    monkeypatch.setattr(journal_module,'cycle_key',lambda task:cycle[0])
    monkeypatch.setattr(game_module,'cycle_key',lambda task:cycle[0])
    original = vision.read
    def read(*args):
        cycle[0] = '2026-10-12'
        return original(*args)
    vision.read = read
    game.farming()
    assert (960,700) not in taps
    assert journal.daily('farming') == ('',0)
    assert journal.daily_context('farming_remaining') is None


def test_old_map_farm_config_migrates_without_a_map_destination():
    previous = Profile().to_dict()
    previous['grain_point'] = {'name':'旧地图农田','x':123,'y':456}
    previous['grain_field'] = [740,602]
    profile = Profile.from_dict(previous)
    assert 'grain_point' not in profile.to_dict()
    assert 'grain_field' not in profile.to_dict()


def test_city_exit_waits_for_more_than_one_loading_frame_without_clicking(farm):
    game,journal,vision,taps,events = farm
    frames = iter(['loading','loading','ready'])
    game.capture = lambda *args:next(frames)
    vision.page = lambda image:'sandbox' if image == 'ready' else 'unknown'
    image = Game.ensure_sandbox(game)
    assert image == 'ready' and game.grain == 150000
    assert not taps


def test_persistently_unknown_page_stops_after_bounded_wait_without_clicking(farm):
    import cv2
    import numpy as np
    game,journal,vision,taps,events = farm
    frame = np.full((1080,1920,3),77,np.uint8)
    captures = []
    def capture(*args):
        captures.append(1)
        return frame
    game.capture = capture
    vision.page = lambda image:'unknown'
    with pytest.raises(GameError,match='未识别'):
        Game.ensure_sandbox(game)
    assert not taps
    assert sum(kind=='status' for kind,value in events) == 4
    assert len(captures) == 5
    evidence = next(value for kind,value in events if kind == 'evidence')
    assert np.array_equal(cv2.imread(evidence),frame)
