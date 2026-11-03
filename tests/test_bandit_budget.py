from types import SimpleNamespace

import pytest

from sanmou_assistant.game import Game
from sanmou_assistant.journal import Journal
from sanmou_assistant.models import Observation, Profile


@pytest.fixture
def bandits(tmp_path):
    profile = Profile()
    policy = profile.teams[4]
    policy.bandits = True
    quota = ['reward', 1]
    calls = []
    rally = {'text':'20级贼寇', 'join':[1290,375]}
    vision = SimpleNamespace(joined=lambda *a:'', bandit_rallies=lambda *a:[rally],
        bandit_availability=lambda *a, **k:tuple(quota))
    journal = Journal(tmp_path/'runtime.sqlite', 'account')
    game = Game(None, vision, profile, journal, tmp_path, lambda *a:None)
    game.account_checked = True
    game.grain = 6000000
    game.open_bandit_list = lambda: calls.append('list') or 'frame'
    game.ensure_sandbox = lambda: calls.append('sandbox')
    game.scan_team = lambda p: Observation(p.key, state='home', strength=33000, maximum=33000,
        stamina=200, supply=100, wounded=False, auto_refill=True)
    game.join_bandits = lambda *a, **k:calls.append(('join', k.get('allow_assist')))
    yield game, quota, calls
    journal.close()


def test_zero_reward_stops_further_navigation_and_is_not_assistance(bandits):
    game, quota, calls = bandits
    quota[:] = ['assist', 0]
    assert not game.bandits(True)
    assert game.j.bandits_exhausted()
    count = len(calls)
    assert not game.bandits(True)
    assert len(calls) == count
    assert not any(isinstance(c, tuple) for c in calls)


def test_additional_assistance_requires_saved_explicit_switch(bandits):
    game, quota, calls = bandits
    quota[:] = ['assist', 0]
    game.p.bandit_allow_assist = True
    assert game.bandits(True)
    assert ('join', True) in calls


def test_unknown_reward_count_never_submits_even_with_assistance_enabled(bandits):
    game, quota, calls = bandits
    game.p.bandit_allow_assist = True
    quota[:] = ['unknown', None]
    assert not game.bandits(True)
    assert not any(isinstance(c, tuple) for c in calls)


def test_old_profile_defaults_to_reward_only_and_switch_has_strict_type():
    saved = Profile().to_dict()
    saved.pop('bandit_allow_assist')
    restored = Profile.from_dict(saved)
    assert restored.bandit_allow_assist is False
    restored.bandit_allow_assist = True
    assert Profile.from_dict(restored.to_dict()).bandit_allow_assist is True
    saved['bandit_allow_assist'] = 'false'
    with pytest.raises(ValueError, match='bandit_allow_assist'):
        Profile.from_dict(saved)
