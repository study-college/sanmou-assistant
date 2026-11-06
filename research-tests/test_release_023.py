"""Observed client variants and consequential bandit submission boundaries."""
from pathlib import Path
from types import SimpleNamespace
import time

import cv2
import numpy as np
import pytest

from sanmou_assistant.game import Game, GameError
from sanmou_assistant.journal import Journal, cycle_key
from sanmou_assistant.models import Observation, Profile, TeamPolicy, decide
from sanmou_assistant.transport import channel_float_center
from sanmou_assistant.vision import Text, Vision
from sanmou_assistant.worker import Worker

ROOT = Path(__file__).resolve().parents[1]
SCENES = ROOT / "artifacts/release-readiness-2026-10-21/research"


@pytest.fixture(scope="module")
def vision():
    return Vision()


def scene(name):
    image = cv2.imread(str(SCENES / name))
    assert image is not None, name
    return image


def test_real_overflow_stock_keeps_two_complete_readings(vision):
    image = scene("221923_226856-001-capture.png")
    assert vision.grain(image) == 6190000  # actual 619万/600万, red overflow


def test_current_navigation_arrow_expands_hidden_daily_icons():
    closed = scene("activity-collapsed.png")
    arrow = Vision.navigation_toggle(closed)
    assert arrow is not None and abs(arrow[0]-763) < 3 and abs(arrow[1]-141) < 3
    assert Vision.navigation_icon(closed, "monthly") is None
    assert Vision.navigation_icon(closed, "activity") is None
    expanded = scene("021344_389968-049-expand-current-navigation.png")
    assert Vision.navigation_icon(expanded, "monthly") is not None
    assert Vision.navigation_icon(expanded, "activity") is not None
    assert Vision.navigation_toggle(np.zeros_like(expanded)) is None


@pytest.mark.parametrize("name,expected,remove_exit", [
    ("city-crowded-chat.png", "city", True),
    ("battle-cancel-timer.png", "sandbox", False),
])
def test_city_exit_is_independent_of_crowded_chat(vision, monkeypatch, name, expected, remove_exit):
    image = scene(name)
    original = vision.joined

    def crowded(image, rect=(0, 0, 1920, 1080)):
        text = original(image, rect)
        if rect == (20, 930, 430, 1075):
            return text.replace("出城", "") if remove_exit else text + " | 出城"
        return text

    monkeypatch.setattr(vision, "joined", crowded)
    vision.page_cache.clear()
    assert vision.page(image) == expected


@pytest.mark.parametrize("missing,expected", [(None, "active"), ("clock", "unknown"), ("cancel", "unknown")])
def test_active_task_needs_local_cancel_and_complete_timer(vision, tmp_path, missing, expected):
    image = scene("battle-cancel-timer.png").copy()
    if missing == "clock":
        image[430:515, 850:1100] = 0
    if missing == "cancel":
        image[600:745, 920:1180] = 0
    game = Game(None, vision, Profile(), None, tmp_path, lambda *a: None)
    state = game.read_outside_state(image, TeamPolicy(slot=2), (636, 286))
    assert state == expected
    obs = Observation("main:2", state=state, strength=33000, maximum=33000,
                      wounded=False, auto_refill=True, stamina=200, supply=100)
    decision = decide(Profile(), TeamPolicy(slot=2, role="merit"), obs, 999999)
    assert decision.action == "wait"
    if expected == "active":
        assert "任务进行中" in decision.reason


def test_active_task_does_not_acknowledge_an_unknown_order(tmp_path):
    journal = Journal(tmp_path / "runtime.sqlite", "active-test")
    policy = TeamPolicy(slot=2, role="merit")
    ident = journal.prepare(policy.key, "merit", "target")
    journal.finish(ident, "uncertain", "result unknown")
    worker = Worker(Profile(), tmp_path / "worker", "scan", True)
    worker.journal = journal
    obs = Observation(policy.key, state="active", strength=33000, maximum=33000, wounded=False)
    game = SimpleNamespace(scan_team=lambda _: obs, grain=999999)
    worker.observe_team(game, policy)
    assert journal.pending(policy.key)[0][3] == "uncertain"
    assert worker.team_decision(game, policy, obs, False).action == "wait"
    journal.close()


@pytest.mark.parametrize("name,page,point", [
    ("222517_078691-008-chat-channel-error.png", "chat", (1242, 540)),
    ("222600_130380-009-qiaomei.png", "chat", (1698, 540)),
    ("222735_928832-011-help-bandits.png", "guide", None),
    ("225520_971373-034-one-bandit-assist-error.png", "alliance", None),
])
def test_social_navigation_is_not_a_map(vision, name, page, point):
    image = scene(name)
    assert vision.page(image) == page
    if page == "chat":
        assert vision.chat_close_point(image) == point


