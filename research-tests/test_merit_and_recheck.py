"""2026-11-04：自然征兵回城、久未产出复核、重伤本地倒数、武勋读数。

这些是纯逻辑单测；真实页面读数在 artifacts/merit-2026-11-04 的实机记录里。
"""
import pathlib
import tempfile
import time
from types import SimpleNamespace

import numpy as np

from sanmou_assistant.game import Game, GameError, stays_at_backup
from sanmou_assistant.journal import Journal
from sanmou_assistant.models import Observation, Point, Profile
from sanmou_assistant.vision import Text, Vision
from sanmou_assistant.worker import Worker


def make_worker(tmp_path):
    return Worker(Profile(), tmp_path, "run", False)


def make_game(profile, journal, coord=(595, 277), arrow=(1744, 257), taps=None):
    image = np.zeros((1080, 1920, 3), np.uint8)
    store = taps if taps is not None else []
    vision = SimpleNamespace(joined=lambda *a: "", page=lambda *a: "sandbox",
                             map_coordinates=lambda im: coord,
                             stationary_return_point=lambda *a: arrow)
    game = Game(SimpleNamespace(tap=lambda *a: store.append(a)), vision, profile, journal,
                pathlib.Path("."), lambda *a: None)
    game.account_checked = True
    game.locate_team = lambda slot: image
    game.capture = lambda *a: image
    game.context_command = lambda *a: None
    return game


def test_backup_phase_stays_outside_until_home_coordinate():
    # 主城 (607,291)，涪县 (595,277)、坐标没读到：都不能当作已回主城。
    assert stays_at_backup("backup", False, (595, 277), (607, 291))
    assert stays_at_backup("backup", False, None, (607, 291))
    # 真的在主城（坐标一致或已判定 home）才解除。
    assert not stays_at_backup("backup", False, (607, 291), (607, 291))
    assert not stays_at_backup("backup", True, (607, 291), (607, 291))
    # 没有 backup 阶段时不干预（城外停留仍是可直接出击的旧规则）。
    assert not stays_at_backup("", False, (595, 277), (607, 291))


def test_backup_recall_accepts_threshold_not_full():
    profile = Profile(account="测试", main_city=Point(x=607, y=291),
                      backups=[Point(name="涪县", x=595, y=277)])
    policy = profile.teams[0]
    taps = []
    journal = Journal(pathlib.Path(tempfile.mkdtemp()) / "j.sqlite", "test")
    game = make_game(profile, journal, taps=taps)
    game.scan_team = lambda p: Observation(p.key, state="returning")
    try:
        # 30,000 / 33,000 未满兵，但已达到 90% 出击下限 29,700 → 允许回城。
        obs = Observation(policy.key, state="outside", strength=30000, maximum=33000, wounded=False)
        game.recall(policy, obs, minimum=policy.threshold(33000))
        assert taps == [(1744, 257)]
        assert journal.phase(policy.key) == "returning"
    finally:
        journal.close()


def test_backup_recall_refuses_below_threshold():
    profile = Profile(account="测试", main_city=Point(x=607, y=291))
    policy = profile.teams[0]
    journal = Journal(pathlib.Path(tempfile.mkdtemp()) / "j.sqlite", "test")
    game = make_game(profile, journal)
    try:
        obs = Observation(policy.key, state="outside", strength=28000, maximum=33000, wounded=False)
        try:
            game.recall(policy, obs, minimum=policy.threshold(33000))
        except GameError as exc:
            assert "回城" in str(exc)
        else:
            raise AssertionError("未达出击下限不应回城")
        assert not journal.db.execute("select count(*) from intents").fetchone()[0]
    finally:
        journal.close()


def test_recheck_due_after_twenty_minutes_of_no_progress(tmp_path):
    worker = make_worker(tmp_path)
    marker = {"state": "busy", "label": "忙碌", "confidence": .9}
    worker.started_at = time.time() - 3600
    worker.last_progress["main:2"] = time.time() - 21 * 60
    worker.progress_marker["main:2"] = "busy"
    assert worker.recheck_due("main:2", marker)
    # 刚读过一次，20 分钟内不重复开详情。
    assert not worker.recheck_due("main:2", marker)
    # 可派的、未识别的都不走这条。
    assert not worker.recheck_due("main:2", {"state": "idle"})
    assert not worker.recheck_due("main:2", {"state": "unknown"})


