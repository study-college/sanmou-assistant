import threading
from types import SimpleNamespace

import numpy as np
import pytest

from sanmou_assistant.game import Game, GameError
from sanmou_assistant.models import Profile
from sanmou_assistant.transport import Control, Stopped
from sanmou_assistant.worker import Worker


def live_game(tmp_path):
    control = Control()
    frames = []
    values = [1000000]
    page = ['sandbox']
    class Device:
        def __init__(self): self.control = control
        def capture(self, passive=False):
            (control.passive_checkpoint if passive else control.checkpoint)()
            frames.append(passive)
            return np.zeros((1080, 1920, 3), np.uint8)
    detector = SimpleNamespace(limit_side_len=736)
    vision = SimpleNamespace(checkpoint=control.checkpoint, ocr=SimpleNamespace(text_det=detector),
                             precise_detector_limit=736, page=lambda image, verify=True: page[0], grain=lambda image: values[0])
    events = []
    game = Game(Device(), vision, Profile(), None, tmp_path, lambda *args: events.append(args))
    game.live_resources = True
    game.resource_refresh_requested = threading.Event()
    return game, values, page, frames, events


def test_manual_resource_change_is_refreshed_without_navigation_and_request_coalesces(tmp_path):
    game, values, page, frames, events = live_game(tmp_path)
    game.passive_refresh()
    assert game.grain == 1000000 and frames == [False]
    values[0] = 1800000
    game.passive_refresh()
    assert game.grain == 1000000 and len(frames) == 1
    game.resource_refresh_requested.set()
    game.resource_refresh_requested.set()
    game.passive_refresh()
    assert game.grain == 1800000 and len(frames) == 2
    assert not game.resource_refresh_requested.is_set()
    assert [v for k, v in events if k == 'grain'] == [1000000, 1800000]
    page[0] = 'unknown'
    game.resource_refresh_requested.set()
    game.passive_refresh()
    assert game.grain == 1800000
    assert [v for k, v in events if k == 'resource_status'][-1]['visible'] is False
    assert game.resource_observed_at is not None


def test_pause_continues_only_reading_and_stop_still_cancels(tmp_path):
    game, values, page, frames, events = live_game(tmp_path)
    control = game.d.control
    control.pause.set()
    def paused_read():
        game.passive_refresh(paused=True)
        assert game.grain == 1000000
        control.stop.set()
    control.on_pause = paused_read
    with pytest.raises(Stopped): control.checkpoint()
    assert frames == [True]
    assert game.v.checkpoint == control.checkpoint
    assert game.v.ocr.text_det.limit_side_len == 736
    with pytest.raises(Stopped): game.refresh_resources(paused=True)


def test_passive_other_device_login_stops_without_clicking(tmp_path):
    game, values, page, frames, events = live_game(tmp_path)
    page[0] = 'blocked'
    with pytest.raises(GameError, match='异地登录'): game.passive_refresh()
    assert len(frames) == 1
    assert any(k == 'evidence' for k, v in events)
    assert not any(k == 'grain' for k, v in events)


def test_pause_network_popup_only_refreshes_without_reconnect_input(tmp_path):
    game, values, page, frames, events = live_game(tmp_path)
    page[0] = 'reconnect'
    game.d.control.pause.set()
    game.reconnect_game = lambda *a: pytest.fail('暂停回调禁止重连点击')
    game.passive_refresh(paused=True)
    assert frames == [True]
    assert [v for k,v in events if k == 'resource_status'][-1]['visible'] is False


def test_action_capture_does_not_insert_resource_ocr_into_navigation(tmp_path):
    game, values, page, frames, events = live_game(tmp_path)
    image = game.capture('same-frame', True)
    assert len(frames) == 1 and game.last_image is image
    assert game.grain is None
    assert not any(k == 'resource_status' for k, _ in events)
    import cv2
    path = next(v for k, v in events if k == 'evidence')
    assert np.array_equal(cv2.imread(path), image)
    game.passive_refresh()
    assert game.grain == 1000000


def test_idle_refresh_does_not_start_team_scans_or_extend_poll_deadline(tmp_path, monkeypatch):
    import sanmou_assistant.worker as module
    clock = [0.0]
    worker = Worker(Profile(poll_seconds=15), tmp_path)
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    worker.control.sleep = lambda seconds: clock.__setitem__(0, clock[0]+seconds)
    refreshes = []
    class Game:
        next_check = 5
        def passive_refresh(self):
            if clock[0] >= self.next_check:
                refreshes.append(clock[0]); clock[0] += .4; self.next_check = clock[0]+5
    worker.wait_for_next_check(Game())
    assert len(refreshes) == 2
    assert clock[0] == pytest.approx(15)


def test_shared_grain_survives_occlusion_but_expires_and_observed_low_stock_wins(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('sanmou_assistant.game.time.time', lambda: clock[0])
    game, values, _, _, events = live_game(tmp_path)
    game.update_grain(np.zeros((1080,1920,3), np.uint8))
    observed = game.resource_observed_at
    clock[0] += 30
    values[0] = None
    game.update_grain(np.zeros((1080,1920,3), np.uint8))
    assert game.grain == 1000000 and game.resource_observed_at == observed
    assert events[-1][1]['visible'] is False
    clock[0] = observed + game.RESOURCE_MAX_AGE + 1
    assert game.grain is None
    values[0] = 0
    game.update_grain(np.zeros((1080,1920,3), np.uint8))
    assert game.grain == 0


def test_all_three_teams_use_shared_stock_even_if_later_dispatch_frames_hide_it(tmp_path):
    from sanmou_assistant.journal import Journal
    from sanmou_assistant.models import Observation, Point
    game, values, _, _, _ = live_game(tmp_path)
    profile = Profile(food_enabled=True, targets=[Point(name='目标', x=10, y=20)])
    teams = profile.teams[:3]
    for team in teams:
        team.merit = True
    worker = Worker(profile, tmp_path, 'run', False)
    worker.journal = Journal(tmp_path/'runtime.sqlite', 'test')
    game.grain = 6000000
    game.account_checked = True  # 本测试只覆盖共享库存；账号预检由批次测试独立覆盖。
    game.scan_team = lambda p: Observation(p.key, state='home', strength=33000, maximum=33000,
        stamina=200, supply=100, wounded=False, auto_refill=True)
    dispatched = []
    def dispatch(policy, *a, **k):
        dispatched.append(policy.key)
        values[0] = None
        game.update_grain(np.zeros((1080,1920,3), np.uint8))
    game.dispatch = dispatch
    try:
        worker.process_team_batch(game, teams)
        assert dispatched == ['main:1', 'main:2', 'main:3']
    finally:
        worker.journal.close()