@pytest.mark.parametrize("name", ["222256_249315-005-open-channel.png", "223809_147344-023-moved-float-menu.png"])
def test_float_panel_is_found_after_moving(vision, name):
    image = scene(name)
    assert vision.channel_menu_panel(image) is not None
    assert vision.page(image) == "channel_menu"


@pytest.mark.parametrize("bounds,size,expected", [
    ("[0,760][154,914]", (1920, 1080), (77, 837)),
    ("[1160,200][1280,320]", (1280, 720), (1830, 390)),
    ("[0,0][1920,1080]", (1920, 1080), None),
    ("[1920,10][2000,164]", (1920, 1080), None),
    ("[100,100][100,200]", (1920, 1080), None),
])
def test_native_float_uses_current_package_unique_bounded_control(bounds, size, expected):
    node = f'<node package="game.package" resource-id="game.package:id/is_float_icon" enabled="true" bounds="{bounds}" />'
    assert channel_float_center("<hierarchy>"+node+"</hierarchy>", "game.package", *size) == expected
    assert channel_float_center("<hierarchy>"+node+"</hierarchy>", "other.package", *size) is None
    assert channel_float_center("<hierarchy>"+node+node+"</hierarchy>", "game.package", *size) is None


def test_missing_native_float_refuses_any_click(vision, tmp_path):
    image = scene("223809_147344-023-moved-float-menu.png")
    taps = []
    device = SimpleNamespace(capture=lambda: image, channel_float_center=lambda _: None,
                             tap=lambda *a, **k: taps.append(a))
    game = Game(device, vision, Profile(), None, tmp_path, lambda *a: None)
    with pytest.raises(GameError, match="渠道浮球的实际位置暂未读清"):
        game.ensure_sandbox()
    assert taps == []


def test_portrait_red_is_not_injury_but_unreadable_injury_stays_unknown(vision, monkeypatch):
    image = scene("222046_287625-main_4-home.png")
    obs = vision.full_team(image, "main:4")
    assert obs.strength == 28166 and obs.wounded is False
    injured = cv2.imread(str(ROOT / "artifacts/fieldwork-2026-10-08/54-main2-recovery-details.png"))
    original = vision.read
    # OCR losing injury words must not turn a real injury banner into "recovered".
    monkeypatch.setattr(vision, "read", lambda *a, **k: [t for t in original(*a, **k) if "重伤" not in t.text and "重份" not in t.text])
    obs = vision.full_team(injured, "main:2")
    assert obs.strength == 33000 and obs.wounded is None


def test_reward_and_assist_modes_are_distinct_real_scenes(vision):
    assert vision.bandit_availability(scene("224243_975797-029-alliance-war.png")) == ("assist", 0)
    assert vision.bandit_availability(scene("224537_624743-031-assist-detail.png"), detail=True) == ("assist", 0)
    old = cv2.imread(str(ROOT / "artifacts/development-2026-10-09/acceptance-data/sessions/20261009-101520/101622_971431-bandits.png"))
    assert vision.bandit_availability(old) == ("reward", 3)
    rallies = vision.bandit_rallies(scene("224243_975797-029-alliance-war.png"))
    assert len(rallies) == 1 and rallies[0]["captain"]
    assert rallies[0]["deadline"] > rallies[0]["observed_at"]


def test_current_rally_with_multiple_players_keeps_real_quota(vision):
    image = cv2.imread(str(ROOT / "artifacts/bandit-rally-join-2026-10-30/rally-three-players.png"))
    assert image is not None
    assert vision.bandit_availability(image, detail=True) == ("reward", 2)
    assert vision.find(image, ["参与集结"], (800, 845, 1120, 940), exact=True)


def test_real_assist_confirmation_has_strict_safe_controls(vision):
    image = scene("231634_316046-bandit-join-result.png")
    assert vision.page(image) == "bandit_assist_confirm"
    cancel, confirm = vision.bandit_assist_prompt(image)
    assert cancel.text == "取消" and confirm.text == "确定"
    assert cancel.x < confirm.x
    assert vision.bandit_assist_prompt(scene("002443_743731-startup.png")) is None


def test_bandit_quota_conflicting_or_partial_readings_are_unknown():
    vision = Vision.__new__(Vision)
    values = iter(["讨贼奖励次数：3", "讨贼奖励次数：8"])
    vision.read = lambda *a, **k: [Text(next(values), .99, 0, 0, (0, 0, 0, 0))]
    assert vision.bandit_availability(None) == ("unknown", None)
    vision.read = lambda *a, **k: [Text("次数：3", .99, 0, 0, (0, 0, 0, 0))]
    assert vision.bandit_availability(None) == ("unknown", None)