def test_recheck_not_due_within_twenty_minutes(tmp_path):
    worker = make_worker(tmp_path)
    worker.started_at = time.time() - 3600
    worker.last_progress["main:3"] = time.time() - 5 * 60
    assert not worker.recheck_due("main:3", {"state": "busy"})


def test_wounded_countdown_blocks_recheck_until_due(tmp_path):
    worker = make_worker(tmp_path)
    policy = worker.profile.teams[0]
    obs = Observation(policy.key, state="wounded", wounded=True, injury_seconds=300)
    worker.note_progress(policy, obs)
    assert worker.wounded_until[policy.key] > time.time()
    assert not worker.recheck_due(policy.key, {"state": "wounded"})
    # 倒计时到点（或超时）才复核一次。
    worker.wounded_until[policy.key] = time.time() - 1
    assert worker.recheck_due(policy.key, {"state": "wounded"})
    assert not worker.recheck_due(policy.key, {"state": "wounded"})
    # 恢复后清掉本地倒计时。
    worker.note_progress(policy, Observation(policy.key, state="home", wounded=False))
    assert policy.key not in worker.wounded_until


def test_first_wounded_sighting_reads_countdown_immediately(tmp_path):
    """刚看到重伤就立刻去读一次倒计时，不要干等 10 分钟。"""
    worker = make_worker(tmp_path)
    policy = worker.profile.teams[0]
    worker.note_progress(policy, Observation(policy.key, state="wounded", wounded=True))
    assert worker.recheck_due(policy.key, {"state": "wounded"})
    assert not worker.recheck_due(policy.key, {"state": "wounded"})
    # 读到倒计时后清掉“待首次试探”，改由本地倒数控制下一次读取。
    worker.note_progress(policy, Observation(policy.key, state="wounded", wounded=True, injury_seconds=600))
    assert policy.key not in worker.wounded_probed
    assert not worker.recheck_due(policy.key, {"state": "wounded"})


def test_merit_sampling_keeps_session_baseline(tmp_path):
    worker = make_worker(tmp_path)
    worker.folder.mkdir(parents=True, exist_ok=True)
    events = []
    worker.event.connect(lambda kind, value: events.append((kind, value)))
    logs = []
    game = SimpleNamespace(read_merit=lambda: (5015000, "501.5万"),
                           log=lambda message, level="INFO": logs.append((level, message)))
    worker.check_merit(game)
    merit = [v for k, v in events if k == "merit"]
    assert merit and merit[0]["value"] == 5015000 and merit[0]["delta"] == 0
    assert merit[0]["unit"] == 1000          # “万”级显示，展示时标“约”
    game.read_merit = lambda: (5018000, "501.8万")
    worker.next_merit_at = 0                  # 模拟 30 分钟到点
    worker.check_merit(game)
    merit = [v for k, v in events if k == "merit"]
    assert merit[-1]["delta"] == 3000 and merit[-1]["step"] == 3000
    assert any("武勋 501.8万" in message for _level, message in logs)


def test_merit_read_failure_does_not_raise(tmp_path):
    worker = make_worker(tmp_path)
    worker.folder.mkdir(parents=True, exist_ok=True)
    logs = []
    def boom():
        raise GameError("boom")
    game = SimpleNamespace(read_merit=boom, log=lambda message, level="INFO": logs.append((level, message)))
    worker.check_merit(game)   # 不应抛出
    assert worker.merit_base is None


def test_profile_merit_picks_single_number_near_label():
    fake = SimpleNamespace(read=lambda image, rect: [
        Text("武勋", .9, 0, 0, (0, 0, 0, 0)),
        Text("501.5万", .95, 0, 0, (0, 0, 0, 0)),
    ])
    value, text = Vision.profile_merit(fake, np.zeros((1080, 1920, 3), np.uint8))
    assert (value, text) == (5015000, "501.5万")


def test_profile_merit_rejects_ambiguous_or_empty():
    ambiguous = SimpleNamespace(read=lambda image, rect: [
        Text("155049", .95, 0, 0, (0, 0, 0, 0)),
        Text("501.5万", .95, 0, 0, (0, 0, 0, 0)),
    ])
    assert Vision.profile_merit(ambiguous, np.zeros((1080, 1920, 3), np.uint8)) == (None, "")
    empty = SimpleNamespace(read=lambda image, rect: [])
    assert Vision.profile_merit(empty, np.zeros((1080, 1920, 3), np.uint8)) == (None, "")
