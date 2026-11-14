from datetime import datetime

import pytest

from sanmou_assistant.journal import Journal, cycle_key


def test_daily_spends_survive_restart_and_are_isolated(tmp_path):
    path = tmp_path / 'runtime.sqlite'
    journal = Journal(path, 'account-A')
    cycle = journal.prepare_daily('visit_free', {'offer': '免费', 'gold_before': 1000})
    journal.close()
    journal = Journal(path, 'account-A')
    other = Journal(path, 'account-B')
    try:
        assert journal.daily_context('visit_free')['offer'] == '免费'
        with pytest.raises(RuntimeError):
            journal.prepare_daily('visit_free', {})
        assert other.daily('visit_free') == ('', 0)
        assert journal.finish_daily('visit_free', cycle, {'offer_after': '半价'})
        assert not journal.finish_daily('visit_free', cycle, {})
        assert journal.daily('visit_free') == ('done', 1)
        with pytest.raises(RuntimeError):
            journal.prepare_daily('visit_free', {})
        half_cycle = journal.prepare_daily('visit_half', {'cost': 99})
        assert journal.daily('visit_free') == ('done', 1)
        assert journal.daily('visit_half') == ('pending', 0)
        assert journal.finish_daily('visit_half', half_cycle, {'cost_after': 198})
    finally:
        journal.close()
        other.close()


def test_midnight_confirmation_stays_on_submission_date(tmp_path, monkeypatch):
    import sanmou_assistant.journal as module
    journal = Journal(tmp_path / 'runtime.sqlite', 'A')
    monkeypatch.setattr(module, 'cycle_key', lambda task: '2026-10-11')
    submitted = journal.prepare_daily('visit_half', {'cost': 99})
    monkeypatch.setattr(module, 'cycle_key', lambda task: '2026-10-12')
    new_cycle = journal.prepare_daily('visit_half', {'cost': 99})
    with pytest.raises(RuntimeError):
        journal.prepare_daily('visit_half', {})
    assert journal.finish_daily('visit_half', submitted, {'result': 'confirmed'})
    assert journal.daily('visit_half') == ('pending', 0)
    assert journal.daily('visit_half', '2026-10-11') == ('done', 1)
    assert journal.finish_daily('visit_half', new_cycle, {'result': 'confirmed'})
    assert journal.daily('visit_half') == ('done', 1)
    journal.close()


def test_unfinished_one_time_milestone_still_blocks_repeat_across_days(tmp_path, monkeypatch):
    import sanmou_assistant.journal as module
    journal = Journal(tmp_path / 'runtime.sqlite', 'A')
    monkeypatch.setattr(module, 'cycle_key', lambda task: '2026-10-11')
    journal.prepare_daily('signin_milestone_35', {'threshold': 35})
    monkeypatch.setattr(module, 'cycle_key', lambda task: '2026-10-12')
    with pytest.raises(RuntimeError):
        journal.prepare_daily('signin_milestone_35', {'threshold': 35})
    assert journal.daily('signin_milestone_35', '2026-10-11')[0] == 'pending'
    journal.close()


