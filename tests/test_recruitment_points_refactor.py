"""2026-11-15 征兵点配置重构：迁移规则、校验边界与界面离线测试。

改动要点：旧全局 backups / 队伍 target_group、backup_points 只做一次性迁移来源；
武勋备用征兵点是 Profile.merit_backup（所有刷武勋队共用，backup_enabled 仍是开关）；
拉线征兵点是 Profile.line_backups（按 main:slot 每队一个点，与武勋不共用）。
全部用例离线运行，不连接模拟器、不读写真实 data。
"""
import json

import pytest
from PySide6.QtWidgets import QApplication, QCheckBox

from sanmou_assistant.models import (Observation, Point, Profile, decide, line_backup_point,
                                     line_target)
from sanmou_assistant.ui import TeamDetailDialog, Window


def _legacy(backups, teams, **extra):
    data = {"schema": 1, "name": "旧配置", "account": "测试",
            "targets": [{"name": "战场", "x": 120, "y": 200}],
            "line_targets": [{"name": "水面", "x": 800, "y": 800}],
            "backups": backups,
            "teams": teams,
            "main_city": {"name": "主城", "x": 100, "y": 200}}
    data.update(extra)
    return data


def _backup(name, x, y, priority=1, enabled=True):
    return {"name": name, "x": x, "y": y, "priority": priority, "enabled": enabled}


def _team(slot, role, backup_points=None):
    team = {"slot": slot, "group": "main", "role": role}
    if backup_points is not None:
        team["backup_points"] = backup_points
    return team


# ---------- 迁移规则 ----------

def test_merit_backup_prefers_first_merit_teams_named_enabled_point():
    """武勋共用点：按队伍顺序，第一支 merit 队 backup_points 里第一个“存在且启用”的点。"""
    legacy = _legacy(
        [_backup("甲", 10, 20, priority=1), _backup("乙", 30, 40, priority=2)],
        [_team(1, "merit", ["不存在", "乙"]), _team(2, "merit", ["甲"])])
    profile = Profile.from_dict(legacy)
    assert (profile.merit_backup.name, profile.merit_backup.x, profile.merit_backup.y) == ("乙", 30, 40)
    # 地址全部保留到保存文件
    saved = Profile.from_dict(json.loads(json.dumps(profile.to_dict(), ensure_ascii=False)))
    assert (saved.merit_backup.name, saved.merit_backup.x) == ("乙", 30)


def test_merit_backup_falls_back_to_global_priority_order():
    """没有任何 merit 队指定可用点时，取旧全局顺序（priority 升序、同优先级原顺序）第一个启用点。"""
    legacy = _legacy([_backup("甲", 10, 20, priority=2), _backup("乙", 30, 40, priority=1)],
                     [_team(1, "merit")])
    profile = Profile.from_dict(legacy)
    assert profile.merit_backup.name == "乙"


def test_merit_backup_skips_disabled_named_point_and_uses_next():
    """队伍指定的点被停用时跳过，取该队列表里下一个启用点。"""
    legacy = _legacy([_backup("甲", 10, 20, enabled=False), _backup("乙", 30, 40)],
                     [_team(1, "merit", ["甲", "乙"])])
    profile = Profile.from_dict(legacy)
    assert profile.merit_backup.name == "乙"


def test_line_backups_migrate_per_team_with_global_fallback():
    """拉线按队迁移：本队指定取首选；未指定回退旧全局首选；允许多队同一点。"""
    legacy = _legacy([_backup("甲", 10, 20, priority=2), _backup("乙", 30, 40, priority=1)],
                     [_team(1, "line", ["甲"]), _team(2, "line"), _team(3, "merit")])
    profile = Profile.from_dict(legacy)
    assert profile.line_backups["main:1"].name == "甲"
    assert profile.line_backups["main:2"].name == "乙"
    assert "main:3" not in profile.line_backups
    # 与武勋不共用
    assert profile.merit_backup.name == "乙"
    assert set(profile.line_backups) == {"main:1", "main:2"}


def test_disabled_legacy_backups_migrate_to_nothing():
    """旧点全部停用时：没有可用点，新字段为空且不报错。"""
    legacy = _legacy([_backup("甲", 10, 20, enabled=False)],
                     [_team(1, "merit", ["甲"]), _team(2, "line")])
    profile = Profile.from_dict(legacy)
    assert profile.merit_backup is None
    assert profile.line_backups == {}


def test_new_fields_win_over_legacy_data():
    """新配置里已填的 merit_backup / line_backups 不被旧数据覆盖。"""
    legacy = _legacy([_backup("甲", 10, 20)], [_team(1, "merit", ["甲"]), _team(2, "line")],
                     merit_backup=_backup("新点", 77, 88),
                     line_backups={"main:2": _backup("队点", 55, 66)})
    profile = Profile.from_dict(legacy)
    assert profile.merit_backup.name == "新点"
    assert profile.line_backups["main:2"].name == "队点"


