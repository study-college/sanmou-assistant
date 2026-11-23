"""Tests for target_ops pure functions and Worker runtime hot-reload / wake."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from sanmou_assistant.game import configured_backup_points
from sanmou_assistant.models import Observation, Point, Profile
from sanmou_assistant.target_ops import (
    format_targets_brief,
    merge_live_target_fields,
    parse_action_list,
    replace_targets,
    set_backup_point,
    summarize_targets,
    switch_primary_target,
)
from sanmou_assistant.worker import Worker


def _sample_profile() -> Profile:
    p = Profile(name="测试账号", account="测试主公", main_city=Point(name="主城", x=100, y=100, expected_name="测试主公"))
    p.teams[0].role = "line"
    p.teams[1].role = "merit"
    p.targets = [
        Point(name="旧武勋点", x=200, y=200, actions=["attack", "occupy"], priority=1, enabled=True),
        Point(name="备选武勋点", x=210, y=210, actions=["guard", "march"], priority=2, enabled=False),
    ]
    p.line_targets = [
        Point(name="旧拉线点", x=300, y=300, actions=["occupy", "march"], priority=1, enabled=True),
    ]
    p.merit_backup = Point(name="旧武勋营帐", x=120, y=120, enabled=True)
    p.line_backups = {"main:1": Point(name="旧拉线营帐", x=130, y=130, enabled=True)}
    p.normalize()
    return p


def test_parse_action_list_supports_chinese_and_english_deduplicated():
    assert parse_action_list("驻守, 攻城 → 行军, guard") == ["guard", "attack", "march"]
    assert parse_action_list(["attack", "攻占", "guard"]) == ["attack", "occupy", "guard"]
    with pytest.raises(ValueError):
        parse_action_list("无效指令")


def test_switch_primary_target_replaces_or_keeps_others_and_promotes_existing():
    p = _sample_profile()

    # 默认 keep_others=False：新目标置顶启用，旧目标停用但保留在表中。
    p2, chosen = switch_primary_target(p, mode="line", address="新战场(456,789)", actions="驻守,攻城,行军", active=True)
    assert (chosen.x, chosen.y, chosen.name, chosen.enabled) == (456, 789, "新战场", True)
    assert chosen.actions == ["guard", "attack", "march"]
    assert len(p2.line_targets) == 2
    assert p2.line_targets[0].x == 456 and p2.line_targets[0].enabled is True and p2.line_targets[0].priority == 1
    assert p2.line_targets[1].x == 300 and p2.line_targets[1].enabled is False and p2.line_targets[1].priority == 2

    # keep_others=True 且命中已有坐标：原地提权并保持另一个启用目标。
    p3, chosen2 = switch_primary_target(p2, mode="line", x=300, y=300, keep_others=True, active=True)
    assert chosen2.x == 300 and chosen2.enabled is True
    assert [pt.x for pt in p3.line_targets if pt.enabled] == [300, 456]
    assert [pt.priority for pt in p3.line_targets] == [1, 2]

    # 不显式传 actions 时继承当前首选目标的指令顺序。
    p4, chosen3 = switch_primary_target(p2, mode="line", address="三号点(500,500)", active=True)
    assert chosen3.actions == ["guard", "attack", "march"]


def test_set_backup_point_merit_and_line_and_clear():
    p = _sample_profile()
    p2 = set_backup_point(p, mode="merit", address="新武勋营(150,160)", enabled=True)
    assert p2.backup_enabled is True
    assert p2.merit_backup is not None and (p2.merit_backup.x, p2.merit_backup.y) == (150, 160)

    p3 = set_backup_point(p2, mode="line", team_key="all", address="前线营帐(400,410)")
    assert (p3.line_backups["main:1"].x, p3.line_backups["main:1"].y) == (400, 410)

    p4 = set_backup_point(p3, mode="line", team_key="main:1", clear=True)
    assert "main:1" not in p4.line_backups

    p5 = set_backup_point(p4, mode="merit", clear=True, enabled=False)
    assert p5.merit_backup is None and p5.backup_enabled is False


def test_merge_live_target_fields_ignores_locked_fields_and_tracks_changed_modes():
    base = _sample_profile()
    tampered = _sample_profile()
    tampered.instance = "9"
    tampered.account = "别人账号"
    tampered.teams[0].role = "idle"
    tampered, _ = switch_primary_target(tampered, mode="line", address="热改点(666,777)")

    merged, changed = merge_live_target_fields(base, tampered, active=True)
    assert changed == {"line"}
    # 被锁字段纹丝不动
    assert merged.instance == base.instance
    assert merged.account == base.account
    assert merged.teams[0].role == "line"
    # 目标字段已更新
    assert merged.line_targets[0].x == 666 and merged.line_targets[0].y == 777


def test_replace_targets_and_summary():
    p = _sample_profile()
    p2 = replace_targets(p, mode="merit", points=[
        {"address": "焦点A(222,333)", "actions": ["guard", "march"], "enabled": True},
        {"name": "焦点B", "x": 223, "y": 334, "actions": "攻城,攻占", "enabled": True},
    ], active=True)
    summary = summarize_targets(p2)
    assert summary["merit"]["primary"]["x"] == 222
    assert len(summary["merit"]["enabled"]) == 2
    brief = format_targets_brief(p2)
    assert "焦点A(222,333)" in brief and "(+1备选)" in brief


def test_worker_hot_reload_applies_at_boundary_clears_stale_cooldowns_and_syncs_game(tmp_path):
    p = _sample_profile()
    worker = Worker(p, tmp_path / "session", command="run", dry_run=False)
    worker.folder.mkdir(parents=True, exist_ok=True)
    worker.active_slots = {1, 2}

    # 模拟旧目标产生了各种冷却与目标不可选中标记，以及一支因缺兵收尾的队伍。
    worker.retry_at["main:2"] = time.time() + 300
    worker.retry_at["line:main:1"] = time.time() + 300
    worker.line_target_blocked[("main:1", 300, 300)] = time.time() + 600
    worker.line_failures["main:1"] = {"count": 3, "target": (300, 300), "at": time.time()}
    worker.line_finished.add("main:1")
    worker.line_finished_reasons["main:1"] = "target"
    worker.line_finished.add("main:3")
    worker.line_finished_reasons["main:3"] = "troops"
    worker.line_recheck_at["main:3"] = time.time() + 300

    logs = []
    game = SimpleNamespace(p=p, log=lambda msg, level="INFO": logs.append((level, msg)))

    updated, _ = switch_primary_target(p, mode="line", address="新点位(410,520)", actions=["guard", "attack", "march"], active=True)
    updated = set_backup_point(updated, mode="line", team_key="main:1", address="新营帐(420,530)", active=True)
    merged, changed = worker.queue_target_update(updated, source="agent")
    assert changed == {"line"}
    assert worker.wake_requested.is_set()
    assert merged.line_targets[0].x == 410

    applied = worker.apply_pending_updates(game)
    assert applied is True
    assert worker.profile.line_targets[0].x == 410
    assert game.p is worker.profile
    assert any((b.x, b.y) == (420, 530) for b in configured_backup_points(game.p))
    # 拉线旧冷却/点不中计数/目标不可选中结束已清
    assert "line:main:1" not in worker.retry_at
    assert worker.line_target_blocked == {}
    assert worker.line_failures == {}
    assert "main:1" not in worker.line_finished
    # 因缺兵结束的 main:3 不强行解封，但清理复核倒计时便于立刻核兵
    assert "main:3" in worker.line_finished
    assert "main:3" not in worker.line_recheck_at
    # 武勋未变更，旧武勋冷却不被误删
    assert "main:2" in worker.retry_at
    assert any("热更新/Agent" in msg for _lvl, msg in logs)
    assert any(ev["event"] == "profile_updated" for ev in worker.published)


def test_worker_wait_for_next_check_wakes_immediately_on_target_update(tmp_path):
    p = _sample_profile()
    p.poll_seconds = 30
    worker = Worker(p, tmp_path / "session", command="run", dry_run=False)
    worker.folder.mkdir(parents=True, exist_ok=True)
    game = SimpleNamespace(passive_refresh=lambda: None)

    def trigger():
        time.sleep(0.15)
        updated, _ = switch_primary_target(p, mode="merit", address="速切点(555,666)", active=True)
        worker.queue_target_update(updated, source="ui")

    t = threading.Thread(target=trigger, daemon=True)
    started = time.monotonic()
    t.start()
    worker.wait_for_next_check(game)
    elapsed = time.monotonic() - started
    t.join(timeout=1)
    assert elapsed < 1.5
    assert worker.apply_pending_updates(SimpleNamespace(p=p, log=lambda *a: None)) is True
    assert worker.profile.targets[0].x == 555


def test_worker_process_line_uses_hot_reloaded_target_without_reinit(tmp_path):
    p = _sample_profile()
    worker = Worker(p, tmp_path / "session", command="run", dry_run=False)
    worker.folder.mkdir(parents=True, exist_ok=True)
    worker.active_slots = {1}
    worker.home_checked = True
    worker.roster_checked = True

    dispatched_targets = []

    class FakeJournal:
        def pending(self, key):
            return []

    class FakeGame:
        def __init__(self, profile):
            self.p = profile
            self.account_checked = True

        def log(self, msg, level="INFO"):
            pass

        def ensure_sandbox(self):
            pass

        def read_all_marker_states(self):
            return {1: {"state": "transfer_stay", "label": "调动停留", "confidence": 0.99}}

        def dispatch(self, policy, obs, point, kind):
            dispatched_targets.append((policy.key, point.x, point.y, list(point.actions), kind))

    worker.journal = FakeJournal()
    worker.line_snapshot["main:1"] = (30000, 30000, time.time())
    game = FakeGame(p)

    # 第一轮：按旧坐标 (300,300) 派出
    worker.process_line(game)
    assert dispatched_targets[-1] == ("main:1", 300, 300, ["occupy", "march"], "line")

    # 运行中热更新目标到 (410,520)
    p2, _ = switch_primary_target(p, mode="line", address="新目标(410,520)", actions=["guard", "attack", "march"], active=True)
    worker.queue_target_update(p2, source="agent")
    worker.apply_pending_updates(game)

    # 重置兵力缓存后跑第二轮：直接打新坐标，无需重新初始化 home/roster
    worker.line_snapshot["main:1"] = (30000, 30000, time.time())
    worker.process_line(game)
    assert dispatched_targets[-1] == ("main:1", 410, 520, ["guard", "attack", "march"], "line")
    assert worker.home_checked is True and worker.roster_checked is True
