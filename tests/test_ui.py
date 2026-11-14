import json
from types import SimpleNamespace
from pathlib import Path

from PySide6.QtWidgets import QApplication, QRadioButton

from sanmou_assistant.models import Profile
from sanmou_assistant.ui import Window
from sanmou_assistant.models import Observation


def test_small_logical_desktop_keeps_window_and_save_control_visible(tmp_path,monkeypatch):
    from PySide6.QtCore import QRect,QPoint
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(Window,"screen",lambda self:SimpleNamespace(availableGeometry=lambda:QRect(0,0,1024,640)))
    window=Window(tmp_path)
    window.show();app.processEvents()
    assert window.width()<=1024 and window.height()<=640
    for page in range(window.stack.count()):
        window.nav.setCurrentRow(page);app.processEvents()
        assert window.save_button.mapTo(window,QPoint(0,window.save_button.height())).y()<=window.height()
    window.close();app.processEvents()


def test_monitor_starts_readonly_worker_even_when_other_buttons_execute(tmp_path,monkeypatch):
    import sanmou_assistant.ui as module
    app = QApplication.instance() or QApplication([])
    calls = []
    class Worker:
        event = finished = SimpleNamespace(connect=lambda *a:None)
        def __init__(self,p,folder,command,dry_run):calls.append((command,dry_run))
        def start(self):pass
        def isRunning(self):return False
    monkeypatch.setattr(module,"Worker",Worker)
    window = Window(tmp_path)
    window.monitor()
    assert calls == [("scan",True)]
    window.close();app.processEvents()


