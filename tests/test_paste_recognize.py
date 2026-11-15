"""2026-11-15 坐标输入统一：所有坐标入口右侧按钮“粘贴识别并添加”。

统一规则：点击后先读系统剪贴板（经 `sanmou_assistant.ui.clipboard_text`，
测试整体替换、绝不写真实剪贴板）；剪贴板为空或识别不出游戏地址时，
回退用输入框现有文字；两者都无效则不添加并提示。普通 Ctrl+V 只做文本粘贴，
不再触发自动识别添加。

覆盖入口：主城、武勋目标、拉线目标、武勋备用征兵点、拉线按队征兵点、铺路路线。
全部离线运行，不连接模拟器。
"""
import json

import pytest
from PySide6.QtWidgets import QApplication

from sanmou_assistant.models import Profile
from sanmou_assistant.ui import PasteLineEdit, PaveRouteDialog, Window


def set_clipboard(monkeypatch, text):
    monkeypatch.setattr("sanmou_assistant.ui.clipboard_text", lambda: text)


# ---------- 规则：剪贴板优先，回退输入框 ----------

def test_targets_button_uses_clipboard_without_ctrl_v(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    set_clipboard(monkeypatch, "甲城(100,100)乙城(200,200)")
    assert window.add_from_clipboard(window.target_table)
    assert window.target_table.rowCount() == 2
    assert [(window.target_table.cellWidget(r, 2).value(), window.target_table.cellWidget(r, 3).value())
            for r in range(2)] == [(100, 100), (200, 200)]
    window.close()
    app.processEvents()


def test_clipboard_wins_over_editor_text(tmp_path, monkeypatch):
    """剪贴板可识别时忽略输入框内容（剪贴板优先规则）。"""
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    window.target_address.setText("丙城(300,300)")
    set_clipboard(monkeypatch, "甲城(100,100)")
    assert window.add_from_clipboard(window.target_table)
    names = [window.target_table.item(r, 1).text() for r in range(window.target_table.rowCount())]
    assert names == ["甲城"], names
    window.close()
    app.processEvents()


def test_editor_fallback_when_clipboard_not_recognizable(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    set_clipboard(monkeypatch, "不是地址的一串字")
    window.target_address.setText("营帐(120,210)")
    assert window.add_from_clipboard(window.target_table)
    assert window.target_table.rowCount() == 1
    # 输入框文字来自剪贴板分支时会被清空
    assert window.target_address.text() == ""
    window.close()
    app.processEvents()


def test_empty_clipboard_falls_back_to_editor(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    set_clipboard(monkeypatch, "")
    window.target_address.setText("营帐(120,210)")
    assert window.add_from_clipboard(window.target_table)
    assert window.target_table.rowCount() == 1
    window.close()
    app.processEvents()


def test_both_sources_invalid_nothing_added(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    set_clipboard(monkeypatch, "垃圾")
    window.target_address.setText("也是垃圾")
    assert not window.add_from_clipboard(window.target_table)
    assert window.target_table.rowCount() == 0
    # 输入框有内容但无效时，状态栏显示具体解析错误；都为空时提示“没有可识别的游戏地址”。
    assert "地址" in window.status.text()
    window.target_address.clear()
    assert not window.add_from_clipboard(window.target_table)
    assert "没有可识别的游戏地址" in window.status.text()
    window.close()
    app.processEvents()


def test_line_targets_entry_uses_same_button(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    set_clipboard(monkeypatch, "水面(800,800)")
    assert window.add_from_clipboard(window.line_table)
    assert window.line_table.rowCount() == 1
    window.close()
    app.processEvents()


# ---------- 主城 ----------

def test_main_city_button_reads_clipboard(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    set_clipboard(monkeypatch, "测试主公(607,291)")
    assert window.paste_recognize_main()
    assert window.profile.main_city.x == 607 and window.profile.account == "测试主公"
    assert window.main_address.text() == "测试主公(607,291)"
    window.close()
    app.processEvents()


def test_main_city_button_falls_back_to_editor(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    set_clipboard(monkeypatch, "甲(1,2)乙(3,4)")  # 多地址不适用于主城
    window.main_address.setText("主公(100,200)")
    assert window.paste_recognize_main()
    assert (window.profile.main_city.x, window.profile.main_city.y) == (100, 200)
    window.close()
    app.processEvents()


def test_main_city_saved_after_clipboard_apply(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    set_clipboard(monkeypatch, "测试主公(607,291)")
    assert window.paste_recognize_main()
    assert window.save()
    saved = Profile.from_dict(json.loads((tmp_path / "active-profile.json").read_text(encoding="utf-8")))
    assert saved.account == "测试主公" and saved.main_city.x == 607
    window.close()
    app.processEvents()


# ---------- 武勋备用征兵点 / 拉线按队征兵点 ----------

def test_merit_backup_button_reads_clipboard(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    set_clipboard(monkeypatch, "伏波营(592,655)")
    assert window.paste_recognize_merit_backup()
    assert window.merit_backup_address.text() == "伏波营(592,655)"
    assert window.save()
    saved = Profile.from_dict(json.loads((tmp_path / "active-profile.json").read_text(encoding="utf-8")))
    assert (saved.merit_backup.name, saved.merit_backup.x) == ("伏波营", 592)
    window.close()
    app.processEvents()


def test_line_backup_button_clipboard_and_editor_fallback(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path)
    window.set_team_role(1, "line")
    editor = window.line_backup_edits["main:2"]
    set_clipboard(monkeypatch, "营帐甲(300,200)")
    assert window.paste_recognize_line_backup(editor)
    assert window.save()
    saved = Profile.from_dict(json.loads((tmp_path / "active-profile.json").read_text(encoding="utf-8")))
    assert saved.line_backups["main:2"].name == "营帐甲"
    # 剪贴板无效时回退输入框
    set_clipboard(monkeypatch, "无效")
    editor.setText("营帐乙(400,300)")
    assert window.paste_recognize_line_backup(editor)
    assert window.save()
    saved = Profile.from_dict(json.loads((tmp_path / "active-profile.json").read_text(encoding="utf-8")))
    assert saved.line_backups["main:2"].name == "营帐乙"
    window.close()
    app.processEvents()


# ---------- 铺路路线 ----------

def test_pave_dialog_button_reads_clipboard_addresses(monkeypatch):
    app = QApplication.instance() or QApplication([])
    dialog = PaveRouteDialog([])
    set_clipboard(monkeypatch, "空地(400,400)空地(300,300)")
    dialog.add_from_clipboard()
    assert [p[:2] for p in dialog.points()] == [[400, 400], [300, 300]]
    dialog.close()
    app.processEvents()


def test_pave_dialog_button_reads_clipboard_plain_coordinates(monkeypatch):
    app = QApplication.instance() or QApplication([])
    dialog = PaveRouteDialog([])
    set_clipboard(monkeypatch, "200,200; 300,300")
    dialog.add_from_clipboard()
    assert [p[:2] for p in dialog.points()] == [[200, 200], [300, 300]]
    dialog.close()
    app.processEvents()


def test_pave_dialog_button_falls_back_to_editor(monkeypatch):
    app = QApplication.instance() or QApplication([])
    dialog = PaveRouteDialog([])
    # 无效剪贴板内容会弹提示框，offscreen 下替换成记录调用，避免模态阻塞。
    warnings = []
    monkeypatch.setattr("sanmou_assistant.ui.QMessageBox.warning",
                        lambda *a, **k: warnings.append(a))
    set_clipboard(monkeypatch, "无效内容")
    dialog.paste.setText("空地(400,400)")
    dialog.add_from_clipboard()
    assert [p[:2] for p in dialog.points()] == [[400, 400]]
    dialog.close()
    app.processEvents()


# ---------- Ctrl+V 不再自动识别 ----------

def test_paste_line_edit_has_no_auto_recognize(tmp_path, monkeypatch):
    """Ctrl+V 只走 QLineEdit 原生文本粘贴：无 pasted 信号、无 keyPressEvent 覆写。"""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QLineEdit
    app = QApplication.instance() or QApplication([])
    assert "pasted" not in PasteLineEdit.__dict__
    assert "keyPressEvent" not in PasteLineEdit.__dict__
    assert "contextMenuEvent" not in PasteLineEdit.__dict__
    window = Window(tmp_path)
    calls = []
    monkeypatch.setattr(window, "add_from_clipboard", lambda *a, **k: calls.append(1) or True)
    # 用空剪贴板桩避免读取真实剪贴板
    monkeypatch.setattr(QApplication, "clipboard", lambda: type("C", (), {"text": lambda self: ""})())
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    window.target_address.keyPressEvent(event)
    app.processEvents()
    assert calls == [], "Ctrl+V 不得触发识别添加"
    assert isinstance(window.target_address, QLineEdit)
    window.close()
    app.processEvents()
