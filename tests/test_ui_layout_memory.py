"""2026-11-14 体验优化（界面与配置）：空闲任务单选、全表列宽记忆、移除“本次铺路”汇总指标。

全部使用 tmp_path 离线窗口，不连接模拟器、不读写真实 data。
"""
import json

from PySide6.QtWidgets import QApplication, QRadioButton

from sanmou_assistant.models import Profile
from sanmou_assistant.ui import Window


def _role_button(window, row, role):
    return next(b for b in window.team_table.cellWidget(row, 2).findChildren(QRadioButton)
                if b.property("role") == role)


def test_idle_role_is_default_radio_and_roundtrips(tmp_path):
    """未选择默认 idle：新配置打开即选中“空闲”；改选后保存重开仍是空闲。"""
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    assert _role_button(window, 0, "idle").isChecked()
    assert not _role_button(window, 0, "merit").isChecked()
    _role_button(window, 0, "merit").click()
    assert window.profile.teams[0].role == "merit"
    _role_button(window, 0, "idle").click()
    assert window.profile.teams[0].role == "idle"
    assert window.save()
    reopened = Window(tmp_path)
    assert _role_button(reopened, 0, "idle").isChecked()
    assert not _role_button(reopened, 0, "merit").isChecked()
    assert reopened.collect().teams[0].role == "idle"
    window.close(); reopened.close(); app.processEvents()


def test_idle_teams_stay_visible_monitored_and_detail_enabled(tmp_path):
    """空闲≠隐藏：运行总览照常显示该队，详情可用，其他任务不被强迫四选一。"""
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    # 默认全部空闲：总览行不隐藏，显示复选框勾选。
    assert not window.live.isRowHidden(0)
    assert window.live.cellWidget(0, 0).isChecked()
    # 任务单选可选回任一执行任务，互斥只在这五者之间（“铺路”走选路线流程，这里直接置角色）。
    for role in ("bandits", "line", "idle", "merit"):
        _role_button(window, 1, role).click()
        assert window.profile.teams[1].role == role
        assert sum(1 for b in window.team_table.cellWidget(1, 2).findChildren(QRadioButton) if b.isChecked()) == 1
    window.set_team_role(1, "pave")
    assert window.profile.teams[1].role == "pave"
    assert _role_button(window, 1, "pave").isChecked()
    # 空闲队详情仍可配置；隐藏队才锁死。
    assert window.team_table.cellWidget(1, 7).isEnabled()
    window.live.cellWidget(1, 0).setChecked(False)
    assert window.profile.teams[1].role == "hidden"
    assert window.live.isRowHidden(1), "隐藏的队伍才从总览消失"
    assert all(not b.isEnabled() for b in window.team_table.cellWidget(1, 2).findChildren(QRadioButton))
    assert not window.team_table.cellWidget(1, 7).isEnabled()
    window.close(); app.processEvents()


def test_role_cell_fits_widened_task_column(tmp_path):
    """五个单选（含空闲）总宽不得超过任务列宽，避免默认布局下被裁切。"""
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    holder = window.team_table.cellWidget(0, 2)
    assert holder.sizeHint().width() <= window.team_table.columnWidth(2)
    window.close(); app.processEvents()


def test_existing_config_roles_still_load_and_collect(tmp_path):
    """已有配置兼容：老配置里的 merit/bandits/pave/line/hidden 原样载入与回写。"""
    import json as _json
    from sanmou_assistant.models import PaveRoute, Point
    app = QApplication.instance() or QApplication([])
    profile = Profile(account="测试", main_city=Point(name="主城", x=10, y=20),
                      pave_routes=[PaveRoute(name="路线一", points=[[100, 100]])])
    profile.teams[0].role = "merit"
    profile.teams[1].role = "bandits"
    profile.teams[2].role = "pave"
    profile.teams[2].route = "路线一"
    profile.teams[3].role = "line"
    profile.teams[4].role = "hidden"
    profile.teams[4].saved_role = "idle"
    (tmp_path / "active-profile.json").write_text(_json.dumps(profile.to_dict(), ensure_ascii=False),
                                                  encoding="utf-8")
    window = Window(tmp_path)
    assert _role_button(window, 0, "merit").isChecked()
    assert _role_button(window, 1, "bandits").isChecked()
    assert _role_button(window, 2, "pave").isChecked()
    assert _role_button(window, 3, "line").isChecked()
    assert window.team_table.isRowHidden(4) is False  # 设置页仍显示，仅总览隐藏
    assert not window.team_table.cellWidget(4, 0).isChecked()
    assert window.save()
    reopened = Window(tmp_path)
    roles = [t.role for t in reopened.collect().teams]
    assert roles[:5] == ["merit", "bandits", "pave", "line", "hidden"]
    window.close(); reopened.close(); app.processEvents()


