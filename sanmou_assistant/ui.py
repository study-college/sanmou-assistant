from __future__ import annotations

import json
import os
import re
import time
import math
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import cv2
from PySide6.QtCore import Qt, QTimer, Signal, QItemSelectionModel
from PySide6.QtGui import QImage, QPixmap, QFont, QDesktopServices, QColor, QBrush
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QLabel, QPushButton, QCheckBox, QComboBox, QSpinBox, QLineEdit, QFormLayout, QVBoxLayout, QHBoxLayout, QGridLayout, QStackedWidget, QListWidget, QListWidgetItem, QDialog, QDialogButtonBox, QTableWidget, QTableWidgetItem, QHeaderView, QFrame, QFileDialog, QMessageBox, QPlainTextEdit, QScrollArea, QSplitter, QAbstractItemView)

from . import __version__
from .models import Profile, Point, TeamPolicy, PaveRoute, pave_label, pave_summary, STATES, ACTION_NAMES, atomic_json, data_directory
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
QLabel#heading { font-size:24px; color:#16263d; }
QLabel#subheading { color:#77859b; font-size:12px; }
QFrame#topBar { background:white; border:1px solid #e6ebf2; border-radius:10px; }
QLabel#currentStatus { color:#53657e; font-size:12px; }
QFrame#card { border:1px solid #e5eaf2; border-radius:10px; }
QLabel#cardValue { font-size:25px; color:#183b65; }
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
QToolTip { background:#20314d; color:white; padding:7px; border:0; }
"""


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
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
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


class PaveRouteDialog(QDialog):
    """铺路路线编辑器：粘贴游戏地址即可生成有序坐标，和地点优先级一样可上下调整。"""

    def __init__(self, points, parent=None, options=None):
        super().__init__(parent)
        self.setWindowTitle("编辑铺路路线")
        self.resize(620, 540)
        layout = QVBoxLayout(self)
        tip = QLabel("列表从上到下就是铺路顺序，脚本到达一个点并停留后才派下一段。\n"
                     "可直接粘贴游戏地址：空地(571,243)空地(566,240)；或写坐标：571,243; 566,240。")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        options = options or {}
        self.list = QListWidget()
        for point in points:
            self.add_entry(point)
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.paste = QLineEdit()
        self.paste.setPlaceholderText("粘贴游戏地址或坐标，例如 空地(571,243)空地(566,240)")
        self.paste.setMaxLength(10000)
        row.addWidget(self.paste, 1)
        row.addWidget(button("识别并添加", self.add_pasted))
        self.paste.returnPressed.connect(self.add_pasted)
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
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
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
        screen = self.screen()
        available = screen.availableGeometry() if screen else None
        width = max(900, available.width()-40) if available else 1440
        height = max(500, available.height()-60) if available else 960
        self.resize(min(1440,width), min(960,height))
        self.setMinimumSize(min(1120,width), min(760,height))
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
        self.nav.addItems(["01   运行总览", "02   部队策略", "03   战场目标", "04   铺路路线", "05   征兵与补粮", "06   签到与日常", "07   连接与配置", "08   日志与诊断"])
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
        topbody.setContentsMargins(16,12,16,10)
        topbody.setSpacing(7)
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
        self.make_targets()
        self.make_pave()
        self.make_recruitment()
        self.make_dailies()
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
            self.metrics["账号与主城"].setText(f"{self.profile.account}\n({self.profile.main_city.x},{self.profile.main_city.y})")
        if config_error:
            self.status.setText(config_error)
            self.append_log({"time":datetime.now().strftime("%H:%M:%S"),"level":"ERROR","message":config_error})

    def page(self, title, description):
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
        return layout

    def showEvent(self, event):
        super().showEvent(event)
        if getattr(self, "live_fit_pending", False):
            QTimer.singleShot(0, self.fit_live_columns)

    def make_dashboard(self):
        layout = self.page("运行总览", "读取游戏真实状态，显示每一队的下一步和等待原因。停止后，游戏中的任务继续进行。")
        layout = self.scroll_settings(layout)
        row = QHBoxLayout()
        self.metrics = {}
        for name, value in (("游戏连接", "未连接"), ("粮食库存", "待识别"), ("账号与主城", "未校验"), ("本次出征", "0")):
            f, inner = card(name)
            label = QLabel(value)
            label.setObjectName("cardValue")
            label.setWordWrap(True)
            if name == "账号与主城":
                label.setStyleSheet("font-size:17px;font-weight:600;color:#20497e;")
            self.metrics[name] = label
            inner.addWidget(label)
            if name == "粮食库存":
                self.resource_time = None
                self.resource_visible = False
                self.resource_refresh_pending = False
                self.resource_freshness = QLabel("尚未读取")
                self.resource_freshness.setObjectName("muted")
                inner.addWidget(self.resource_freshness)
            row.addWidget(f)
        layout.addLayout(row)
        controls = QHBoxLayout()
        self.scan_button = button("读取全部队伍", lambda:self.start("scan"))
        controls.addWidget(self.scan_button)
        self.refresh_button = button("刷新画面与资源", self.request_refresh)
        controls.addWidget(self.refresh_button)
        controls.addWidget(button("已派出队伍回城", lambda:self.start("return")))
        controls.addStretch()
        self.last_scan = QLabel("尚无本次观测")
        self.last_scan.setObjectName("muted")
        controls.addWidget(self.last_scan)
        layout.addLayout(controls)
        self.live = table(["队伍 / 阵容", "状态", "兵力", "体力 / 补给", "预备兵池", "下一步 / 等待原因"])
        self.live.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.live.horizontalHeader()
        # 每列都可以拖动列宽；宽度保存在数据目录，重启后沿用上次的布局。
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(52)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.live.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.live_columns = (175,105,130,120,165,375)
        saved = self.saved_widths("live", self.live_columns)
        for i,w in enumerate(saved or self.live_columns):
            self.live.setColumnWidth(i,w)
        self.live_fit_pending = saved is None
        self.width_save_timer = QTimer(self)
        self.width_save_timer.setSingleShot(True)
        self.width_save_timer.setInterval(400)
        self.width_save_timer.timeout.connect(lambda:self.save_widths("live", self.live))
        header.sectionResized.connect(lambda *a:self.width_save_timer.start())
        self.live.setMinimumHeight(280)
        self.live.verticalHeader().setDefaultSectionSize(36)
        layout.addWidget(self.live, 3)
        lower = QHBoxLayout()
        f, inner = card("游戏画面", "最近一次截图；窗口最小化也可更新")
        f.setMaximumHeight(244)
        self.preview = Preview()
        self.preview.setMinimumSize(180,100)
        self.preview.setMaximumHeight(170)
        inner.addWidget(self.preview,1)
        lower.addWidget(f, 1)
        f, inner = card("实时日志", "本次任务的最新进度；重新启动任务时清空显示")
        f.setMaximumHeight(244)
        self.recent_events = QListWidget()
        self.recent_events.setObjectName("recentEvents")
        self.recent_events.setWordWrap(True)
        self.recent_events.setMinimumHeight(100)
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
        log_actions.addWidget(button("完整日志 →",lambda:self.nav.setCurrentRow(7)))
        inner.addLayout(log_actions)
        lower.addWidget(f, 1)
        layout.addLayout(lower, 2)

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

    def make_teams(self):
        layout = self.page("部队策略", "自由选择刷武勋与讨贼的队伍。最低兵力用于主城出击；城外备用补兵始终等满兵。")
        layout = self.scroll_settings(layout)
        notice = QLabel("开启任务前先校验账号与主城。亲卫自动出征暂未启用；讨贼加入与结算仍需实机验收，未知页面会暂停。")
        notice.setObjectName("notice")
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.team_table = table(["队伍", "武勋", "讨贼", "兵力方式", "出击下限", "体力", "补给", "目标组", "备用点", "优先任务"])
        self.team_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.team_table.verticalHeader().setDefaultSectionSize(46)
        widths = [95, 44, 44, 96, 100, 65, 65, 75, 90, 90]
        for i,w in enumerate(widths):
            self.team_table.setColumnWidth(i,w)
        self.team_table.horizontalHeader().setStretchLastSection(False)
        self.team_table.setColumnWidth(9,120)
        for column in (7,8):
            self.team_table.horizontalHeader().setSectionResizeMode(column,QHeaderView.ResizeMode.Stretch)
        f, inner = card("队伍安排", "主城与亲卫分别显示；按每队实际最大兵力计算出击下限")
        inner.addWidget(self.team_table,1)
        layout.addWidget(f, 1)
        note = QLabel("百分比按当前最大兵力向上取整，例如 33000 × 90% = 29700。\n每次派兵核对槽位、兵力和重伤；未知字段显示待识别，不按满兵处理。")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)

    def make_targets(self):
        layout = self.page("战场目标", "以坐标为准，名称仅作备注。按列表从上到下尝试同组已启用目标；建筑变化不影响定位。")
        layout = self.scroll_settings(layout)
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
        n = QLabel("列表上方优先；同组内先尝试第一个启用目标，指令全部不可用才换下一个目标。可按 Ctrl / Shift 多选后一并移动。\n坐标必须与游戏详情一致；营帐变空地、箭塔或易主后，仍只尝试你允许的指令。新目标默认关闭。")
        n.setObjectName("notice")
        n.setWordWrap(True)
        layout.addWidget(n)

    def make_pave(self):
        layout = self.page("铺路路线", "按坐标顺序逐段攻占；到点停留后继续下一段。目标不相邻时游戏会自动铺路，脚本只负责核对与提交。")
        layout = self.scroll_settings(layout)
        f, inner = card("铺路路线", "每队一条路线；列表顺序就是推进顺序，部队到达并停留后才派下一段")
        self.pave_table = table(["启用", "路线名称", "队伍", "路线（按顺序）", "段数", "编辑"])
        self.pave_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self.pave_table.horizontalHeader()
        header.setStretchLastSection(False)
        for column, width in ((0, 55), (1, 150), (2, 80), (4, 55), (5, 105)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self.pave_table.setColumnWidth(column, width)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.pave_table.setMinimumHeight(230)
        inner.addWidget(self.pave_table)
        row = QHBoxLayout()
        row.addWidget(button("添加路线", self.add_pave_route))
        row.addWidget(button("删除选中路线", self.remove_pave_row))
        row.addWidget(button("重置铺路进度", lambda: self.start("pave-reset")))
        row.addStretch()
        inner.addLayout(row)
        tip = QLabel("点“编辑路线”粘贴游戏地址（空地(571,243)空地(566,240)）或坐标（571,243; 566,240），"
                     "再像地点优先级一样上移/下移/置顶/置底调整顺序。\n"
                     "已完成段数会保存；队伍停在任意位置（主城或城外）都会恢复当前段继续铺。\n"
                     "到点判定只看角标状态（停留），途中遇敌/战斗会暂停推进、等现场恢复。")
        tip.setObjectName("notice")
        tip.setWordWrap(True)
        inner.addWidget(tip)
        layout.addWidget(f)

    def add_pave_route(self):
        names = {self.pave_table.item(r, 1).text() for r in range(self.pave_table.rowCount())
                 if self.pave_table.item(r, 1)}
        number = self.pave_table.rowCount() + 1
        while f"铺路 {number}" in names:
            number += 1
        self.insert_pave_route(PaveRoute(name=f"铺路 {number}", points=[]))

    def insert_pave_route(self, route):
        t = self.pave_table
        r = t.rowCount()
        t.insertRow(r)
        t.setRowHeight(r, 44)
        cell_check(t, r, 0, route.enabled)
        item(t, r, 1, route.name)
        t.setCellWidget(r, 2, combo([(f"{i} 队", f"main:{i}") for i in range(1, 6)], route.team))
        item(t, r, 3, pave_summary(route.points) if route.points else "（尚未添加坐标）", False)
        item(t, r, 4, str(len(route.points)), False)
        editor = QPushButton("编辑路线 ✎")
        editor._points = [list(p) for p in route.points]
        editor.setToolTip("粘贴游戏地址、调整顺序、删除坐标点")
        editor.clicked.connect(lambda _, b=editor: self.edit_pave_route(b))
        t.setCellWidget(r, 5, editor)

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
            if self.pave_table.cellWidget(r, 5) is b:
                item(self.pave_table, r, 3, pave_summary(points), False)
                item(self.pave_table, r, 4, str(len(points)), False)
                break
        self.saved_label.setText("铺路路线已修改 · 待保存")

    def remove_pave_row(self):
        t = self.pave_table
        if not self.can_apply_addresses():
            return
        for r in sorted({i.row() for i in t.selectedIndexes()}, reverse=True):
            t.removeRow(r)

    def read_pave_route(self, r):
        t = self.pave_table
        name = (t.item(r, 1).text() if t.item(r, 1) else "").strip()
        team = t.cellWidget(r, 2).currentData()
        editor = t.cellWidget(r, 5)
        points = [list(p) for p in (editor._points if editor else [])]
        return PaveRoute(name=name, team=team, points=points, enabled=t.cellWidget(r, 0).isChecked(), **getattr(editor, "_options", {}))

    def make_recruitment(self):
        layout = self.page("征兵与补粮", "主城自动补兵不足时使用备用地点。不会把预计主城预备兵加进城外兵力提前撤回。")
        layout = self.scroll_settings(layout)
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
        tip = QLabel("已为队伍指定备用点时优先遵守该设置。补兵点仍校验实际名称和调动菜单，确保能进驻征兵。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        inner.addWidget(tip)
        layout.addWidget(f)
        f, inner = card("游戏隐藏显示", "游戏右下角小眼睛：眼睛=元素可见，带斜杠=隐藏已开启。勾选后每次开始任务前确保它是开启状态（带斜杠），全程保持。")
        self.hide_before_run = QCheckBox("开始任务前打开游戏隐藏显示（小眼睛斜杠）")
        self.hide_before_run.setChecked(self.profile.hide_before_run)
        inner.addWidget(self.hide_before_run)
        tip = QLabel("为什么打开：地图上没有部队模型、名字和建筑名，点坐标不会误选到别的队伍，也更快。任务结束不恢复。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        inner.addWidget(tip)
        layout.addWidget(f)
        f, inner = card("低粮补给", "低于触发库存进入补粮流程，达到恢复库存才恢复出击；每个 7 时刷新周期限制屯田次数。")
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
        self.farm_position = QLabel("通过城务进入本人城内，在领土→开垦列表自动查找农田，定位后核对粮食收益和次数；无需填写地址或标定农田位置。")
        self.farm_position.setWordWrap(True)
        self.farm_position.setObjectName("muted")
        inner.addWidget(self.farm_position)
        layout.addWidget(f)
        self.rates = QPlainTextEdit()
        self.rates.setReadOnly(True)
        self.rates.setFixedHeight(90)
        self.rates.setPlainText("征兵池与快慢速按游戏实读；预计时间不能替代真实兵力。")
        f, inner = card("主城征兵速度", "只读当前兵池信息，不修改加速或补兵开关")
        inner.addWidget(self.rates)
        inner.addWidget(button("读取征兵速度", lambda:self.start("rates")))
        layout.addWidget(f)

    def make_dailies(self):
        layout = self.page("签到与日常", "按账号记录每日完成状态；签到、月卡和寻访当天完成后，重启也不重复执行。")
        layout = self.scroll_settings(layout)
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

    def make_connection(self):
        layout = self.page("连接与配置", "切换电脑或账号时重新发现实例、校验主城。队伍与目标配置可以导入导出。")
        layout = self.scroll_settings(layout)
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
        self.main_address = QLineEdit(self.point_address(self.profile.main_city))
        self.main_address.setPlaceholderText("粘贴本人主城地址：主公名字(X,Y)")
        mainrow.addWidget(self.main_address, 1)
        mainrow.addWidget(button("应用主城地址", self.apply_main_address))
        mainrow.addWidget(button("复制主城地址", self.copy_main_address))
        self.main_address.returnPressed.connect(self.apply_main_address)
        inner.addLayout(mainrow)
        self.account_label = QLabel("尚未校验" if not self.profile.main_city else f"{self.profile.account} · 主城 ({self.profile.main_city.x},{self.profile.main_city.y})")
        self.account_label.setObjectName("muted")
        inner.addWidget(self.account_label)
        note = QLabel("首次使用：①选择 MuMu 安装目录内 nx_main/mumu-cli.exe；②自动发现实例，按名称和编号选中自己的游戏；"
                      "③手动启动该实例并登录游戏，点击“发现游戏包名”；④校验账号与主城；⑤先监控，再开启任务。\n"
                      "推荐 Windows 10/11 x64 + MuMu 5（本机基准见使用说明），横屏 1920×1080、简体中文；"
                      "运行时可最小化 MuMu。切换账号请新建配置并重新校验。")
        note.setObjectName("muted")
        note.setWordWrap(True)
        inner.addWidget(note)
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
        layout = self.page("日志与诊断", "本次运行的完整过程与异常统一在这里查看，包含顶部进度记录。保留最近 2000 条，原始记录仍在会话目录。")
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
        note = QLabel("出征结果未确认时不再锁队等待人工核对：本轮停在该队，下一轮读取现场仍可出击就自动重派。"
                      "“重新核对主城命令”只用于清理异常退出后残留的在途记录；暂停和停止不撤军。")
        note.setObjectName("notice")
        note.setWordWrap(True)
        layout.addWidget(note)

    def fill_tables(self):
        self.observations.clear()
        self.decisions.clear()
        self.team_activity.clear()
        self.metrics["本次出征"].setText("0")
        self.team_table.setRowCount(len(self.profile.teams))
        self.live.setRowCount(len(self.profile.teams))
        for r,t in enumerate(self.profile.teams):
            item(self.team_table,r,0,t.display)
            cell_check(self.team_table,r,1,t.merit)
            cell_check(self.team_table,r,2,t.bandits)
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
            item(self.team_table,r,7,t.target_group)
            item(self.team_table,r,8,t.backup)
            self.team_table.setCellWidget(r,9,combo([("武勋优先","merit"),("讨贼优先","bandits")],t.priority))
            for c,val in enumerate((t.display,"待识别","—","—","—","尚未读取")):
                item(self.live,r,c,val,False)
        for t,points in ((self.target_table,self.profile.targets),(self.backup_table,self.profile.backups)):
            t.setRowCount(0)
            for p in sorted(points, key=lambda p:p.priority):
                self.insert_point(t,p)
        self.pave_table.setRowCount(0)
        for route in self.profile.pave_routes:
            self.insert_pave_route(route)

    def insert_point(self,t,p):
        r = t.rowCount()
        t.insertRow(r)
        t.setRowHeight(r,44)
        cell_check(t,r,0,p.enabled)
        item(t,r,1,p.name)
        t.item(r,1).setData(Qt.ItemDataRole.UserRole,asdict(p))
        t.setCellWidget(r,2,spin(p.x,0,3000))
        t.setCellWidget(r,3,spin(p.y,0,3000))
        if t is self.target_table:
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
        if t is self.target_table:
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
        editor = QLineEdit()
        editor.setPlaceholderText("粘贴游戏地址，可连续多个：城池(100,200)营帐(120,210)")
        editor.setMaxLength(10000)
        row.addWidget(editor, 1)
        row.addWidget(button("识别并添加", lambda:self.add_shared_points(target)))
        editor.returnPressed.connect(lambda:self.add_shared_points(target))
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
        editor = self.target_address if target is self.target_table else self.backup_address
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
        check_column = 1 if target is self.target_table else 5
        addresses = [SharedAddress(target.item(r,check_column).text().strip() or target.item(r,1).text(), target.cellWidget(r,2).value(), target.cellWidget(r,3).value()).text() for r in rows]
        QApplication.clipboard().setText("\n".join(addresses))
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
            self.metrics["账号与主城"].setText(f"{account}\n({address.x},{address.y})")
            self.status.setText("主城地址已填入，保存后生效；出征时仍核对现场归属")
            return True
        except ValueError as exc:
            self.status.setText(str(exc))
            return False

    def copy_main_address(self):
        try:
            address = single_address(self.main_address.text())
            QApplication.clipboard().setText(address.text())
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
            item(t,r,6 if t is self.target_table else 4,r+1,False)

    def collect(self):
        for target, editor in ((self.target_table,self.target_address),(self.backup_table,self.backup_address)):
            if editor.text().strip():
                self.add_shared_points(target, strict=True)
        p = Profile.from_dict(self.profile.to_dict())
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
            t.label = self.team_table.item(r,0).text().strip()
            t.merit, t.bandits = self.team_table.cellWidget(r,1).isChecked(), self.team_table.cellWidget(r,2).isChecked()
            t.min_mode, t.minimum = self.team_table.cellWidget(r,3).currentData(), self.team_table.cellWidget(r,4).value()
            t.stamina, t.supply = self.team_table.cellWidget(r,5).value(), self.team_table.cellWidget(r,6).value()
            t.target_group, t.backup = self.team_table.item(r,7).text().strip(), self.team_table.item(r,8).text().strip()
            t.priority = self.team_table.cellWidget(r,9).currentData()
        p.targets, p.backups = [], []
        for table_, points in ((self.target_table,p.targets),(self.backup_table,p.backups)):
            for r in range(table_.rowCount()):
                points.append(self.read_point(table_,r))
        p.pave_routes = [self.read_pave_route(r) for r in range(self.pave_table.rowCount())]
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
        for page in range(1,7):
            self.stack.widget(page).setEnabled(False)
        if command in ("run", "scan"):
            self.observations.clear()
            self.decisions.clear()
            self.team_activity.clear()
            self.metrics["本次出征"].setText("0")
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
        for page in range(1,7):
            self.stack.widget(page).setEnabled(True)
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
            self.profile.account = value["account"]
            self.profile.main_city = Point(**value["main_city"])
            self.main_address.setText(self.point_address(self.profile.main_city))
            self.account_label.setText(f"{value['account']} · 主城 ({value['main_city']['x']},{value['main_city']['y']})")
            self.metrics["账号与主城"].setText(self.account_label.text())
            # 不读取尚未保存的其它表单，校验只更新账号字段。
            saved = self.collect()
            if self.account and self.manager:
                self.manager.store.save_profile(self.account_id, saved)
                self.manager.update_account(self)
            else:
                atomic_json(self.config_path,saved.to_dict())
        elif kind == "observation":
            self.observations[value["key"]] = value
            self.team_activity.pop(value["key"], None)
            self.decisions.pop(value["key"], None)
            self.refresh_live()
            self.last_scan.setText(f"最新读数 · {datetime.now():%H:%M:%S}")
        elif kind == "decision":
            self.decisions[value["team"]] = value["reason"]
            self.refresh_live()
            team_name = next((p.display for p in self.profile.teams if p.key == value['team']),value['team'])
            self.append_log({"time":datetime.now().strftime("%H:%M:%S"),"level":"INFO",
                             "message":f"{team_name} · {value['reason']}"})
        elif kind == "dispatch":
            self.metrics["本次出征"].setText(str(int(self.metrics["本次出征"].text())+1))
            self.team_activity[value["team"]] = {"state":value.get("state","confirmed"),"reason":f"已确认{value.get('action','出征')} → {value['target']}；继续监控"}
            self.refresh_live()
            team_name = next((p.display for p in self.profile.teams if p.key == value['team']),value['team'])
            self.append_log({"time":datetime.now().strftime("%H:%M:%S"),"level":"INFO", "message":f"{team_name} · {value.get('action','出征')} → {value['target']} 已确认"})
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
            self.status.setText(f"铺路 · {value.get('route','')} · 已完成 {value.get('done',0)}/{value.get('total',0)} 段")
            self.append_log({"time":datetime.now().strftime("%H:%M:%S"),"level":"INFO",
                             "message":f"铺路路线 {value.get('route','')}：{value.get('done',0)}/{value.get('total',0)} 段，"
                                       f"状态 {value.get('status','')}"})
        elif kind == "error":
            self.nav.setCurrentRow(7)

    def refresh_live(self):
        self.refresh_resource_age()
        for r,t in enumerate(self.profile.teams):
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
            labels = {**STATES,"confirming":"核对出发","confirmed":"已确认出发","return_confirming":"核对回城","return_confirmed":"回城已受理","fighting":"战斗中","retrying":"未确认 · 自动重试"}
            shown = display_label(obs.get("marker_label"), state, labels)
            values = [f"{t.display} · {obs['identity']}" if obs.get("identity") else t.display, shown, f"{obs['strength']}/{obs['maximum']}" if obs.get("strength") is not None else "待识别", f"{obs.get('stamina') if obs.get('stamina') is not None else '—'} / {obs.get('supply') if obs.get('supply') is not None else '—'}", f"{obs.get('pool','未知')} {obs['reserve']}/{obs['capacity']}" if obs.get("reserve") is not None else obs.get("pool","未知"), reason]
            for c,val in enumerate(values):
                item(self.live,r,c,val,False)
                self.live.item(r,c).setToolTip(str(val))
            color = "#b87422" if state in ("unknown","wounded","retrying") else "#2f6bbb" if state in ("marching","returning","confirming","return_confirming") else "#248565"
            self.live.item(r,1).setForeground(QBrush(QColor(color)))
            if obs.get("observed_at"):
                age = max(0,round(time.time()-obs["observed_at"]))
                for c in (2,3,4):
                    self.live.item(r,c).setToolTip(f"{values[c]}\n数值读取于 {datetime.fromtimestamp(obs['observed_at']):%H:%M:%S}，约 {age} 秒前")

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

    def export_diagnostics(self):
        from .diagnostics import export_diagnostics
        default = self.data_dir / "feedback" / f"三谋诊断-{datetime.now():%Y%m%d-%H%M%S}.zip"
        path, _ = QFileDialog.getSaveFileName(self, "导出问题诊断包（含游戏截图、昵称、坐标及本机路径，请核对后再发送）", str(default), "诊断包 (*.zip)")
        if not path:
            return
        try:
            export_diagnostics(self.data_dir, Path(path))
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
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
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
        screen = self.screen()
        available = screen.availableGeometry() if screen else None
        width = max(900, available.width()-40) if available else 1440
        height = max(500, available.height()-60) if available else 960
        self.resize(min(1440, width), min(960, height))
        self.setMinimumSize(min(1120, width), min(820, height))
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
        if self.store.migration_error:
            self.current_window.status.setText(self.store.migration_error)

    @property
    def current_window(self):
        return self.windows[self.account_stack.currentIndex()]

    def _append_account(self, account):
        window = Window(self.data_dir, account=account, manager=self)
        window.setMinimumSize(900, 500)
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