def test_saved_config_drops_legacy_fields_and_is_idempotent():
    """保存后不再写 backups / target_group / backup_points；再读一次结果一致。"""
    legacy = _legacy([_backup("甲", 10, 20)], [_team(1, "merit", ["甲"]), _team(2, "line")])
    profile = Profile.from_dict(legacy)
    data = profile.to_dict()
    assert "backups" not in data
    assert all("target_group" not in team and "backup_points" not in team for team in data["teams"])
    again = Profile.from_dict(json.loads(json.dumps(data, ensure_ascii=False)))
    assert again.to_dict() == data


def test_example_profile_migrates_without_errors():
    """仓库里的旧示例配置（带 backups、无 backup_points）载入即迁移，校验干净。

    `profiles/` 是使用者数据目录，按发布规则不进公开导出；公开副本没有这个样本时跳过，
    旧格式迁移本身由本文件其余合成旧配置用例覆盖。
    """
    from pathlib import Path
    example = Path(__file__).resolve().parents[1] / "profiles" / "current-account.example.json"
    if not example.is_file():
        pytest.skip("本机示例配置不进公开导出；迁移由合成旧配置用例覆盖")
    profile = Profile.from_dict(json.loads(example.read_text(encoding="utf-8")))
    assert profile.merit_backup is not None and profile.merit_backup.name == "自己的营帐"
    assert profile.validate() == []


# ---------- 校验边界 ----------

def test_line_backups_never_require_line_targets_or_block_merit():
    """只有拉线征兵点（甚至挂在非拉线队上）时，不得要求拉线目标、不得拦刷武勋。"""
    profile = Profile(account="测试", main_city=Point(x=100, y=200),
                      targets=[Point(name="战场", x=120, y=200, enabled=True)],
                      merit_backup=Point(name="营帐", x=300, y=200),
                      line_backups={"main:1": Point(name="拉线点", x=9, y=9)})
    profile.teams[0].role = "merit"
    profile.backup_enabled = True
    assert profile.validate(active=True) == []


def test_bad_line_backup_key_rejected():
    data = Profile(account="测试", main_city=Point(x=100, y=200),
                   line_backups={"guard:1": Point(name="x", x=9, y=9)}).to_dict()
    with pytest.raises(ValueError, match="拉线征兵点"):
        Profile.from_dict(data)
    data["line_backups"] = {"main:9": {"name": "x", "x": 9, "y": 9}}
    with pytest.raises(ValueError, match="拉线征兵点"):
        Profile.from_dict(data)


def test_backup_point_fields_validated_strictly():
    """征兵点沿用地点严格校验：坐标/优先级/指令非法必须拒绝。"""
    base = Profile(account="测试", main_city=Point(x=100, y=200),
                   merit_backup=Point(name="营帐", x=300, y=200))
    data = base.to_dict()
    data["merit_backup"]["x"] = -1
    with pytest.raises(ValueError):
        Profile.from_dict(data)
    data = base.to_dict()
    data["line_backups"] = {"main:1": {"name": "点", "x": 1, "y": 2, "actions": ["fly"]}}
    with pytest.raises(ValueError):
        Profile.from_dict(data)


def test_shared_line_backup_point_is_not_a_duplicate_name_error():
    """多支拉线队共用同一个点：共享合法，不报“地点名称重复”。"""
    profile = Profile(account="测试", main_city=Point(x=100, y=200),
                      targets=[Point(name="战场", x=120, y=200, enabled=True)],
                      line_targets=[Point(name="水面", x=800, y=800, enabled=True)],
                      line_backups={"main:1": Point(name="营帐", x=300, y=200),
                                    "main:2": Point(name="营帐", x=300, y=200)})
    profile.teams[0].role = "merit"
    profile.teams[1].role = "line"
    profile.teams[2].role = "line"
    assert profile.validate(active=True) == []


def test_decide_uses_merit_backup_and_targets_ignore_groups():
    """decide：transfer 目标固定为 merit_backup；目标不按组过滤。"""
    profile = Profile(account="测试", main_city=Point(x=100, y=200),
                      targets=[Point(name="战场", x=120, y=200, enabled=True, group="别的组")],
                      merit_backup=Point(name="营帐", x=300, y=200))
    profile.teams[0].role = "merit"
    profile.backup_enabled = True
    low = Observation("main:1", state="home", strength=100, maximum=1000, stamina=100,
                      supply=100, wounded=False, auto_refill=True)
    full = Observation("main:1", state="home", strength=1000, maximum=1000, stamina=100,
                       supply=100, wounded=False, auto_refill=True)
    assert decide(profile, profile.teams[0], low, 10 ** 7).target.name == "营帐"
    assert decide(profile, profile.teams[0], full, 10 ** 7).action == "merit"
    profile.merit_backup = None
    assert decide(profile, profile.teams[0], low, 10 ** 7).action == "wait"


def test_line_target_and_line_backup_point_without_groups():
    profile = Profile(line_targets=[Point(name="丙", x=3, y=3, enabled=True, group="乙组", priority=2),
                                    Point(name="甲", x=1, y=1, enabled=True, group="甲组", priority=1)])
    policy = profile.teams[0]
    policy.role = "line"
    assert line_target(profile, policy).name == "甲"
    profile.line_backups = {"main:1": Point(name="营帐", x=9, y=9)}
    assert line_backup_point(profile, policy).name == "营帐"
    del profile.line_backups["main:1"]
    assert line_backup_point(profile, policy) is None