def test_old_visit_pending_is_preserved_without_blocking_new_cycle(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import sanmou_assistant.daily_tasks as daily_tasks
    import sanmou_assistant.journal as module
    journal = Journal(tmp_path / 'runtime.sqlite', 'A')
    monkeypatch.setattr(module, 'cycle_key', lambda task: '2026-10-11')
    journal.prepare_daily('visit_free', {'gold_before': 1000, 'tickets_before': 2})
    monkeypatch.setattr(daily_tasks, 'cycle_key', lambda task: '2026-10-12')
    monkeypatch.setattr(daily_tasks, 'visit_offer', lambda game, image: ('free', 0, None))
    monkeypatch.setattr(daily_tasks, 'visit_balances', lambda game, image: (1000, 2))

    daily_tasks.reconcile_visit(SimpleNamespace(j=journal), object())

    assert journal.daily('visit_free', '2026-10-11')[0] == 'pending'
    monkeypatch.setattr(module, 'cycle_key', lambda task: '2026-10-12')
    assert journal.prepare_daily('visit_free', {'gold_before': 1000, 'tickets_before': 2})
    journal.close()


def test_claimed_monthly_panel_completes_new_cycle_with_old_pending(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import sanmou_assistant.daily_tasks as daily_tasks
    import sanmou_assistant.journal as module
    journal = Journal(tmp_path / 'runtime.sqlite', 'A')
    monkeypatch.setattr(module, 'cycle_key', lambda task: '2026-10-11')
    journal.prepare_daily('monthly', {'panel': '旧领取'})
    monkeypatch.setattr(module, 'cycle_key', lambda task: '2026-10-12')
    monkeypatch.setattr(daily_tasks, 'cycle_key', lambda task: '2026-10-12')
    image = object()
    game = SimpleNamespace(
        j=journal,
        account_checked=True,
        d=SimpleNamespace(tap=lambda *args: None),
        v=SimpleNamespace(navigation_icon=lambda image, name: (1, 1),
                          page=lambda image: 'monthly_claimed'),
        capture=lambda *args: image,
        ensure_sandbox=lambda: image,
        emit=lambda *args: None,
    )

    daily_tasks.monthly(game)

    assert journal.daily('monthly', '2026-10-11')[0] == 'pending'
    assert journal.daily('monthly', '2026-10-12') == ('done', 0)
    journal.close()


def test_monthly_submission_allows_new_cycle_with_old_pending(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from sanmou_assistant.game import Game
    import sanmou_assistant.journal as module
    journal = Journal(tmp_path / 'runtime.sqlite', 'A')
    monkeypatch.setattr(module, 'cycle_key', lambda task: '2026-10-11')
    journal.prepare_daily('monthly', {'panel': '旧领取'})
    monkeypatch.setattr(module, 'cycle_key', lambda task: '2026-10-12')
    game = Game.__new__(Game)
    game.j = journal
    game.v = SimpleNamespace(page=lambda image: 'monthly',
                             joined=lambda *args: '月卡 每日 领取 恭喜获得',
                             find=lambda *args, **kwargs: SimpleNamespace(x=10, y=20))
    game.d = SimpleNamespace(tap=lambda *args: None)
    game.capture = lambda *args: object()
    game.emit = lambda *args: None
    game.dismiss_overlay = lambda *args: None

    game.monthly_popup(True)

    assert journal.daily('monthly', '2026-10-11')[0] == 'pending'
    assert journal.daily('monthly', '2026-10-12') == ('done', 1)
    journal.close()


def test_event_submission_allows_new_cycle_with_old_pending(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from sanmou_assistant.game import Game
    import sanmou_assistant.journal as module
    journal = Journal(tmp_path / 'runtime.sqlite', 'A')
    monkeypatch.setattr(module, 'cycle_key', lambda task: '2026-10-11')
    journal.prepare_daily('events', {'activity': '旧活动'})
    monkeypatch.setattr(module, 'cycle_key', lambda task: '2026-10-12')
    image = object()
    game = Game.__new__(Game)
    game.j = journal
    game.open_activity = lambda: image
    game.v = SimpleNamespace(
        find=lambda image, labels, *args, **kwargs: SimpleNamespace(
            x=10, y=20, text='活动签到' if '活动签到' in labels else '免费领取'),
        joined=lambda *args: '恭喜获得',
    )
    game.d = SimpleNamespace(tap=lambda *args: None)
    game.capture = lambda *args: image
    game.emit = lambda *args: None
    game.dismiss_overlay = lambda *args: None
    game.ensure_sandbox = lambda: image

    game.event_signin(True)

    assert journal.daily('events', '2026-10-11')[0] == 'pending'
    assert journal.daily('events', '2026-10-12') == ('done', 1)
    journal.close()


@pytest.mark.parametrize('task', ['monthly', 'signin', 'visit', 'visit_free', 'visit_half'])
def test_daily_tasks_reset_at_midnight_not_noon(task):
    assert cycle_key(task, datetime(2026, 10, 11, 0, 0)) == '2026-10-11'
    assert cycle_key(task, datetime(2026, 10, 11, 23, 59)) == '2026-10-11'


def test_worker_skips_completed_daily_pages_after_restart(tmp_path):
    from types import SimpleNamespace
    from sanmou_assistant.worker import Worker
    from sanmou_assistant.models import Profile
    profile = Profile()
    profile.dailies = dict(monthly=True,signin=True,visit=True,half_price=True)
    journal = Journal(tmp_path/'runtime.sqlite','A')
    for task in ('monthly','signin','visit_free','visit_half'):
        journal.record_daily(task,'done')
    journal.close()
    worker = Worker(profile,tmp_path,'run',False)
    worker.journal = Journal(tmp_path/'runtime.sqlite','A')
    calls = []
    game = SimpleNamespace(**{name:lambda commit,n=name:calls.append(n)
                              for name in ('monthly','signin','event_signin','drill','visit')})
    try:
        worker.do_dailies(game,max_tasks=1)
        assert calls == []
        # 用户晚些时候启用半价，仅补做半价；免费完成记录不受汇总状态影响。
        worker.journal.db.execute("DELETE FROM daily WHERE task='visit_half'")
        worker.journal.db.commit()
        worker.do_dailies(game,max_tasks=1)
        assert calls == ['visit']
        assert worker.journal.daily('visit_free')[0] == 'done'
    finally:
        worker.journal.close()
