from __future__ import annotations

import copy
import json
import os
import re
import time
import math
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import cv2
from PySide6.QtCore import Qt, QTimer, Signal, QItemSelectionModel, QByteArray
from PySide6.QtGui import QImage, QPixmap, QFont, QDesktopServices, QColor, QBrush, QKeySequence
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QLabel, QPushButton, QCheckBox, QComboBox, QSpinBox, QLineEdit, QFormLayout, QVBoxLayout, QHBoxLayout, QGridLayout, QStackedWidget, QListWidget, QListWidgetItem, QDialog, QDialogButtonBox, QTableWidget, QTableWidgetItem, QHeaderView, QFrame, QFileDialog, QMessageBox, QPlainTextEdit, QScrollArea, QSplitter, QAbstractItemView, QMenu, QTabWidget, QRadioButton, QInputDialog)

from . import __version__
from .models import (Profile, Point, TeamPolicy, PaveRoute, line_target, pave_label, pave_summary,
                     point_label, team_label, STATES, ACTION_NAMES, atomic_json, data_directory)
from .team_states import display_label
from .locations import SharedAddress, parse_addresses, single_address
from .worker import Worker


STYLE = """
QWidget { font-family: 'Microsoft YaHei UI','Microsoft YaHei'; font-size: 13px; color:#27374d; }
QMainWindow, QWidget#content { background:#f3f6fb; }
QFrame#sidebar { background:#17253e; border:0; }
QLabel#brand { color:#ffffff; font-size:24px; font-weight:700; }
QLabel#brandSub { color:#91a4c1; font-size:11px; }
QListWidget#nav { background:transparent; border:0; outline:0; color:#b7c5db; font-size:14px; }
QListWidget#nav::item { border-radius:7px; padding:13px 15px; margin:3px 5px; }
QListWidget#nav::item:selected { background:#2c4b7d; color:#ffffff; }
QListWidget#nav::item:hover { background:#243752; }
QLabel#heading { font-size:25px; font-weight:700; color:#17253e; }
QLabel#subheading { color:#718097; }
QFrame#card { background:white; border:1px solid #e4eaf3; border-radius:9px; }
QLabel#cardValue { font-size:24px; font-weight:700; color:#20497e; }
QLabel#muted { color:#7d8ca3; font-size:12px; }
QLabel#notice { background:#eaf1fc; color:#41638e; border-radius:6px; padding:10px; }
QPushButton { background:white; border:1px solid #d5dfec; border-radius:6px; padding:8px 13px; }
QPushButton:hover { background:#eaf1fc; border-color:#a9bfdc; }
QPushButton:disabled { color:#aab4c3; background:#f1f4f8; }
QPushButton#primary { background:#3066d4; color:white; border:1px solid #3066d4; font-weight:600; }
QPushButton#primary:hover { background:#2455b8; }
QPushButton#danger { color:#b84242; }
QLineEdit, QSpinBox, QComboBox { background:white; border:1px solid #d5dfec; border-radius:5px; padding:6px; min-height:21px; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color:#477dd6; }
QCheckBox { spacing:8px; }
QCheckBox::indicator { width:16px; height:16px; }
QTableWidget { background:white; border:1px solid #e4eaf3; border-radius:6px; gridline-color:#eff2f7; selection-background-color:#eaf1fc; selection-color:#27374d; }
QHeaderView::section { background:#edf2f9; border:0; border-bottom:1px solid #e4eaf3; padding:9px; color:#62748e; }
QTableWidget::item { padding:7px; }
QPlainTextEdit { background:#18263c; color:#cfdbec; border:0; border-radius:7px; font-family:Consolas,'Microsoft YaHei'; }
QScrollArea { border:0; background:transparent; }
QScrollBar:vertical { background:#f0f3f8; width:9px; }
QScrollBar::handle:vertical { background:#c5d0df; min-height:30px; border-radius:4px; }
"""

# 全部页面共用视觉规则；每个账号的交互和读取流程沿用各自独立的 Worker。
STYLE += """
QWidget { color:#263449; font-size:13px; }
QMainWindow, QWidget#content { background:#f5f7fb; }
QFrame#sidebar { background:#142238; }
QLabel#brand { font-size:23px; letter-spacing:2px; }
QListWidget#nav::item { padding:14px 12px; margin:3px 0; border-radius:8px; }
QListWidget#nav::item:selected { background:#2a4f80; font-weight:600; }
QLabel#heading { font-size:22px; color:#16263d; }
QLabel#subheading { color:#77859b; font-size:12px; }
QLabel#sectionTitle { font-size:17px; font-weight:700; color:#17253e; padding-top:8px; }
QFrame#topBar { background:white; border:1px solid #e6ebf2; border-radius:10px; }
QLabel#currentStatus { color:#53657e; font-size:12px; }
QFrame#card { border:1px solid #e5eaf2; border-radius:10px; }
QLabel#cardValue { font-size:22px; color:#183b65; }
QLabel#notice { background:#ecf2fc; color:#526c91; font-size:12px; padding:10px 12px; border-radius:7px; }
QPushButton { padding:8px 14px; border-radius:7px; min-height:18px; }
QPushButton#primary { background:#3167d5; }
QLineEdit, QSpinBox, QComboBox { padding:6px 9px; border-radius:6px; min-height:22px; selection-background-color:#dce9ff; }
QTableWidget { border:1px solid #e4eaf2; border-radius:8px; alternate-background-color:#f8fafd; outline:0; }
QTableWidget::item { border-bottom:1px solid #eef2f7; padding:8px; }
QTableWidget::item:selected { background:#e6efff; color:#194e9e; }
QTableWidget QSpinBox, QTableWidget QComboBox { border:0; background:transparent; padding:4px 7px; }
QTableWidget QSpinBox:focus, QTableWidget QComboBox:focus { background:white; border:1px solid #7da5ec; }
QHeaderView::section { background:#f0f4fa; color:#6c7c92; font-size:12px; padding:11px 8px; border-bottom:1px solid #e5eaf2; }
QPlainTextEdit { background:white; color:#465871; border:1px solid #e5eaf2; padding:8px; font-family:'Microsoft YaHei UI'; }
QPlainTextEdit#diagnosticLog { background:#15243a; color:#d1deef; border:0; font-family:Consolas,'Microsoft YaHei'; }
QListWidget#recentEvents { border:0; background:transparent; outline:0; font-size:12px; }
QListWidget#recentEvents::item { padding:3px 0; border-bottom:1px solid #edf1f7; }
QScrollArea > QWidget > QWidget { background:transparent; }
QFrame#metric { background:white; border:1px solid #e5eaf2; border-radius:8px; }
QLabel#metricCap { color:#7d8ca3; font-size:11px; }
QLabel#metricValue { color:#20497e; font-size:15px; font-weight:700; }
QTabWidget::pane { border:1px solid #e5eaf2; border-radius:8px; background:white; top:-1px; }
QTabBar::tab { background:transparent; padding:8px 18px; color:#6c7c92; border:0; }
QTabBar::tab:selected { color:#1c3f74; font-weight:600; border-bottom:2px solid #3167d5; }
QTabBar::tab:hover { color:#3167d5; }
QPushButton#disclosure { background:transparent; border:0; color:#5b7ba8; text-align:left; padding:2px 0; font-size:12px; }
QPushButton#disclosure:hover { color:#3167d5; }
QLabel#disclosureBody { color:#74839a; font-size:12px; padding:7px 10px; background:#f6f8fc; border-radius:6px; }
QPushButton#detailButton { text-align:left; background:transparent; border:0; color:#31547f; padding:4px 8px; }
QPushButton#detailButton:hover { background:#eaf1fc; border-radius:5px; }
QPushButton#linkButton { background:transparent; border:0; color:#5b7ba8; padding:2px 0; text-align:left; font-size:12px; }
QPushButton#linkButton::menu-indicator { image:none; width:0; }
QToolTip { background:#20314d; color:white; padding:7px; border:0; }
"""


def set_clipboard(text: str) -> None:
    """写系统剪贴板的唯一入口；测试替换它，避免跑测试时改掉使用者的剪贴板。"""
    QApplication.clipboard().setText(text)


def button(text, callback, primary=False):
    b = QPushButton(text)
    if primary:
        b.setObjectName("primary")
    b.clicked.connect(callback)
    return b


def spin(value, low=0, high=10000000, suffix=""):
    s = QSpinBox()
    s.setRange(low, high)
    s.setValue(value)
    s.setSuffix(suffix)
    s.setGroupSeparatorShown(True)
    s.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
    return s


def combo(items, current=None):
    c = QComboBox()
    for label, value in items:
        c.addItem(label, value)
    if current is not None:
        c.setCurrentIndex(max(0, c.findData(current)))
    return c


def table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().hide()
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    t.setAlternatingRowColors(True)
    t.setShowGrid(False)
    t.verticalHeader().setDefaultSectionSize(44)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setMinimumHeight(240)
    return t


def item(t, row, column, value, editable=True):
    i = t.item(row, column)
    if i is None:
        i = QTableWidgetItem(str(value))
        if not editable:
            i.setFlags(i.flags() & ~Qt.ItemFlag.ItemIsEditable)
        t.setItem(row, column, i)
    elif i.text() != str(value):
        i.setText(str(value))
    if i.toolTip() != str(value):
        i.setToolTip(str(value))


def cell_check(t, row, column, checked):
    c = QCheckBox()
    c.setChecked(checked)
    c.setStyleSheet("padding-left:12px;")
    t.setCellWidget(row, column, c)
    return c


def actions_label(actions):
    return " → ".join(ACTION_NAMES[a] for a in actions) if actions else "（未选择指令）"


class ActionOrderDialog(QDialog):
    """单个目的地的指令编辑器：勾选可用指令并用上下移排列优先级。

    列表顺序即执行顺序：越靠上越先尝试；现场菜单没有该指令时依次往后推。
    """

    def __init__(self, actions, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑指令顺序")
        layout = QVBoxLayout(self)
        tip = QLabel("勾选这个目的地允许的指令，上面的先执行。\n现场没有该指令时自动试下一个；全部不可用就换下一个优先级目的地。")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        body = QHBoxLayout()
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        ordered = list(actions) + [a for a in ACTION_NAMES if a not in actions]
        for key in ordered:
            entry = QListWidgetItem(ACTION_NAMES[key])
            entry.setData(Qt.ItemDataRole.UserRole, key)
            entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            entry.setCheckState(Qt.CheckState.Checked if key in actions else Qt.CheckState.Unchecked)
            self.list.addItem(entry)
        body.addWidget(self.list, 1)
        side = QVBoxLayout()
        up = QPushButton("上移")
        down = QPushButton("下移")
        up.clicked.connect(lambda: self.move(-1))
        down.clicked.connect(lambda: self.move(1))
        side.addWidget(up)
        side.addWidget(down)
        side.addStretch()
        body.addLayout(side)
        layout.addLayout(body, 1)
        buttons = dialog_buttons()
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def move(self, delta):
        row = self.list.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < self.list.count():
            return
        entry = self.list.takeItem(row)
        self.list.insertItem(target, entry)
        self.list.setCurrentRow(target)

    def actions(self):
        result = []
        for row in range(self.list.count()):
            entry = self.list.item(row)
            if entry.checkState() == Qt.CheckState.Checked:
                result.append(entry.data(Qt.ItemDataRole.UserRole))
        return result


class PasteLineEdit(QLineEdit):
    """粘贴后立即触发识别，省掉再点一次“识别并添加”。"""

    pasted = Signal()

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Paste):
            super().keyPressEvent(event)
            self.pasted.emit()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        menu = self.createStandardContextMenu()
        menu.addSeparator()
        action = menu.addAction("粘贴并识别")
        action.triggered.connect(self.paste_now)
        menu.exec(event.globalPos())

    def paste_now(self):
        self.paste()
        self.pasted.emit()


class PaveRouteDialog(QDialog):
    """铺路路线编辑器：粘贴游戏地址即可生成有序坐标，和地点优先级一样可上下调整。"""

    def __init__(self, points, parent=None, options=None):
        super().__init__(parent)
        self.setWindowTitle("编辑铺路路线")
        self.resize(620, 540)
        layout = QVBoxLayout(self)
        tip = QLabel("列表从上到下就是铺路顺序，脚本到达一个点并停留后才派下一段。\n"
                     "可直接粘贴游戏地址：空地(200,200)空地(300,300)；或写坐标：200,200; 300,300。")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        options = options or {}
        self.list = QListWidget()
        for point in points:
            self.add_entry(point)
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.paste = PasteLineEdit()
        self.paste.setPlaceholderText("粘贴游戏地址或坐标，例如 空地(200,200)空地(300,300)")
        # 铺路路线不再限制段数，粘贴框也别先卡住（10000 字符只够五百多段）。
        self.paste.setMaxLength(200000)
        row.addWidget(self.paste, 1)
        row.addWidget(button("识别并添加", self.add_pasted))
        self.paste.returnPressed.connect(self.add_pasted)
        self.paste.pasted.connect(lambda: QTimer.singleShot(0, self.add_pasted))
        layout.addLayout(row)
        ops = QHBoxLayout()
        for label, callback in (("↑ 上移", lambda: self.move(-1)),
                                ("↓ 下移", lambda: self.move(1)),
                                ("置顶", self.to_first),
                                ("置底", self.to_last),
                                ("删除选中", self.remove_selected),
                                ("清空", self.list.clear)):
            ops.addWidget(button(label, callback))
        layout.addLayout(ops)
        buttons = dialog_buttons()
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def add_entry(self, point):
        name = point[2] if len(point) > 2 and isinstance(point[2], str) else ""
        entry = QListWidgetItem(pave_label([int(point[0]), int(point[1]), name]))
        entry.setData(Qt.ItemDataRole.UserRole, [int(point[0]), int(point[1]), name])
        self.list.addItem(entry)

    def options(self):
        return {}

    def add_pasted(self):
        text = self.paste.text().strip()
        if not text:
            return
        parsed = []
        try:
            parsed = [[a.x, a.y, a.name] for a in parse_addresses(text)]
        except ValueError:
            for part in re.split(r"[;；\n]", text):
                part = part.strip()
                if not part:
                    continue
                match = re.fullmatch(r"\(?\s*(\d{1,4})\s*[,，、]\s*(\d{1,4})\s*\)?", part)
                if not match:
                    QMessageBox.warning(self, "地址无法识别", f"这一段不是游戏地址或坐标：{part}")
                    return
                parsed.append([int(match[1]), int(match[2]), ""])
            if not parsed:
                QMessageBox.warning(self, "地址无法识别", "没有识别到坐标")
                return
        existing = {tuple(self.list.item(i).data(Qt.ItemDataRole.UserRole)[:2]) for i in range(self.list.count())}
        added = 0
        for point in parsed:
            key = (point[0], point[1])
            if key in existing:
                continue
            self.add_entry(point)
            existing.add(key)
            added += 1
        self.paste.clear()
        if not added:
            QMessageBox.information(self, "没有新增", "这些坐标已经在路线里了")

    def move(self, delta):
        row = self.list.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < self.list.count():
            return
        entry = self.list.takeItem(row)
        self.list.insertItem(target, entry)
        self.list.setCurrentRow(target)

    def to_first(self):
        row = self.list.currentRow()
        if row > 0:
            self.list.insertItem(0, self.list.takeItem(row))
            self.list.setCurrentRow(0)

    def to_last(self):
        row = self.list.currentRow()
        if 0 <= row < self.list.count() - 1:
            entry = self.list.takeItem(row)
            self.list.addItem(entry)
            self.list.setCurrentRow(self.list.count() - 1)

    def remove_selected(self):
        for row in sorted({i.row() for i in self.list.selectedIndexes()}, reverse=True):
            self.list.takeItem(row)

    def points(self):
        return [list(self.list.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.list.count())]