def test_two_tables_remember_widths_independently_and_reopen(tmp_path):
    """拖动后备份表与铺路表分别保存，重开后各自恢复、互不串键。"""
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    window.add_backup()
    window.add_pave_route()
    window.backup_table.setColumnWidth(1, 210)
    window.backup_table.setColumnWidth(2, 120)
    window.pave_table.setColumnWidth(0, 260)
    window.pave_table.setColumnWidth(4, 150)
    window.save_widths("backups", window.backup_table)
    window.save_widths("pave", window.pave_table)
    state = json.loads((tmp_path / "ui-state.json").read_text(encoding="utf-8"))
    assert set(("backups", "pave")) <= set(state)
    reopened = Window(tmp_path)
    assert reopened.backup_table.columnWidth(1) == 210
    assert reopened.backup_table.columnWidth(2) == 120
    assert reopened.pave_table.columnWidth(0) == 260
    assert reopened.pave_table.columnWidth(4) == 150
    window.close(); reopened.close(); app.processEvents()


def test_column_drag_schedules_debounced_save(tmp_path):
    """拖动列宽后防抖计时器启动（400ms 单次），不会每次像素都写盘。"""
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    for name in ("teams", "targets", "backups", "lines", "pave"):
        window._width_timers[name].stop()
    window.target_table.setColumnWidth(1, 220)
    assert window._width_timers["targets"].isActive()
    window._width_timers["targets"].stop()
    window.team_table.setColumnWidth(0, 70)
    assert window._width_timers["teams"].isActive()
    window.close(); app.processEvents()


def test_corrupt_ui_state_falls_back_to_defaults(tmp_path):
    """ui-state.json 损坏时所有表回退当前默认列宽，窗口照常构建。"""
    app = QApplication.instance() or QApplication([])
    (tmp_path / "ui-state.json").write_text("{损坏", encoding="utf-8")
    window = Window(tmp_path)
    assert window.target_table.columnWidth(1) == 150
    assert window.backup_table.columnWidth(1) == 160
    assert window.team_table.columnWidth(2) == 360
    window.close(); app.processEvents()


def test_column_count_change_falls_back_without_harming_other_tables(tmp_path):
    """一张表列数变化只回退该表；其他表记录与分栏状态不被覆盖。"""
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    window.add_target()
    window.target_table.setColumnWidth(1, 233)
    window.save_widths("targets", window.target_table)
    (tmp_path / "ui-state.json").write_text(json.dumps({
        "targets": [1, 2, 3],  # 列数不对：整张表回退默认
        "backups": [44, 210, 120, 90, 90, 30],
        "split-dashboard": "QUJD",  # 分栏状态必须原样保留
    }, ensure_ascii=False), encoding="utf-8")
    reopened = Window(tmp_path)
    assert reopened.target_table.columnWidth(1) == 150, "列数变化的表回退默认"
    assert reopened.backup_table.columnWidth(1) == 210, "其他表不受影响"
    reopened.save_widths("pave", reopened.pave_table)
    state = json.loads((tmp_path / "ui-state.json").read_text(encoding="utf-8"))
    assert state["split-dashboard"] == "QUJD", "保存列宽不得覆盖分栏状态"
    assert state["backups"][1] == 210
    assert "pave" in state
    window.close(); reopened.close(); app.processEvents()


def test_pave_summary_metric_removed_but_per_team_progress_stays(tmp_path):
    """右上角不再有跨队“本次铺路”指标；pave 事件、逐队行内进度与日志全部保留。"""
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    assert "本次铺路" not in window.metrics
    window.profile.teams[0].role = "pave"  # 行内进度按原设计只显示给铺路队
    window.handle_event("pave", {"team": "main:1", "route": "测试路", "done": 2, "total": 5,
                                 "status": "moving", "next": [300, 300]})
    assert window.pave_progress["main:1"]["done"] == 2
    assert "铺路 2/5" in window.live.item(0, 6).text(), "逐队进度仍显示在总览行内"
    assert any("2/5" in record["message"] for record in window.log_records)
    assert "已完成 2/5 段" in window.status.text(), "状态栏仍按事件报告本路线进度"
    window.handle_event("pave", {"team": "main:2", "route": "另一路", "done": 1, "total": 9, "status": "moving"})
    assert "本次铺路" not in window.metrics, " pave 事件不得再创建汇总指标"
    window.close(); app.processEvents()
