from types import SimpleNamespace

import numpy as np
import pytest

from sanmou_assistant.game import Game, GameError
from sanmou_assistant.journal import Journal
from sanmou_assistant.models import Observation, Profile


@pytest.mark.parametrize('has_arrow', [True, False])
def test_manual_return_uses_current_verified_arrow_and_never_guesses(tmp_path, has_arrow):
    profile = Profile()
    policy = profile.teams[0]
    image = np.zeros((1080,1920,3), np.uint8)
    taps = []
    vision = SimpleNamespace(joined=lambda *a:'', page=lambda *a:'sandbox',
        map_coordinates=lambda im:(200,200),
        stationary_return_point=lambda *a:(1744,257) if has_arrow else None)
    journal = Journal(tmp_path/'runtime.sqlite', 'test')
    game = Game(SimpleNamespace(tap=lambda *a:taps.append(a)), vision, profile, journal, tmp_path, lambda *a:None)
    game.account_checked = True
    game.locate_team = lambda slot:image
    game.capture = lambda *a:image
    game.context_command = lambda *a:None
    game.scan_team = lambda p:Observation(p.key, state='returning')
    obs = Observation(policy.key, state='outside', strength=33000, maximum=33000, wounded=False)
    try:
        if has_arrow:
            game.recall(policy, obs, manual=True)
            assert taps == [(1744,257)]
            assert journal.phase(policy.key) == 'returning'
            assert journal.db.execute('select status from intents').fetchone()[0] == 'accepted'
        else:
            with pytest.raises(GameError, match='回城按钮'):
                game.recall(policy, obs, manual=True)
            assert not taps
            assert journal.db.execute('select count(*) from intents').fetchone()[0] == 0
    finally:
        journal.close()