class TeamDetailDialog(QDialog):
    """单队任务细节：按任务显示目标组、补兵点/营帐顺序。

    - 拉线：目标组取自「目标与铺路 → 拉线目标」；补兵点就是回城后调动的营帐，
      补不动时回城 → 按这里的顺序调动过去；到营帐后够线就拉、不够就自然征兵。
    - 刷武勋/讨贼：目标组取自武勋目标，补兵点按顺序尝试。
    """

    def __init__(self, team, profile, parent=None):
        super().__init__(parent)
        self.team = team
        self.line_role = team.role == "line"
        self.setWindowTitle(f"{team.display} · 任务细节")
        self.resize(560, 460)
        layout = QVBoxLayout(self)
        tip = QLabel(
            "拉线队使用「目标与铺路 → 拉线目标」里的坐标：本队目标组内从上到下取第一个启用的目标。"
            "补不动时回城，再按下面的补兵点顺序调动到营帐；到营帐后够线就拉、不够就自然征兵。"
            if self.line_role else
            "刷武勋与讨贼也使用目标组；“备用征兵地点”按顺序尝试，留空表示使用全部已启用的地点。"
            "拉线队同样使用这里的补兵点（回城后调动的营帐）。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        form = QFormLayout()
        self.group = QComboBox()
        self.group.setEditable(True)
        source = profile.line_targets if self.line_role else profile.targets
        groups = ["默认"] + sorted({p.group for p in source if p.group and p.group != "默认"})
        self.group.addItems(groups)
        self.group.setCurrentText(team.target_group or "默认")
        form.addRow("拉线目标组" if self.line_role else "任务目标组", self.group)
        self.fallback = QComboBox()
        self.fallback.addItem("回城后去补兵点（营帐）补兵", "recall")
        self.fallback.addItem("停手等下一轮", "stop")
        self.fallback.setCurrentIndex(1 if team.refill_fallback == "stop" else 0)
        if self.line_role:
            form.addRow("拉线补不动时", self.fallback)
        layout.addLayout(form)

        layout.addWidget(QLabel("补兵点 / 营帐（从上到下）" if self.line_role
                                else "备用征兵地点（从上到下，刷武勋/讨贼/拉线回城后使用）"))
        self.points = QListWidget()
        for name in team.backup_points:
            self.points.addItem(QListWidgetItem(name))
        layout.addWidget(self.points, 1)

        row = QHBoxLayout()
        self.choice = QComboBox()
        self.choice.addItem("选择补兵点…", "")
        for p in profile.backups:
            self.choice.addItem(p.name, p.name)
        row.addWidget(self.choice, 1)
        row.addWidget(button("添加", self.add_point))
        row.addWidget(button("删除选中", self.remove_point))
        row.addWidget(button("↑", lambda: self.move_point(-1)))
        row.addWidget(button("↓", lambda: self.move_point(1)))
        layout.addLayout(row)
        note = QLabel("补兵点在地点列表里增删后，这里的同名条目会随之失效；留空表示使用全部已启用地点。")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = dialog_buttons()
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def add_point(self):
        name = self.choice.currentData()
        if not name:
            return
        if any(self.points.item(i).text() == name for i in range(self.points.count())):
            return
        self.points.addItem(QListWidgetItem(name))

    def remove_point(self):
        for row in sorted({i.row() for i in self.points.selectedIndexes()}, reverse=True):
            self.points.takeItem(row)

    def move_point(self, delta):
        row = self.points.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < self.points.count():
            return
        item_ = self.points.takeItem(row)
        self.points.insertItem(target, item_)
        self.points.setCurrentRow(target)

    def values(self):
        points = [self.points.item(i).text() for i in range(self.points.count())]
        return (self.group.currentText().strip() or "默认", points,
                self.fallback.currentData())


def dialog_buttons():
    box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    box.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
    box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
    return box


def card(title, description=""):
    f = QFrame()
    f.setObjectName("card")
    layout = QVBoxLayout(f)
    layout.setContentsMargins(18, 16, 18, 18)
    h = QLabel(title)
    h.setStyleSheet("font-size:14px; font-weight:600; color:#223b5a;")
    layout.addWidget(h)
    if description:
        d = QLabel(description)
        d.setObjectName("muted")
        d.setWordWrap(True)
        layout.addWidget(d)
    return f, layout


class Preview(QLabel):
    marked = Signal(int, int)

    def __init__(self):
        super().__init__("连接游戏后显示实时画面")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background:#e6ecf5; color:#8192ab; border-radius:7px;")
        self.setMinimumSize(320, 180)
        self.source = None

    def display(self, image):
        if isinstance(image, QPixmap):
            self.source = image
        else:
            q = QImage(image.data, image.shape[1], image.shape[0], image.strides[0], QImage.Format.Format_BGR888).copy()
            self.source = QPixmap.fromImage(q)
        if self.isVisible():
            self.rescale()

    def showEvent(self, event):
        super().showEvent(event)
        self.rescale()

    def rescale(self):
        if self.source:
            self.setPixmap(self.source.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.rescale()

    def mousePressEvent(self, event):
        if self.source and self.pixmap():
            pix = self.pixmap()
            left, top = (self.width()-pix.width())/2, (self.height()-pix.height())/2
            x, y = event.position().x()-left, event.position().y()-top
            if 0 <= x < pix.width() and 0 <= y < pix.height():
                self.marked.emit(round(x/pix.width()*1920), round(y/pix.height()*1080))


class Window(QMainWindow):
    def __init__(self, data_dir: Path | None = None, account=None, manager=None):
        super().__init__()
        self.data_dir = data_dir or data_directory()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.account = account
        self.account_id = account.id if account else ""
        self.manager = manager
        self.config_path = account.profile_path if account else self.data_dir / "active-profile.json"
        self.session_root = account.sessions_path if account else self.data_dir / "sessions"
        self.profile = Profile()
        self.worker = None
        self.observations = {}
        self.decisions = {}
        self.team_activity = {}
        self.pave_progress = {}
        # 最近一次检测到的实际主队槽位；None 表示本次任务还没读过。
        self.team_slots = None
        self.latest_evidence = ""
        self.latest_mark = None
        self.paused = False
        self.close_requested = False
        config_error = ""
        if account:
            try:
                self.profile = account.load_profile()
            except (ValueError, OSError) as exc:
                config_error = f"账号配置无法读取，已保留原文件：{exc}"
        elif self.config_path.exists():
            try:
                self.profile = Profile.from_dict(json.loads(self.config_path.read_text(encoding="utf-8")))
            except (ValueError, OSError) as exc:
                config_error = f"原配置无法读取，已保留原文件：{exc}"
        self.setWindowTitle(f"三谋助手  {__version__}")
        # 按当前屏幕可用区域决定初始尺寸：小屏笔记本（1366×768、125%/150% 缩放）也能完整显示。
        available = self.screen().availableGeometry() if self.screen() else None
        avail_w = available.width() if available else 1440
        avail_h = available.height() if available else 940
        width = min(1680, max(900, avail_w - 56))
        height = min(1040, max(560, avail_h - 72))
        self.resize(width, height)
        self.setMinimumSize(min(900, avail_w), min(520, avail_h))
        self.setStyleSheet(STYLE)
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setCentralWidget(root)
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(190)
        navlayout = QVBoxLayout(side)
        navlayout.setContentsMargins(18, 30, 18, 20)
        brand = QLabel("三谋助手")
        brand.setObjectName("brand")
        navlayout.addWidget(brand)
        sub = QLabel("部队与日常 · 桌面控制台")
        sub.setObjectName("brandSub")
        navlayout.addWidget(sub)
        navlayout.addSpacing(30)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.addItems(["01   运行总览", "02   部队任务", "03   目标与铺路", "04   补兵与日常", "05   连接与配置", "06   日志与诊断"])
        self.log_page_index = 5
        navlayout.addWidget(self.nav)
        foot = QLabel(f"v{__version__}  Windows x64\n每个账号独立控制一个实例")
        foot.setObjectName("brandSub")
        navlayout.addWidget(foot)
        layout.addWidget(side)
        content = QWidget()
        content.setObjectName("content")
        body = QVBoxLayout(content)
        body.setContentsMargins(24, 20, 24, 16)
        topbar = QFrame()
        topbar.setObjectName("topBar")
        topbody = QVBoxLayout(topbar)
        topbody.setContentsMargins(16,10,16,8)
        topbody.setSpacing(5)
        top = QHBoxLayout()
        caption = QLabel("当前配置")
        caption.setObjectName("muted")
        top.addWidget(caption)
        self.profile_name = QLineEdit(self.profile.name)
        self.profile_name.setMinimumWidth(200)
        self.profile_name.setMaximumWidth(330)
        top.addWidget(self.profile_name)
        top.addStretch()
        self.run_state = QLabel("● 待启动")
        self.run_state.setStyleSheet("color:#79869a; font-weight:600;")
        top.addWidget(self.run_state)
        self.minimize_box = QCheckBox("缩小模拟器窗口")
        self.minimize_box.setChecked(self.profile.minimize_emulator)
        self.minimize_box.setToolTip("勾选后启动时把模拟器窗口压到最小化，截图与点击全部走后台输入；取消勾选则保持窗口现状")
        top.addWidget(self.minimize_box)
        self.start_button = button("开始执行", self.toggle_start, True)
        self.monitor_button = button("只监控", self.monitor)
        self.monitor_button.setToolTip("连接并导航读取全部队伍；不派兵、不撤军、不领取奖励")
        top.addWidget(self.start_button)
        top.addWidget(self.monitor_button)
        topbody.addLayout(top)
        self.status = QLabel("当前进度：尚未连接，选择开始执行或只监控")
        self.status.setObjectName("currentStatus")
        self.status.setWordWrap(True)
        topbody.addWidget(self.status)
        body.addWidget(topbar)
        body.addSpacing(6)
        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)
        self.make_dashboard()
        self.make_teams()
        self.make_targets_pave()
        self.make_supply_daily()
        self.make_connection()
        self.make_logs()
        bottom = QHBoxLayout()
        self.saved_label = QLabel("设置保存到本机，可导出后迁移")
        self.saved_label.setObjectName("muted")
        bottom.addWidget(self.saved_label, 1)
        self.save_button = button("保存设置", self.save, True)
        bottom.addWidget(self.save_button)
        body.addLayout(bottom)
        layout.addWidget(content, 1)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)
        self.fill_tables()
        self.live_timer = QTimer(self)
        self.live_timer.setInterval(1000)
        self.live_timer.timeout.connect(self.refresh_live)
        self.live_timer.start()
        if self.profile.main_city:
            self.metrics["账号与主城"].setText(f"{self.profile.account} ({self.profile.main_city.x},{self.profile.main_city.y})")
        if config_error:
            self.status.setText(config_error)
            self.append_log({"time":datetime.now().strftime("%H:%M:%S"),"level":"ERROR","message":config_error})

    def page(self, title, description, scroll=True):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(13)
        h = QLabel(title)
        h.setObjectName("heading")
        layout.addWidget(h)
        d = QLabel(description)
        d.setObjectName("subheading")
        d.setWordWrap(True)
        layout.addWidget(d)
        self.stack.addWidget(page)
        if scroll:
            return self.scroll_settings(layout)
        return layout

    def page_section(self, layout, title, description=""):
        h = QLabel(title)
        h.setObjectName("sectionTitle")
        layout.addWidget(h)
        if description:
            d = QLabel(description)
            d.setObjectName("muted")
            d.setWordWrap(True)
            layout.addWidget(d)

    def collapsible(self, layout, title, text):
        """渐进式披露：默认只显示一行摘要，点开才看规则细节。"""
        holder = QWidget()
        inner = QVBoxLayout(holder)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(2)
        button = QPushButton(f"▸ {title}")
        button.setObjectName("disclosure")
        button.setCheckable(True)
        body = QLabel(text)
        body.setObjectName("disclosureBody")
        body.setWordWrap(True)
        body.setVisible(False)
        button.toggled.connect(lambda checked: (body.setVisible(checked),
                                                button.setText(("▾ " if checked else "▸ ") + title)))
        inner.addWidget(button)
        inner.addWidget(body)
        layout.addWidget(holder)
        return body

    def make_dashboard(self):
        layout = self.page("运行总览", "读取游戏真实状态，显示每一队的下一步和等待原因。停止后，游戏中的任务继续进行。", scroll=False)
        layout.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.metrics = {}
        for name, value in (("游戏连接", "未连接"), ("粮食库存", "待识别"), ("账号与主城", "未校验"), ("本次出征", "0"), ("本次武勋", "待读取"), ("本次铺路", "—")):
            chip = QFrame()
            chip.setObjectName("metric")
            inner = QHBoxLayout(chip)
            inner.setContentsMargins(12, 5, 12, 5)
            inner.setSpacing(8)
            cap = QLabel(name)
            cap.setObjectName("metricCap")
            inner.addWidget(cap)
            label = QLabel(value)
            label.setObjectName("metricValue")
            label.setToolTip(value)
            self.metrics[name] = label
            inner.addWidget(label)
            if name == "粮食库存":
                self.resource_time = None
                self.resource_visible = False
                self.resource_refresh_pending = False
                self.resource_freshness = QLabel("尚未读取")
                self.resource_freshness.setObjectName("metricCap")
                inner.addWidget(self.resource_freshness)
            inner.addStretch()
            row.addWidget(chip)
        layout.addLayout(row)
        controls = QHBoxLayout()
        self.scan_button = button("读取全部队伍", lambda:self.start("scan"))
        self.scan_button.setToolTip("读取队伍并确认本次游戏里实际有几队；没有的槽位随后会在设置里锁住")
        controls.addWidget(self.scan_button)
        self.refresh_button = button("刷新画面与资源", self.request_refresh)
        controls.addWidget(self.refresh_button)
        controls.addWidget(button("已派出队伍回城", lambda:self.start("return")))
        controls.addStretch()
        self.last_scan = QLabel("尚无本次观测")
        self.last_scan.setObjectName("muted")
        controls.addWidget(self.last_scan)
        layout.addLayout(controls)
        self.live = table(["显示", "队伍 / 阵容", "状态", "兵力", "体力 / 补给", "预备兵池", "下一步 / 等待原因"])
        self.live.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.live.horizontalHeader()
        # 除最后一列外都可以拖动；最后一列自动吃掉剩余宽度，窗口放大时不再大片留白。
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setMinimumSectionSize(44)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.live.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.live_columns = (52, 175, 105, 130, 120, 165, 375)
        saved = self.saved_widths("live", self.live_columns)
        for i, w in enumerate(saved or self.live_columns):
            self.live.setColumnWidth(i, w)
        self.live_fit_pending = False
        self.width_save_timer = QTimer(self)
        self.width_save_timer.setSingleShot(True)
        self.width_save_timer.setInterval(400)
        self.width_save_timer.timeout.connect(lambda: self.save_widths("live", self.live))
        header.sectionResized.connect(lambda *a: self.width_save_timer.start())
        self.live.setMinimumHeight(140)
        self.live.verticalHeader().setDefaultSectionSize(34)

        # 上下分区可拖动：队伍表、画面、日志按窗口大小自由分配，不再用固定高度留白。
        split = QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(False)
        split.addWidget(self.live)

        lower = QSplitter(Qt.Orientation.Horizontal)
        lower.setChildrenCollapsible(False)
        f, inner = card("游戏画面")
        f.setToolTip("最近一次截图；窗口最小化也可更新")
        self.preview = Preview()
        self.preview.setMinimumSize(140, 70)
        inner.addWidget(self.preview, 1)
        lower.addWidget(f)
        f, inner = card("实时日志")
        f.setToolTip("本次任务的最新进度；重新启动任务时清空显示")
        self.recent_events = QListWidget()
        self.recent_events.setObjectName("recentEvents")
        self.recent_events.setWordWrap(True)
        self.recent_events.setMinimumHeight(70)
        self.event_history = []
        inner.addWidget(self.recent_events, 1)
        log_actions = QHBoxLayout()
        self.overview_follow = QCheckBox("跟随最新")
        self.overview_follow.setChecked(True)
        self.overview_follow.toggled.connect(lambda checked: self.recent_events.scrollToBottom() if checked else None)
        log_actions.addWidget(self.overview_follow)
        self.clear_log_button = button("清空显示", self.clear_logs)
        self.clear_log_button.setToolTip("清空界面日志，磁盘运行记录保留供排查")
        log_actions.addWidget(self.clear_log_button)
        log_actions.addWidget(button("完整日志 →", lambda: self.nav.setCurrentRow(self.log_page_index)))
        inner.addLayout(log_actions)
        lower.addWidget(f)
        lower.setStretchFactor(0, 1)
        lower.setStretchFactor(1, 1)
        split.addWidget(lower)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        layout.addWidget(split, 1)
        self.dashboard_split = split
        self.dashboard_lower = lower
        self.restore_splitter("dashboard", split, [430, 210])
        self.restore_splitter("dashboard-lower", lower, [560, 460])

        # 隐藏的队伍不参与任何任务，也就不出现在表格里；保留一个恢复入口，避免找不到它们。
        self.hidden_hint = QPushButton("")
        self.hidden_hint.setObjectName("linkButton")
        self.hidden_hint.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hidden_hint.setVisible(False)
        layout.addWidget(self.hidden_hint)

    def ui_state_path(self):
        return self.data_dir / "ui-state.json"

    def saved_widths(self, name, defaults):
        """读取上次保存的列宽；缺失、损坏或列数变化时返回 None，由调用方回退默认值。"""
        try:
            state = json.loads(self.ui_state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        widths = state.get(name) if isinstance(state, dict) else None
        if not isinstance(widths, list) or len(widths) != len(defaults):
            return None
        try:
            return [max(40, int(w)) for w in widths]
        except (TypeError, ValueError):
            return None

    def fit_live_columns(self):
        """首次打开时把剩余宽度补给最后一列，避免右侧留白；用户拖过以后不再自动改动。"""
        if not self.live_fit_pending:
            return
        self.live_fit_pending = False
        viewport = self.live.viewport().width()
        total = sum(self.live.columnWidth(i) for i in range(self.live.columnCount()))
        if viewport > total + 40:
            last = self.live.columnCount() - 1
            self.live.setColumnWidth(last, self.live.columnWidth(last) + viewport - total)

    def save_widths(self, name, widget):
        """拖动结束后防抖保存，不因为每一像素拖动都写盘。"""
        widths = [widget.columnWidth(i) for i in range(widget.columnCount())]
        try:
            state = json.loads(self.ui_state_path().read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        state[name] = widths
        try:
            atomic_json(self.ui_state_path(), state)
        except OSError as exc:
            self.append_log({"time":datetime.now().strftime("%H:%M:%S"),"level":"WARN",
                             "message":f"列宽未能保存：{exc}"})

    def load_ui_state(self):
        try:
            state = json.loads(self.ui_state_path().read_text(encoding="utf-8"))
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def restore_splitter(self, key, splitter, defaults):
        """恢复上次拖动过的分区比例；没有记录时按窗口大小给默认值。"""
        value = self.load_ui_state().get("split-" + key)
        if isinstance(value, str):
            try:
                if splitter.restoreState(QByteArray.fromBase64(value.encode("ascii"))):
                    splitter.splitterMoved.connect(lambda *_: self.schedule_splitter_save())
                    return
            except (ValueError, TypeError):
                pass
        splitter.setSizes(defaults)
        splitter.splitterMoved.connect(lambda *_: self.schedule_splitter_save())

    def schedule_splitter_save(self):
        if not hasattr(self, "split_save_timer"):
            self.split_save_timer = QTimer(self)
            self.split_save_timer.setSingleShot(True)
            self.split_save_timer.setInterval(500)
            self.split_save_timer.timeout.connect(self.persist_splitters)
        self.split_save_timer.start()

    def persist_splitters(self):
        state = self.load_ui_state()
        for key, splitter in (("dashboard", getattr(self, "dashboard_split", None)),
                              ("dashboard-lower", getattr(self, "dashboard_lower", None))):
            if splitter is not None:
                state["split-" + key] = bytes(splitter.saveState().toBase64()).decode("ascii")
        try:
            atomic_json(self.ui_state_path(), state)
        except OSError:
            pass

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_live()
        self.update_hidden_hint()

    def make_teams(self):
        layout = self.page("部队任务", "每支队伍只做一件事：刷武勋、讨贼或铺路。第一次先点“读取全部队伍”确认本次实际有几队；没有的槽位会自动锁住，保留配置但不读取、不派兵。", scroll=False)
        self.team_table = table(["显示", "队伍", "任务", "兵力方式", "出击下限", "体力", "补给", "目标组 / 补兵点", "快速征兵", "回城自动补兵"])
        header = self.team_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setMinimumSectionSize(44)
        self.team_table.verticalHeader().setDefaultSectionSize(46)
        for i, w in enumerate((52, 100, 235, 105, 100, 62, 62, 88, 78, 108)):
            self.team_table.setColumnWidth(i, w)
        f, inner = card("队伍安排", "点“详情”配置目标组和补兵点顺序；不用的队伍可在运行总览里隐藏")
        inner.addWidget(self.team_table, 1)
        layout.addWidget(f, 1)
        hint = QLabel("选“铺路”时会让你挑一条已建好的路线；路线坐标仍在“目标与铺路 → 铺路路线”里编辑。"
                      "灰色行是已隐藏的队伍，可在运行总览底部恢复。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.collapsible(layout, "亲卫与派兵核对",
                         "亲卫自动出征尚未开放，亲卫队暂不参与任务。百分比按当前最大兵力向上取整，"
                         "例如 33000 × 90% = 29700。每次派兵核对槽位、兵力和重伤；"
                         "未知字段显示待识别，不按满兵处理。")

    def team_detail_summary(self, t):
        if t.role == "pave":
            return f"路线：{t.route or '未选择'}"
        if t.role == "line":
            # 拉线只认「目标与铺路 → 拉线目标」里本组第一个启用的点；补兵点是回城后调动的营帐。
            target = line_target(self.profile, t)
            state = f"目标 {point_label(target)}" if target else "还没有启用的拉线目标"
            backups = f"补兵点 {len(t.backup_points)} 个" if t.backup_points else "补兵点：自动"
            return f"组：{t.target_group} · {state} · {backups}"
        parts = [f"组：{t.target_group}"]
        parts.append(f"补兵点 {len(t.backup_points)} 个" if t.backup_points else "补兵点：自动")
        return " · ".join(parts)

    def role_cell(self, t, r):
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(8, 0, 6, 0)
        row.setSpacing(10)
        if t.group != "main":
            label = QLabel("暂不参与")
            label.setObjectName("muted")
            row.addWidget(label)
            row.addStretch()
            return holder
        for role, text in (("merit", "刷武勋"), ("bandits", "讨贼"), ("pave", "铺路"), ("line", "拉线")):
            button = QRadioButton(text)
            button.setProperty("role", role)
            button.clicked.connect(lambda checked=False, row=r, role=role: self.team_role_clicked(row, role))
            row.addWidget(button)
        row.addStretch()
        return holder

    def team_available(self, t):
        """这支队伍本次是否真的存在，能不能在界面里设置/使用。

        启动确认前（team_slots 还没读到）不拦，避免第一次打开什么都不能配；
        读到部队清单后，没有的主城槽位和亲卫行都锁住。
        """
        if t.group == "guard":
            return False
        if self.team_slots is None:
            return True
        return t.slot in self.team_slots

    def sync_team_row(self, r):
        t = self.profile.teams[r]
        available = self.team_available(t)
        editable = available and t.role != "hidden"
        box = self.team_table.cellWidget(r, 0)
        box.blockSignals(True)
        box.setChecked(t.role != "hidden")
        box.setEnabled(available)
        box.blockSignals(False)
        holder = self.team_table.cellWidget(r, 2)
        for button in holder.findChildren(QRadioButton):
            role = button.property("role")
            button.blockSignals(True)
            button.setChecked(t.role == role)
            button.setEnabled(editable)
            button.blockSignals(False)
        detail = self.team_table.cellWidget(r, 7)
        detail.setText(self.team_detail_summary(t) if available else "未拥有")
        detail.setEnabled(editable)
        for c in (3, 4, 5, 6):
            widget = self.team_table.cellWidget(r, c)
            if widget:
                widget.setEnabled(editable)
        refill = self.team_table.cellWidget(r, 8)
        if isinstance(refill, QCheckBox):
            refill.blockSignals(True)
            refill.setChecked(bool(t.refill_enabled))
            refill.setEnabled(editable and t.role == "line")
            refill.blockSignals(False)
        auto = self.team_table.cellWidget(r, 9)
        if isinstance(auto, QComboBox):
            auto.blockSignals(True)
            if t.auto_refill_pending is not None:
                auto.setCurrentIndex(1 if t.auto_refill_pending else 2)
                auto.setToolTip("待写入：下次访问该队详情页时写回游戏")
            elif t.auto_refill is None:
                auto.setCurrentIndex(0)
                auto.setToolTip("未知：还没读过该队详情页的“回城自动补兵”")
            else:
                auto.setCurrentIndex(1 if t.auto_refill else 2)
                auto.setToolTip(f"游戏内当前{'已开' if t.auto_refill else '未开'}（读到真值，外部改动以游戏为准）")
            auto.setEnabled(editable)
            auto.blockSignals(False)
        name = t.display if available else f"{t.display}（未拥有）"
        cell = self.team_table.item(r, 1)
        if cell:
            cell.setText(name)
            cell.setToolTip("本次部队列表里没有这支队伍，不能设置或使用" if not available else "")
        for c in range(self.team_table.columnCount()):
            cell = self.team_table.item(r, c)
            if cell:
                cell.setForeground(QBrush(QColor("#9aa7b8" if (t.role == "hidden" or not available) else "#27374d")))

    def apply_team_presence(self, slots):
        """把本次实际队伍应用到设置页：没有的槽位锁住，不让人配置/启用。"""
        self.team_slots = set(slots or [])
        for r, t in enumerate(self.profile.teams):
            if t.group == "main":
                self.sync_team_row(r)
        self.refresh_live()

    def choose_pave_route(self, r):
        t = self.profile.teams[r]
        names = [route.name for route in self.profile.pave_routes if route.name.strip() and route.points]
        if not names:
            QMessageBox.information(self, "还没有可用的铺路路线",
                                    "先在“目标与铺路 → 铺路路线”里创建路线并编辑至少一个坐标，再回来选择。")
            return None
        labels, entries = [], []
        for name in names:
            owner = next((other for other in self.profile.teams
                          if other is not t and other.role == "pave" and other.route == name), None)
            labels.append(f"{name}（已由 {owner.display} 使用）" if owner else name)
            entries.append((name, owner))
        current = labels.index(t.route) if t.route in names else 0
        choice, ok = QInputDialog.getItem(self, "选择铺路路线", "本队执行哪条路线？", labels, current, False)
        if not ok:
            return None
        name, owner = entries[labels.index(choice)]
        if owner is not None:
            answer = QMessageBox.question(self, "路线已被占用",
                                          f"{owner.display} 也在执行这条路线，改由本队执行吗？")
            if answer != QMessageBox.StandardButton.Yes:
                return None
            for index, other in enumerate(self.profile.teams):
                if other is owner:
                    other.role, other.route, other.saved_role = "idle", "", ""
                    self.sync_team_row(index)
        return name

    def team_role_clicked(self, r, role):
        t = self.profile.teams[r]
        if t.role == "hidden" or not self.team_available(t):
            return
        if role == "pave":
            name = self.choose_pave_route(r)
            if name is None:
                self.sync_team_row(r)
                return
            t.route = name
            self.set_team_role(r, "pave")
            self.refresh_pave_table()
        else:
            self.set_team_role(r, role)

    def set_team_role(self, r, role):
        t = self.profile.teams[r]
        if role != t.role:
            if role == "hidden":
                t.saved_role = t.role if t.role != "hidden" else t.saved_role
            else:
                t.saved_role = ""
            t.role = role
            if role != "pave":
                t.route = ""
            self.saved_label.setText("队伍任务已修改 · 待保存")
        self.sync_team_row(r)
        if role != "pave":
            self.refresh_pave_table()

    def set_visibility_enabled(self, enabled):
        for row in range(self.live.rowCount()):
            box = self.live.cellWidget(row, 0)
            if isinstance(box, QCheckBox) and row < len(self.profile.teams):
                box.setEnabled(enabled and self.team_available(self.profile.teams[row]))

    def visible_toggled(self, r, checked):
        t = self.profile.teams[r]
        if not self.team_available(t):
            self.sync_team_row(r)
            return
        if checked:
            self.set_team_role(r, t.saved_role or "idle")
        else:
            self.set_team_role(r, "hidden")
        self.refresh_live()
        self.update_hidden_hint()

    def team_refill_toggled(self, r, checked):
        self.profile.teams[r].refill_enabled = bool(checked)
        self.saved_label.setText("快速征兵已修改 · 待保存")

    def team_auto_refill_changed(self, r):
        widget = self.team_table.cellWidget(r, 9)
        value = widget.currentData() if isinstance(widget, QComboBox) else None
        self.profile.teams[r].auto_refill_pending = value
        self.saved_label.setText("回城自动补兵已标记待写入 · 下次访问该队详情页时写回游戏")

    def edit_team_detail(self, r):
        t = self.profile.teams[r]
        if not self.team_available(t):
            return
        dialog = TeamDetailDialog(t, self.profile, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        t.target_group, t.backup_points, t.refill_fallback = dialog.values()
        self.sync_team_row(r)
        self.saved_label.setText("队伍细节已修改 · 待保存")

    def update_hidden_hint(self):
        hidden = [t for t in self.profile.teams if t.role == "hidden"]
        self.hidden_hint.setVisible(bool(hidden))
        if not hidden:
            return
        self.hidden_hint.setText(f"已隐藏 {len(hidden)} 队 · 点此恢复")
        menu = QMenu(self.hidden_hint)
        for t in hidden:
            action = menu.addAction(f"恢复 {t.display}")
            action.triggered.connect(lambda _=False, team=t: self.restore_team(team.key))
        self.hidden_hint.setMenu(menu)

    def restore_team(self, key):
        for r, t in enumerate(self.profile.teams):
            if t.key == key:
                self.set_team_role(r, t.saved_role or "idle")
        self.refresh_live()
        self.update_hidden_hint()

    def make_targets(self, layout=None, section=True):
        if layout is None:
            layout = self.page("战场目标", "以坐标为准，名称仅作备注。按列表从上到下尝试同组已启用目标；建筑变化不影响定位。")
        elif section:
            self.page_section(layout, "武勋目标", "按坐标识别，名称只作备注；同一组内从上到下尝试已启用的目标。")
        self.target_table = table(["启用", "名称备注", "X", "Y", "目标组", "指令顺序（点击编辑）", "尝试顺序"])
        self.target_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self.target_table.horizontalHeader()
        header.setStretchLastSection(False)
        for column, width in ((1,150), (2,90), (3,90), (4,90)):
            header.setSectionResizeMode(column,QHeaderView.ResizeMode.Interactive)
            self.target_table.setColumnWidth(column,width)
        header.setSectionResizeMode(5,QHeaderView.ResizeMode.Stretch)
        self.target_address = self.shared_address_row(layout, self.target_table)
        layout.addWidget(self.target_table, 1)
        self.target_order_buttons = self.point_order_row(layout,self.target_table)
        row = QHBoxLayout()
        row.addWidget(button("添加目标", self.add_target))
        row.addWidget(button("删除选中目标", lambda:self.remove_row(self.target_table)))
        row.addWidget(button("复制游戏地址", lambda:self.copy_addresses(self.target_table)))
        row.addStretch()
        layout.addLayout(row)
        self.collapsible(layout, "顺序与坐标核对",
                         "列表上方优先；同组内先尝试第一个启用目标，指令全部不可用才换下一个目标。"
                         "可按 Ctrl / Shift 多选后一并移动。坐标必须与游戏详情一致；"
                         "营帐变空地、箭塔或易主后，仍只尝试你允许的指令。新目标默认关闭。")

    def make_line_targets(self, layout=None, section=True):
        """拉线目标：与武勋目标完全同一套表格和粘贴解析，只认坐标、名称仅作备注。"""
        if layout is None:
            layout = self.page("拉线目标", "按坐标识别，名称只作备注；拉线队取自己目标组里第一个启用的目标。")
        elif section:
            self.page_section(layout, "拉线目标", "和武勋目标一样按坐标与目标组管理；拉线队在上面的“部队任务”里选目标组。")
        self.line_table = table(["启用", "名称备注", "X", "Y", "目标组", "指令顺序（点击编辑）", "尝试顺序"])
        self.line_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self.line_table.horizontalHeader()
        header.setStretchLastSection(False)
        for column, width in ((1,150), (2,90), (3,90), (4,90)):
            header.setSectionResizeMode(column,QHeaderView.ResizeMode.Interactive)
            self.line_table.setColumnWidth(column,width)
        header.setSectionResizeMode(5,QHeaderView.ResizeMode.Stretch)
        self.line_address = self.shared_address_row(layout, self.line_table)
        layout.addWidget(self.line_table, 1)
        self.line_order_buttons = self.point_order_row(layout,self.line_table)
        row = QHBoxLayout()
        row.addWidget(button("添加拉线目标", self.add_line_target))
        row.addWidget(button("删除选中目标", lambda:self.remove_row(self.line_table)))
        row.addWidget(button("复制游戏地址", lambda:self.copy_addresses(self.line_table)))
        row.addStretch()
        layout.addLayout(row)
        self.collapsible(layout, "拉线目标怎么用",
                         "直接粘贴游戏地址，例如 水面(100,100)，点“识别并添加”后勾选启用；"
                         "同一目标组内从上到下取第一个启用目标。拉线只认坐标，名称仅作备注；"
                         "“指令顺序”和刷武勋目标一样由你排列（攻城/攻占/驻守/行军），脚本按这个顺序尝试。"
                         "哪支队拉线在“部队任务”里选。")

    def add_line_target(self):
        names = {self.line_table.item(r,1).text() for r in range(self.line_table.rowCount())}
        number = self.line_table.rowCount()+1
        while f"拉线目标 {number}" in names:
            number += 1
        self.insert_point(self.line_table, Point(name=f"拉线目标 {number}", enabled=False,
                                                actions=["occupy", "march"]))

    def is_target_like(self, t):
        """武勋目标与拉线目标共用同一套表格结构（启用/名称/X/Y/组/指令/顺序）。"""
        return t is self.target_table or t is getattr(self, "line_table", None)

    def make_pave(self, layout=None, section=True):
        if layout is None:
            layout = self.page("铺路路线", "按坐标顺序逐段攻占；到点停留后继续下一段。目标不相邻时游戏会自动铺路，脚本只负责核对与提交。")
        elif section:
            self.page_section(layout, "铺路路线", "先在这里建好路线，再到“部队任务”把路线分配给一支主城队伍。")
        f, inner = card("铺路路线", "这里建路线、编辑坐标；由哪支队执行，在“部队任务”里选")
        self.pave_table = table(["路线名称", "执行队伍", "路线（按顺序）", "段数", "编辑"])
        self.pave_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self.pave_table.horizontalHeader()
        header.setStretchLastSection(False)
        for column, width in ((0, 170), (1, 120), (3, 55), (4, 120)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self.pave_table.setColumnWidth(column, width)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.pave_table.setMinimumHeight(230)
        inner.addWidget(self.pave_table)
        row = QHBoxLayout()
        row.addWidget(button("添加路线", self.add_pave_route))
        row.addWidget(button("删除选中路线", self.remove_pave_row))
        row.addWidget(button("重置铺路进度", lambda: self.start("pave-reset")))
        row.addStretch()
        inner.addLayout(row)
        self.collapsible(inner, "路线规则与恢复",
                         "点“编辑路线”可直接粘贴游戏地址或坐标，再上移/下移调整顺序。"
                         "已完成段数会保存；队伍停在任意位置都会恢复当前段继续铺。"
                         "到点判定只看角标状态（停留），途中遇敌或战斗会暂停推进、等现场恢复。")
        layout.addWidget(f)

    def add_pave_route(self):
        names = {self.pave_table.item(r, 0).text() for r in range(self.pave_table.rowCount())
                 if self.pave_table.item(r, 0)}
        number = self.pave_table.rowCount() + 1
        while f"铺路 {number}" in names:
            number += 1
        self.insert_pave_route(PaveRoute(name=f"铺路 {number}", points=[]))
        self.sync_routes_from_table()
        self.saved_label.setText("已添加铺路路线 · 到“部队任务”里选给一支队伍后才会执行")

    def insert_pave_route(self, route):
        t = self.pave_table
        r = t.rowCount()
        t.insertRow(r)
        t.setRowHeight(r, 44)
        owner = next((team for team in self.profile.teams if team.role == "pave" and team.route == route.name), None)
        item(t, r, 0, route.name)
        item(t, r, 1, owner.display if owner else "未分配", False)
        item(t, r, 2, pave_summary(route.points) if route.points else "（尚未添加坐标）", False)
        item(t, r, 3, str(len(route.points)), False)
        editor = QPushButton("编辑路线 ✎")
        editor._points = [list(p) for p in route.points]
        editor.setToolTip("粘贴游戏地址、调整顺序、删除坐标点")
        editor.clicked.connect(lambda _, b=editor: self.edit_pave_route(b))
        t.setCellWidget(r, 4, editor)

    def sync_routes_from_table(self):
        """把路线表写回配置；归属仍由队伍任务决定，路线自身只保存执行队伍。"""
        old = {route.name: route for route in self.profile.pave_routes}
        routes = []
        for r in range(self.pave_table.rowCount()):
            route = self.read_pave_route(r)
            if route.name in old:
                route.resume_after_return = old[route.name].resume_after_return
                route.replenish = old[route.name].replenish
            routes.append(route)
        self.profile.pave_routes = routes
        self.profile.normalize()
        names = {route.name for route in routes}
        cleared = [team for team in self.profile.teams
                   if team.role == "pave" and team.route and team.route not in names]
        for team in cleared:
            team.role, team.route = "idle", ""
        if cleared:
            self.saved_label.setText("路线已删除或改名，相关队伍已改为空闲 · 待保存")

    def refresh_pave_table(self):
        self.sync_routes_from_table()
        self.pave_table.setRowCount(0)
        for route in self.profile.pave_routes:
            self.insert_pave_route(route)

    def edit_pave_route(self, b):
        dialog = PaveRouteDialog(b._points, self, getattr(b, "_options", {}))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        points = dialog.points()
        if not points:
            QMessageBox.warning(self, "路线为空", "铺路路线至少需要一个坐标")
            return
        b._points = points
        b._options = dialog.options()
        for r in range(self.pave_table.rowCount()):
            if self.pave_table.cellWidget(r, 4) is b:
                item(self.pave_table, r, 2, pave_summary(points), False)
                item(self.pave_table, r, 3, str(len(points)), False)
                break
        self.sync_routes_from_table()
        self.saved_label.setText("铺路路线已修改 · 待保存")

    def remove_pave_row(self):
        t = self.pave_table
        if not self.can_apply_addresses():
            return
        for r in sorted({i.row() for i in t.selectedIndexes()}, reverse=True):
            t.removeRow(r)
        self.sync_routes_from_table()
        self.refresh_pave_table()

    def read_pave_route(self, r):
        t = self.pave_table
        name = (t.item(r, 0).text() if t.item(r, 0) else "").strip()
        editor = t.cellWidget(r, 4)
        points = [list(p) for p in (editor._points if editor else [])]
        return PaveRoute(name=name, points=points, **getattr(editor, "_options", {}))

    def make_recruitment(self, layout=None, section=True):
        if layout is None:
            layout = self.page("征兵与补粮", "主城自动补兵不足时使用备用地点。不会把预计主城预备兵加进城外兵力提前撤回。")
        elif section:
            self.page_section(layout, "补兵与粮食", "主城补不满时去备用点征满再回城；粮食低于触发值才屯田。")
        f, inner = card("备用征兵地点", "同队未指定备用点时，按列表从上到下尝试已启用地点")
        self.backup_enabled = QCheckBox("启用城外备用补兵：普通征满 → 回主城")
        self.backup_enabled.setChecked(self.profile.backup_enabled)
        inner.addWidget(self.backup_enabled)
        self.backup_table = table(["启用", "名称", "X", "Y", "尝试顺序", "补兵点名称校验"])
        self.backup_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self.backup_table.horizontalHeader()
        header.setStretchLastSection(False)
        for column,width in ((1,160),(2,90),(3,90),(4,90)):
            header.setSectionResizeMode(column,QHeaderView.ResizeMode.Interactive)
            self.backup_table.setColumnWidth(column,width)
        header.setSectionResizeMode(5,QHeaderView.ResizeMode.Stretch)
        self.backup_address = self.shared_address_row(inner, self.backup_table)
        self.backup_table.setMinimumHeight(174)
        inner.addWidget(self.backup_table)
        self.backup_order_buttons = self.point_order_row(inner,self.backup_table)
        row = QHBoxLayout()
        row.addWidget(button("添加城池 / 营帐", self.add_backup))
        row.addWidget(button("删除选中地点", lambda:self.remove_row(self.backup_table)))
        row.addWidget(button("复制游戏地址", lambda:self.copy_addresses(self.backup_table)))
        inner.addLayout(row)
        self.collapsible(inner, "备用补兵规则",
                         "队伍在“部队任务”里指定了补兵点顺序时优先遵守；没有指定就按这里的顺序尝试。"
                         "补兵点仍会校验实际名称和调动菜单，确保能进驻征兵。")
        layout.addWidget(f)
        f, inner = card("游戏隐藏显示", "开始任务前确保游戏内隐藏已开启（小眼睛带斜杠），全程保持。")
        self.hide_before_run = QCheckBox("开始任务前打开游戏隐藏显示（小眼睛斜杠）")
        self.hide_before_run.setChecked(self.profile.hide_before_run)
        inner.addWidget(self.hide_before_run)
        self.collapsible(inner, "为什么要打开隐藏显示",
                         "地图上没有部队模型、名字和建筑名，点坐标不会误选到别的队伍，也更快。任务结束不恢复。")
        layout.addWidget(f)
        f, inner = card("低粮补给", "低于触发库存开始屯田，回到恢复库存才恢复出击。")
        self.food_enabled = QCheckBox("粮食不足时自动屯田补粮")
        self.food_enabled.setChecked(self.profile.food_enabled)
        inner.addWidget(self.food_enabled)
        form = QFormLayout()
        self.food_low = spin(self.profile.food_low,0,100000000)
        self.food_recover = spin(self.profile.food_recover,1,100000000)
        self.food_budget = spin(self.profile.farming_budget,0,30," 次")
        form.addRow("触发库存",self.food_low)
        form.addRow("恢复库存",self.food_recover)
        form.addRow("本周期屯田上限",self.food_budget)
        inner.addLayout(form)
        self.farm_position = QLabel("屯田由脚本从城务进入城内自动定位，无需填写地址或标定农田位置。")
        self.farm_position.setWordWrap(True)
        self.farm_position.setObjectName("muted")
        inner.addWidget(self.farm_position)
        self.collapsible(inner, "屯田怎么核对",
                         "在领土→开垦列表自动查找同一行当前农田类型和主城内链接，"
                         "定位后核对粮食收益和实际次数，次数不足时暂停新屯田。")
        layout.addWidget(f)
        self.rates = QPlainTextEdit()
        self.rates.setReadOnly(True)
        self.rates.setFixedHeight(90)
        self.rates.setPlainText("征兵池与快慢速按游戏实读；预计时间不能替代真实兵力。")
        f, inner = card("主城征兵速度", "只读当前兵池信息，不修改加速或补兵开关")
        inner.addWidget(self.rates)
        inner.addWidget(button("读取征兵速度", lambda:self.start("rates")))
        layout.addWidget(f)

    def make_dailies(self, layout=None, section=True):
        if layout is None:
            layout = self.page("签到与日常", "按账号记录每日完成状态；签到、月卡和寻访当天完成后，重启也不重复执行。")
        elif section:
            self.page_section(layout, "每日任务", "当天完成过的任务不会重复执行；讨贼按队伍任务自动参与。")
        self.daily_checks = {}
        self.daily_results = {}
        f, inner = card("日常清单", "勾选需要执行的任务，下方显示任务说明或本次结果")
        grid = QGridLayout()
        entries = [("monthly","月卡奖励","主动寻找月卡入口，领取今日俸禄；不购买或续费"), ("signin","签到有礼","28天循环日卡及下方已达标的累计进度奖励"), ("events","其他限时活动签到","只处理已识别的免费签到按钮"), ("drill","军演挂机奖励","监控征战右上角宝箱；出现才领取，一天可领多次，不自动挑战") , ("alliance_rewards","同盟奖励宝箱","监控同盟右上角宝箱；出现才领取，不按零点或7时猜测"), ("visit","每日免费寻访","自动勾选快速寻访，每天免费一次；完成后不重复"), ("half_price","追加每日半价寻访","需同时开启免费寻访；每天追加半价一次，按页面实际价格消耗黄金"), ("bandits","讨贼集结","按游戏实际奖励余数参加；次数归零后停止新讨贼，下一周期重新读取"), ("career","职业技能","入口与消耗待校验，当前不执行")]
        for i,(key,label,tip) in enumerate(entries):
            c = QCheckBox(label)
            c.setChecked(self.profile.dailies.get(key,False))
            c.setToolTip(tip)
            self.daily_checks[key] = c
            result = QLabel(tip)
            result.setObjectName("muted")
            result.setWordWrap(True)
            self.daily_results[key] = result
            grid.addWidget(c,i,0)
            grid.addWidget(result,i,1)
            c.setMinimumHeight(37)
        grid.setColumnStretch(1,1)
        grid.setHorizontalSpacing(28)
        grid.setVerticalSpacing(8)
        inner.addLayout(grid)
        timing = QFormLayout()
        self.bandit_interval = spin(self.profile.bandit_interval,30,3600," 秒")
        timing.addRow("讨贼检查间隔（查完回沙盘）",self.bandit_interval)
        self.bandit_allow_assist = QCheckBox("奖励次数用完后继续协助讨贼（无次数奖励）")
        self.bandit_allow_assist.setChecked(self.profile.bandit_allow_assist)
        self.bandit_allow_assist.setToolTip("默认关闭。开启后仍只用勾选的讨贼队伍，不自动新建集结；次数不明时不提交。")
        timing.addRow(self.bandit_allow_assist)
        inner.addLayout(timing)
        layout.addWidget(f)
        row = QHBoxLayout()
        row.addWidget(button("检查 / 执行日常", lambda:self.start("dailies"),True))
        row.addStretch()
        layout.addLayout(row)
        layout.addStretch()

    def tab_layout(self, scroll=False):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        if not scroll:
            return page, layout
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(page)
        return area, layout

    def make_targets_pave(self):
        layout = self.page("目标与铺路", "武勋目标与拉线目标都按坐标和目标组选择；铺路路线按顺序逐段推进，到点停留后再派下一段。", scroll=False)
        tabs = QTabWidget()
        page, inner = self.tab_layout()
        self.make_targets(inner, section=False)
        tabs.addTab(page, "武勋目标")
        page, inner = self.tab_layout()
        self.make_line_targets(inner, section=False)
        tabs.addTab(page, "拉线目标")
        page, inner = self.tab_layout()
        self.make_pave(inner, section=False)
        tabs.addTab(page, "铺路路线")
        layout.addWidget(tabs, 1)

    def make_supply_daily(self):
        layout = self.page("补兵与日常", "备用补兵、低粮屯田和每日固定任务。所有操作先核对现场，不替游戏改设置。", scroll=False)
        tabs = QTabWidget()
        page, inner = self.tab_layout(scroll=True)
        self.make_recruitment(inner, section=False)
        tabs.addTab(page, "补兵与粮食")
        page, inner = self.tab_layout(scroll=True)
        self.make_dailies(inner, section=False)
        tabs.addTab(page, "每日任务")
        layout.addWidget(tabs, 1)

    def make_connection(self):
        layout = self.page("连接与配置", "切换电脑或账号时重新发现实例、校验主城。队伍与目标配置可以导入导出。")
        f, inner = card("MuMu 连接")
        form = QFormLayout()
        self.cli_path = QLineEdit(self.profile.cli_path)
        clirow = QHBoxLayout()
        clirow.addWidget(self.cli_path,1)
        clirow.addWidget(button("选择文件",self.choose_cli))
        form.addRow("mumu-cli.exe",clirow)
        self.instance = QComboBox()
        self.instance.addItem(f"MuMu · 编号 {self.profile.instance}",self.profile.instance)
        form.addRow("目标实例",self.instance)
        self.package = QLineEdit(self.profile.package)
        form.addRow("游戏包名",self.package)
        self.poll = spin(self.profile.poll_seconds,15,3600," 秒")
        self.retry = spin(self.profile.retry_seconds,30,7200," 秒")
        form.addRow("每轮结束后的监控间隔",self.poll)
        form.addRow("同队重试冷却",self.retry)
        inner.addLayout(form)
        row = QHBoxLayout()
        row.addWidget(button("自动发现实例",lambda:self.start("discover")))
        row.addWidget(button("发现游戏包名",lambda:self.start("packages")))
        row.addWidget(button("启动模拟器与游戏",lambda:self.start("launch")))
        row.addWidget(button("校验账号与主城",lambda:self.start("calibrate"),True))
        inner.addLayout(row)
        mainrow = QHBoxLayout()
        self.main_address = PasteLineEdit(self.point_address(self.profile.main_city))
        self.main_address.setPlaceholderText("粘贴本人主城地址：主公名字(X,Y)；粘贴后自动应用")
        mainrow.addWidget(self.main_address, 1)
        mainrow.addWidget(button("应用主城地址", self.apply_main_address))
        mainrow.addWidget(button("复制主城地址", self.copy_main_address))
        self.main_address.returnPressed.connect(self.apply_main_address)
        self.main_address.pasted.connect(lambda: QTimer.singleShot(0, self.apply_main_address))
        inner.addLayout(mainrow)
        self.account_label = QLabel("尚未校验" if not self.profile.main_city else f"{self.profile.account} · 主城 ({self.profile.main_city.x},{self.profile.main_city.y})")
        self.account_label.setObjectName("muted")
        inner.addWidget(self.account_label)
        note = QLabel("首次使用：选 mumu-cli.exe → 发现实例 → 启动游戏 → 发现包名 → 校验账号与主城。")
        note.setObjectName("muted")
        note.setWordWrap(True)
        inner.addWidget(note)
        self.collapsible(inner, "首次使用与运行环境",
                         "选择 MuMu 安装目录内 nx_main/mumu-cli.exe；自动发现实例，按名称和编号选中自己的游戏；"
                         "手动启动实例并登录游戏，点“发现游戏包名”；校验账号与主城；先监控，再开启任务。"
                         "推荐 Windows 10/11 x64 + MuMu 5，横屏 1920×1080、简体中文；运行时可最小化 MuMu。"
                         "切换账号请新建配置并重新校验。")
        layout.addWidget(f)
        f, inner = card("配置迁移与画面标定", "导出不包含本机安装路径。支持横屏 16:9；输入坐标按实际分辨率缩放。")
        row = QHBoxLayout()
        row.addWidget(button("导入配置",self.import_config))
        row.addWidget(button("导出配置",self.export_config))
        row.addWidget(button("新建配置",self.new_config))
        row.addWidget(button("打开数据目录",lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir)))))
        inner.addLayout(row)
        self.mark_preview = Preview()
        self.mark_preview.setMaximumHeight(200)
        self.mark_preview.marked.connect(self.mark_point)
        inner.addWidget(self.mark_preview)
        row = QHBoxLayout()
        row.addWidget(button("读取当前画面",lambda:self.start("preview")))
        self.mark_label = QLabel("点击预览标记位置，不会点击游戏")
        self.mark_label.setObjectName("muted")
        row.addWidget(self.mark_label,1)
        inner.addLayout(row)
        layout.addWidget(f,1)

    def make_logs(self):
        layout = self.page("日志与诊断", "本次运行的完整过程与异常统一在这里查看，包含顶部进度记录。保留最近 2000 条，原始记录仍在会话目录。", scroll=False)
        row = QHBoxLayout()
        self.log_level = combo([("全部级别",""),("警告与错误","attention"),("仅错误","ERROR"),("仅信息","INFO")])
        self.log_level.currentIndexChanged.connect(self.refresh_logs)
        row.addWidget(self.log_level)
        self.log_filter = QLineEdit()
        self.log_filter.setPlaceholderText("筛选队伍、任务或错误文字")
        self.log_filter.textChanged.connect(self.refresh_logs)
        row.addWidget(self.log_filter,1)
        self.log_follow = QCheckBox("跟随最新")
        self.log_follow.setChecked(True)
        self.log_follow.toggled.connect(lambda checked: self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum()) if checked else None)
        row.addWidget(self.log_follow)
        layout.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(button("打开最新截图",self.open_evidence))
        row.addWidget(button("运行环境检测",lambda:self.start("runtime")))
        row.addWidget(button("导出问题诊断包",self.export_diagnostics))
        row.addWidget(button("重新核对主城命令",lambda:self.start("review")))
        row.addWidget(button("清空显示",self.clear_logs))
        row.addStretch()
        self.log_count = QLabel("暂无记录")
        self.log_count.setObjectName("muted")
        row.addWidget(self.log_count)
        layout.addLayout(row)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("diagnosticLog")
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("运行后在这里查看读取过程与异常。可按级别和关键字筛选记录。")
        self.log_view.setMaximumBlockCount(2000)
        layout.addWidget(self.log_view,1)
        self.log_lines = []
        self.log_records = []
        self.collapsible(layout, "未确认出征怎么处理",
                     "出征结果未确认时不锁队等待人工核对：本轮停在该队，下一轮读取现场仍可出击就自动重派。"
                     "“重新核对主城命令”只用于清理异常退出后残留的在途记录；暂停和停止不撤军。")

    def fill_tables(self):
        self.team_table.setRowCount(0)
        self.live.setRowCount(0)
        self.observations.clear()
        self.decisions.clear()
        self.team_activity.clear()
        self.pave_progress.clear()
        self.metrics["本次出征"].setText("0")
        self.metrics["本次武勋"].setText("待读取")
        self.metrics["本次武勋"].setToolTip("每 30 分钟读一次个人信息页的武勋；只作展示，不影响派兵。")
        self.metrics["本次铺路"].setText("—")
        self.metrics["本次铺路"].setToolTip("本次铺路进度：已完成段数 / 总段数")
        self.team_table.setRowCount(len(self.profile.teams))
        self.live.setRowCount(len(self.profile.teams))
        for r,t in enumerate(self.profile.teams):
            if t.group == "guard":
                # 三个自动任务只用主城队伍；亲卫不参与，设置页也不再占一行。
                self.team_table.setRowHidden(r, True)
            visible = QCheckBox()
            visible.setToolTip("取消勾选 = 这支队伍不参与任何任务，也不会被读取")
            visible.toggled.connect(lambda checked, row=r: self.visible_toggled(row, checked))
            self.team_table.setCellWidget(r,0,visible)
            item(self.team_table,r,1,t.display,False)
            self.team_table.setCellWidget(r,2,self.role_cell(t, r))
            mode = combo([("百分比 %","percent"),("固定兵力","absolute")],t.min_mode)
            self.team_table.setCellWidget(r,3,mode)
            minimum = spin(t.minimum,1,100 if t.min_mode=="percent" else 1000000)
            minimum.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
            mode.currentIndexChanged.connect(lambda _,c=mode,s=minimum:s.setMaximum(100 if c.currentData()=="percent" else 1000000))
            self.team_table.setCellWidget(r,4,minimum)
            for column,value,maximum in ((5,t.stamina,200),(6,t.supply,100)):
                widget = spin(value,0,maximum)
                widget.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
                self.team_table.setCellWidget(r,column,widget)
            detail = QPushButton("详情")
            detail.setObjectName("detailButton")
            detail.setToolTip("配置该队的目标组和补兵点顺序")
            detail.clicked.connect(lambda _, row=r: self.edit_team_detail(row))
            self.team_table.setCellWidget(r,7,detail)
            refill = QCheckBox("补兵")
            refill.setToolTip("拉线：兵力低于出击线时，出征页有“一键补兵”就点；"
                              "补不动时回城，再按补兵点（营帐）调动过去继续")
            refill.setChecked(bool(t.refill_enabled))
            refill.toggled.connect(lambda checked, row=r: self.team_refill_toggled(row, checked))
            self.team_table.setCellWidget(r,8,refill)
            auto = QComboBox()
            auto.addItem("未知", None)
            auto.addItem("开", True)
            auto.addItem("关", False)
            auto.setToolTip("游戏内“回城自动补兵”：显示游戏真值；改动标为待写入，下次访问该队详情页才写回。")
            auto.currentIndexChanged.connect(lambda _, row=r: self.team_auto_refill_changed(row))
            self.team_table.setCellWidget(r,9,auto)
            self.sync_team_row(r)
            for c,val in enumerate((t.display,"待识别","—","—","—","尚未读取")):
                item(self.live,r,c+1,val,False)
            check = QCheckBox()
            check.setToolTip("取消勾选 = 这支队伍不参与任何任务")
            check.setChecked(t.role != "hidden")
            check.toggled.connect(lambda checked, row=r: self.visible_toggled(row, checked))
            self.live.setCellWidget(r,0,check)
        self.refresh_live()
        for t,points in ((self.target_table,self.profile.targets),(self.backup_table,self.profile.backups),(self.line_table,self.profile.line_targets)):
            t.setRowCount(0)
            for p in sorted(points, key=lambda p:p.priority):
                self.insert_point(t,p)
        self.pave_table.setRowCount(0)
        for route in self.profile.pave_routes:
            self.insert_pave_route(route)
        self.update_hidden_hint()

    def insert_point(self,t,p):
        r = t.rowCount()
        t.insertRow(r)
        t.setRowHeight(r,44)
        cell_check(t,r,0,p.enabled)
        item(t,r,1,p.name)
        t.item(r,1).setData(Qt.ItemDataRole.UserRole,asdict(p))
        t.setCellWidget(r,2,spin(p.x,0,3000))
        t.setCellWidget(r,3,spin(p.y,0,3000))
        if self.is_target_like(t):
            for column in (2,3):
                t.cellWidget(r,column).setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
            item(t,r,4,p.group)
            editor = QPushButton(actions_label(p.actions) + "  ✎")
            editor._actions = list(p.actions)
            editor.setToolTip("点击编辑：勾选指令并调整优先级顺序")
            editor.clicked.connect(lambda _, b=editor: self.edit_actions(b))
            t.setCellWidget(r,5,editor)
            item(t,r,6,r+1,False)
        else:
            item(t,r,4,r+1,False)
            item(t,r,5,p.expected_name)

    def edit_actions(self, b):
        dialog = ActionOrderDialog(b._actions, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            chosen = dialog.actions()
            if not chosen:
                QMessageBox.warning(self, "指令为空", "每个目的地至少勾选一条指令")
                return
            b._actions = chosen
            b.setText(actions_label(chosen) + "  ✎")

    def add_target(self):
        names = {self.target_table.item(r,1).text() for r in range(self.target_table.rowCount())}
        number = self.target_table.rowCount()+1
        while f"目标 {number}" in names:
            number += 1
        self.insert_point(self.target_table,Point(name=f"目标 {number}", enabled=False))

    def read_point(self, t, r):
        p = Point(**(t.item(r,1).data(Qt.ItemDataRole.UserRole) or {}))
        p.name = t.item(r,1).text().strip()
        p.x, p.y = t.cellWidget(r,2).value(), t.cellWidget(r,3).value()
        p.enabled = t.cellWidget(r,0).isChecked()
        if self.is_target_like(t):
            p.group = t.item(r,4).text().strip()
            p.actions = list(t.cellWidget(r,5)._actions)
            p.priority = r+1
            # 战场分享名称只作备注，不再保存建筑名约束。
            p.expected_name = ""
        else:
            p.priority = r+1
            p.expected_name = t.item(r,5).text().strip()
        return p

    def move_targets(self, direction):
        self.move_points(self.target_table,direction)

    def point_order_row(self, layout, target):
        row = QHBoxLayout()
        buttons = []
        for label, direction in (("↑ 上移", "up"), ("↓ 下移", "down"), ("置顶", "first"), ("置底", "last")):
            b = button(label, lambda _, t=target,d=direction:self.move_points(t,d))
            row.addWidget(b)
            buttons.append(b)
        hint = QLabel("选中行调整顺序 · 支持 Ctrl / Shift 多选")
        hint.setObjectName("muted")
        row.addWidget(hint)
        row.addStretch()
        layout.addLayout(row)
        return buttons

    def move_points(self, t, direction):
        if not self.can_apply_addresses():
            return
        selected = {i.row() for i in t.selectedIndexes()}
        if not selected:
            self.status.setText("请先选中要排序的地点")
            return
        points = [self.read_point(t,r) for r in range(t.rowCount())]
        order = list(range(len(points)))
        if direction in ("first", "last"):
            chosen = [i for i in order if i in selected]
            others = [i for i in order if i not in selected]
            order = chosen+others if direction == "first" else others+chosen
        else:
            indexes = range(1,len(order)) if direction == "up" else range(len(order)-2,-1,-1)
            delta = -1 if direction == "up" else 1
            for i in indexes:
                if order[i] in selected and order[i+delta] not in selected:
                    order[i], order[i+delta] = order[i+delta], order[i]
        t.setRowCount(0)
        for i in order:
            self.insert_point(t,points[i])
        for r, i in enumerate(order):
            if i in selected:
                t.selectionModel().select(t.model().index(r,1), QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        first = next(r for r,i in enumerate(order) if i in selected)
        t.scrollToItem(t.item(first,1))
        self.saved_label.setText("地点顺序已调整 · 待保存")

    def add_backup(self):
        names = {self.backup_table.item(r,1).text() for r in range(self.backup_table.rowCount())}
        number = self.backup_table.rowCount()+1
        while f"补兵点 {number}" in names:
            number += 1
        self.insert_point(self.backup_table,Point(name=f"补兵点 {number}",enabled=False))

    @staticmethod
    def scroll_settings(layout):
        area = QScrollArea()
        area.setWidgetResizable(True)
        contents = QWidget()
        inner = QVBoxLayout(contents)
        inner.setContentsMargins(0, 0, 9, 0)
        inner.setSpacing(13)
        area.setWidget(contents)
        layout.addWidget(area, 1)
        return inner

    @staticmethod
    def point_address(point):
        return SharedAddress(point.expected_name or point.name, point.x, point.y).text() if point else ""

    def shared_address_row(self, layout, target):
        row = QHBoxLayout()
        editor = PasteLineEdit()
        editor.setPlaceholderText("粘贴游戏地址，可连续多个：城池(100,200)营帐(120,210)；粘贴后自动识别")
        editor.setMaxLength(10000)
        row.addWidget(editor, 1)
        row.addWidget(button("识别并添加", lambda:self.add_shared_points(target)))
        editor.returnPressed.connect(lambda:self.add_shared_points(target))
        editor.pasted.connect(lambda: QTimer.singleShot(0, lambda: self.add_shared_points(target)))
        layout.addLayout(row)
        tip = QLabel("支持连续、分行或中文括号。新增地点默认关闭，勾选后生效；相同坐标保留已有设置。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        return editor

    def can_apply_addresses(self):
        if self.worker and self.worker.isRunning():
            self.status.setText("请先停止当前任务，再应用地点设置")
            return False
        return True

    def add_shared_points(self, target, text=None, strict=False):
        if not self.can_apply_addresses():
            return False
        editor = (self.target_address if target is self.target_table else
                  self.line_address if target is self.line_table else self.backup_address)
        text = editor.text() if text is None else text
        try:
            addresses = parse_addresses(text)
            coords = {(target.cellWidget(r,2).value(), target.cellWidget(r,3).value()) for r in range(target.rowCount())}
            names = {target.item(r,1).text() for r in range(target.rowCount())}
            added = 0
            for address in addresses:
                if (address.x, address.y) in coords:
                    continue
                base = address.name or f"地点 {address.x},{address.y}"
                name, suffix = base, 2
                while name in names:
                    name = f"{base} {suffix}"
                    suffix += 1
                self.insert_point(target, Point(name=name, x=address.x, y=address.y, expected_name=address.name, enabled=False))
                coords.add((address.x, address.y))
                names.add(name)
                added += 1
            editor.clear()
            self.status.setText(f"已添加 {added} 个地点，跳过 {len(addresses)-added} 个已有坐标；勾选并保存后生效")
            return True
        except ValueError as exc:
            if strict:
                raise
            self.status.setText(str(exc))
            return False

    def copy_addresses(self, target):
        rows = sorted({i.row() for i in target.selectedIndexes()}) or list(range(target.rowCount()))
        check_column = 1 if self.is_target_like(target) else 5
        addresses = [SharedAddress(target.item(r,check_column).text().strip() or target.item(r,1).text(), target.cellWidget(r,2).value(), target.cellWidget(r,3).value()).text() for r in rows]
        set_clipboard("\n".join(addresses))
        self.status.setText(f"已复制 {len(addresses)} 个游戏地址")

    def apply_main_address(self):
        if not self.can_apply_addresses():
            return False
        try:
            address = single_address(self.main_address.text())
            account = address.name or self.profile.account
            if not account:
                raise ValueError("主城地址请带上本人主公名字，或先从游戏校验账号")
            self.profile.account = account
            self.profile.main_city = Point(name=account, x=address.x, y=address.y, expected_name=account)
            self.main_address.setText(self.point_address(self.profile.main_city))
            self.account_label.setText(f"{account} · 主城 ({address.x},{address.y})")
            self.metrics["账号与主城"].setText(f"{account} ({address.x},{address.y})")
            self.status.setText("主城地址已填入，保存后生效；出征时仍核对现场归属")
            return True
        except ValueError as exc:
            self.status.setText(str(exc))
            return False

    def copy_main_address(self):
        try:
            address = single_address(self.main_address.text())
            set_clipboard(address.text())
            self.status.setText("已复制主城游戏地址")
        except ValueError as exc:
            self.status.setText(str(exc))

    def remove_row(self,t):
        if not self.can_apply_addresses():
            return
        rows = sorted({i.row() for i in t.selectedIndexes()},reverse=True)
        for r in rows:
            t.removeRow(r)
        for r in range(t.rowCount()):
            # 武勋/拉线表顺序在第 6 列，补兵表在第 4 列。
            item(t,r,6 if self.is_target_like(t) else 4,r+1,False)

    def collect(self):
        for target, editor in ((self.target_table,self.target_address),(self.backup_table,self.backup_address),(self.line_table,self.line_address)):
            if editor.text().strip():
                self.add_shared_points(target, strict=True)
        p = copy.deepcopy(self.profile)
        if self.main_address.text().strip():
            address = single_address(self.main_address.text())
            account = address.name or p.account
            if not account:
                raise ValueError("请填写本人主公名字，或先校验游戏账号")
            if self.main_address.text().strip() != self.point_address(p.main_city):
                p.account = account
                p.main_city = Point(name=account,x=address.x,y=address.y,expected_name=account)
        p.name = self.profile_name.text().strip()
        p.cli_path = self.cli_path.text().strip()
        p.instance = str(self.instance.currentData() or "0")
        p.package = self.package.text().strip()
        p.poll_seconds, p.retry_seconds = self.poll.value(), self.retry.value()
        p.backup_enabled, p.food_enabled = self.backup_enabled.isChecked(), self.food_enabled.isChecked()
        p.minimize_emulator = self.minimize_box.isChecked()
        p.hide_before_run = self.hide_before_run.isChecked()
        p.food_low, p.food_recover, p.farming_budget = self.food_low.value(), self.food_recover.value(), self.food_budget.value()
        p.dailies = {k:c.isChecked() for k,c in self.daily_checks.items()}
        p.bandit_interval = self.bandit_interval.value()
        p.bandit_allow_assist = self.bandit_allow_assist.isChecked()
        for r,t in enumerate(p.teams):
            # 设置页给“本次没有的队伍”显示“（未拥有）”；那是状态提示，不是名称，写回前去掉。
            t.label = team_label(self.team_table.item(r,1).text().strip())
            t.min_mode = self.team_table.cellWidget(r,3).currentData()
            t.minimum = self.team_table.cellWidget(r,4).value()
            t.stamina, t.supply = self.team_table.cellWidget(r,5).value(), self.team_table.cellWidget(r,6).value()
            current = self.profile.teams[r]
            t.role, t.saved_role = current.role, current.saved_role
            t.route = current.route if t.role == "pave" else ""
            t.target_group, t.backup_points = current.target_group, list(current.backup_points)
            t.refill_fallback = current.refill_fallback
            t.auto_refill = current.auto_refill
            refill_widget = self.team_table.cellWidget(r, 8)
            t.refill_enabled = bool(refill_widget.isChecked()) if isinstance(refill_widget, QCheckBox) else current.refill_enabled
            auto_widget = self.team_table.cellWidget(r, 9)
            t.auto_refill_pending = auto_widget.currentData() if isinstance(auto_widget, QComboBox) else None
        p.targets, p.backups, p.line_targets = [], [], []
        for table_, points in ((self.target_table,p.targets),(self.backup_table,p.backups),(self.line_table,p.line_targets)):
            for r in range(table_.rowCount()):
                points.append(self.read_point(table_,r))
        p.pave_routes = [self.read_pave_route(r) for r in range(self.pave_table.rowCount())]
        p.normalize()
        return p

    def save(self):
        if self.worker and self.worker.isRunning():
            self.status.setText("请停止当前账号的任务后保存并应用设置")
            return False
        try:
            p = self.collect()
            errors = p.validate()
            if errors:
                raise ValueError("\n".join(errors))
            if self.account and self.manager:
                self.manager.store.save_profile(self.account_id, p)
            else:
                atomic_json(self.config_path,p.to_dict())
            self.profile = p
            self.saved_label.setText(f"已保存 · {datetime.now():%H:%M:%S}")
            if self.manager:
                self.manager.update_account(self)
            return True
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self,"设置未保存",str(exc))
            return False

    def start(self, command):
        if self.worker and self.worker.isRunning():
            self.status.setText("当前账号的任务仍在运行，请先停止；该账号的游戏操作共用一个队列")
            return
        try:
            p = self.collect()
        except (ValueError, TypeError) as exc:
            QMessageBox.warning(self,"请检查设置",str(exc))
            return
        errors = p.validate(active=command=="run")
        if errors:
            QMessageBox.warning(self,"请检查设置","\n".join(errors))
            return
        if self.manager and not self.manager.can_start(self, p, command):
            return
        if not self.save():
            return
        folder = self.session_root / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.clear_logs()
        self.status.setText("正在启动本次任务")
        self.worker = Worker(p,folder,command,command not in ("run", "return", "dailies", "farming"))
        self.worker.event.connect(self.handle_event)
        self.worker.finished.connect(self.finished)
        if self.manager:
            self.worker.finished.connect(self.manager.worker_finished)
        self.paused = False
        self.start_button.setText("停止任务")
        self.start_button.setObjectName("danger")
        self.start_button.style().unpolish(self.start_button)
        self.start_button.style().polish(self.start_button)
        self.monitor_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.profile_name.setEnabled(False)
        for page in range(1, max(1, self.stack.count() - 1)):
            self.stack.widget(page).setEnabled(False)
        self.set_visibility_enabled(False)
        if command in ("run", "scan"):
            self.observations.clear()
            self.decisions.clear()
            self.team_activity.clear()
            self.pave_progress.clear()
            self.metrics["本次出征"].setText("0")
            self.metrics["本次武勋"].setText("待读取")
            self.metrics["本次铺路"].setText("—")
            self.last_scan.setText("正在读取本次状态")
            self.refresh_live()
        self.run_state.setText("● 执行中" if command == "run" else "● 读取中" if command == "scan" else "● 处理中")
        self.run_state.setStyleSheet("color:#248565; font-weight:600;")
        self.worker.start()
        if self.manager:
            self.manager.update_account(self)

    def toggle_start(self):
        if self.worker and self.worker.isRunning():
            self.stop()
        else:
            self.start("run")

    def monitor(self):
        """连接并导航读队伍，不提交派兵、回城或领奖。"""
        self.start("scan")

    def request_refresh(self):
        if self.worker and self.worker.isRunning():
            self.worker.request_refresh()
            self.resource_refresh_pending = True
            self.resource_freshness.setText("已请求刷新，等待当前操作的读取点")
        else:
            self.start("preview")

    def stop(self):
        if self.worker:
            self.worker.stop()
            self.status.setText("正在停止新动作，等待当前读数结束")

    def finished(self):
        self.start_button.setText("开始执行")
        self.start_button.setObjectName("primary")
        self.start_button.style().unpolish(self.start_button)
        self.start_button.style().polish(self.start_button)
        self.monitor_button.setEnabled(True)
        self.save_button.setEnabled(True)
        self.profile_name.setEnabled(True)
        for page in range(1, max(1, self.stack.count() - 1)):
            self.stack.widget(page).setEnabled(True)
        self.set_visibility_enabled(True)
        self.run_state.setText("● 待启动")
        self.run_state.setStyleSheet("color:#79869a; font-weight:600;")
        if self.manager:
            self.manager.update_account(self)
        if self.close_requested and not self.manager:
            QTimer.singleShot(0, self.close)

    def handle_event(self,kind,value):
        if kind == "status":
            if self.status.text() != value:
                self.status.setText(value)
                self.status.setToolTip(value)
                self.append_log({"time":datetime.now().strftime("%H:%M:%S"),"level":"INFO","message":value})
        elif kind == "log":
            self.append_log(value)
        elif kind == "preview":
            self.preview.display(value)
            self.mark_preview.display(self.preview.source)
        elif kind == "evidence":
            self.latest_evidence = value
        elif kind == "instances":
            self.cli_path.setText(value["cli"])
            old = self.instance.currentData()
            self.instance.clear()
            for info in value["instances"]:
                self.instance.addItem(f"{info.get('name','MuMu')} · 编号 {info['index']} {'（已启动）' if info.get('is_android_started') else '（未启动）'}",str(info['index']))
            if old is not None and self.instance.findData(old) < 0:
                self.instance.addItem(f"原编号 {old}（未发现，请手动选中自己的实例）", old)
            self.instance.setCurrentIndex(max(0,self.instance.findData(old)))
        elif kind == "connection":
            self.metrics["游戏连接"].setText("已连接 1 台")
            self.metrics["游戏连接"].setToolTip(f"{value.get('name','MuMu')} · 编号 {value['instance']}\n{value['serial']} · {value['resolution']}")
        elif kind == "packages":
            if len(value) == 1:
                self.package.setText(value[0])
            elif value:
                from PySide6.QtWidgets import QInputDialog
                selected, ok = QInputDialog.getItem(self, "选择游戏渠道", "请选中所登录的游戏包名", value, 0, False)
                if ok:
                    self.package.setText(selected)
        elif kind == "grain":
            if value is not None:
                self.metrics["粮食库存"].setText(f"{value/10000:.1f} 万")
        elif kind == "resource_status":
            self.resource_refresh_pending = False
            self.resource_time = value.get("observed_at")
            self.resource_visible = value["visible"]
            self.refresh_resource_age()
        elif kind == "calibrated":
            # 只写回已核实的账号与主城：从当前已保存配置深拷贝，不夹带界面上
            # 尚未保存的阈值等表单；校验通过并落盘成功后才同步 self.profile/UI。
            saved = copy.deepcopy(self.profile)
            saved.account = value["account"]
            saved.main_city = Point(**value["main_city"])
            errors = saved.validate()
            if not errors:
                try:
                    if self.account and self.manager:
                        self.manager.store.save_profile(self.account_id, saved)
                    else:
                        atomic_json(self.config_path,saved.to_dict())
                except OSError as exc:
                    errors = [f"写入失败：{exc}"]
            if errors:
                message = "账号校准结果未保存：" + "；".join(errors)
                self.status.setText(message)
                self.append_log({"time":datetime.now().strftime("%H:%M:%S"),"level":"ERROR","message":message})
                return
            self.profile = saved
            self.main_address.setText(self.point_address(self.profile.main_city))
            self.account_label.setText(f"{value['account']} · 主城 ({value['main_city']['x']},{value['main_city']['y']})")
            self.metrics["账号与主城"].setText(self.account_label.text())
            if self.manager:
                self.manager.update_account(self)
        elif kind == "observation":
            self.observations[value["key"]] = value
            self.team_activity.pop(value["key"], None)
            self.decisions.pop(value["key"], None)
            self.refresh_live()
            self.last_scan.setText(f"最新读数 · {datetime.now():%H:%M:%S}")
        elif kind == "team_presence":
            # 本次实际有几队：没有的槽位在设置和总览里锁住，不能配置/启用。
            self.apply_team_presence(value.get("slots") or [])
        elif kind == "decision":
            self.decisions[value["team"]] = value["reason"]
            self.refresh_live()
            team_name = next((p.display for p in self.profile.teams if p.key == value['team']),value['team'])
            self.append_log({"time":datetime.now().strftime("%H:%M:%S"),"level":"INFO",
                             "message":f"{team_name} · {value['reason']}"})
        elif kind == "dispatch":
            self.metrics["本次出征"].setText(str(int(self.metrics["本次出征"].text())+1))
            if value.get("kind") == "pave":
                progress = self.pave_progress.get(value["team"], {})
                done, total = progress.get("done", 0), progress.get("total", 0)
                reason = f"铺路 {done}/{total} · 前往 {value['target']}"
            else:
                reason = f"已确认{value.get('action','出征')} → {value['target']}；继续监控"
            self.team_activity[value["team"]] = {"state":value.get("state","confirmed"),"reason":reason,
                                                 "target":value.get("target")}
            self.refresh_live()
            team_name = next((p.display for p in self.profile.teams if p.key == value['team']),value['team'])
            self.append_log({"time":datetime.now().strftime("%H:%M:%S"),"level":"INFO", "message":f"{team_name} · {value.get('action','出征')} → {value['target']} 已确认"})
        elif kind == "merit":
            # 只作展示：当前武勋 + 本次任务累计。读数单位是“万”时标“约”。
            unit = int(value.get("unit", 1)) or 1
            delta = int(value.get("delta", 0))
            current = int(value.get("value", 0))
            if unit > 1:
                text = f"{current/unit:.1f} 万（约 {delta/unit:+.1f}）"
                log_delta = f"约 {delta/unit:+.1f} 万"
            else:
                text = f"{current}（{delta:+d}）"
                log_delta = f"{delta:+d}"
            self.metrics["本次武勋"].setText(text)
            self.metrics["本次武勋"].setToolTip(
                f"当前武勋 {value.get('text','')}；本次任务开始时 {value.get('base',0)}。"
                "每 30 分钟读一次个人信息页，只作展示，不参与派兵。")
            self.append_log({"time":datetime.now().strftime("%H:%M:%S"),"level":"INFO",
                             "message":f"武勋 {value.get('text','')} · 本次累计 {log_delta}"})
        elif kind == "team_action":
            self.team_activity[value["team"]] = value
            self.refresh_live()
        elif kind == "daily":
            label = self.daily_results.get(value["task"])
            if label:
                label.setText(value["result"])
            task = self.daily_checks.get(value['task'])
            result = {"time":datetime.now().strftime("%H:%M:%S"),"level":"INFO","message":f"{task.text()+' · ' if task else ''}{value['result']}"}
            self.append_log(result)
        elif kind == "rates":
            text = "\n".join(f"{p['name']}：{p['text']}" for p in value)
            self.rates.setPlainText(text)
            self.append_log({"time":datetime.now().strftime("%H:%M:%S"),"level":"INFO","message":text})
        elif kind == "pave":
            team = value.get("team")
            self.pave_progress[team] = value
            done, total = value.get("done", 0), value.get("total", 0)
            self.metrics["本次铺路"].setText(f"{done}/{total}" if total else "—")
            tip = f"{value.get('route', '')} · 已完成 {done}/{total} 段"
            if value.get("next"):
                tip += f" · 下一点 ({value['next'][0]},{value['next'][1]})"
            self.metrics["本次铺路"].setToolTip(tip)
            self.status.setText(f"铺路 · {value.get('route','')} · 已完成 {done}/{total} 段")
            self.append_log({"time":datetime.now().strftime("%H:%M:%S"),"level":"INFO",
                             "message":f"铺路路线 {value.get('route','')}：{done}/{total} 段，"
                                       f"状态 {value.get('status','')}"})
            self.refresh_live()
        elif kind == "error":
            # 故障跳到日志页。Worker 停止时已经发布过同一条 ERROR 日志，
            # 用最近记录去重：已有对应 ERROR 时只切页、不重复追加；
            # 只有 error 事件而没有对应日志时，才在这里补记一条。
            message = str(value)
            recent = self.log_records[-5:]
            if not any(record.get("level") == "ERROR" and record.get("message") == message
                       for record in recent):
                self.append_log({"time":datetime.now().strftime("%H:%M:%S"),"level":"ERROR","message":message})
            self.nav.setCurrentRow(self.log_page_index)

    def refresh_live(self):
        self.refresh_resource_age()
        for r,t in enumerate(self.profile.teams):
            hidden = t.role == "hidden"
            self.live.setRowHidden(r, hidden)
            check = self.live.cellWidget(r, 0)
            if isinstance(check, QCheckBox):
                check.blockSignals(True)
                check.setChecked(not hidden)
                check.setEnabled(self.team_available(t))
                check.blockSignals(False)
            if hidden:
                continue
            if t.group == "guard":
                # 三个自动任务只使用主城队伍；亲卫不参与，也不再占运行总览的行。
                self.live.setRowHidden(r, True)
                continue
            if (t.group == "main" and self.team_slots is not None
                    and t.slot not in self.team_slots):
                values = [t.display, "未拥有", "—", "—", "—",
                          "本赛季尚未获得该队；不读取、不派兵，获得后下一轮自动纳入"]
                for c, val in enumerate(values):
                    item(self.live, r, c + 1, val, False)
                    self.live.item(r, c + 1).setToolTip(str(val))
                self.live.item(r, 2).setForeground(QBrush(QColor("#9aa7b8")))
                continue
            obs = self.observations.get(t.key, {})
            activity = self.team_activity.get(t.key)
            state = activity["state"] if activity else obs.get("state", "unknown")
            reason = self.decisions.get(t.key,"已读取，等待本批核对" if obs else "等待读取")
            if activity:
                reason = activity["reason"]
            elif obs.get("wounded") and (t.key not in self.decisions or reason.startswith("等待重伤结束")):
                seconds = obs.get("injury_seconds")
                remaining = max(0, math.ceil(seconds - max(0,time.time()-obs["observed_at"]))) if seconds is not None else None
                reason = f"等待重伤结束（约 {remaining} 秒）" if remaining else "重伤计时已到，等待现场复核" if remaining == 0 else "等待重伤结束"
            elif not activity and t.key not in self.decisions and state in ("marching","returning","guarding","active","recruiting"):
                reason = STATES[state]+"，继续监控"
            if t.role == "pave" and t.key in self.pave_progress:
                # 铺路队的进度常驻：?/?? 直接显示在这一行，详细过程在日志里。
                prog = self.pave_progress[t.key]
                done, total = prog.get("done", 0), prog.get("total", 0)
                if prog.get("status") == "done" or (total and done >= total):
                    reason = f"铺路完成 {done}/{total}"
                else:
                    nxt = prog.get("next")
                    where = f" · 下一点 ({nxt[0]},{nxt[1]})" if nxt else ""
                    reason = f"铺路 {done}/{total}{where}"
            labels = {**STATES,"confirming":"核对出发","confirmed":"已确认出发","return_confirming":"核对回城","return_confirmed":"回城已受理","fighting":"战斗中","retrying":"未确认 · 自动重试"}
            shown = display_label(obs.get("marker_label"), state, labels)
            values = [f"{t.display} · {obs['identity']}" if obs.get("identity") else t.display, shown, f"{obs['strength']}/{obs['maximum']}" if obs.get("strength") is not None else "待识别", f"{obs.get('stamina') if obs.get('stamina') is not None else '—'} / {obs.get('supply') if obs.get('supply') is not None else '—'}", f"{obs.get('pool','未知')} {obs['reserve']}/{obs['capacity']}" if obs.get("reserve") is not None else obs.get("pool","未知"), reason]
            for c,val in enumerate(values):
                item(self.live,r,c+1,val,False)
                self.live.item(r,c+1).setToolTip(str(val))
            color = "#b87422" if state in ("unknown","wounded","retrying") else "#2f6bbb" if state in ("marching","returning","confirming","return_confirming") else "#248565"
            self.live.item(r,2).setForeground(QBrush(QColor(color)))
            if obs.get("observed_at"):
                age = max(0,round(time.time()-obs["observed_at"]))
                for c in (3,4,5):
                    self.live.item(r,c).setToolTip(f"{values[c-1]}\n数值读取于 {datetime.fromtimestamp(obs['observed_at']):%H:%M:%S}，约 {age} 秒前")

    def refresh_resource_age(self):
        if self.resource_refresh_pending:
            self.resource_freshness.setText("已请求刷新，等待当前操作的读取点")
            return
        if not self.resource_time:
            self.resource_freshness.setText("尚未读取" if self.resource_visible else "当前画面无法读取库存")
            return
        age = max(0, round(time.time()-self.resource_time))
        state = "" if self.resource_visible else " · 当前画面不可读"
        self.resource_freshness.setText(f"{datetime.fromtimestamp(self.resource_time):%H:%M:%S} · {age}秒前{state}")
        self.metrics["粮食库存"].setToolTip("最后一次确认的库存；打开背包等页面时保留旧值，回到可读画面后更新。")

    def append_log(self,value):
        line = f"{value['time']}  {value['level']:<5}  {value['message']}"
        self.log_lines.append(line)
        self.log_lines = self.log_lines[-2000:]
        self.log_records.append(dict(value))
        self.log_records = self.log_records[-2000:]
        self.add_recent_event(value)
        self.log_count.setText(f"保留 {len(self.log_lines)} 条")
        if self.log_matches(value,line):
            bar = self.log_view.verticalScrollBar()
            at_bottom = bar.value() >= bar.maximum()-2
            previous = bar.value()
            self.log_view.appendPlainText(line)
            if self.log_follow.isChecked() and at_bottom:
                bar.setValue(bar.maximum())
            else:
                bar.setValue(previous)

    def add_recent_event(self, value):
        entry = dict(value)
        self.event_history.append(entry)
        self.event_history = self.event_history[-300:]
        i = QListWidgetItem(f"{entry['time']}  {entry['level']}  {entry['message']}")
        i.setToolTip(entry['message'])
        if entry['level'] in ('WARN', 'ERROR'):
            i.setForeground(QBrush(QColor('#be4d4d' if entry['level']=='ERROR' else '#a86a27')))
        self.recent_events.addItem(i)
        while self.recent_events.count() > 300:
            self.recent_events.takeItem(0)
        if self.overview_follow.isChecked():
            self.recent_events.scrollToBottom()

    def clear_logs(self):
        self.event_history.clear()
        self.recent_events.clear()
        self.log_records.clear()
        self.log_lines.clear()
        self.log_view.clear()
        self.log_count.setText("暂无记录")

    def log_matches(self, record, line):
        level = self.log_level.currentData()
        allowed = record['level'] in ('WARN','ERROR') if level == 'attention' else not level or record['level'] == level
        return allowed and self.log_filter.text().casefold() in line.casefold()

    def refresh_logs(self):
        self.log_view.setPlainText("\n".join(line for record,line in zip(self.log_records,self.log_lines) if self.log_matches(record,line)))
        if self.log_follow.isChecked():
            self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    def choose_cli(self):
        path,_ = QFileDialog.getOpenFileName(self,"选择 MuMu CLI","","mumu-cli.exe (mumu-cli.exe)")
        if path:
            self.cli_path.setText(path)

    def import_config(self):
        if self.worker and self.worker.isRunning():
            self.status.setText("请先停止当前任务再切换配置")
            return
        path,_ = QFileDialog.getOpenFileName(self,"导入账号配置","","配置 (*.json)")
        if path:
            try:
                p = Profile.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
                # 校验通过才写盘：无效配置不能覆盖当前账号的原配置。
                errors = p.validate()
                if errors:
                    raise ValueError("\n".join(errors))
                if self.account and self.manager:
                    self.manager.store.save_profile(self.account_id, p)
                else:
                    atomic_json(self.config_path,p.to_dict())
                self.replace_window(p)
            except (ValueError,OSError) as exc:
                QMessageBox.warning(self,"导入失败",str(exc))

    def export_config(self):
        p = self.collect()
        errors = p.validate()
        if errors:
            QMessageBox.warning(self,"配置无效","\n".join(errors))
            return
        path,_ = QFileDialog.getSaveFileName(self,"导出账号配置",p.name+".json","配置 (*.json)")
        if path:
            data = p.to_dict()
            data["cli_path"] = ""
            atomic_json(Path(path),data)

    def new_config(self):
        if self.manager:
            self.manager.add_account()
            return
        if self.worker and self.worker.isRunning():
            self.status.setText("请先停止再新建配置")
            return
        p = Profile(name="新账号配置")
        atomic_json(self.config_path,p.to_dict())
        self.replace_window(p)

    def replace_window(self,p):
        if self.manager:
            self.manager.replace_account(self, p)
            return
        # 使用新窗口重新建表，旧配置不留下阵容/目标读数。
        replacement = Window(self.data_dir)
        replacement.show()
        app = QApplication.instance()
        app.main_window = replacement
        self.close()

    def mark_point(self,x,y):
        self.latest_mark = (x,y)
        self.mark_label.setText(f"已标记 ({x},{y})")

    def open_evidence(self):
        if self.latest_evidence:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.latest_evidence).resolve())))

    def choose_diagnostics_scope(self):
        """导出前让用户选诊断范围：返回 "safe" / "full" / None（取消）。测试可整体替换本方法。"""
        box = QMessageBox(self)
        box.setWindowTitle("导出问题诊断包")
        box.setText("安全诊断（默认，不含配置、数据库、截图、原始日志）\n完整诊断（含昵称、坐标、本机路径等敏感信息）")
        safe = box.addButton("安全诊断", QMessageBox.ButtonRole.AcceptRole)
        full = box.addButton("完整诊断", QMessageBox.ButtonRole.ActionRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is safe:
            return "safe"
        if clicked is full:
            return "full"
        return None

    def confirm_full_diagnostics(self):
        answer = QMessageBox.question(self,"确认完整诊断",
            "完整诊断包包含昵称、坐标、本机路径等敏感信息，确认导出？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def export_diagnostics(self):
        from .diagnostics import export_diagnostics
        scope = self.choose_diagnostics_scope()
        if scope is None:
            self.status.setText("已取消导出诊断包")
            return
        # 默认安全诊断（后端 include_private=False）；完整诊断必须再次明确确认。
        include_private = scope == "full"
        if include_private and not self.confirm_full_diagnostics():
            self.status.setText("已取消导出诊断包")
            return
        default = self.data_dir / "feedback" / f"三谋诊断-{datetime.now():%Y%m%d-%H%M%S}.zip"
        title = "导出完整诊断包（含昵称、坐标及本机路径，请核对后再发送）" if include_private else "导出安全诊断包（不含配置、数据库、截图和原始日志）"
        path, _ = QFileDialog.getSaveFileName(self, title, str(default), "诊断包 (*.zip)")
        if not path:
            return
        try:
            export_diagnostics(self.data_dir, Path(path), include_private=include_private)
            self.status.setText(f"诊断包已保存：{path}")
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "导出失败", str(exc))

    def closeEvent(self,event):
        if self.worker and self.worker.isRunning():
            self.close_requested = True
            self.worker.stop()
            event.ignore()
            self.status.setText("正在停止，结束后可关闭窗口")
        else:
            self.live_timer.stop()
            event.accept()


class AddAccountDialog(QDialog):
    def __init__(self, discovered, default_name, default_instance, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加控制账号")
        self.resize(520, 220)
        layout = QVBoxLayout(self)
        tip = QLabel("每个账号拥有独立设置、运行任务、日志和订单记录。添加只保存配置，不会启动或操作游戏。")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        form = QFormLayout()
        self.name = QLineEdit(default_name)
        self.name.setMaxLength(80)
        form.addRow("账号显示名称", self.name)
        self.instance = QComboBox()
        for info in discovered:
            label = f"{info.get('name', 'MuMu')} · 编号 {info['index']}" + ("（已启动）" if info.get("is_android_started") else "（未启动）")
            self.instance.addItem(label, str(info["index"]))
        if self.instance.findData(str(default_instance)) >= 0:
            self.instance.setCurrentIndex(self.instance.findData(str(default_instance)))
        form.addRow("绑定 MuMu 实例", self.instance)
        self.copy_settings = QCheckBox("复制当前账号的策略作为初始模板（账号与主城仍需重新校验）")
        self.copy_settings.setChecked(True)
        layout.addLayout(form)
        layout.addWidget(self.copy_settings)
        buttons = dialog_buttons()
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class AccountManagerWindow(QMainWindow):
    def __init__(self, data_dir: Path | None = None):
        super().__init__()
        from .accounts import AccountStore

        self.data_dir = data_dir or data_directory()
        self.store = AccountStore(self.data_dir)
        self.closing = False
        self.windows = []
        self.setWindowTitle(f"三谋助手  {__version__}")
        available = self.screen().availableGeometry() if self.screen() else None
        avail_w = available.width() if available else 1440
        avail_h = available.height() if available else 940
        width = min(1680, max(940, avail_w - 48))
        height = min(1040, max(580, avail_h - 64))
        self.resize(width, height)
        self.setMinimumSize(min(940, avail_w), min(580, avail_h))
        self.setStyleSheet(STYLE)
        root = QWidget()
        root.setObjectName("content")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(10)
        self.setCentralWidget(root)
        bar = QFrame()
        bar.setObjectName("topBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(16, 10, 16, 10)
        row.addWidget(QLabel("控制账号"))
        self.account_combo = QComboBox()
        self.account_combo.setMinimumWidth(260)
        row.addWidget(self.account_combo)
        row.addWidget(button("添加账号", self.add_account, True))
        self.account_hint = QLabel("切换只改变当前页面，其他账号的后台任务继续运行")
        self.account_hint.setObjectName("muted")
        row.addWidget(self.account_hint, 1)
        layout.addWidget(bar)
        self.account_stack = QStackedWidget()
        layout.addWidget(self.account_stack, 1)
        for account in self.store.accounts:
            self._append_account(account)
        selected = next((i for i, window in enumerate(self.windows) if window.account_id == self.store.selected), 0)
        self.account_combo.currentIndexChanged.connect(self.select_account)
        self.account_combo.setCurrentIndex(selected)
        self.account_stack.setCurrentIndex(selected)
        self.select_account(selected)
        storage_warnings = ([self.store.migration_error] if self.store.migration_error else []) + self.store.load_errors
        if storage_warnings:
            self.current_window.status.setText("；".join(storage_warnings))

    @property
    def current_window(self):
        return self.windows[self.account_stack.currentIndex()]

    def _append_account(self, account):
        window = Window(self.data_dir, account=account, manager=self)
        window.setMinimumSize(880, 480)
        self.windows.append(window)
        self.account_stack.addWidget(window)
        self.account_combo.addItem(account.name, account.id)
        self.update_account(window)
        return window

    def select_account(self, index):
        if not 0 <= index < len(self.windows):
            return
        self.account_stack.setCurrentIndex(index)
        window = self.windows[index]
        self.store.select(window.account_id)
        self.update_account(window)

    def update_account(self, window):
        index = self.windows.index(window)
        account = self.store.get(window.account_id)
        running = bool(window.worker and window.worker.isRunning())
        label = account.name + (" · 运行中" if running else "")
        self.account_combo.blockSignals(True)
        self.account_combo.setItemText(index, label)
        self.account_combo.blockSignals(False)
        if window is self.current_window:
            self.account_hint.setText(f"MuMu 实例 {window.profile.instance} · 独立设置与任务；切换不会停止后台")

    def can_start(self, window, profile, command):
        if command in ("discover", "runtime", "pave-reset"):
            return True
        cli = os.path.normcase(os.path.abspath(profile.cli_path)) if profile.cli_path else ""
        for other in self.windows:
            if other is window or not other.worker or not other.worker.isRunning():
                continue
            running = other.worker.profile
            other_cli = os.path.normcase(os.path.abspath(running.cli_path)) if running.cli_path else ""
            if str(running.instance) == str(profile.instance) and other_cli == cli:
                window.status.setText(f"实例 {profile.instance} 正由账号“{other.profile.account or other.profile.name}”使用，请先停止该账号任务")
                return False
        return True

    def add_account(self):
        from .transport import discover_cli, instances

        current = self.current_window
        try:
            cli = discover_cli(current.cli_path.text())
            discovered = instances(cli)
            if not discovered:
                raise RuntimeError("没有发现 MuMu 实例")
        except (OSError, RuntimeError) as exc:
            QMessageBox.warning(self, "无法添加账号", str(exc))
            return
        used = {str(window.profile.instance) for window in self.windows}
        default = next((str(info["index"]) for info in discovered if str(info["index"]) not in used), str(current.profile.instance))
        dialog = AddAccountDialog(discovered, f"新账号 {len(self.windows)+1}", default, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            template = current.collect() if dialog.copy_settings.isChecked() else None
            account = self.store.create_account(dialog.name.text(), dialog.instance.currentData(), template)
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "账号未添加", str(exc))
            return
        self._append_account(account)
        self.account_combo.setCurrentIndex(len(self.windows)-1)

    def replace_account(self, window, profile):
        index = self.windows.index(window)
        account = self.store.get(window.account_id)
        replacement = Window(self.data_dir, account=account, manager=self)
        replacement.setMinimumSize(900, 500)
        self.windows[index] = replacement
        self.account_stack.removeWidget(window)
        self.account_stack.insertWidget(index, replacement)
        window.live_timer.stop()
        window.deleteLater()
        self.account_stack.setCurrentIndex(index)
        self.update_account(replacement)

    def start(self, command):
        self.current_window.start(command)

    def worker_finished(self):
        if self.closing and not any(window.worker and window.worker.isRunning() for window in self.windows):
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event):
        running = [window for window in self.windows if window.worker and window.worker.isRunning()]
        if running:
            self.closing = True
            for window in running:
                window.stop()
            self.account_hint.setText(f"正在安全停止 {len(running)} 个账号任务，结束后自动关闭")
            event.ignore()
            return
        for window in self.windows:
            window.live_timer.stop()
        event.accept()