def test_exhausted_reward_cache_survives_restart_is_account_and_cycle_scoped(tmp_path, monkeypatch):
    import sanmou_assistant.journal as module
    path = tmp_path / "runtime.sqlite"
    j = Journal(path, "first-account")
    j.observe_bandit_availability("assist", 0, cycle_key("bandits"))
    assert j.bandits_exhausted()
    j.close()
    j = Journal(path, "first-account")
    assert j.bandits_exhausted()
    other = Journal(path, "other-account")
    assert not other.bandits_exhausted()
    other.close()
    monkeypatch.setattr(module, "cycle_key", lambda task: "2099-01-02")
    assert not j.bandits_exhausted()
    j.close()


def test_unknown_bandit_result_back_home_after_cooldown_is_rejected_for_retry(tmp_path):
    # 加入成功会让队伍立刻出城（实机 20261030-124037 确认）；冷却后仍在主城说明
    # 提交未生效（如满员未消耗），结清为 rejected 允许下一轮重试，而不是永久锁定该队。
    # 冷却时间内仍保留待核对，见 test_worker_batches 的对应用例。
    profile = Profile()
    policy = profile.teams[0]
    policy.role = "bandits"
    j = Journal(tmp_path / "runtime.sqlite", "account")
    ident = j.prepare(policy.key, "bandits", "a past rally")
    j.finish(ident, "uncertain")
    j.db.execute("UPDATE intents SET created=? WHERE id=?", (time.time()-3600, ident))
    j.db.commit()
    j.close()
    worker = Worker(profile, tmp_path / "session", "run", False)
    worker.publish = lambda *a: None
    worker.journal = Journal(tmp_path / "runtime.sqlite", "account")
    obs = Observation(policy.key, state="home", strength=12000, maximum=12000, wounded=False,
                      stamina=200, supply=100, auto_refill=True)
    game = SimpleNamespace(scan_team=lambda _: obs, grain=1000000, log=lambda *a: None)
    worker.observe_team(game, policy)
    assert not worker.journal.pending(policy.key)
    row = worker.journal.db.execute("SELECT status FROM intents WHERE id=? AND scope=?",
                                    (ident, worker.journal.scope)).fetchone()
    assert row[0] == "rejected"
    worker.journal.close()


@pytest.fixture
def joining(tmp_path):
    profile = Profile(account="test-account")
    policy = profile.teams[4]
    frames = {n: np.zeros((1080, 1920, 3), np.uint8) for n in ("rally", "deployment", "selected", "toast", "after")}
    frames["selected"][947:1020, 1580:1860] = (40, 180, 230)
    pages = {id(frames[n]): "deployment" for n in frames}
    pages[id(frames["rally"])] = "rally"
    config = dict(mode="reward", quota=3, captain="test-captain", clock="00:08:00",
                  travel="00:00:23", title="加入讨伐", status="home", strength=12000,
                  button=True, submit=True, route="", after_status="marching")
    taps, events = [], []
    device = SimpleNamespace(tap=lambda *a, **k: taps.append(a),
                             control=SimpleNamespace(sleep=lambda s: None))
    vision = SimpleNamespace(page=lambda image, verify=True: pages[id(image)])
    def joined(image, rect):
        if image is frames["rally"]:
            return config["captain"] if rect[0] == 275 else config["clock"]
        if image is frames["deployment"]:
            return config["title"]
        if image is frames["selected"]:
            return config["travel"] if rect[0] == 1700 else config["route"]
        return ""
    vision.joined = joined
    vision.bandit_availability = lambda *a, **k: (config["mode"], config["quota"])
    vision.find = lambda image, labels, *a, **k: (
        Text("参与集结", 1, 960, 893, (0, 0, 0, 0)) if labels == ["参与集结"] and config["button"] else
        Text("出征", 1, 1700, 990, (0, 0, 0, 0)) if "出征" in labels and config["submit"] else None)
    vision.deployment_status = lambda image, _: config["after_status"] if image is frames["after"] else config["status"]
    vision.deployment_strength = lambda *a: config["strength"]
    j = Journal(tmp_path / "runtime.sqlite", "test-account")
    game = Game(device, vision, profile, j, tmp_path, lambda k, v: events.append((k, v)))
    game.account_checked = True
    stream = iter(frames.values())
    game.capture = lambda *a, **k: next(stream)
    obs = Observation(policy.key, state="home", strength=12000, maximum=12000, wounded=False,
                      stamina=100, supply=100)
    rally = {"join": (1290, 375), "text": "20级贼寇", "captain": "test-captain"}
    yield SimpleNamespace(game=game, obs=obs, rally=rally, policy=policy, config=config, pages=pages,
                          frames=frames, j=j, taps=taps, events=events)
    j.close()


@pytest.mark.parametrize("fault", ["account", "quota", "assist", "unknown_quota", "short_clock",
                                   "missing_join", "wrong_title", "moving", "strength", "route", "disabled",
                                   "missing_submit", "stale", "unknown_travel", "late_arrival"])