# ---------- 界面离线 ----------

def test_window_layout_at_1366x768_and_page_renames(tmp_path, monkeypatch):
    """1366×768 小屏下窗口放得下，第四页改名“屯田和日常”，备用征兵表移除。"""
    from types import SimpleNamespace
    from PySide6.QtCore import QRect, QPoint
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(Window, "screen", lambda self: SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1366, 768)))
    window = Window(tmp_path)
    window.show()
    app.processEvents()
    assert window.width() <= 1366 and window.height() <= 768
    assert window.nav.item(3).text().startswith("04   屯田和日常")
    assert not hasattr(window, "backup_table")
    assert window.team_table.columnCount() == 9
    headers = [window.team_table.horizontalHeaderItem(i).text() for i in range(window.team_table.columnCount())]
    assert "目标组 / 补兵点" not in headers
    for table in (window.target_table, window.line_table):
        labels = [table.horizontalHeaderItem(i).text() for i in range(table.columnCount())]
        assert "目标组" not in labels
    # 每个页面（含新增卡片）在 1366×768 下都不把保存按钮顶出窗口
    for page in range(window.stack.count()):
        window.nav.setCurrentRow(page)
        app.processEvents()
        assert window.save_button.mapTo(window, QPoint(0, window.save_button.height())).y() <= window.height()
    window.close()
    app.processEvents()


def test_merit_backup_editor_roundtrip_and_clear(tmp_path):
    app = QApplication.instance() or QApplication([])
    profile = Profile(account="测试", main_city=Point(x=100, y=200),
                      targets=[Point(name="战场", x=120, y=200, enabled=True)],
                      merit_backup=Point(name="伏波营", x=11, y=21, priority=4, capacity=2,
                                         actions=["guard"], expected_name="伏波营"))
    profile.teams[0].role = "merit"
    (tmp_path / "active-profile.json").write_text(json.dumps(profile.to_dict(), ensure_ascii=False), encoding="utf-8")
    window = Window(tmp_path)
    assert window.merit_backup_address.text() == "伏波营(11,21)"
    assert window.save()
    # 地址未变：优先级等既有设置保留
    assert window.collect().merit_backup.priority == 4
    window.merit_backup_address.setText("新营帐(50,60)")
    assert window.save()
    saved = Profile.from_dict(json.loads((tmp_path / "active-profile.json").read_text(encoding="utf-8")))
    assert (saved.merit_backup.name, saved.merit_backup.x) == ("新营帐", 50)
    window.merit_backup_address.clear()
    assert window.save()
    assert json.loads((tmp_path / "active-profile.json").read_text(encoding="utf-8"))["merit_backup"] is None
    window.close()
    app.processEvents()


def test_line_backup_rows_follow_teams_and_save(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    assert window.line_backup_edits == {}
    window.set_team_role(1, "line")
    window.set_team_role(2, "line")
    assert set(window.line_backup_edits) == {"main:2", "main:3"}
    window.line_backup_edits["main:2"].setText("营帐甲(300,200)")
    window.line_backup_edits["main:3"].setText("营帐甲(300,200)")
    assert window.save()
    saved = Profile.from_dict(json.loads((tmp_path / "active-profile.json").read_text(encoding="utf-8")))
    assert saved.line_backups["main:2"].name == "营帐甲"
    assert saved.line_backups["main:3"].name == "营帐甲"
    # 角色切走后该行不再收集，但已保存的条目保留（切回拉线自动恢复，不丢地址）。
    window.set_team_role(2, "merit")
    assert "main:3" not in window.line_backup_edits
    assert set(window.line_backup_edits) == {"main:2"}
    assert window.save()
    saved = Profile.from_dict(json.loads((tmp_path / "active-profile.json").read_text(encoding="utf-8")))
    assert set(saved.line_backups) == {"main:2", "main:3"}
    assert saved.validate() == []
    # 切回拉线：地址从保存的条目回显。
    window.set_team_role(2, "line")
    assert window.line_backup_edits["main:3"].text() == "营帐甲(300,200)"
    window.close()
    app.processEvents()


def test_team_detail_dialog_line_only(tmp_path):
    """TeamDetailDialog 只保留拉线补兵回退；非拉线队从部队表无法打开。"""
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    dialog = TeamDetailDialog(window.profile.teams[0], window.profile)
    assert dialog.values() == "recall"
    dialog.fallback.setCurrentIndex(1)
    assert dialog.values() == "stop"
    dialog.close()
    # 非拉线队：详情按钮不可见，edit_team_detail 直接返回
    holder = window.team_table.cellWidget(0, 7)
    assert holder.findChild(QCheckBox) is not None
    window.edit_team_detail(0)
    assert window.profile.teams[0].refill_fallback == "recall"
    window.close()
    app.processEvents()
