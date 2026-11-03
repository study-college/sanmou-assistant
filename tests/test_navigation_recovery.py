from types import SimpleNamespace

import pytest

from sanmou_assistant.game import Game, GameError, NavigationRecovered
from sanmou_assistant.journal import Journal
from sanmou_assistant.models import Point, Profile


def recovery(tmp_path, pages):
    taps, events = [], []
    frames = iter(pages)
    device = SimpleNamespace(tap=lambda *a: taps.append(a), control=SimpleNamespace(sleep=lambda _: None))
    vision = SimpleNamespace(page=lambda image, **_: image, grain=lambda _: 6000000,
        navigation_back_point=lambda _: dict(x=138, y=38, title="活动详情", score=.99))
    journal = Journal(tmp_path / "runtime.sqlite", "test")
    game = Game(device, vision, Profile(), journal, tmp_path, lambda *a: events.append(a))
    game.capture = lambda *a: next(frames)
    game.save_image = lambda *a: None
    game.account_checked = True
    return game, taps, events


def test_return_invalidates_batch_and_preserves_unconfirmed_order(tmp_path):
    game, taps, events = recovery(tmp_path, ["navigable_page", "sandbox"])
    try:
        ident = game.j.prepare("main:1", "merit", "target")
        game.j.finish(ident, "uncertain")
        with pytest.raises(NavigationRecovered):
            game.ensure_sandbox()
        assert taps == [(138, 38, .8)]
        assert not game.account_checked
        assert game.j.pending("main:1")[0][0] == ident
        assert game.grain == 6000000
        assert any(k == "navigation_recovery" and v["result"] == "sandbox" for k, v in events)
    finally:
        game.j.close()


@pytest.mark.parametrize("page", ["unknown", "blocked", "maintenance"])
def test_ambiguous_or_protected_page_is_never_blindly_clicked(tmp_path, page):
    game, taps, _ = recovery(tmp_path, [page] * 5)
    try:
        with pytest.raises(GameError):
            game.ensure_sandbox()
        assert not taps
    finally:
        game.j.close()


def test_recovery_is_bounded_per_incident_and_across_cycles(tmp_path):
    game, taps, _ = recovery(tmp_path, ["navigable_page"] * 8)
    try:
        with pytest.raises(GameError, match="上限"):
            game.ensure_sandbox()
        assert len(taps) == 2
        with pytest.raises(GameError, match="上限"):
            game.ensure_sandbox()
        assert len(taps) == 3
    finally:
        game.j.close()


@pytest.mark.parametrize("page", ["navigable_page", "activity", "welfare"])
def test_transition_recovery_does_not_resume_old_navigation(tmp_path, page):
    game, taps, _ = recovery(tmp_path, [page, "sandbox"])
    try:
        with pytest.raises(NavigationRecovered):
            game.wait_transition(page, expected=("worldmap",))
        assert taps == [(138, 38, .8)]
    finally:
        game.j.close()


def test_target_deselection_uses_map_drag_and_verifies_before_zoom():
    game = Game.__new__(Game)
    calls = []
    game.ensure_sandbox = lambda: "sandbox"
    game.v = SimpleNamespace(page=lambda _: "sandbox", map_coordinates=lambda _: (100, 100))
    game.d = SimpleNamespace(swipe=lambda *a: calls.append(("swipe", *a)),
        tap=lambda *a: calls.append(("tap", *a)), zoom_out=lambda: calls.append(("zoom",)))
    game.capture = lambda: "unexpected"
    def transition(*a, **kw):
        raise GameError("page changed")
    game.wait_transition = transition
    with pytest.raises(GameError, match="page changed"):
        game.go_to(Point(x=101, y=101))
    assert calls == [("swipe", 1580, 575, 1480, 575, 250)]


def test_startup_can_recover_activity_and_finish_launch(tmp_path, monkeypatch):
    import sanmou_assistant.worker as module
    pages, events = ["navigable_page"], []
    class Device:
        serial, width, height = "test", 1920, 1080
        def __init__(self, *a): pass
        def connect(self, **kw): pass
        def capture(self): return pages[0]
        def launch_game(self, *a): pass
        def close(self): pass
    class StartupGame:
        last_image = None
        def __init__(self, *a): pass
        def capture(self, *a): return pages[0]
        def ensure_sandbox(self):
            pages[0] = "sandbox"
            raise NavigationRecovered("returned")
        def log(self, *a): pass
    monkeypatch.setattr(module, "Device", Device)
    monkeypatch.setattr(module, "Game", StartupGame)
    monkeypatch.setattr(module, "Vision", lambda *a: SimpleNamespace(page=lambda im: im))
    monkeypatch.setattr(module, "discover_cli", lambda *a: "test")
    monkeypatch.setattr(module, "instances", lambda *a: [])
    worker = module.Worker(Profile(minimize_emulator=False), tmp_path / "session", "launch", True)
    worker.publish = lambda *a: events.append(a)
    worker.run()
    assert ("status", "游戏已就绪") in events
    assert not any(k == "error" for k, _ in events)