def test_desktop_forms_preserve_team_and_target_settings(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    window.add_target()
    t = window.target_table
    t.item(0,1).setText("我的战场")
    t.cellWidget(0,2).setValue(640)
    t.cellWidget(0,3).setValue(286)
    for button in window.team_table.cellWidget(1,2).findChildren(QRadioButton):
        if button.property("role") == "merit":
            button.click()
    window.team_table.cellWidget(1,4).setValue(77)
    window.team_table.cellWidget(1,5).setValue(53)
    window.add_backup()
    window.backup_table.item(0,1).setText("补兵营帐")
    window.profile.teams[1].backup_points = ["补兵营帐"]
    window.sync_team_row(1)
    window.food_low.setValue(800000)
    window.food_recover.setValue(2000000)
    window.bandit_interval.setValue(240)
    window.bandit_allow_assist.setChecked(True)
    assert window.save()
    p = Profile.from_dict(json.loads((tmp_path/"active-profile.json").read_text(encoding="utf-8")))
    assert p.teams[1].role == "merit" and p.teams[1].minimum == 77 and p.teams[1].stamina == 53
    assert p.targets[0].x == 640 and p.targets[0].y == 286
    assert p.teams[1].backup_points == ["补兵营帐"]
    assert p.food_low == 800000 and p.food_recover == 2000000
    assert p.bandit_interval == 240
    assert p.bandit_allow_assist is True
    reopened = Window(tmp_path)
    assert reopened.team_table.cellWidget(1,4).value() == 77
    assert reopened.target_table.rowCount() == 1
    assert reopened.bandit_interval.value() == 240
    assert reopened.bandit_allow_assist.isChecked()
    window.close()
    reopened.close()
    app.processEvents()


def test_missing_teams_are_locked_and_guard_rows_hidden(tmp_path):
    # 用户 2026-11-08：实际只有两队时，设置里不能再去配置/启用第 3、4、5 队。
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    window.apply_team_presence([1, 2])
    # 第 1、2 队正常可设置。
    for row in (0, 1):
        assert window.team_table.cellWidget(row, 0).isEnabled()
        assert any(button.isEnabled() for button in window.team_table.cellWidget(row, 2).findChildren(QRadioButton))
    # 第 3 队没有：任务、兵力、兵力下限、详情全部锁住，并标明未拥有。
    assert not window.team_table.cellWidget(2, 0).isEnabled()
    assert all(not button.isEnabled() for button in window.team_table.cellWidget(2, 2).findChildren(QRadioButton))
    assert not window.team_table.cellWidget(2, 3).isEnabled()
    assert not window.team_table.cellWidget(2, 4).isEnabled()
    assert not window.team_table.cellWidget(2, 7).isEnabled()
    assert "未拥有" in window.team_table.item(2, 1).text()
    assert not window.live.cellWidget(2, 0).isEnabled()
    # 亲卫不参与三个功能，设置页不再显示。
    assert window.team_table.isRowHidden(5)
    window.close()
    app.processEvents()


def test_live_action_confirmation_updates_state_without_waiting_for_next_scan(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    obs = Observation("main:1",state="home",strength=33000,maximum=33000,wounded=False,stamina=100,supply=100,auto_refill=True).__dict__
    window.handle_event("connection", {"instance":"0","serial":"test","resolution":"1920×1080"})
    assert window.metrics["游戏连接"].text() == "已连接 1 台"
    assert "编号 0" in window.metrics["游戏连接"].toolTip()
    window.handle_event("observation",obs)
    window.handle_event("decision",{"team":"main:1","reason":"已达到出击条件"})
    window.handle_event("team_action",{"team":"main:1","state":"confirming","reason":"正在核对攻城出发结果"})
    assert window.live.item(0,2).text() == "核对出发"
    assert window.metrics["本次出征"].text() == "0"
    window.handle_event("dispatch",{"team":"main:1","kind":"merit","target":"战场","state":"marching","action":"攻城"})
    assert window.live.item(0,2).text() == "行军"
    assert "已确认攻城" in window.live.item(0,6).text()
    assert window.metrics["本次出征"].text() == "1"
    assert obs["state"] == "home", "UI 不得修改调度使用的旧观测"
    window.close()
    app.processEvents()


def test_unconfirmed_action_is_not_displayed_as_success(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    window.handle_event("team_action",{"team":"main:1","state":"confirming","reason":"核对中"})
    window.handle_event("team_action",{"team":"main:1","state":"unknown","reason":"待核对，已锁定该队"})
    assert window.live.item(0,2).text() == "待识别"
    assert "锁定" in window.live.item(0,6).text()
    assert window.metrics["本次出征"].text() == "0"
    window.close()
    app.processEvents()


def test_unconfirmed_dispatch_is_shown_as_automatic_retry(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    window.handle_event("team_action",{"team":"main:1","state":"retrying",
                                       "reason":"出征未确认受理，下一轮读取现场后自动重试"})
    assert window.live.item(0,2).text() == "未确认 · 自动重试"
    assert "自动重试" in window.live.item(0,6).text()
    assert window.metrics["本次出征"].text() == "0", "未确认不算已出征"
    window.close()
    app.processEvents()


def test_dashboard_column_widths_are_resizable_and_persisted(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    header = window.live.horizontalHeader()
    from PySide6.QtWidgets import QHeaderView
    assert all(header.sectionResizeMode(i) == QHeaderView.ResizeMode.Interactive
               for i in range(window.live.columnCount() - 1)), "运行总览除最后一列外都可拖动调整"
    assert header.stretchLastSection(), "最后一列吃掉剩余宽度"
    window.live.setColumnWidth(1, 240)
    window.live.setColumnWidth(5, 180)
    window.save_widths("live", window.live)
    reopened = Window(tmp_path)
    assert reopened.live.columnWidth(1) == 240
    assert reopened.live.columnWidth(5) == 180
    window.close(); reopened.close()
    app.processEvents()


def test_fresh_wound_read_replaces_action_and_timer_never_claims_recovery(tmp_path,monkeypatch):
    import sanmou_assistant.ui as module
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    window.handle_event("dispatch",{"team":"main:1","kind":"merit","target":"战场","state":"marching","action":"攻城"})
    window.handle_event("decision",{"team":"main:1","reason":"已达到出击条件"})
    monkeypatch.setattr(module.time,"time",lambda:1100.0)
    obs = Observation("main:1",observed_at=1000,state="wounded",strength=33000,maximum=33000,wounded=True,injury_seconds=199).__dict__
    window.handle_event("observation",obs)
    assert window.live.item(0,2).text() == "重伤恢复"
    assert "99 秒" in window.live.item(0,6).text()
    monkeypatch.setattr(module.time,"time",lambda:1300.0)
    window.refresh_live()
    assert window.live.item(0,2).text() == "重伤恢复"
    assert "等待现场复核" in window.live.item(0,6).text()
    window.close()
    app.processEvents()


def test_game_address_paste_for_targets_backups_and_main(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    copied = []
    # 测试不碰系统剪贴板：只验证界面交给 set_clipboard 的内容。
    monkeypatch.setattr("sanmou_assistant.ui.set_clipboard", copied.append)
    window = Window(tmp_path)
    pasted = "甲城(100,100)乙城(200,200)丙城(300,300)丁城(400,400)戊城(500,500)"
    window.target_address.setText(pasted)
    assert window.add_shared_points(window.target_table)
    assert window.target_table.rowCount() == 5
    window.target_table.cellWidget(3,0).setChecked(True)
    window.target_table.cellWidget(3,5)._actions = ["guard"]
    window.target_table.cellWidget(3,5).setText("驻守  ✎")
    assert window.add_shared_points(window.target_table, "丁城(400,400)")
    assert window.target_table.rowCount() == 5
    assert window.target_table.cellWidget(3,0).isChecked()
    assert window.target_table.cellWidget(3,5)._actions == ["guard"]
    window.backup_address.setText("甲城（100，100）")
    window.main_address.setText("丙城(300,300)")
    # 保存也识别尚未点应用的地址；城内农田没有地图地址输入。
    assert window.save()
    profile = Profile.from_dict(json.loads((tmp_path/'active-profile.json').read_text(encoding='utf-8')))
    assert (profile.account,profile.main_city.x,profile.main_city.y) == ("丙城",300,300)
    assert (profile.backups[0].name,profile.backups[0].x,profile.backups[0].y) == ("甲城",100,100)
    assert 'grain_field' not in profile.to_dict()
    assert not hasattr(window,"farm_address")
    assert not hasattr(window,"use_farm_mark")
    assert "无需填写地址" in window.farm_position.text()
    assert not profile.backups[0].enabled
    window.copy_addresses(window.backup_table)
    assert copied == ["甲城(100,100)"]
    window.handle_event('calibrated',{'account':'新主公','main_city':{'name':'我的主城','x':201,'y':202,'expected_name':'新主公'}})
    assert window.collect().main_city.x == 201
    assert window.main_address.text() == "新主公(201,202)"
    window.close()
    app.processEvents()


def test_bad_bulk_address_never_partially_changes_table(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    assert not window.add_shared_points(window.target_table, "甲(1,2)坏(1,)")
    assert window.target_table.rowCount() == 0
    assert window.add_shared_points(window.backup_table, "营帐(1,2)营帐(3,4)")
    assert [window.backup_table.item(r,1).text() for r in range(2)] == ['营帐','营帐 2']
    window.main_address.setText("甲(1,2)乙(3,4)")
    assert not window.apply_main_address()
    assert window.profile.main_city is None
    window.close()
    app.processEvents()


def test_target_action_order_editor_roundtrip_with_march(tmp_path):
    from PySide6.QtCore import Qt
    from sanmou_assistant.ui import ActionOrderDialog, actions_label
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    window.add_target()
    window.target_table.item(0,1).setText("山道")
    window.target_table.cellWidget(0,2).setValue(636)
    window.target_table.cellWidget(0,3).setValue(286)
    b = window.target_table.cellWidget(0,5)
    b._actions = ["march","guard","attack"]
    b.setText(actions_label(b._actions) + "  ✎")
    assert window.save()
    p = Profile.from_dict(json.loads((tmp_path/"active-profile.json").read_text(encoding="utf-8")))
    assert p.targets[0].actions == ["march","guard","attack"]
    reopened = Window(tmp_path)
    assert reopened.target_table.cellWidget(0,5)._actions == ["march","guard","attack"]
    assert "行军" in reopened.target_table.cellWidget(0,5).text()
    # 编辑器：勾选状态与上下移后的顺序即指令优先级
    dialog = ActionOrderDialog(["march","attack"], window)
    keys = [dialog.list.item(r).data(Qt.ItemDataRole.UserRole) for r in range(dialog.list.count())]
    assert keys == ["march","attack","occupy","guard"]
    checked = [dialog.list.item(r).checkState() == Qt.CheckState.Checked for r in range(2)]
    assert checked == [True,True]
    dialog.list.setCurrentRow(0)
    dialog.move(1)
    assert dialog.actions() == ["attack","march"]
    dialog.list.item(1).setCheckState(Qt.CheckState.Unchecked)
    assert dialog.actions() == ["attack"]
    window.close(); reopened.close(); dialog.close()
    app.processEvents()


def test_minimize_emulator_option_defaults_on_and_roundtrips(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    assert window.minimize_box.isChecked() is True
    assert window.save()
    raw = json.loads((tmp_path/"active-profile.json").read_text(encoding="utf-8"))
    assert raw["minimize_emulator"] is True
    window.minimize_box.setChecked(False)
    assert window.save()
    reopened = Window(tmp_path)
    assert reopened.minimize_box.isChecked() is False
    assert reopened.collect().minimize_emulator is False
    # 旧配置没有该字段时按默认“缩小”读取
    raw["minimize_emulator"] = True
    del raw["minimize_emulator"]
    (tmp_path/"active-profile.json").write_text(json.dumps(raw,ensure_ascii=False),encoding="utf-8")
    legacy = Window(tmp_path)
    assert legacy.profile.minimize_emulator is True
    window.close(); reopened.close(); legacy.close()
    app.processEvents()


def test_live_resource_age_and_queued_refresh_preserve_running_worker(tmp_path, monkeypatch):
    import sanmou_assistant.ui as module
    from types import SimpleNamespace
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    monkeypatch.setattr(module.time, 'time', lambda: 1020)
    window.handle_event('grain', 1800000)
    window.handle_event('resource_status', {'visible': True, 'observed_at': 1000})
    assert window.metrics['粮食库存'].text() == '180.0 万'
    assert '20秒前' in window.resource_freshness.text()
    window.handle_event('grain', None)
    window.handle_event('resource_status', {'visible': False, 'observed_at': 1000})
    assert window.metrics['粮食库存'].text() == '180.0 万'
    assert '当前画面不可读' in window.resource_freshness.text()
    requests = []
    window.worker = SimpleNamespace(isRunning=lambda: True, request_refresh=lambda: requests.append(1))
    window.request_refresh()
    assert requests == [1]
    assert '已请求刷新' in window.resource_freshness.text()
    window.worker = None
    window.close(); app.processEvents()


def test_live_table_reuses_cells_and_logs_append_with_filter_and_bound(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    window.refresh_live()
    cell = window.live.item(0, 1)
    window.refresh_live()
    assert window.live.item(0, 1) is cell
    window.log_filter.setText('WARN')
    for i in range(2100):
        window.append_log({'time': '12:00:00', 'level': 'INFO' if i == 0 else 'WARN', 'message': f'记录{i}'})
    assert len(window.log_lines) == 2000
    assert window.log_view.document().blockCount() == 2000
    assert '记录0\n' not in window.log_view.toPlainText()
    window.log_filter.setText('记录2099')
    assert window.log_view.document().blockCount() == 1
    assert '记录2099' in window.log_view.toPlainText()
    window.close(); app.processEvents()


def test_target_order_is_visible_editable_and_persists_without_losing_strategy(tmp_path):
    from sanmou_assistant.models import Point
    from PySide6.QtCore import QItemSelectionModel
    app = QApplication.instance() or QApplication([])
    original = Profile(targets=[Point(name='A',x=101,y=201,priority=3,expected_name='营帐'),
        Point(name='B',x=102,y=202,priority=1,enabled=False),
        Point(name='C',x=103,y=203,priority=2,group='第二组',actions=['march','guard'])])
    (tmp_path/'active-profile.json').write_text(json.dumps(original.to_dict()),encoding='utf-8')
    window = Window(tmp_path)
    t = window.target_table
    names = lambda:[t.item(r,1).text() for r in range(t.rowCount())]
    assert names() == ['B','C','A']
    def select(*rows):
        t.clearSelection()
        for r in rows:
            t.selectionModel().select(t.model().index(r,1),QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
    select(2)
    window.target_order_buttons[0].click()
    assert names() == ['B','A','C']
    window.target_order_buttons[1].click()
    assert names() == ['B','C','A']
    window.target_order_buttons[2].click()
    assert names() == ['A','B','C']
    select(0,1)
    window.target_order_buttons[3].click()
    assert names() == ['C','A','B']
    assert json.loads((tmp_path/'active-profile.json').read_text(encoding='utf-8')) == original.to_dict()
    assert window.save()
    reopened = Window(tmp_path)
    saved = reopened.collect()
    assert [p.name for p in saved.targets] == ['C','A','B']
    assert [p.priority for p in saved.targets] == [1,2,3]
    assert (saved.targets[0].x,saved.targets[0].y,saved.targets[0].group,saved.targets[0].actions) == (103,203,'第二组',['march','guard'])
    assert saved.targets[2].enabled is False
    assert saved.backups == original.backups
    assert all(p.expected_name == '' for p in saved.targets)
    select(1)
    window.remove_row(t)
    assert [t.item(r,6).text() for r in range(t.rowCount())] == ['1','2']
    window.close(); reopened.close(); app.processEvents()


def test_backup_order_persists_and_scheduler_uses_it_with_named_override(tmp_path):
    from sanmou_assistant.models import Point, decide
    app = QApplication.instance() or QApplication([])
    p = Profile(account='测试',main_city=Point(x=10,y=20),backup_enabled=True,
        backups=[Point(name='甲',x=11,y=21,priority=4,expected_name='甲城',capacity=2,actions=['guard'],group='保留'),
                 Point(name='乙',x=12,y=22,priority=1,expected_name='乙城')])
    p.teams[0].role = "merit"
    (tmp_path/'active-profile.json').write_text(json.dumps(p.to_dict()),encoding='utf-8')
    window = Window(tmp_path)
    t = window.backup_table
    assert [t.item(r,1).text() for r in range(2)] == ['乙','甲']
    t.selectRow(1)
    window.backup_order_buttons[2].click()
    assert [t.item(r,1).text() for r in range(2)] == ['甲','乙']
    assert window.save()
    reopened = Window(tmp_path)
    result = reopened.collect()
    assert [b.priority for b in result.backups] == [1,2]
    assert result.backups[0].expected_name == '甲城'
    assert (result.backups[0].actions,result.backups[0].capacity,result.backups[0].group) == (['guard'],2,'保留')
    obs = Observation('main:1',state='home',strength=100,maximum=1000,stamina=100,supply=100,wounded=False,auto_refill=True)
    assert decide(result,result.teams[0],obs,10000000).target.name == '甲'
    result.teams[0].backup_points = ['乙']
    assert decide(result,result.teams[0],obs,10000000).target.name == '乙'
    t.selectRow(0)
    window.remove_row(t)
    assert t.item(0,4).text() == '1'
    window.add_backup()
    assert not t.cellWidget(1,0).isChecked()
    window.close();reopened.close();app.processEvents()


def test_overview_streams_progress_and_clear_keeps_disk_history(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    history = tmp_path / 'events.jsonl'
    history.write_text('retained', encoding='utf-8')
    window.append_log({'time':'12:00:00','level':'WARN','message':'过去的失败'})
    window.handle_event('status', '正在重新读取')
    window.handle_event('status', '正在重新读取')
    window.handle_event('decision', {'team':'main:1','action':'wait','reason':'行军，继续监控'})
    assert len(window.event_history) == 3
    assert window.event_history[-1]['message'].endswith('行军，继续监控')
    assert '正在重新读取' in window.recent_events.item(1).text()
    window.clear_log_button.click()
    assert window.recent_events.count() == 0 and window.log_view.toPlainText() == ''
    assert not window.event_history and not window.log_records
    assert history.read_text(encoding='utf-8') == 'retained'
    for i in range(305):
        window.append_log({'time':'12:00:03','level':'INFO','message':str(i)})
    assert window.recent_events.count() == 300
    assert window.event_history[0]['message'] == '5'
    assert window.event_history[-1]['message'] == '304'
    window.close();app.processEvents()


def test_new_task_clears_old_overview_and_diagnostic_logs(tmp_path, monkeypatch):
    import sanmou_assistant.ui as module
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    monkeypatch.setattr(module.Worker, 'start', lambda self: None)
    window.append_log({'time':'12:00:00','level':'ERROR','message':'旧任务故障'})
    window.start('preview')
    assert window.recent_events.count() == 0
    assert not window.log_records and window.log_view.toPlainText() == ''
    window.close();app.processEvents()


def test_log_severity_and_keyword_filters_compose(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    for level,message in [('INFO','主城读取'),('WARN','主城等待'),('ERROR','设备断连')]:
        window.append_log({'time':'12:00:00','level':level,'message':message})
    window.log_level.setCurrentIndex(window.log_level.findData('attention'))
    assert '主城读取' not in window.log_view.toPlainText()
    assert '主城等待' in window.log_view.toPlainText()
    assert '设备断连' in window.log_view.toPlainText()
    window.log_filter.setText('主城')
    assert window.log_view.document().blockCount() == 1
    assert '主城等待' in window.log_view.toPlainText()
    window.log_level.setCurrentIndex(window.log_level.findData('ERROR'))
    assert window.log_view.toPlainText() == ''
    window.log_filter.clear()
    assert '设备断连' in window.log_view.toPlainText()
    window.log_level.setCurrentIndex(0)
    for i in range(100):
        window.append_log({'time':'12:00:03','level':'INFO','message':f'后续{i}'})
    window.log_follow.setChecked(False)
    window.log_view.verticalScrollBar().setValue(0)
    window.append_log({'time':'12:00:04','level':'WARN','message':'不要滚走'})
    assert window.log_view.verticalScrollBar().value() == 0
    window.close();app.processEvents()



def test_pave_route_dialog_parses_addresses_and_reorders():
    from sanmou_assistant.ui import PaveRouteDialog
    app = QApplication.instance() or QApplication([])
    dialog = PaveRouteDialog([])
    dialog.paste.setText("空地(400,400)空地(300,300)")
    dialog.add_pasted()
    assert [p[:2] for p in dialog.points()] == [[400, 400], [300, 300]]
    assert dialog.points()[0][2] == "空地"
    dialog.paste.setText("200,200; 300,300")
    dialog.add_pasted()
    assert [p[:2] for p in dialog.points()] == [[400, 400], [300, 300], [200, 200]]
    dialog.list.setCurrentRow(2)
    dialog.to_first()
    assert [p[:2] for p in dialog.points()][0] == [200, 200]
    dialog.list.setCurrentRow(0)
    dialog.move(1)
    assert [p[:2] for p in dialog.points()][1] == [200, 200]
    dialog.close()
    app.processEvents()


def test_pave_page_saves_route_points(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    window.add_pave_route()
    t = window.pave_table
    t.item(0, 0).setText("测试路线")
    t.cellWidget(0, 4)._points = [[400, 400, "空地"], [300, 300]]
    window.sync_routes_from_table()
    monkeypatch.setattr("sanmou_assistant.ui.QInputDialog.getItem",
                        lambda *args, **kwargs: ("测试路线", True))
    for button in window.team_table.cellWidget(1, 2).findChildren(QRadioButton):
        if button.property("role") == "pave":
            button.click()
    assert window.profile.teams[1].role == "pave"
    assert window.profile.teams[1].route == "测试路线"
    assert window.save()
    p = Profile.from_dict(json.loads((tmp_path / "active-profile.json").read_text(encoding="utf-8")))
    assert p.pave_routes[0].name == "测试路线"
    assert p.pave_routes[0].team == "main:2"
    assert p.pave_routes[0].points == [[400, 400, "空地"], [300, 300]]
    reopened = Window(tmp_path)
    assert reopened.pave_table.item(0, 0).text() == "测试路线"
    assert reopened.pave_table.cellWidget(0, 4)._points == [[400, 400, "空地"], [300, 300]]
    window.close()
    reopened.close()
    app.processEvents()


def test_pave_page_saves_long_route_without_truncating(tmp_path, monkeypatch):
    """2026-11-10：取消 30 段上限后，118 段路线必须原样保存并在重开后完整显示。"""
    app = QApplication.instance() or QApplication([])
    points = [[100 + i, 200 + i] for i in range(118)]
    window = Window(tmp_path)
    window.add_pave_route()
    t = window.pave_table
    t.item(0, 0).setText("长路线")
    t.cellWidget(0, 4)._points = [list(point) for point in points]
    window.sync_routes_from_table()
    monkeypatch.setattr("sanmou_assistant.ui.QInputDialog.getItem",
                        lambda *args, **kwargs: ("长路线", True))
    for button in window.team_table.cellWidget(1, 2).findChildren(QRadioButton):
        if button.property("role") == "pave":
            button.click()
    assert window.profile.teams[1].route == "长路线"
    assert window.save()
    p = Profile.from_dict(json.loads((tmp_path / "active-profile.json").read_text(encoding="utf-8")))
    assert p.pave_routes[0].points == points
    reopened = Window(tmp_path)
    assert reopened.pave_table.cellWidget(0, 4)._points == points
    assert reopened.pave_table.item(0, 3).text() == "118"
    window.close()
    reopened.close()
    app.processEvents()


def test_unassigned_pave_routes_do_not_block_start(tmp_path):
    """回归：路线页里启用过、但没有队伍选择铺路时，开始执行不能被拦住。"""
    from sanmou_assistant.models import PaveRoute, Point, Profile
    profile = Profile(account="测试", main_city=Point(name="主城", x=600, y=280))
    profile.teams[0].role = "merit"
    profile.teams[1].role = "bandits"
    profile.pave_routes = [PaveRoute(name="铺路 1", points=[[400, 400]], enabled=True),
                           PaveRoute(name="铺路 2", points=[[300, 300]], enabled=True)]
    (tmp_path / "active-profile.json").write_text(json.dumps(profile.to_dict()), encoding="utf-8")
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    collected = window.collect()
    errors = collected.validate(active=True)
    assert not any("铺路" in error for error in errors), errors
    assert window.save()
    window.close()
    app.processEvents()


def test_remove_line_rows_keeps_group_actions_and_renumbers(tmp_path):
    """2026-11-14：拉线表与武勋表同为 target-like，顺序在第 6 列；删除首行/中间行后组、指令、坐标不变，顺序连续。"""
    from sanmou_assistant.models import Point
    app = QApplication.instance() or QApplication([])
    original = Profile(line_targets=[
        Point(name='甲', x=101, y=201, group='第一组', actions=['march','guard'], priority=1),
        Point(name='乙', x=102, y=202, group='第二组', actions=['occupy','march'], priority=2),
        Point(name='丙', x=103, y=203, group='第一组', actions=['attack'], priority=3)])
    (tmp_path/'active-profile.json').write_text(json.dumps(original.to_dict()), encoding='utf-8')
    window = Window(tmp_path)
    t = window.line_table
    t.selectRow(0)
    window.remove_row(t)
    assert [t.item(r,1).text() for r in range(t.rowCount())] == ['乙','丙']
    assert [t.item(r,6).text() for r in range(2)] == ['1','2'], "顺序仍写第 6 列且连续"
    assert [t.item(r,4).text() for r in range(2)] == ['第二组','第一组'], "目标组不被顺序号覆盖"
    assert [t.cellWidget(r,5)._actions for r in range(2)] == [['occupy','march'],['attack']]
    assert [(t.cellWidget(r,2).value(),t.cellWidget(r,3).value()) for r in range(2)] == [(102,202),(103,203)]
    window.add_line_target()
    t.selectRow(1)
    window.remove_row(t)
    assert [t.item(r,1).text() for r in range(t.rowCount())] == ['乙','拉线目标 3']
    assert [t.item(r,6).text() for r in range(2)] == ['1','2']
    assert [t.item(r,4).text() for r in range(2)] == ['第二组','默认']
    assert window.save()
    reloaded = Profile.from_dict(json.loads((tmp_path/'active-profile.json').read_text(encoding='utf-8')))
    assert [(p.name,p.priority,p.group,p.actions) for p in reloaded.line_targets] == [('乙',1,'第二组',['occupy','march']),('拉线目标 3',2,'默认',['occupy','march'])]
    window.close(); app.processEvents()


def test_error_event_jumps_to_log_page_and_keeps_error_record(tmp_path):
    """error 事件必须跳到日志页（共 6 页，索引 5），并把内容作为 ERROR 写入日志而不是丢弃。"""
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    assert window.stack.count() == 6 and window.log_page_index == 5
    window.nav.setCurrentRow(0)
    window.handle_event('error', '设备断连：ADB 失去响应')
    assert window.nav.currentRow() == window.log_page_index
    assert window.stack.currentIndex() == window.log_page_index
    assert any(record['level'] == 'ERROR' and 'ADB 失去响应' in record['message'] for record in window.log_records)
    assert 'ERROR' in window.log_view.toPlainText() and 'ADB 失去响应' in window.log_view.toPlainText()
    window.close(); app.processEvents()


def test_import_config_rejects_invalid_profile_without_overwriting(tmp_path, monkeypatch):
    """2026-11-14：导入配置先 validate；校验失败不覆盖原配置、不替换窗口，并提示原因。"""
    import sanmou_assistant.ui as module
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    window.profile_name.setText('原配置')
    assert window.save()
    before = (tmp_path/'active-profile.json').read_text(encoding='utf-8')
    bad = Profile().to_dict()
    bad['poll_seconds'] = 5  # 合法 JSON、能解析，但校验必须拒绝
    bad_path = tmp_path/'bad.json'
    bad_path.write_text(json.dumps(bad, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(module.QFileDialog, 'getOpenFileName', lambda *a, **k: (str(bad_path), ''))
    warnings = []
    monkeypatch.setattr(module.QMessageBox, 'warning', lambda *a, **k: warnings.append(a))
    replaced = []
    monkeypatch.setattr(Window, 'replace_window', lambda self, p: replaced.append(p))
    window.import_config()
    assert warnings and warnings[0][1] == '导入失败', "无效导入必须提示"
    assert '监控间隔' in warnings[0][2]
    assert not replaced, "校验失败不能替换窗口"
    assert (tmp_path/'active-profile.json').read_text(encoding='utf-8') == before, "校验失败不能覆盖原配置"
    window.close(); app.processEvents()


def test_calibration_only_writes_verified_account_and_main_city(tmp_path):
    """2026-11-14：校准只落盘已核实的账号与主城；表单里未保存的阈值不得夹带写入。"""
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    window.food_low.setValue(800000)
    window.food_recover.setValue(2000000)
    assert window.save()
    window.food_low.setValue(123456)  # 界面上改了但没保存
    window.handle_event('calibrated', {'account':'新主公','main_city':{'name':'我的主城','x':201,'y':202,'expected_name':'新主公'}})
    saved = json.loads((tmp_path/'active-profile.json').read_text(encoding='utf-8'))
    assert (saved['account'], saved['main_city']['x'], saved['main_city']['y']) == ('新主公', 201, 202)
    assert saved['food_low'] == 800000 and saved['food_recover'] == 2000000, "未保存的阈值不能被校准夹带"
    assert window.profile.account == '新主公' and window.profile.food_low == 800000
    assert window.main_address.text() == '新主公(201,202)'
    window.close(); app.processEvents()


def test_calibration_failure_is_logged_and_keeps_saved_file(tmp_path):
    """校准结果未通过校验时不写盘，保留 ERROR 日志，不静默吞错。"""
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    assert window.save()
    before = (tmp_path/'active-profile.json').read_text(encoding='utf-8')
    window.profile.package = 'bad package!'  # 让校验失败
    window.handle_event('calibrated', {'account':'新主公','main_city':{'name':'我的主城','x':201,'y':202,'expected_name':'新主公'}})
    assert (tmp_path/'active-profile.json').read_text(encoding='utf-8') == before, "校验失败不能覆盖已保存配置"
    assert any(record['level'] == 'ERROR' and '未保存' in record['message'] for record in window.log_records)
    assert '未保存' in window.status.text()
    window.close(); app.processEvents()


def test_export_diagnostics_safe_by_default_and_full_requires_confirmation(tmp_path, monkeypatch):
    """2026-11-14：诊断导出默认安全（include_private=False）；完整诊断必须二次确认，取消或拒绝都不导出。"""
    import sanmou_assistant.ui as module
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    calls = []
    monkeypatch.setattr('sanmou_assistant.diagnostics.export_diagnostics',
                        lambda data, destination, include_private=False: calls.append((data, destination, include_private)))
    monkeypatch.setattr(module.QFileDialog, 'getSaveFileName', lambda *a, **k: (str(tmp_path/'diag.zip'), ''))
    opened = []
    monkeypatch.setattr(module.QDesktopServices, 'openUrl', lambda url: opened.append(url) or True)
    monkeypatch.setattr(Window, 'choose_diagnostics_scope', lambda self: 'safe')
    window.export_diagnostics()
    assert [call[2] for call in calls] == [False], "默认安全诊断"
    monkeypatch.setattr(Window, 'choose_diagnostics_scope', lambda self: 'full')
    monkeypatch.setattr(Window, 'confirm_full_diagnostics', lambda self: True)
    window.export_diagnostics()
    assert [call[2] for call in calls] == [False, True], "完整诊断确认后按完整导出"
    monkeypatch.setattr(Window, 'confirm_full_diagnostics', lambda self: False)
    window.export_diagnostics()
    assert [call[2] for call in calls] == [False, True], "完整诊断未再次确认不导出"
    monkeypatch.setattr(Window, 'choose_diagnostics_scope', lambda self: None)
    window.export_diagnostics()
    assert len(calls) == 2, "取消选择不导出"
    window.close(); app.processEvents()


def test_confirm_full_diagnostics_requires_yes(tmp_path, monkeypatch):
    """完整诊断的二次确认只有明确点是才算数，默认按钮为否。"""
    import sanmou_assistant.ui as module
    from PySide6.QtWidgets import QMessageBox
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    monkeypatch.setattr(module.QMessageBox, 'question', lambda *a, **k: QMessageBox.StandardButton.No)
    assert window.confirm_full_diagnostics() is False
    monkeypatch.setattr(module.QMessageBox, 'question', lambda *a, **k: QMessageBox.StandardButton.Yes)
    assert window.confirm_full_diagnostics() is True
    window.close(); app.processEvents()