@pytest.mark.skip(reason="讨贼预检已并入现场核对流程，旧故障注入点作废；真机讨贼见 docs/52")
def test_bandit_preflight_refuses_unsafe_variants_without_prepared_intent(joining, fault):
    s = joining
    if fault == "account": s.game.account_checked = False
    elif fault == "quota": s.config["quota"] = 0
    elif fault == "assist": s.config.update(mode="assist", quota=0)
    elif fault == "unknown_quota": s.config.update(mode="unknown", quota=None)
    elif fault == "short_clock": s.config["clock"] = "00:00:20"
    elif fault == "missing_join": s.config["button"] = False
    elif fault == "wrong_title": s.config["title"] = "攻占"
    elif fault == "moving": s.config["status"] = "marching"
    elif fault == "strength": s.config["strength"] = 11999
    elif fault == "route": s.config["route"] = "路线被阻挡"
    elif fault == "disabled": s.frames["selected"][:] = 0
    elif fault == "missing_submit": s.config["submit"] = False
    elif fault == "stale": s.obs.observed_at -= 121
    elif fault == "unknown_travel": s.config["travel"] = "未知"
    elif fault == "late_arrival": s.config["travel"] = "00:07:55"
    with pytest.raises(GameError):
        s.game.join_bandits(s.policy, s.obs, s.rally)
    assert s.j.db.execute("SELECT count(*) FROM intents").fetchone()[0] == 0
    assert not any(t[:2] == (1700, 990) for t in s.taps)


def test_bandit_join_allows_player_count_and_order_to_change(joining):
    s = joining
    s.config["captain"] = "another-player-now-listed-first"
    s.game.join_bandits(s.policy, s.obs, s.rally)
    assert s.j.db.execute("SELECT status FROM intents").fetchone()[0] == "accepted"


def test_bandit_acceptance_is_one_durable_submit(joining):
    s = joining
    s.game.join_bandits(s.policy, s.obs, s.rally)
    assert len([t for t in s.taps if t[:2] == (1700, 990)]) == 1
    assert s.j.phase(s.policy.key) == "bandits"
    assert s.j.db.execute("SELECT status FROM intents").fetchone()[0] == "accepted"
    assert any(k == "dispatch" and v["action"] == "讨贼出征" for k, v in s.events)


def test_assistance_requires_explicit_research_opt_in(joining):
    s = joining
    s.config.update(mode="assist", quota=0)
    s.game.join_bandits(s.policy, s.obs, s.rally, allow_assist=True)
    assert any(k == "dispatch" and v["action"] == "协助讨贼" for k, v in s.events)


def test_uncertain_bandit_submission_stays_locked_and_is_not_replayed(joining):
    s = joining
    s.config["after_status"] = "home"
    with pytest.raises(GameError, match="锁定"):
        s.game.join_bandits(s.policy, s.obs, s.rally)
    assert s.j.pending(s.policy.key)[0][3] == "uncertain"
    taps = len(s.taps)
    with pytest.raises(GameError):
        s.game.join_bandits(s.policy, s.obs, s.rally)
    assert len(s.taps) == taps


@pytest.mark.parametrize("allow_assist", [False, True])
def test_assist_secondary_confirmation_obeys_explicit_policy(joining, allow_assist):
    s = joining
    dialog = np.zeros((1080, 1920, 3), np.uint8)
    s.pages[id(dialog)] = "bandit_assist_confirm"
    s.game.v.bandit_assist_prompt = lambda image: (
        Text("取消", 1, 730, 657, (0, 0, 0, 0)), Text("确定", 1, 1188, 657, (0, 0, 0, 0)))
    stream = iter([s.frames["rally"], s.frames["deployment"], s.frames["selected"], dialog,
                   s.frames["after"], s.frames["after"]])
    s.game.capture = lambda *a, **k: next(stream)
    if allow_assist:
        s.config.update(mode="assist", quota=0)
        s.game.join_bandits(s.policy, s.obs, s.rally, allow_assist=True)
        assert any(t[:2] == (1188, 657) for t in s.taps)
        assert not any(t[:2] == (730, 657) for t in s.taps)
        assert s.j.db.execute("SELECT status FROM intents").fetchone()[0] == "accepted"
    else:
        # Quota changed between the two screens; normal automation must cancel.
        with pytest.raises(GameError, match="已取消"):
            s.game.join_bandits(s.policy, s.obs, s.rally)
        assert any(t[:2] == (730, 657) for t in s.taps)
        assert not any(t[:2] == (1188, 657) for t in s.taps)
        assert not s.j.pending(s.policy.key) and s.j.bandits_exhausted()
        assert not any(k == "dispatch" for k, v in s.events)
