from __future__ import annotations

import json
import re
import time
import math
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from .journal import Journal, cycle_key
from .evidence import EvidenceStore, EvidenceLimitError
from .models import (ACTION_NAMES, Observation, Point, Profile, TeamPolicy, bandit_decision,
                     observation_expired, point_label)
from .team_markers import HUD_OFFSETS, SEARCH_DX, TeamMarkerReader
from .team_states import LEGACY_STATE, can_dispatch, marker_with_home_evidence, label as state_label
from .transport import Device, Stopped, TransportUnavailable
from .vision import Vision, clock_seconds, pairs, quantity


def _vcall(vision, name, default, *args):
    """测试替身可能没有新增的视觉方法：缺方法时用保守默认值。"""
    method = getattr(vision, name, None)
    return default if method is None else method(*args)


def stays_at_backup(phase, home, coord, city):
    """自然征兵的队伍只在确认回到主城坐标后，才解除“城外征满”阶段。

    2026-11-04 实机：涪县征兵时角标读到“空闲”，LEGACY_STATE 把空闲一律当 home，
    程序据此清了 backup 阶段并从征兵点直接拉线。这里以发兵记录为准：
    只要没确认回到主城，就按城外处理（交给 decide 走“够兵→回城”）。
    """
    if phase != "backup" or home:
        return False
    return city is None or coord != city


def configured_backup_points(profile):
    points = [profile.merit_backup] if profile.merit_backup is not None else []
    points += list(profile.line_backups.values())
    points += [point for point in profile.backups if all(point is not other for other in points)]
    return points


class GameError(RuntimeError):
    pass


class RecognitionUnavailable(GameError):
    """画面或导航控件暂不可读；本轮停止输入，冷却后从现场重新开始。"""


class ObservationExpired(GameError):
    """队伍观测失效，须退出目标候选循环；不表示当前目的地不可用。"""


class FarmingUnavailable(GameError):
    """A known, safely recoverable lookup failure; never permits a new spend."""


class NetworkRecovered(GameError):
    """当前操作上下文失效，Worker必须重新读取，不能续用重连前观测。"""


class NavigationRecovered(NetworkRecovered):
    """意外页面已返回沙盘；沿用上下文失效处理，禁止续用之前的观测。"""


class PaveUnknown(GameError):
    """铺路命令已经点击提交，但结果未确认；订单保持待核对，不能重发。"""


class PavePointUnavailable(GameError):
    """当前铺路点没有“攻占”指令（常见于别人已铺到该点）。

    用户 2026-11-10：这种情况跳过该点，直接攻占下一个点继续铺。
    """


class LineRefillUnavailable(GameError):
    """拉线：兵力低于出击下限且当前补不动（没有一键补兵按钮 / 点完区块归零）。

    用户 2026-11-12：按钮不存在比读数字更稳，是“当前无法快速征兵”的硬判据，
    必须走回城/停手兜底，绝不能当成“点了没反应”去重试。
    """


class DispatchUnconfirmed(GameError):
    """提交后没读到受理证据。

    用户 2026-11-03 口径：这不算“失败”，而是“这一条指令在当前现场没成”。
    队伍仍然停在原地（可派）时，按配置顺序改试下一条；队伍已经忙起来就本轮停手，
    下一轮读取现场后自动重试。两条路都不会把队伍锁死。
    """

    def __init__(self, message, action="", busy=True, marker=None, confidence=None, page="", status=""):
        super().__init__(message)
        self.action = action          # 没受理的是哪条指令
        self.busy = busy              # 提交后是否已确认队伍忙起来（忙＝不再补发）
        self.marker = marker          # 提交后读到的角标状态（可为 None）
        self.confidence = confidence  # 该角标的置信度
        self.page = page              # 提交后读到的页型
        self.status = status          # 提交后读到的卡状态（出征页）


class TargetNotSelectable(GameError):
    """坐标导航后，屏幕中心选不中要求的格子（常见于目标被建筑/部队模型盖住）。

    2026-11-19 实机：拉线目标 空地(740,683) 被太虚民港(741,683) 的建筑模型盖住，
    点中心永远选中港口；旧代码只在日志里写“坐标不符”，下一轮又从头再来，
    形成每 3 分钟一次的无限重试。这里用独立异常让上层做有界重试/结束该队，
    而不是把“目标不可选中”当成一次普通可重试错误。
    """

    def __init__(self, message, requested=None, selected=None):
        super().__init__(message)
        self.requested = requested
        self.selected = selected


class TeamAbsent(GameError):
    """本赛季该主队槽位尚未拥有；不读取、不派兵，也不是故障。"""


class RallyJoinRejected(GameError):
    """集结已确认不可加入（如满员）且奖励次数未被消耗；可换其他集结有限重试。"""


# 提交未读到受理证据时，同一出征页最多连续提交几次。重复派出只多一次行军，
# 漏派才会让整队空转，所以宁可重试。
DISPATCH_ATTEMPTS = 3
DISPATCH_PROMPT_PAGES = ("dispatch_confirm", "enemy_warning", "pave_stamina_prompt", "supply_prompt")


class ActionUnavailable(GameError):
    """仅用于提交前已确认不可用的指令；允许尝试同目标后续指令。"""

    def __init__(self, message, action):
        super().__init__(message)
        self.action = action


class Game:
    # 启动时从部队列表确认的主队槽位；None=尚未确认（只用于启动读取第1队）。
    # 放在类上，测试或旧工具用 __new__ 构造 Game 时也不会缺字段。
    active_slots = None
    # 本次是否有实际刷武勋队；False 时粮食低也不拦讨贼/铺路。
    food_gate = True
    # 本次运行是否进过城内/征兵所：游戏会在进出主城后自动关掉隐藏，回沙盘要重核。
    city_visited = False
    # 地图现场（详见 __init__）：类上也给默认值，__new__ 构造的旧测试/工具不会缺字段。
    view_point = None
    view_epoch = 0
    view_at = 0.0
    VIEW_TTL = 45.0
    # 只读库存刷新间隔。每次刷新要截图 + 页面识别 + 读数，和派兵抢同一个 OCR 线程；
    # 5 秒一次时等待期间几乎一直在识别，反而拖慢真正的游戏操作。
    RESOURCE_INTERVAL = 10
    RESOURCE_MAX_AGE = 120
    # 出征页五张卡的横向中心（实机实测间距 250）与卡片中心高度。
    CARD_X = (135, 385, 635, 885, 1135)
    CARD_Y = 880
    # 2026-11-08 实机：出征卡槽不是固定五列，而是按实际队伍数整体居中。
    # 两队在 x≈519/768（卡宽 250、间距 250），五队仍是 x≈142..1142。
    CARD_AREA_CENTER = 642
    CARD_SPACING = 250
    # 出征按钮位置（实机实测）：1540–1895 × 932–1036。
    SUBMIT_POINT = (1717, 984)
    # 配置动作 -> 菜单图标文件名。
    ICON_FOR_ACTION = {"attack": "siege", "occupy": "occupy", "guard": "guard", "march": "march"}
    # 右上角小眼睛与“隐藏”设置面板。面板七项固定，勾选后才能避免战术指令/同盟计划
    # 盖住地图（用户 2026-11-19 截图确认）。最后一项是两行文字，复选框比文字中心低。
    HIDE_EYE = (1583, 142)
    HIDE_OPTIONS = (
        "隐藏部队模型", "隐藏部队名字", "隐藏行军线", "隐藏建筑名字",
        "隐藏战术指令", "隐藏同盟计划", "最低视角时自动触发设置",
    )
    HIDE_OPTION_LAST_Y = 818
    HIDE_OPTION_X = 1350

    def __init__(self, device: Device, vision: Vision, profile: Profile, journal: Journal, folder: Path, emit):
        self.d, self.v, self.p, self.j = device, vision, profile, journal
        self.folder, self.emit = folder, emit
        self.folder.mkdir(parents=True, exist_ok=True)
        # 每个槽位上一次确认的角标状态与时刻：用于“无角标”防抖（见 read_team_marker）。
        self.marker_last = {}
        self.last_image = None
        self.last_image_at = None
        self._grain = None
        self.account_checked = False
        self.claim_login_rewards = False
        self.farming_retry_at = 0.0
        self.live_resources = False
        self.next_resource_check = 0.0
        self.resource_refresh_requested = None
        self.resource_observed_at = None
        self.network_attempts = []
        self.navigation_attempts = []
        # 地图现场：镜头此刻居中、并且已经核对通过的地块坐标；None＝不知道镜头在哪。
        # 规则（2026-11-18 实机根因）：镜头是共享可变状态，读队点头像、缩放、拖图、
        # 出城都会改它。所以只有“刚把镜头放过去并核对过”才能着手里的结论，
        # 任何移动镜头的动作都必须 drop_view()。宁可多走一次坐标导航，
        # 也不能拿旧结论往屏幕正中点——点错会打开整屏弹层，退不出来。
        self.view_point = None
        self.view_epoch = 0
        self.view_at = 0.0
        # 本赛季实际拥有的主队槽位；None 表示尚未检测，不能据此过滤配置。
        self.active_slots = None
        # 是否有实际刷武勋队；Worker 每轮写入，讨贼决定据此忽略粮食门槛。
        self.food_gate = True
        # 本次运行是否进过城内/征兵所：游戏会在进出主城后自动关掉隐藏，回沙盘要重核。
        self.city_visited = False
        # 阵容名缓存：同一次任务运行内每队只取一次（见 _remember_identity）。
        self._identity_cache = {}

    def log(self, message, level="INFO"):
        self.emit("log", {"time": datetime.now().strftime("%H:%M:%S"), "level": level, "message": message})

    @property
    def grain(self):
        # 粮食是账号共用库存。页面暂时遮挡时保留最近成功读数，但不无限沿用旧值。
        observed = self.resource_observed_at
        if observed is None or not 0 <= time.time() - observed <= self.RESOURCE_MAX_AGE:
            return None
        return self._grain

    @grain.setter
    def grain(self, value):
        self._grain = value
        self.resource_observed_at = time.time() if value is not None else None

    def capture(self, name="", publish=False):
        image = self.d.capture()
        captured_at = time.time()
        self.last_image = image
        self.last_image_at = captured_at
        # 截图只取帧；库存由沙盘检查和空闲等待刷新。导航中插入库存 OCR
        # 曾让三队读取超过观测期限，随后又因过期重新读取，形成空转。
        self.save_image(image, name, publish)
        return image

    def save_image(self, image, name="", publish=False):
        if name:
            name = re.sub(r"[^\w.-]", "_", name)
            if not hasattr(self, "evidence_store"):
                self.evidence_store = EvidenceStore(self.folder)
            store = self.evidence_store
            if store.due(name):
                try:
                    ok, encoded = cv2.imencode(".png", image)
                    if not ok:
                        raise OSError("截图编码失败")
                    path = store.write(name, encoded.tobytes())
                except (OSError, EvidenceLimitError) as exc:
                    path = None
                    self.log(f"截图未保存，继续运行：{exc}", "WARN")
                if path:
                    self.emit("evidence", str(path))
        if publish:
            self.emit("preview", image)

    def resources_due(self):
        return time.monotonic() >= self.next_resource_check or bool(
            self.resource_refresh_requested and self.resource_refresh_requested.is_set())

    def update_grain(self, image, captured_at=None):
        value = self.v.grain(image)
        if value is not None:
            self._grain = value
            self.resource_observed_at = captured_at or (self.last_image_at if image is self.last_image else None) or time.time()
        self.emit("grain", self.grain)
        self.emit("resource_status", {"visible": value is not None,
                  "observed_at": self.resource_observed_at, "checked_at": time.time()})

    def refresh_resources(self, image=None, paused=False, captured_at=None):
        """只读当前画面，绝不为更新库存退出用户页面或点击游戏。"""
        if self.resource_refresh_requested:
            self.resource_refresh_requested.clear()
        self.next_resource_check = time.monotonic() + self.RESOURCE_INTERVAL
        checkpoint = self.v.checkpoint
        detector = getattr(self.v.ocr, "text_det", None)
        detector_limit = getattr(detector, "limit_side_len", None)
        try:
            if paused:
                self.v.checkpoint = self.d.control.passive_checkpoint
            if image is None:
                image = self.d.capture(passive=True) if paused else self.d.capture()
                captured_at = time.time()
            self.last_image = image
            self.last_image_at = captured_at or time.time()
            page = self.v.page(image)
            if page == "maintenance":
                self.save_image(image, "server-maintenance", True)
                raise GameError("游戏服务器维护，已停止任务；维护结束后手动开始")
            if page == "blocked":
                self.save_image(image, "login-blocked", True)
                raise GameError("游戏提示异地登录或断线，已停止操作")
            if page in ("sandbox", "city"):
                # 暂停检查可能由页面低尺寸识别中的checkpoint进入，金额仍使用原精度。
                if detector_limit is not None:
                    detector.limit_side_len = self.v.precise_detector_limit
                self.update_grain(image, captured_at)
            else:
                self.emit("resource_status", {"visible": False,
                          "observed_at": self.resource_observed_at, "checked_at": time.time()})
            self.emit("preview", image)
        finally:
            self.v.checkpoint = checkpoint
            if detector_limit is not None:
                detector.limit_side_len = detector_limit
            # 慢帧不连续追赶，避免截图/OCR忙循环。
            self.next_resource_check = time.monotonic() + self.RESOURCE_INTERVAL

    def passive_refresh(self, paused=False):
        if self.resources_due():
            self.refresh_resources(paused=paused)

    def reconnect_game(self, image):
        self.d.control.checkpoint()
        prompt, button = self.v.connection_prompt(image)
        if prompt != "reconnect" or button is None:
            self.save_image(image, "reconnect-refused", True)
            raise GameError("没有明确的网络断线正文和重连按钮，停止操作")
        now = time.monotonic()
        self.network_attempts = [t for t in self.network_attempts if now-t < 600]
        if len(self.network_attempts) >= 2:
            self.save_image(image, "reconnect-limit", True)
            raise GameError("10分钟内网络重连已尝试两次，停止操作")
        self.network_attempts.append(now)
        self.account_checked = False
        self.grain = None
        self.save_image(image, "network-disconnected", True)
        self.emit("status", "网络断线 · 点击一次重连并等待，不重放任务")
        self.d.tap(button.x, button.y, .8)
        end = time.monotonic() + 30
        known = {"sandbox", "city", "team", "overview", "deployment", "monthly", "monthly_claimed",
                 "worldmap", "city_buildings", "affairs", "activity", "welfare", "visit", "visit_result", "territory",
                 "war", "rally", "reserve", "drill", "campaign", "dismissible_overlay", "channel_menu", "war_summary", "chat", "guide", "alliance", "bandit_assist_confirm", "navigable_page"}
        while time.monotonic() < end:
            current = self.capture()
            page = self.v.page(current)
            if page == "blocked":
                self.save_image(current, "login-blocked", True)
                raise GameError("重连后提示异地登录或需要重新登录，停止操作")
            if page in known:
                self.save_image(current, "network-restored", True)
                self.log("游戏网络重连已返回已知页面；重新读取账号和队伍，保留未确认记录")
                return
            self.d.control.sleep(1)
        self.save_image(current, "network-reconnect-timeout", True)
        raise GameError("点击一次重连后仍未恢复已知游戏页面，停止操作")

    def close_panel(self, image=None):
        image = self.capture() if image is None else image
        page = self.v.page(image)
        point = self.v.navigation_close_point(image, page)
        if point is None:
            self.save_image(image, "close-control-unconfirmed", True)
            raise RecognitionUnavailable(f"{page} 页面关闭按钮暂未识别")
        self.d.tap(*point, .6)

    def dismiss_overlay(self, image):
        point = self.v.dismiss_point(image)
        if point is None:
            self.save_image(image, "dismiss-control-unconfirmed", True)
            raise RecognitionUnavailable("弹层关闭提示暂未识别，等待重读")
        self.d.tap(*point, .6)

    def resolve_enemy_warning(self, image):
        """强敌预警弹窗：勾选「该等级今日不再提示」→ 等倒计时走完 → 点「继续进攻」。

        用户 2026-11-12 口径（R13/R14）：程序设定好的安排也继续进攻，并勾选今日不再提示。
        倒计时未走完时按钮是灰的点不动，必须先等待，不能当成“点了没反应”重试。
        """
        info = self.v.enemy_warning(image)
        if info is None:
            raise RecognitionUnavailable("强敌预警弹窗暂未读清，等待重读")
        box = info.get("checkbox")
        if box:
            state = self.v.checked(image, *box)
            if state is False:
                self.d.tap(*box, .3)
                image = self.capture()
                state = self.v.checked(image, *box)
            if state:
                self.log("强敌预警：已勾选“该等级今日不再提示”")
            else:
                self.log("强敌预警：未能确认“该等级今日不再提示”已勾选，仍继续进攻", "WARN")
        else:
            self.log("强敌预警：未定位“该等级今日不再提示”复选框，仍继续进攻", "WARN")
        wait = info.get("countdown") or 0
        if wait:
            self.emit("status", f"强敌预警 · 等待继续进攻倒计时 {wait} 秒")
            self.d.control.sleep(min(wait + .6, 8))
        self.save_image(image, "enemy-warning-continue", True)
        self.d.tap(*info["continue_point"], 1.0)

    def clear_dispatch_prompt(self, image, policy=None, target=""):
        """按识别到的出征提醒处理一层，并返回页型。"""
        page = self.v.page(image)
        if page == "dispatch_confirm":
            info = self.v.confirm_dialog(image)
            if info is None:
                return None
            self.emit("status", "出征二次确认 · 选择继续出征")
            self.log(f"出征二次确认（{info['kind']}）· 按无人值守口径选择继续出征")
            self.save_image(image, "dispatch-confirm-continue", True)
            self.d.tap(*info["continue_point"], .9)
            return page
        if page == "enemy_warning":
            self.resolve_enemy_warning(image)
            return page
        if page == "pave_stamina_prompt":
            return page if self.resolve_pave_stamina_prompt(image) else None
        if page == "supply_prompt":
            return page if self.handle_supply_prompt(image, policy, target) else None
        return None

    def _settle_dispatch_prompts(self, image, policy, target):
        # 一次出征可以接连出现体力、补给、加点、战法和强敌提醒。每次处理完重新截图。
        for _ in range(5):
            page = self.clear_dispatch_prompt(image, policy, target)
            if page is None:
                break
            image = self.capture_clear(f"dispatch-{page}-answer", True)
        return image

    def resolve_pave_stamina_prompt(self, image):
        """「本次出征中铺路地块将消耗体力，是否确认出征？」：勾选今日不再提示→确定。

        用户 2026-11-15 明确要求：这个页面要勾上“今日登录不再提示”再继续出征。
        勾选状态必须复读确认；读不到复选框/勾选未确认时只记 WARN，仍按用户口径确认出征。
        返回是否真的处理了（识别不齐返回 False，不点击）。
        """
        info = _vcall(self.v, "pave_stamina_prompt", None, image)
        if info is None:
            return False
        self.save_image(image, "pave-stamina-prompt", True)
        self.emit("status", "出征提示 · 勾选“今日登录不再提示”并继续出征")
        self._confirm_dispatch_reminder(image, info, "铺路体力提示")
        self.log("铺路体力提示：已确认出征，继续核对出发", "INFO")
        return True

    def _confirm_dispatch_reminder(self, image, info, description):
        label = info.get("remember_label")
        box = _vcall(self.v, "checkbox_before", None, image, label) if label is not None else None
        if box is None:
            self.log(f"{description}：未定位不再提示复选框，本次只确认", "WARN")
        else:
            state = _vcall(self.v, "checked", None, image, *box)
            if state is False:
                self.d.tap(*box, .35)
                image = self.capture()
                state = _vcall(self.v, "checked", None, image, *box)
            if state:
                self.log(f"{description}：已勾选“{label.text}”", "INFO")
            else:
                self.log(f"{description}：未能确认“{label.text}”已勾选，本次只确认", "WARN")
        self.d.tap(*info["confirm_point"], 1.2)

    def clear_map_selection(self, image=None, expected_page="sandbox"):
        # 往返拖动会平移镜头：现场结论立即作废。
        self.drop_view()
        image = self.ensure_sandbox(refresh_grain=False) if image is None else image
        if self.v.page(image) != expected_page or expected_page not in ("sandbox", "city"):
            raise GameError("未确认游戏地图，不拖动地图")
        self.d.swipe(1580, 575, 1480, 575, 250)
        # 两次手势之间也重判，页面变化时不继续输入。
        self.wait_transition(self.capture(), expected=(expected_page,))
        self.d.swipe(1480, 575, 1580, 575, 250)
        return self.wait_transition(self.capture(), expected=(expected_page,))

    def deselect_unknown_page(self):
        """未知页面（例如战斗/攻城特效 + 建筑详情）先按用户确认的做法取消选中。

        2026-11-13 实机：全屏“拆除 全体拉线”特效时页面识别为 unknown，左右滑动一次
        （地图往返拖动）后建筑详情关闭、页面恢复 sandbox；特效文字本身还在，不影响读队。
        只在重判为沙盘时返回 True，其他情况返回 False，由调用方继续等待。
        """
        # 地图往返拖动会平移镜头：现场结论立即作废。
        self.drop_view()
        self.d.swipe(1580, 575, 1480, 575, 250)
        self.d.control.sleep(.25)
        image = self.capture()
        if self.v.page(image, verify=False) == "sandbox":
            return True
        self.d.swipe(1480, 575, 1580, 575, 250)
        self.d.control.sleep(.25)
        image = self.capture()
        return self.v.page(image, verify=False) == "sandbox"

    def open_visit(self):
        image = self.ensure_sandbox()
        button = self.v.find(image, ["寻访"], (1680, 890, 1915, 1065), exact=True)
        if button is None:
            raise GameError("寻访入口未识别，不点击旧位置")
        self.d.tap(button.x, button.y, .8)
        image = self.capture("visit-open", True)
        if self.v.page(image) != "visit":
            raise GameError("没有进入已确认的寻访页面")
        return image

    def capture_clear(self, name="", publish=False, tries=3):
        """截一帧并确认已从云雾过渡恢复；过渡中重截，最多 tries 次。"""
        image = self.capture(name, publish)
        for _ in range(max(1, tries) - 1):
            if _vcall(self.v, "frame_clear", True, image):
                break
            self.emit("status", "画面还在云雾过渡 · 正在刷新")
            self.d.control.sleep(.25)
            image = self.capture()
        return image

    def capture_stable(self, tries=4):
        """稳定帧：连续两帧关键内容一致才采纳，过渡/动画帧在采集层丢弃。

        2026-11-21：页面切换动画中的瞬时帧曾是「部队页未打开」等误判的来源。
        在输入端等画面静止，而不是让下游去容错坏帧。
        """
        prev = self.capture()
        for _ in range(max(1, tries) - 1):
            self.d.control.sleep(.35)
            image = self.capture()
            a = cv2.resize(prev, (160, 90)).astype(np.int16)
            b = cv2.resize(image, (160, 90)).astype(np.int16)
            if float(np.abs(a - b).mean()) < 2.0:
                return image
            prev = image
        return prev


    def _hide_option_rows(self, image):
        """识别隐藏面板七行的复选框中心；面板没开或行读不全返回 None。"""
        if not self.v.hide_panel_visible(image):
            return None
        rows = []
        for label in self.HIDE_OPTIONS[:-1]:
            found = self.v.find(image, [label], (1260, 170, 1780, 790), exact=False)
            if found is None:
                return None
            rows.append((label, (self.HIDE_OPTION_X, round(found.y))))
        # 最后一项是两行文字，复选框中心固定低于文字中心；先确认这一段仍是
        # “最低视角/触发设置”，避免游戏新增选项后把固定坐标点到别的项上。
        tail = self.v.joined(image, (1360, 770, 1700, 880)).replace(" ", "")
        if "视角" not in tail and "触发" not in tail:
            return None
        rows.append((self.HIDE_OPTIONS[-1], (self.HIDE_OPTION_X, self.HIDE_OPTION_LAST_Y)))
        return rows

    def _ensure_hide_options(self, image):
        """把隐藏面板七项全部勾上；只对明确未勾选的项点击，读不清不动。

        返回 (image, checked, total)；total=0 表示面板/行未读全，未做任何切换。
        """
        rows = self._hide_option_rows(image)
        if not rows:
            self.log("隐藏设置面板未打开或选项行未读全，本轮不切换选项", "WARN")
            return image, 0, 0
        checked = 0
        for label, (x, y) in rows:
            state = self.v.hide_option_checked(image, x, y)
            if state is None:
                self.log(f"隐藏选项「{label}」勾选状态未读清，保持原样", "WARN")
                continue
            if not state:
                self.d.tap(x, y, .35)
                self.d.control.sleep(.25)
                image = self.capture()
                state = self.v.hide_option_checked(image, x, y)
                if state is True:
                    self.log(f"隐藏选项「{label}」已勾选", "INFO")
                else:
                    self.log(f"隐藏选项「{label}」点击后仍未确认勾选", "WARN")
            if state is True:
                checked += 1
        if checked == len(rows):
            self.log(f"隐藏选项已全部勾选（{checked}/{len(rows)}），战术指令/同盟计划不再遮挡地图", "INFO")
        else:
            self.log(f"隐藏选项核对 {checked}/{len(rows)}；未读清或未确认的项保持原样", "WARN")
        return image, checked, len(rows)

    def _restore_hide_on(self):
        """配置中断时尽力把隐藏重新打开，避免把地图留在全显示状态。"""
        try:
            image = self.capture()
            state, _ = self.v.hide_mode(image)
            if state == "open":
                self.d.tap(*self.HIDE_EYE, .6)
                self.d.control.sleep(.8)
        except Exception:
            pass

    def ensure_hide_on(self):
        """任务开始前打开游戏的“隐藏”显示，并勾选全部七项。

        2026-11-19 用户要求：以后隐藏时把“隐藏部队模型/名字、行军线、建筑名字、
        战术指令、同盟计划、最低视角自动触发”全部勾上，避免战术指令和同盟计划
        影响判断。实机流程：小眼睛开着时先点一下关掉隐藏，再长按小眼睛展开面板；
        逐项核对勾选后左右拖动收起面板，隐藏会自动重新打开。
        """
        try:
            image = self.capture()
            state, _scores = self.v.hide_mode(image)
            if state is None:
                # 启动瞬间可能在移镜头/过渡：等半秒重读一次，避免把“没读清”当成“已开”。
                self.d.control.sleep(.5)
                image = self.capture()
                state, _scores = self.v.hide_mode(image)
            if state is None:
                return None
            # 面板只能在隐藏关闭（小眼睛可见）时长按打开；隐藏开着时先关掉。
            if state == "hidden":
                self.d.tap(*self.HIDE_EYE, .6)
                self.d.control.sleep(.8)
                image = self.capture()
                state, _scores = self.v.hide_mode(image)
                if state != "open":
                    self.log("为配置隐藏选项而关闭隐藏时未确认小眼睛状态，本轮不切换", "WARN")
                    self._restore_hide_on()
                    return state
            self.log("配置游戏隐藏显示：长按小眼睛展开全部选项", "INFO")
            self.d.long_press(*self.HIDE_EYE, seconds=1.5)
            self.d.control.sleep(.6)
            image = self.capture()
            self.save_image(image, "hide-panel", True)
            checked = total = 0
            if self.v.hide_panel_visible(image):
                image, checked, total = self._ensure_hide_options(image)
            else:
                self.log("隐藏设置面板未打开，跳过选项勾选", "WARN")
            # 收起面板：实测左右拖动会关闭面板并自动把隐藏打开。
            self.d.swipe(900, 620, 980, 620, 300)
            self.d.control.sleep(.8)
            after = self.capture()
            self.save_image(after, "hide-on-after", True)
            state_after, _ = self.v.hide_mode(after)
            if self.v.hide_panel_visible(after):
                # 面板还开着：点小眼睛收起并打开隐藏。
                self.d.tap(*self.HIDE_EYE, .6)
                self.d.control.sleep(.8)
                after = self.capture()
                state_after, _ = self.v.hide_mode(after)
            if state_after == "open":
                self.d.tap(*self.HIDE_EYE, .6)
                self.d.control.sleep(.8)
                after = self.capture()
                state_after, _ = self.v.hide_mode(after)
            if state_after == "hidden":
                if total and checked == total:
                    self.log("游戏隐藏显示已开启（全部选项已核对），继续任务", "INFO")
                elif total:
                    self.log(f"游戏隐藏显示已开启；隐藏选项核对 {checked}/{total}，未读清项保持原样", "WARN")
                else:
                    self.log("游戏隐藏显示已开启；选项面板未核对", "WARN")
            else:
                self.log(f"打开隐藏后状态为 {state_after}，本轮继续执行", "WARN")
            return state_after
        except (Stopped, TransportUnavailable):
            # 停止/断连不是“没认出小眼睛”：原样传播，不能吞掉后当本轮继续。
            raise
        except GameError:
            raise
        except Exception as exc:
            self.log(f"打开游戏隐藏显示失败：{exc}；尝试恢复隐藏", "WARN")
            self._restore_hide_on()
            return None

    def ensure_sandbox(self, refresh_grain=True, recover_navigation=False):
        recovered = False
        back_steps = 0
        for attempt in range(5):
            image = self.capture()
            page = self.v.page(image)
            if page in ("city", "reserve", "camp_recruit", "camp_recruit_list"):
                # 进入城内/征兵所会让游戏自动取消隐藏；回到沙盘后由 Worker 重核小眼睛。
                self.city_visited = True
            if page == "sandbox":
                if refresh_grain and (self.grain is None or self.resources_due()):
                    self.update_grain(image)
                    self.next_resource_check = time.monotonic() + self.RESOURCE_INTERVAL
                if recovered:
                    self.save_image(image, "navigation-restored", True)
                    self.emit("navigation_recovery", {"result": "sandbox", "steps": back_steps})
                    raise NavigationRecovered("意外页面已返回沙盘，本批操作作废，重新核对账号和队伍")
                return image
            if page == "maintenance":
                self.save_image(image, "server-maintenance", True)
                raise GameError("游戏服务器维护，已停止任务；维护结束后手动开始")
            if page == "blocked":
                self.save_image(image,"login-blocked", True)
                raise GameError("游戏提示异地登录或断线，已停止操作")
            if page == "reconnect":
                self.reconnect_game(image)
                raise NetworkRecovered("网络已重连，本次操作作废，重新核对账号和队伍")
            if page == "navigable_page" or (recover_navigation and page in ("activity", "welfare", "visit", "visit_result")):
                button = self.v.navigation_back_point(image)
                now = time.monotonic()
                self.navigation_attempts = [t for t in self.navigation_attempts if now-t < 600]
                if button is None or back_steps >= 2 or len(self.navigation_attempts) >= 3:
                    self.save_image(image, "navigation-recovery-refused", True)
                    raise RecognitionUnavailable("意外页面返回控件未确认或恢复次数已达上限，等待重读")
                self.account_checked = False
                self.navigation_attempts.append(now)
                self.save_image(image, "navigation-unexpected", True)
                self.log(f"误入页面「{button['title']}」，点击已核对的返回箭头；返回后重读队伍", "WARN")
                self.emit("navigation_recovery", {"result": "back", **button})
                self.d.tap(button["x"], button["y"], .8)
                recovered = True
                back_steps += 1
            elif page == "monthly":
                self.monthly_popup(self.claim_login_rewards and self.account_checked and self.p.dailies.get("monthly", False))
            elif page == "bandit_assist_confirm":
                buttons = self.v.bandit_assist_prompt(image)
                if buttons is None:
                    raise RecognitionUnavailable("协助讨贼确认框暂未读清，等待重读")
                self.emit("status", "取消奖励次数耗尽后的协助确认 · 不自动协助")
                self.d.tap(buttons[0].x, buttons[0].y, .7)
            elif page in DISPATCH_PROMPT_PAGES:
                if self.clear_dispatch_prompt(image) is None:
                    raise RecognitionUnavailable("出征提醒暂未读清，等待重读")
            elif page == "monthly_claimed":
                # 关闭已核对的月卡结果面板，不再领取，也不进入购买入口。
                self.close_panel(image)
            elif page == "dismissible_overlay":
                # 按本帧明确的关闭提示收起，不把旧空白坐标当作关闭控件。
                self.emit("status", "关闭活动/奖励弹层 · 按已识别提示")
                self.dismiss_overlay(image)
            elif page == "channel_menu":
                self.emit("status", "关闭游戏渠道福利浮窗 · 不进入购买入口")
                point = self.d.channel_float_center(self.p.package)
                if point is None:
                    self.save_image(image, "channel-float-unconfirmed", True)
                    raise RecognitionUnavailable("渠道浮球的实际位置暂未读清，等待重读")
                self.d.tap(*point, delay=.5)
            elif page == "chat":
                point = self.v.chat_close_point(image)
                if point is None:
                    self.save_image(image, "chat-close-unconfirmed", True)
                    raise RecognitionUnavailable("聊天面板收起控件暂未读清，等待重读")
                self.emit("status", "收起聊天/乔妹问答，返回部队监控")
                self.d.tap(*point, delay=.7)
            elif page == "guide":
                self.close_panel(image)
            elif page == "war_summary":
                button = self.v.war_summary_button(image)
                if not button:
                    self.save_image(image, "war-summary-unconfirmed", True)
                    raise RecognitionUnavailable("战情摘要的批复按钮暂未读清，等待重读")
                self.emit("status", "关闭战情摘要 · 点击批复")
                self.d.tap(button.x, button.y, .8)
            elif page == "team":
                self.close_panel(image)
            elif page == "overview":
                self.close_panel(image)
            elif page == "profile":
                # 个人信息页只用于读武勋；用左上返回箭头退出，不点其他功能入口。
                self.close_panel(image)
            elif page == "deployment":
                self.emit("status", "返回沙盘 · 关闭出征选择页")
                self.close_panel(image)
            elif page == "camp_recruit_list":
                self.close_panel(image)
            elif page == "camp_recruit":
                self.close_panel(image)
            elif page == "worldmap":
                # 用户 2026-11-13：缩放改用左下角放大按钮；双指手势位置不稳、易被遮挡。
                if not self.zoom_in_to_sandbox():
                    self.d.zoom_out(steps=22, delta=120)
                    self.d.control.sleep(.5)
            elif page in ("war", "rally", "reserve", "territory"):
                self.close_panel(image)
            elif page in ("affairs", "drill", "campaign", "activity", "welfare", "visit", "visit_result", "alliance"):
                self.close_panel(image)
            elif page == "city":
                exit_button = self.v.find(image, ["出城"], (15, 1000, 180, 1075), exact=True)
                if not exit_button:
                    self.save_image(image, "city-exit-unconfirmed", True)
                    raise RecognitionUnavailable("出城按钮暂未读清，等待重读")
                # 旧点位靠近渠道浮球，可能打开福利菜单；只点已读到的出城文字。
                self.drop_view()
                self.d.tap(exit_button.x, exit_button.y, 2.5)
            elif page == "city_buildings":
                # 已核对建筑选择抽屉；拖动其上方地图收起，不点旧建筑坐标。
                self.drop_view()
                self.d.swipe(1500, 570, 1380, 570, 250)
                self.wait_transition(self.capture(), expected=("city",))
            else:
                # 整屏活动/礼包弹层（首登常见）：只认右上角已校准的叉号，读到才点。
                close_x = _vcall(self.v, "close_x_point", None, image)
                if close_x is not None and back_steps < 2:
                    self.save_image(image, "overlay-close-x", True)
                    self.log("关闭活动/礼包弹层 · 点击已识别的右上叉号", "INFO")
                    self.d.tap(close_x[0], close_x[1], .8)
                    recovered = True
                    back_steps += 1
                    continue
                # 城内出城等切换可能跨过一次重读；有上限地等待，不在未知页点击。
                if attempt < 4:
                    if attempt == 1:
                        # 用户 2026-11-15：选中地块/建筑详情会遮住底栏标志，导致页面无法识别。
                        # 未知页只做一次地图往返拖动（不点击）取消选中，每次手势后都重判；
                        # 仍读不清就交给后面的缩放按钮和等待，不在未知页反复拖动。
                        if self.deselect_unknown_page():
                            continue
                    if attempt >= 2:
                        # 地图放大到水寨近景时，地图标签可能压住状态栏；只点识别到的缩小按钮。
                        zoom = getattr(self.v, "map_zoom_point", None)
                        point = zoom(image, zoom_in=False) if zoom else None
                        if point is not None:
                            self.save_image(image, "unknown-map-labelled", True)
                            self.d.long_press(point[0], point[1], 1.8)
                            continue
                    self.emit("status", "画面暂未读清 · 短暂刷新后重看")
                    self.d.control.sleep(.35)
                    continue
                self.save_image(image,"unknown-page", True)
                raise RecognitionUnavailable("当前页面暂未识别，等待画面恢复后自动重读")
        raise RecognitionUnavailable("本轮未能返回沙盘，稍后重新识别页面")

    def map_zoom(self, zoom_in: bool, seconds: float = 1.8) -> bool:
        """长按地图左下角常驻的放大/缩小按钮；认不出按钮就不按。

        用户 2026-11-13：双指缩放在实机不稳；连点也可能不生效，长按 1~2 秒就能一路
        缩到最小（地图沙盘）或放到最大，位置固定，只按识别到的按钮。
        """
        self.drop_view()
        zoom = getattr(self.v, "map_zoom_point", None)
        if zoom is None:
            return False
        image = self.capture()
        point = zoom(image, zoom_in=zoom_in)
        if point is None:
            return False
        self.d.long_press(point[0], point[1], seconds)
        return True

    def zoom_out_to_map(self, attempts: int = 3, image=None):
        """长按缩小按钮退到“地图沙盘”（坐标输入页），返回该页画面。

        每次长按后都重新读页型，到了就停；不到就再长按一次，最多 3 次。
        调用方已持有一帧干净沙盘画面时可直接传入 image，省掉首轮重复截屏与识页。
        """
        self.drop_view()
        zoom = getattr(self.v, "map_zoom_point", None)
        for attempt in range(attempts):
            if attempt == 0 and image is not None:
                current = image
            else:
                current = self.capture()
                if self.v.page(current, verify=False) == "worldmap":
                    return current
            if zoom is None:
                break
            point = zoom(current, zoom_in=False)
            if point is None:
                if attempt == 0 and image is not None:
                    # 复用帧上没认出缩小按钮时，立刻现截一帧兜底核对一次。
                    current = self.capture()
                    if self.v.page(current, verify=False) == "worldmap":
                        return current
                    point = zoom(current, zoom_in=False)
                if point is None:
                    raise RecognitionUnavailable("地图缩小按钮暂未识别，等待重读")
            self.d.long_press(point[0], point[1], 1.8)
            self.d.control.sleep(.8)
        current = self.capture()
        if self.v.page(current) == "worldmap":
            return current
        self.save_image(current, "coordinate-map-not-open", True)
        raise GameError("未打开坐标地图（长按缩小仍未到地图沙盘），停止定位")

    def zoom_in_to_sandbox(self, attempts: int = 3) -> bool:
        """从“地图沙盘”长按放大按钮回到沙盘；认不出按钮或没到就返回 False。"""
        self.drop_view()
        zoom = getattr(self.v, "map_zoom_point", None)
        if zoom is None:
            return False
        for _ in range(attempts):
            image = self.capture()
            if self.v.page(image, verify=False) == "sandbox":
                return True
            point = zoom(image, zoom_in=True)
            if point is None:
                return False
            self.d.long_press(point[0], point[1], 1.8)
            self.d.control.sleep(.8)
        return self.v.page(self.capture(), verify=False) == "sandbox"

    def note_team_roster(self, image):
        """从部队页/部队总览左侧行确认本次运行实际拥有的主队。

        用户 2026-11-08：队伍数量以游戏自己的部队列表为准，不看右侧头像；
        启动时读一次，运行期间队伍数量视为不变。行号必须从 1 连续，
        行号缺口说明 OCR 漏了一行，本次不确认，交给上层重试。
        """
        if self.active_slots is not None or image is None:
            return bool(self.active_slots)
        if self.v.page(image) != "overview":
            # 部队详情页固定列 5 个槽位（空槽位也有“部队三/四/五”文字），
            # 只有“部队总览”才只列实际拥有的队伍。
            return False
        reader = getattr(self.v, "full_team_rows", None)
        if reader is None:
            return False
        rows = reader(image)
        slots = {slot for slot in rows if 1 <= slot <= 5}
        if not slots:
            return False
        if slots != set(range(1, max(slots) + 1)):
            self.log(f"部队列表行号不连续（读到 {sorted(slots)}），本次不确认", "WARN")
            return False
        self.active_slots = slots
        self.emit("team_presence", {"slots": sorted(slots)})
        self.log(f"部队列表确认本次运行共 {len(slots)} 队：槽位 {sorted(slots)}")
        return True

    def open_team_page(self, slot):
        """打开部队页（详情或总览），不读页码、不做“第 1 页”证明。

        2026-11-09 根因：右侧头像页码是旧八队布局的分页状态，和“这是不是
        当前队伍页”无关；用固定屏幕区域的颜色去猜页码，会让地图底色决定
        三队能否读取。这里只做一件事：点本槽位头像，直到进入部队页/总览。
        进入后由部队列表行和选中槽位核对身份；点错队不会变成读错队。
        """
        # 点头像会把镜头拉到该队，现场结论作废。
        self.drop_view()
        y = (245, 383, 520, 660, 785)[slot - 1]
        image = self.ensure_sandbox(refresh_grain=False)
        for attempt in range(3):
            page = self.v.page(image)
            if page in ("team", "overview"):
                return image
            if page == "sandbox":
                self.d.tap(1840, y, .7)
                image = self.wait_transition(self.capture(), expected=("sandbox", "overview", "team", "city"))
                if self.v.page(image) == "sandbox":
                    # 第一下可能只是选中地图，再点一次头像。
                    self.d.tap(1840, y, .7)
                    image = self.wait_transition(self.capture(), expected=("sandbox", "overview", "team", "city"))
                if self.v.page(image) in ("team", "overview"):
                    return image
            if self.v.page(image) == "city":
                # 头像进入内城：出城后重试，仍在沙盘上核对。
                image = self.ensure_sandbox()
                continue
            image = self.ensure_sandbox()
        self.save_image(image, f"team-page-unconfirmed-{slot}", True)
        raise RecognitionUnavailable(f"未打开第 {slot} 队部队页，等待重读")

    def read_roster(self, slot):
        """打开“部队总览”，只读实际拥有的主队行。

        用户 2026-11-08 实机：部队详情页固定列 5 个槽位（空槽位也有文字），
        “部队总览”才只列实际队伍。这里只点探针队自己的头像；如果进了部队详情，
        再点左下“部队总览”。不点其他队，避免碰正在集结的第一队。
        """
        for attempt in range(2):
            image = self.open_team_page(slot)
            page = self.v.page(image)
            if page == "team":
                button = self.v.find(image, ["部队总览"], (0, 740, 330, 940), exact=True)
                if not button:
                    self.save_image(image, "roster-overview-button-unreadable", True)
                    self.ensure_sandbox()
                    continue
                self.d.tap(button.x, button.y, .8)
                image = self.capture("roster-overview", True)
                page = self.v.page(image)
            if page == "overview" and self.note_team_roster(image):
                self.close_panel(image)
                self.ensure_sandbox()
                return set(self.active_slots or ())
            if page == "overview":
                self.close_panel(image)
            self.ensure_sandbox()
        raise GameError("未从部队总览读到有效队伍列表")

    def wait_transition(self, image, expected=("sandbox","overview"), transitional=("unknown",)):
        for attempt in range(5):
            # 等切页时没看清就再截一帧；昂贵的原精度复核只留给最后一次，避免每帧多花 4~8 秒。
            page = self.v.page(image, verify=attempt == 4)
            if page in expected:
                # 云雾过渡帧会整屏洗白：此时角标、血条、数字都不可信，等同"还在切换"。
                if page == "sandbox" and not _vcall(self.v, "frame_clear", True, image) and attempt < 4:
                    self.emit("status", "画面还在云雾过渡 · 正在刷新")
                    self.d.control.sleep(.25)
                    image = self.capture()
                    continue
                return image
            if page == "blocked":
                self.save_image(image,"login-blocked",True)
                raise GameError("游戏提示异地登录或断线，已停止操作")
            if page == "reconnect":
                self.reconnect_game(image)
                raise NetworkRecovered("网络已重连，本次读取作废，重新核对队伍")
            if page in ("navigable_page", "activity", "welfare", "visit", "visit_result"):
                self.ensure_sandbox(recover_navigation=True)
                raise NavigationRecovered("意外页面已返回沙盘，重新读取队伍")
            if page == "dismissible_overlay":
                # 奖励/活动弹层可能异步出现；核对关闭提示后收起，占用一次尝试上限。
                self.emit("status", "关闭活动/奖励弹层 · 按已识别提示")
                self.dismiss_overlay(image)
                image = self.capture()
                continue
            if page not in transitional or attempt == 4:
                self.save_image(image,"transition-not-recognized",True)
                raise RecognitionUnavailable("游戏画面切换未完成或页面暂未识别")
            self.emit("status","等待游戏画面切换 · 正在刷新")
            self.d.control.sleep(.25)
            image = self.capture()
        raise GameError("页面切换未完成")

    def locate_team(self, slot):
        # 不再先证明“第 1 页”：头像列表只有一页时页码为什么读不到，与队伍读取无关。
        # 定位只负责把镜头带到目标队；读到的数据随后由部队列表行/选中槽位核对。
        # 镜头被移动后，“镜头还在某个目标上”的结论一律作废（2026-11-18 实机根因）。
        self.drop_view()
        image = self.ensure_sandbox(refresh_grain=False)
        if self.active_slots is not None and slot not in self.active_slots:
            raise TeamAbsent(f"主城 {slot} 队不在本次部队列表中（当前 {sorted(self.active_slots)} 队）")
        # 当前已选中的本队就在本人主城时，复用现场详情，免去重复切头像和拖图。
        if (image is not None and self.p.main_city
                and self.v.home_team_visible(image, slot)
                and self.main_city_selected(image)):
            return image
        y = (245, 383, 520, 660, 785)[slot-1]
        # 2026-11-08 用户要求：第一队正在集结时不能碰。取消选中不再点另一支队，
        # 只点目标队自己的头像；打开的详情/总览先关掉，再用地图往返拖动取消选中。
        self.d.tap(1840, y, .7)
        image = self.wait_transition(self.capture(), expected=("sandbox", "overview", "team", "city"))
        if self.v.page(image) in ("overview", "team", "city"):
            image = self.ensure_sandbox()
        # 拖动地图取消选择；点击地图空地会选中土地，再点头像反而打开行军确认页。
        self.clear_map_selection(image)
        # 反向拖回原视角，避免同在主城的头像没有重新居中时不断累积地图偏移。
        self.d.tap(1840, y, .8)
        image = self.wait_transition(self.capture(), expected=("sandbox", "overview", "team", "city"))
        if self.v.page(image) == "team":
            image = self.ensure_sandbox()
        if self.v.page(image) == "city":
            # 主城头像在已定位状态下可能直接进内城；退出后仍须按坐标和归属核对。
            self.emit("status", "主城头像进入内城 · 出城后继续核对队伍")
            image = self.ensure_sandbox()
        if self.v.page(image) == "overview":
            self.close_panel(image)
            image = self.wait_transition(self.capture(), expected=("sandbox",))
        if self.v.page(image) == "sandbox" and self.v.map_coordinates(image) is None:
            # 头像有时仅将视角移到城池，关闭总览后没有选中城池详情。
            # 选中定位中心，仍由后续坐标、归属名和主城菜单核对，不猜在主城。
            self.d.tap(960,539,.5)
            image = self.wait_transition(self.capture(), expected=("sandbox", "overview", "city"))
            if self.v.page(image) == "city":
                self.emit("status", "主城中心进入内城 · 出城后继续核对队伍")
                image = self.ensure_sandbox()
            if self.v.page(image) == "overview":
                self.close_panel(image)
                image = self.wait_transition(self.capture(), expected=("sandbox",))
        return image

    def main_city_selected(self, image, coord=None):
        """当前画面是否选中了自己主城。

        旧赛季主城菜单有“亲卫整编”；2026-11-08 新赛季该菜单已换成行军/驻守/外观。
        两种布局都认，但必须坐标一致且归属名匹配，不能只看坐标。
        """
        if not self.p.main_city:
            return False
        coord = coord if coord is not None else self.v.map_coordinates(image)
        if coord != (self.p.main_city.x, self.p.main_city.y):
            return False
        if self.v.find(image, ["亲卫整编"], (1000, 595, 1668, 716)):
            return True
        return bool(self.home_matches_account(image))

    def calibrate_home(self):
        # 回城与点主城中心都会移动镜头：现场结论作废。
        self.drop_view()
        image = self.ensure_sandbox()
        # 新赛季“回城”可能直接进入城内；先回沙盘，再点主城中心读取坐标和归属。
        self.d.tap(90, 1000, 1.0)
        image = self.ensure_sandbox()
        self.d.tap(960, 512, 1.0)
        image = self.capture("home-calibration", True)
        coord = self.v.map_coordinates(image)
        labels = self.v.read(image, (540, 370, 717, 437))
        owner = max(labels, key=lambda t: t.score).text if labels else ""
        if not coord or not owner:
            raise GameError("回城后仍未确认主城坐标和归属名")
        if owner in ("主公", "驻城部队"):
            raise GameError("主城账号名称未识别")
        if self.p.account and owner != self.p.account:
            raise GameError(f"当前游戏账号 {owner} 与配置档 {self.p.account} 不符；请确认账号后再运行")
        self.p.account = owner
        if not self.home_matches_account(image):
            raise GameError("主城标题与归属名不一致，未确认主城")
        self.p.main_city = Point(name="我的主城", x=coord[0], y=coord[1], expected_name=owner)
        self.account_checked = True
        self.emit("calibrated", {"account": owner, "main_city": asdict(self.p.main_city)})
        self.log(f"已校验主城 {coord}，账号 {owner}")

    def check_account(self, image):
        if not self.p.main_city or not self.p.account:
            return
        # 主城详情必须同时满足坐标、归属名和已选中主城；旧“亲卫整编”和新版菜单都认。
        if self.main_city_selected(image):
            if not self.home_matches_account(image):
                raise GameError("主城账号与配置档不符，请重新校验账号后运行")
            self.account_checked = True

    def verify_account(self) -> bool:
        """用主城详情核对账号；铺路时队伍都不在主城也能完成校验。"""
        if self.account_checked:
            return True
        image = self.go_to(self.p.main_city)
        self.check_account(image)
        return self.account_checked

    def home_matches_account(self,image):
        # 自己主城的标题也是归属名；单个归属行漏字时用完整标题核对，均须完全匹配。
        return bool(self.p.account and (
            self.v.find(image,[self.p.account],(700,238,1180,345),exact=True)
            or self.v.find(image,[self.p.account],(320,370,745,438),exact=True)))

    @contextmanager
    def team_reading_batch(self, policies):
        if getattr(self, "_team_batch_policies", None) is not None:
            yield
            return
        self._team_batch_policies = tuple(p for p in policies if p.group == "main")
        self._team_detail_cache = {}
        self._team_overview_cache = {}
        try:
            yield
        finally:
            self._team_batch_policies = None
            self._team_detail_cache = {}
            self._team_overview_cache = {}

    def read_full_team_batch(self, image, current):
        ordered = [current] + [p for p in self._team_batch_policies if p.key != current.key]
        try:
            for policy in ordered:
                try:
                    image = self.select_full_team_slot(image, policy.slot)
                    obs = self._full_team(image, policy.key)
                    self._team_detail_cache[policy.key] = (obs, time.time())
                    self.save_image(image, f"{policy.key}-detail", True)
                    self.sync_auto_refill(image, policy)
                except GameError as exc:
                    if policy.key == current.key:
                        raise
                    self.log(f"{policy.display} 批量详情读取未完成：{exc}；后续单独核对", "WARN")
        finally:
            self.close_panel(image)
        obs, _ = self._team_detail_cache[current.key]
        return obs

    def read_overview_batch(self, image, current):
        ordered = [current] + [p for p in self._team_batch_policies if p.key != current.key]
        try:
            for policy in ordered:
                try:
                    image = self.select_full_team_slot(image, policy.slot)
                    obs = self.read_overview(image, policy.key)
                    self._team_overview_cache[policy.key] = (obs, time.time())
                    self.save_image(image, f"{policy.key}-overview", True)
                except GameError as exc:
                    if policy.key == current.key:
                        raise
                    self.log(f"{policy.display} 批量总览读取未完成：{exc}；后续单独核对", "WARN")
        finally:
            self.close_panel(image)
        obs, _ = self._team_overview_cache[current.key]
        return obs

    def scan_team(self, policy: TeamPolicy) -> Observation:
        if policy.group != "main":
            return Observation(policy.key, note="亲卫槽位可配置，自动读数与出征待实机验收")
        if self.active_slots is not None and policy.slot not in self.active_slots:
            raise TeamAbsent(f"主城 {policy.slot} 队尚未拥有，跳过读取")
        self.emit("status", f"正在读取 {policy.display} · 定位与核对兵力")
        y = (245, 383, 520, 660, 785)[policy.slot-1]
        image = self.locate_team(policy.slot)
        # 移镜头的云雾过渡仍保留沙盘底栏，page=sandbox并不证明详情文字已经可读。
        # 主城菜单/坐标缺失时有限重读；不能把菜单“行军”当成这支队伍已经出发。
        for attempt in range(3):
            coord = self.v.map_coordinates(image)
            home_coordinate = bool(self.p.main_city and coord == (self.p.main_city.x, self.p.main_city.y))
            home_detail = self.main_city_selected(image, coord) if home_coordinate else False
            if coord is not None and (not home_coordinate or home_detail):
                break
            if coord is None:
                marker = cv2.cvtColor(image[y+3:y+45,1840:1900], cv2.COLOR_BGR2HSV)
                if cv2.countNonZero(cv2.inRange(marker,(90,70,110),(125,255,255))) > 90:
                    break  # 真实行军蓝旗无需地块详情，不把它当作移镜头文字缺失。
            if attempt < 2:
                self.d.control.sleep(.8)
                image = self.wait_transition(self.capture())
        self.save_image(image, f"{policy.key}-located", True)
        if getattr(self, "live_resources", False) and (self.grain is None or
                time.time() - self.resource_observed_at >= 60):
            self.update_grain(image)
            self.next_resource_check = time.monotonic() + self.RESOURCE_INTERVAL
        coord = self.v.map_coordinates(image)
        if self.p.main_city and self.v.home_team_visible(image, policy.slot) and not self.main_city_selected(image, coord):
            # 刚到家时头像已显示主城军旗，但镜头未选中城池；只按已确认军旗补核对主城。
            # 不能因为主动前往主城，就把原本仍在城外的队伍当作在家。
            image = self.go_to(self.p.main_city)
            if not self.v.home_team_visible(image, policy.slot):
                raise GameError("主城定位后队伍标记变化，本次读数作废")
            coord = self.v.map_coordinates(image)
        self.check_account(image)
        home = bool(self.p.main_city and self.main_city_selected(image, coord))
        # 与状态判定同一帧读角标，避免"兵马数值一帧、状态另一帧"。
        marker = marker_with_home_evidence(self.read_team_marker(image, policy.slot), home)
        detail_observed_at = None
        if home:
            # 与主城定位使用同一套详情裁剪，名称和坐标共同确认账号。
            if not self.home_matches_account(image):
                raise GameError("主城归属名未核对通过，不读取为可出击队伍")
            self.account_checked = True
            cached = self._team_detail_cache.get(policy.key) if getattr(self, "_team_batch_policies", None) is not None else None
            if cached is not None:
                obs, detail_observed_at = cached
                obs = replace(obs)
            else:
                self.d.tap(1745, y, .6)
                full = self.capture()
                if self.v.page(full) != "team":
                    raise GameError(f"{policy.display} 的部队页面未打开")
                if getattr(self, "_team_batch_policies", None) is not None:
                    obs = self.read_full_team_batch(full, policy)
                    detail_observed_at = self._team_detail_cache[policy.key][1]
                else:
                    full = self.select_full_team_slot(full, policy.slot)
                    obs = self._full_team(full, policy.key)
                    detail_observed_at = time.time()
                    self.save_image(full, f"{policy.key}-home", True)
                    self.sync_auto_refill(full, policy)
                    self.close_panel(full)
            obs.state = "wounded" if obs.wounded else "home"
            obs.location = coord
        else:
            state = self.read_outside_state(image,policy,coord)
            cached = self._team_overview_cache.get(policy.key) if getattr(self, "_team_batch_policies", None) is not None else None
            overview = image
            if cached is not None:
                obs, detail_observed_at = cached
                obs = replace(obs)
                obs.state, obs.location = state, coord
            else:
                self.d.tap(1840, y, .6)
                overview = self.capture()
                obs = Observation(policy.key, state=state, location=coord, note="城外/行动中的队伍，保留现有任务")
                # 点击头像可能先选中地图上的本队（出现表情/回城），再点一次才打开总览。
                # 只在已识别沙盘继续点同一头像；未知页绝不补点。
                if self.v.page(overview) == "sandbox":
                    if state == "unknown":
                        state = self.read_outside_state(overview, policy, coord)
                    self.d.tap(1840, y, .6)
                    overview = self.capture()
                    obs.state = state
                if self.v.page(overview) == "overview":
                    if getattr(self, "_team_batch_policies", None) is not None:
                        obs = self.read_overview_batch(overview, policy)
                        detail_observed_at = self._team_overview_cache[policy.key][1]
                    else:
                        # 头像入口不承担身份证明：总览里按槽位选中本队并核对高亮，
                        # 点错头像也不会把别的队数据当成本队。
                        overview = self.select_full_team_slot(overview, policy.slot)
                        obs = self.read_overview(overview, policy.key)
                        self.close_panel(overview)
                    obs.state, obs.location = state, coord
            if obs.state == "unknown" or obs.strength is None and obs.state not in ("marching", "returning", "active"):
                self.save_image(overview, f"{policy.key}-state-unconfirmed", True)
        # 单帧角标是当前状态的最高优先级证据：同帧读出五头像右下的真实状态。
        # 认不出（unknown）时保留旧流程结果，不猜；主城分支仍以详情页的重伤/兵力为准。
        if marker:
            obs.marker_state = marker["state"]
            obs.marker_label = marker["label"]
            obs.marker_confidence = marker["confidence"]
            obs.marker_at = time.time()
            if marker["state"] != "unknown" and not home:
                obs.state = LEGACY_STATE.get(marker["state"], obs.state)
            if marker["state"] == "wounded" or obs.wounded is True:
                obs.wounded = True
                obs.marker_state = "wounded"
                obs.marker_label = state_label("wounded")
        # 自然征兵的队伍只能从主城出发。2026-11-04 实机：涪县征兵期间角标读到“空闲”，
        # LEGACY_STATE 把空闲一律当 home，程序据此清了 backup 阶段并从征兵点直接拉线。
        # 这里以发兵记录为准：没确认回到主城坐标就按城外处理，交给 decide() 走“够兵→回城”。
        city = (self.p.main_city.x, self.p.main_city.y) if self.p.main_city else None
        phase = self.j.phase(policy.key) if self.j is not None else ""
        if stays_at_backup(phase, home, coord, city):
            obs.state = "outside"
            obs.location = coord
        # 批量详情保留各队实际读取时间；地图状态使用本次定位时间，均不从旧日志恢复。
        obs.observed_at = detail_observed_at if detail_observed_at is not None else time.time()
        self.emit("observation", asdict(obs))
        return obs

    def scan_pave_team(self, policy):
        """铺路/拉线等待期的轻量观测：只读本队坐标、角标和重伤，不打开总览/详情。

        用户 2026-11-10：到阶段点后部队头像状态马上就会变，应马上衔接下一段；
        等待期间不要反复开面板做无意义操作。兵力沿用最近一次完整读数（由
        Worker.observe_pave_team / observe_line_team 填充），本方法只负责现场位置与状态。
        """
        self.emit("status", f"正在读取 {policy.display} · 轻量快照")
        image = self.locate_team(policy.slot)
        for attempt in range(2):
            coord = self.v.map_coordinates(image)
            if coord is not None:
                break
            if attempt == 0:
                self.d.control.sleep(.6)
                image = self.wait_transition(self.capture())
        home = bool(self.p.main_city and coord == (self.p.main_city.x, self.p.main_city.y))
        marker = marker_with_home_evidence(self.read_team_marker(image, policy.slot), home)
        obs = Observation(policy.key, state=LEGACY_STATE.get(marker["state"], "unknown") if marker else "unknown",
                          location=coord, wounded=marker["wounded"] if marker else None, note="铺路快照")
        if marker:
            obs.marker_state = marker["state"]
            obs.marker_label = marker["label"]
            obs.marker_confidence = marker["confidence"]
            obs.marker_at = time.time()
            if marker["state"] == "unknown":
                obs.note = marker["reason"]
        self.emit("observation", asdict(obs))
        return obs

    def marker_reader(self):
        reader = getattr(self, "_marker_reader", None)
        if reader is None:
            try:
                reader = TeamMarkerReader()
            except Exception as exc:
                self.log(f"角标模板不可用：{exc}", "WARN")
                reader = False
            self._marker_reader = reader
        return reader

    def _marker_with_wound(self, image, slot, marker):
        """同帧读取头像文字，已识别的行动角标也可能叠加重伤。"""
        if marker.get("reason") in ("画面未确认", "队伍行未确认"):
            return {**marker, "wounded": None}
        wounded = _vcall(self.v, "team_wounded", None, image, slot)
        if wounded:
            return {**marker, "wounded": True, "state": "wounded",
                    "label": state_label("wounded"), "reason": "头像重伤文字"}
        return {**marker, "wounded": wounded}

    def read_team_marker(self, image, slot):
        """单帧读一个槽位的角标状态；画面还在过渡就重截，读不到返回 None。"""
        self.marker_frame = None
        reader = self.marker_reader()
        if not reader:
            return None
        if self.active_slots is not None and slot not in self.active_slots:
            return None
        try:
            if not hasattr(self, "marker_last"):
                self.marker_last = {}
            for attempt in range(2):
                if _vcall(self.v, "frame_clear", True, image) and reader.avatars_visible(image):
                    marker = self._marker_with_wound(image, slot, reader.read_slot(image, slot))
                    # 防抖（2026-11-13 实机）：空闲=无角标，但“到点/开战”的一瞬角标也会消失。
                    # 上一状态还是行军/调动/撤退/战斗/忙碌，这一帧却“无角标”时，再截一帧确认；
                    # 只有连续两帧都无角标才认空闲，不能拿一帧的瞬时缺失当成回主城。
                    # 不设时间窗：上次读到的状态就是最近一次现场，确认过一次空闲后
                    # marker_last 会变成 idle，后续不会再补读。
                    previous = self.marker_last.get(slot)
                    if (marker["state"] == "idle" and previous
                            and previous[0] not in ("idle", "training")):
                        self.d.control.sleep(.6)
                        again = self.capture()
                        if _vcall(self.v, "frame_clear", True, again) and reader.avatars_visible(again):
                            image = again
                            marker = self._marker_with_wound(image, slot, reader.read_slot(image, slot))
                        else:
                            image = None
                            marker = {"slot": slot, "state": "unknown", "label": state_label("unknown"),
                                      "reason": "画面未确认", "confidence": 0.0, "wounded": None}
                    self.marker_frame = image
                    self.marker_last[slot] = (marker["state"], time.time())
                    return marker
                if attempt == 0:
                    # 云雾过渡/切页瞬间：等半秒重截一帧，而不是拿洗白的画面下结论。
                    self.d.control.sleep(.5)
                    image = self.capture()
            return None
        except (Stopped, TransportUnavailable):
            # 停止/断连必须原样传播：吞成“读不到”会让 Worker 拿着停止后的画面继续派兵。
            raise
        except Exception as exc:
            self.log(f"角标读取失败：{exc}", "WARN")
            return None

    def read_all_marker_states(self):
        """一帧读出本次部队列表里的队伍状态；画面不可用时返回空，调用方退回逐队读取。"""
        reader = self.marker_reader()
        if not reader:
            return {}
        slots = sorted(self.active_slots or ())
        if not slots:
            return {}
        try:
            image = self.capture()
            if not _vcall(self.v, "frame_clear", True, image) or not reader.avatars_visible(image):
                self.d.control.sleep(.5)
                image = self.capture()
            if not _vcall(self.v, "frame_clear", True, image) or not reader.avatars_visible(image):
                image = self.ensure_sandbox(refresh_grain=False)
            if not _vcall(self.v, "frame_clear", True, image) or not reader.avatars_visible(image):
                return {}
            states = {}
            # 优先批量读：team_markers 提供 read_slots(image, slots) 时一次读出全部槽位；
            # 旧 reader 只有 read_slot 时回退逐槽读取，保持同一帧上的重伤覆盖。
            read_slots = getattr(reader, "read_slots", None)
            markers = read_slots(image, slots) if callable(read_slots) else None
            if markers is None:
                markers = {slot: reader.read_slot(image, slot) for slot in slots}
            elif isinstance(markers, list):
                markers = {marker["slot"]: marker for marker in markers}
            for slot in slots:
                marker = markers.get(slot)
                if not marker:
                    continue
                states[slot] = self._marker_with_wound(image, slot, marker)
            return states
        except (Stopped, TransportUnavailable):
            # 停止/暂停检查与设备断连穿过整帧读取时必须原样抛出：
            # 吞成“识别失败”会让 Worker 拿着停止后的画面继续派兵。
            raise
        except GameError:
            raise
        except Exception as exc:
            self.log(f"整帧角标读取失败：{exc}", "WARN")
            return {}


    def read_outside_state(self, image, policy, coord):
        # 已核对的普通停留箭头无需再扫描整片动态地图；铺路/补兵仍保留活动与征兵复核。
        phase = self.j.phase(policy.key) if self.j else ""
        if self.v.returning_team_visible(image, policy.slot):
            return "returning"
        if phase == "guarding":
            return "guarding"
        if phase not in ("backup", "pave") and self.v.stationary_return_visible(image, policy.slot):
            return "outside"
        state_text = self.v.joined(image, (770, 260, 1540, 785))
        state_text += " | "+" | ".join(self.v.joined(image,r) for r in ((300,600,880,795),(750,600,1330,795),(1150,600,1600,795)))
        state = "unknown"
        for label, value in (("返回", "returning"), ("征兵", "recruiting"), ("证兵", "recruiting"),
                             ("驻守中", "guarding"), ("停留", "outside"), ("回城", "outside")):
            if label in state_text:
                state = value
                break
        y = (245,383,520,660,785)[policy.slot-1]
        # 这里只用标准布局的颜色扫描（负责人真正的状态由角标读取给出）：
        # 建筑内队伍整行左移时，用 -25 去扫地图区域会把地图上的金色/蓝色当成征兵/驻守。
        marker = cv2.cvtColor(image[y+3:y+45,1840:1900],cv2.COLOR_BGR2HSV)
        blue = cv2.countNonZero(cv2.inRange(marker,(90,70,110),(125,255,255)))
        gold = cv2.countNonZero(cv2.inRange(marker,(18,90,130),(40,255,255)))
        if "中止" in state_text and self.v.active_task_visible(image):
            # 自动铺路每格耗时可随时段变化，任务仍在运行就一直等待。
            return "active"
        if self.v.stationary_return_visible(image,policy.slot):
            # 普通回城箭头证明已停留；地块菜单“行军”不代表这支队伍在移动。
            # 铺路只认“停留/行军/占领”，不再读营帐标题猜状态（角标已经给出答案）。
            base = bool(coord and any((b.x,b.y) == coord for b in configured_backup_points(self.p)))
            if phase == "backup" and base:
                return "recruiting" if gold > 70 else "outside"
            return "outside"
        if blue > 90:
            return "returning" if phase == "returning" or state == "returning" else "marching"
        if "中止" in state_text and state == "unknown":
            state = "guarding" if gold > 70 else "unknown"
            if state == "unknown" and self.v.active_task_visible(image):
                state = "active"
        if state == "unknown":
            if gold > 90 and self.v.team_task_stop_visible(image, policy.slot):
                state = "active"
        return state

    @staticmethod
    def full_row_position(page, rows, slot):
        """目标行的点击位置：OCR 实测位置优先；缺行按已识别行的等距几何插值；
        整页 OCR 失效才用页型几何。紧凑列表下第 N 行永远是第 N 队或空白，
        插值/几何点击不会点到别队——是否存在由点击后的金底校验回答。"""
        if slot in rows:
            return rows[slot].x, rows[slot].y
        if rows:
            slots = sorted(rows)
            nearest = min(slots, key=lambda s: abs(s - slot))
            gaps = [rows[b].y - rows[a].y for a, b in zip(slots, slots[1:])]
            gap = sorted(gaps)[len(gaps) // 2] if gaps else (108 if page == "overview" else 113)
            return rows[nearest].x, rows[nearest].y + gap * (slot - nearest)
        fallback = (329, 437, 545, 653, 761) if page == "overview" else (222, 335, 448, 561, 674)
        fallback_x = 264 if page == "overview" else 126
        return fallback_x, fallback[slot-1]

    def select_full_team_slot(self, image, slot):
        # 2026-11-21 重构：单帧 OCR 缺行不再有「队伍不存在」的判定权。
        # 缺行按等距几何补位点击，是否真不存在由「连续 3 次点击后仍无选中金底」
        # 这一确定性证据回答（紧凑列表下该位置是空白），不再单帧误判停摆。
        for attempt in range(3):
            page = self.v.page(image)
            if page not in ("team", "overview"):
                break
            rows = self.v.full_team_rows(image)
            x, y = self.full_row_position(page, rows, slot)
            self.d.tap(x, y, .5)
            image = self.capture_stable()
            if self.v.page(image) in ("team", "overview"):
                if self.selected_full_slot(image, slot, y):
                    return image
        self.save_image(image, f"main_{slot}-slot-unconfirmed", True)
        raise TeamAbsent(f"第 {slot} 队位置连续 3 次点击仍未选中（按尚未拥有处理）")

    @staticmethod
    def selected_full_slot(image, slot, row_y=None):
        # 左侧选中队伍的浅金色底；排除顶部分组按钮。
        y = round(row_y) if row_y is not None else (221, 335, 448, 560, 674)[slot-1]
        roi = cv2.cvtColor(image[y-32:y+32, 5:210], cv2.COLOR_BGR2HSV)
        gold = cv2.inRange(roi, (12, 25, 140), (40, 170, 255))
        return cv2.countNonZero(gold) > roi.shape[0]*roi.shape[1]*.35

    def _remember_identity(self, key: str, identity: str) -> str:
        """阵容名每队只取一次：非空新读数更新缓存，空读数回填缓存值。

        用户 2026-11-14：同一次任务运行内，后续 observation 不得把 UI 已显示的
        阵容弄丢。缓存挂在 Game 实例上（键为队伍键），不跨 Game/账号/队伍，
        任务结束随实例丢弃。名称来自详情/总览英雄区的同一次 OCR 跨度过滤，
        不额外跑识别；兵力/体力/补给/重伤/角标/位置等动态字段不走这里，
        每次都从现场画面重读。惰性初始化以兼容 __new__ 构造的测试工具。
        """
        cache = getattr(self, "_identity_cache", None)
        if cache is None:
            cache = self._identity_cache = {}
        if identity:
            cache[key] = identity
            return identity
        return cache.get(key, "")

    def remembered_identity(self, key: str) -> str:
        """本次任务运行内该队已读到的阵容名；未读过返回 ""（见 _remember_identity）。"""
        return getattr(self, "_identity_cache", {}).get(key, "")

    def _full_team(self, image, key):
        obs = self.v.full_team(image, key)
        obs.identity = self._remember_identity(key, obs.identity)
        return obs

    def read_overview(self, image, key):
        obs = Observation(key)
        right = self.v.read(image, (1325, 475, 1800, 607))
        army = [p for t in right if t.y < 549 for p in pairs(t.text) if p[1] >= 1000 and p[0] <= p[1]]
        sup = [p for t in right if t.y > 549 for p in pairs(t.text) if p[1] == 100 and p[0] <= 100]
        heroes = self.v.read(image, (477, 374, 1304, 666))
        hp = [p for t in heroes if 567 < t.y < 614 for p in pairs(t.text) if p[1] >= 100 and p[0] <= p[1]]
        if len(army) == 1 and len(hp) == 3 and tuple(map(sum, zip(*hp))) == army[0]:
            obs.strength, obs.maximum = army[0]
        if len(sup) == 1:
            obs.supply = sup[0][0]
        stamina = [p[0] for t in heroes if 614 < t.y < 654 for p in pairs(t.text) if p[1] == 200]
        if len(stamina) == 3:
            obs.stamina = min(stamina)
        obs.wounded = True if any("重伤" in t.text for t in heroes) else (False if obs.strength is not None else None)
        names = sorted([t for t in heroes if 510 < t.y < 554 and re.match(r"\d+", t.text)], key=lambda t:t.x)
        obs.identity = self._remember_identity(key, "/".join(re.sub(r"^[\d\s]+", "", t.text) for t in names))
        return obs

    def read_merit(self):
        """点头像进个人信息页读“武勋”累计值；只读、不点其他入口，读完回沙盘。

        用户 2026-11-04：这个数只放总览给使用者看“这轮挂机刷了多少”，不参与派兵；
        读不到就跳过本轮，绝不重试成环或改点旧坐标。
        """
        image = self.ensure_sandbox()
        if self.v.page(image) != "sandbox":
            raise GameError("未确认沙盘，不打开个人信息页")
        self.d.tap(75, 70, 1.2)
        image = self.capture_clear("profile-open", True)
        page = self.v.page(image)
        if page != "profile":
            self.save_image(image, "profile-unconfirmed", True)
            # 恢复途中 ensure_sandbox 可能抛出异地登录/维护/重连作废等真实错误，
            # 必须原样传播；吞掉后降级成“本轮不读武勋”会把停止延后一拍。
            self.ensure_sandbox()
            raise RecognitionUnavailable(f"个人信息页未确认（{page}），本轮不读武勋")
        value, text = _vcall(self.v, "profile_merit", (None, ""), image)
        self.save_image(image, "profile-merit", False)
        back = self.v.navigation_back_point(image, require_title=False)
        if not back:
            self.save_image(image, "profile-back-unconfirmed", True)
            raise RecognitionUnavailable("个人信息页返回箭头未确认，等待重读")
        self.d.tap(back["x"], back["y"], .8)
        if self.v.page(self.capture()) != "sandbox":
            self.ensure_sandbox()
        if value is None:
            raise RecognitionUnavailable("个人信息页武勋数值未识别")
        return value, text

    def note_view(self, point):
        """记下“镜头此刻放在这个坐标上，并且已经核对通过”。

        只允许在两种情况下调用：坐标导航（前往）落地并核对之后，或重新选中目标
        且坐标核对通过之后。别处不得凭“我记得”写入。
        """
        self.view_point = (point.x, point.y)
        self.view_at = time.monotonic()
        self.view_epoch += 1

    def drop_view(self):
        """镜头或选中状态可能已经变了：作废现场结论。

        所有会移动镜头/改变比例的动作（点头像定位队伍、长按缩放、地图往返拖动、
        出城、未知页取消选中）都必须调用。宁可多走一次坐标导航，也不能拿旧结论
        往屏幕正中点——点错会打开整屏弹层，退不出来。
        """
        if self.view_point is not None:
            self.view_point = None
        self.view_at = 0.0
        self.view_epoch += 1

    def view_on(self, point) -> bool:
        """镜头是否确实停在这个坐标上（只有 note_view 之后且在有效期内才为真）。

        2026-11-23 防误触护栏：
        - 禁止对自己主城走点击屏幕中心快路（点主城中心可能直接进内城）；
        - 超过 VIEW_TTL（45 秒）或跨轮后自动作废，避免用户中途手动拖图导致点歪。
        """
        if self.view_point is None or self.view_point != (point.x, point.y):
            return False
        profile = getattr(self, "p", None)
        main_city = getattr(profile, "main_city", None) if profile is not None else None
        if main_city is not None and (point.x, point.y) == (main_city.x, main_city.y):
            return False
        if self.view_at and (time.monotonic() - self.view_at) > self.VIEW_TTL:
            return False
        return True

    def tap_target_center(self, label):
        """点屏幕正中，选中“镜头中心”的那一格，并读回这一帧。

        这是全游戏唯一能拿到目标动作菜单的办法：前往只移动镜头，必须再选中地块
        才会弹出 行军/攻占/调动。注意它只负责“选中并读”，不负责保证镜头在哪；
        调用方要么刚用坐标导航把镜头放过去，要么已经用 view_on 核对过。
        """
        self.d.tap(960, 539, .5)
        image = self.capture(f"target-{label}", True)
        if self.v.map_coordinates(image) is None and self.v.page(image) == "sandbox":
            # 目标中心有移动部队时可能先选中队伍；只试下方地块，仍必须核对原坐标。
            self.d.tap(960, 583, .4)
            image = self.capture(f"target-ground-{label}", True)
        return image

    def go_to(self, point: Point):
        """定位到地图坐标，以“实际到达的地块坐标”作为唯一放行条件。

        2026-11-10 用户：不再把输入框回读当成放行条件。输入框是代理指标，
        目标地块坐标才是结果；前往只移动镜头、不派兵不消耗，所以按结果校验
        更稳，也不依赖输入框主题/字体/亮度。输入没生效或坐标不符时有界重输
        一次，仍不对就停，绝不盲点攻占。
        """
        # 从这里开始镜头会被缩放、拖动、前往反复移动；旧的“镜头在哪”一律作废。
        self.drop_view()
        last = ""
        for attempt in range(2):
            image = self.ensure_sandbox()
            # 上一个目标的详情会挡住缩放。地图往返拖动取消选中；左上“空地”会随活动入口变化。
            if image is not None and self.v.page(image) == "sandbox" and self.v.map_coordinates(image) is not None:
                image = self.clear_map_selection(image)
            # 详情关闭后的比例不固定；用户 2026-11-13 要求改用左下角缩小按钮一路退到
            # “地图沙盘”（坐标输入页），不再用双指缩放。到不了就停，不盲点。
            image = self.zoom_out_to_map(image=image)
            self.d.tap(1483, 998)
            self.d.text(f"{point.x}.{point.y}")
            self.d.finish_coordinate_input()
            image = self.capture()
            page = self.v.page(image)
            if page == "blocked":
                self.save_image(image, "login-blocked", True)
                raise GameError("游戏提示异地登录或断线，停止定位")
            if page != "worldmap":
                self.save_image(image, "coordinate-input-page-unconfirmed", True)
                raise GameError("坐标输入后未确认地图页面，停止定位")
            button = self.v.find(image, ["前往"], (1650, 935, 1900, 1055))
            if not button:
                raise GameError("未识别地图前往按钮")
            self.d.tap(button.x, button.y, 1)
            try:
                image = self.wait_transition(self.capture(), ("sandbox",), ("unknown", "worldmap"))
            except GameError:
                # 仍停在坐标地图：输入未生效（空、旧值、没提交）。按结果重输一次。
                check = self.capture()
                if self.v.page(check) == "worldmap":
                    last = "前往未生效（仍停在坐标地图）"
                    self.save_image(check, "coordinate-not-taken", True)
                    continue
                raise
            image = self.tap_target_center(f"{point.x}-{point.y}")
            coord = self.v.map_coordinates(image)
            if coord != (point.x, point.y):
                last = f"目标坐标 {coord}，要求 {(point.x, point.y)}"
                self.save_image(image, "coordinate-target-mismatch", True)
                if attempt == 0:
                    continue
                raise TargetNotSelectable(
                    f"坐标定位两次仍未到达目标：{last}，不提交命令",
                    requested=(point.x, point.y), selected=coord)
            self.verify_target(image, point)
            self.note_view(point)
            return image
        raise TargetNotSelectable(f"坐标定位失败：{last or '未到达目标'}，不提交命令",
                                  requested=(point.x, point.y))

    def verify_target(self, image, point):
        coord = self.v.map_coordinates(image)
        if coord != (point.x, point.y):
            raise TargetNotSelectable(
                f"目标详情坐标不同：{coord}，不提交命令",
                requested=(point.x, point.y), selected=coord)
        if point.expected_name:
            text = self.v.joined(image, (700, 238, 1180, 414))
            if point.expected_name not in text:
                # 城池标题的描边字体可能误读；左侧详情另有清晰名称和等级。
                # 仍须前面的真实坐标一致，并完整匹配名称，不替换疑似错字。
                heading = self.v.read(image, (243, 277, 680, 345), minimum=.85)
                named = any(t.text.replace(" ", "") == point.expected_name for t in heading)
                leveled = any(re.fullmatch(r"Lv[.．]?\s*\d+", t.text, re.IGNORECASE) for t in heading)
                if not (named and leveled):
                    raise GameError(f"目标名称不符，预期 {point.expected_name}，实际 {text}")

    def reopen_target(self, point):
        """重新选中目标地块并返回已核对的目标详情帧（派兵入口）。

        2026-11-18 实机根因：这里以前无条件点屏幕正中，理由是“上一队刚在同一目标
        提交，镜头还锁在目标上”。但派兵前必然先读队，而读队点头像会把镜头拉到本队；
        于是这一点经常落在本队/自己的城池上——轻则点出城池菜单，重则点开
        「请选择撤退的地点」这类整屏弹层，页面识别不了就再也退不出去。

        现在这条快路必须先证明“镜头确实还在这个坐标上”（view_on），证明不了就老老实实
        用坐标导航（go_to）。快路自己也不猜：点完立刻读坐标核对，对不上就作废现场、
        重新导航，绝不拿一次没核对上的点击继续往下提交。
        """
        if not self.view_on(point):
            return self.go_to(point)
        self.ensure_sandbox()
        if not self.view_on(point):
            # 回沙盘过程中如果动过镜头（出城、拖动、缩放），现场同样作废。
            return self.go_to(point)
        image = self.tap_target_center(f"refresh-{point.x}-{point.y}")
        if self.v.map_coordinates(image) != (point.x, point.y):
            self.save_image(image, "target-refresh-mismatch", True)
            self.drop_view()
            # 2026-11-23 防误触自愈：若点中心意外进入城内或可返回页面，先安全退回沙盘
            # 再走标准坐标导航 go_to，避免误触导致整轮罢工。
            if _vcall(self.v, "page", "sandbox", image) != "sandbox":
                try:
                    self.ensure_sandbox(recover_navigation=True)
                except NavigationRecovered:
                    pass
            return self.go_to(point)
        self.verify_target(image, point)
        self.note_view(point)
        return image

    def dispatch(self, policy: TeamPolicy, obs: Observation, point: Point, kind: str):
        # 战场是固定坐标，建筑/归属/标题会变。兼容旧配置中遗留的 expected_name，
        # 仅武勋流程不以名称拦截；主城和备用调动继续严格核验身份。
        if kind == "merit":
            point = replace(point, expected_name="")
        # 用户 2026-11-03：永远按配置的指令顺序走，脚本不按“队伍站在哪”裁剪指令。
        remaining = list(point.actions)
        try:
            while True:
                try:
                    return self._dispatch(policy, obs, replace(point, actions=remaining), kind)
                except ActionUnavailable as exc:
                    # 结果未知、标题不符等普通 GameError 不允许在同目标继续点击。
                    if kind == "transfer" or self.j.pending(policy.key) or exc.action not in remaining:
                        raise
                    remaining = remaining[remaining.index(exc.action)+1:]
                    if not remaining:
                        raise
                    self.log(f"{point.name}：{exc}；核对同目标的后续允许指令", "WARN")
                except DispatchUnconfirmed as exc:
                    # 没受理＝这一条在这里没成。只有“确认队伍还停在原地（可派）”才换下一条，
                    # 并且用提交后读到的真实角标刷新观测，避免拿旧状态做受理对比。
                    if (kind != "merit" or exc.busy or self.j.pending(policy.key)
                            or exc.action not in remaining):
                        raise
                    remaining = remaining[remaining.index(exc.action)+1:]
                    if not remaining:
                        raise
                    if exc.marker and exc.marker != "unknown":
                        obs = replace(obs, marker_state=exc.marker,
                                      marker_label=state_label(exc.marker),
                                      marker_confidence=exc.confidence if exc.confidence is not None else obs.marker_confidence,
                                      marker_at=time.time(),
                                      state=LEGACY_STATE.get(exc.marker, obs.state))
                    self.log(f"{point.name}：{exc}；队伍仍在原地，按配置顺序改试下一条", "WARN")
        except BaseException:
            # 派兵失败或异常后，屏幕上可能已经不是目标详情（弹层、错误页、断连）。
            # 现场结论一律作废：下一次派兵必须重新确认镜头，宁可多走一次坐标导航。
            self.drop_view()
            raise

    def pick_action(self, image, point, kind):
        """按配置顺序挑当前可用的指令：图标模板优先，全不中退回文字识别。"""
        buttons = _vcall(self.v, "action_buttons", {}, image)
        if kind == "transfer":
            if "transfer" in buttons:
                x, y, _score = buttons["transfer"]
                return "调动", "transfer", (x, y)
            button = self.v.find(image, ["调动"], (1100, 150, 1800, 860), exact=True)
            if not button:
                raise GameError(f"目标 {point.name} 当前没有调动指令")
            return "调动", "transfer", (button.x, button.y)
        for action in point.actions:
            icon = self.ICON_FOR_ACTION.get(action)
            if icon and icon in buttons:
                x, y, _score = buttons[icon]
                return ACTION_NAMES[action], action, (x, y)
        # 2026-11-08 实机：菜单动画/高光会让图标相似度在某一帧掉到阈值以下。
        # 只重试一次、约 0.2 秒；仍不中才退回文字，避免无谓拖慢正常派兵。
        if callable(getattr(self.d, "capture", None)):
            self.d.control.sleep(.2)
            fresh = self.capture("action-icons-retry")
            if _vcall(self.v, "frame_clear", True, fresh):
                retry_buttons = _vcall(self.v, "action_buttons", {}, fresh)
                for action in point.actions:
                    icon = self.ICON_FOR_ACTION.get(action)
                    if icon and icon in retry_buttons:
                        x, y, _score = retry_buttons[icon]
                        return ACTION_NAMES[action], action, (x, y)
                image = fresh
        labels = [ACTION_NAMES[a] for a in point.actions]
        button = self.v.find(image, labels, (1100, 150, 1800, 860), exact=True)
        if not button:
            raise GameError(f"目标 {point.name} 当前没有允许的动作 {labels}，稍后重读或换备用目标")
        chosen = button.text.replace(" ", "")
        action = next((a for a in point.actions if ACTION_NAMES[a] == chosen), None)
        if action is None:
            # 2026-11-08 实机：新赛季“行军”按钮被 OCR 读成“研究”，但点击位置正确。
            # 当前目标只配置了一条动作时，按配置动作执行，日志也用配置名；
            # 多动作时仍拒绝，避免把另一个按钮当成配置动作。
            if len(point.actions) == 1:
                action = point.actions[0]
                chosen = ACTION_NAMES[action]
            else:
                raise GameError(f"{point.name} 的动作文字未核对：读到 {chosen!r}")
        return chosen, action, (button.x, button.y)

    def card_center(self, slot):
        """出征页第 slot 队的卡片中心 x。

        用户 2026-11-08 实机纠正：卡槽会按实际队伍数整体居中，不是固定五列。
        已知实际队伍数时按“卡宽250、间距250、整组居中”计算；没读到清单时才退回旧五列。
        """
        slots = sorted(getattr(self, "active_slots", None) or ())
        if not slots:
            return self.CARD_X[slot - 1]
        try:
            index = slots.index(slot)
        except ValueError:
            index = slot - 1
        return round(self.CARD_AREA_CENTER + (index - (len(slots) - 1) / 2) * self.CARD_SPACING)

    # 出征页卡片上这些状态明确不能派兵：观测到“可以拉”到真正提交之间，
    # 队伍可能已经被打了（2026-11-13 实机：观测停留 → 走到目标 → 卡片已是“撤退/重伤”，
    # 旧代码仍连点 3 次出征）。只在读到明确状态时拦，unknown 不拦，避免识别抖动卡死派兵。
    BLOCKED_CARD_STATES = {"wounded": "重伤", "retreat": "撤退", "fighting": "战斗",
                           "queued_battle": "排队进入战斗", "occupying": "占领",
                           "marching": "行军", "recruiting": "征兵", "guarding": "驻守"}

    def dispatch_card_block(self, image, policy: TeamPolicy, allow=(), assume_recruiting=False,
                            require_known=False) -> str:
        """提交前核对本队卡片状态；返回需要放弃本次派兵的中文原因（空串＝可以提交）。

        allow：允许通过的卡片状态（只给“拉线到线就拉”和“回城后调动营帐”用）。
        assume_recruiting：指令里没有攻占时，占领不可能是本次拉线的结果，把卡片“读不出”
        按征兵处理（用户 2026-11-13：从根上解决 unknown 干等）。
        require_known：卡片读不出时不提交（拉线含攻占、可能是占领时用）。
        """
        status = _vcall(self.v, "deployment_status", "unknown", image, policy.slot,
                        self.card_center(policy.slot))
        if status in allow:
            return ""
        if status == "unknown":
            if assume_recruiting:
                return ""
            if require_known:
                return "未识别"
            return ""
        return self.BLOCKED_CARD_STATES.get(status, "")

    def _dispatch(self, policy: TeamPolicy, obs: Observation, point: Point, kind: str):
        if self.j.pending(policy.key):
            raise GameError("该队上一笔命令还在核对中，稍后自动重试")
        if not self.account_checked and not self.verify_account():
            raise GameError("本次运行尚未核对账号，禁止派兵")
        self.emit("status", f"{policy.display} · 正在定位 {point_label(point)}")
        # 目标选中只有一条路：reopen_target 内部决定“镜头还在目标上就轻读，否则坐标导航”。
        # 调用方不再用布尔值替它猜镜头在哪（2026-11-18 实机根因）。
        image = self.reopen_target(point)
        chosen_action, action_key, tap_point = self.pick_action(image, point, kind)
        self.d.tap(tap_point[0], tap_point[1], .45)
        image = self.capture_clear("dispatch-preflight", True)
        self.emit("status", f"{policy.display} · 核对出征页面")
        if self.v.page(image) != "deployment":
            # 点了这一项却没进入出征页（如建造期间的攻城被拒）：换配置里的下一个动作再试。
            self.save_image(image, "action-page-unconfirmed", True)
            raise ActionUnavailable(f"{point.name} 的 {chosen_action} 未进入出征页", action_key)
        # 出征按钮灰色＝当前不能提交（地块超出范围、没有连接地等）：读上方说明并入异常，不盲点。
        if not _vcall(self.v, "deploy_button_enabled", True, image):
            hint = _vcall(self.v, "deployment_hint", "", image)
            self.save_image(image, "departure-disabled", True)
            self.emit("diagnostic", {"stage": "departure_disabled", "team": policy.key,
                                     "target": point.name, "reason": hint})
            self.log(f"{policy.display} / {point.name}：出征按钮不可用 —— {hint or '未读到上方说明'}", "WARN")
            raise GameError(f"出征按钮不可用：{hint or '未读到上方说明'}")
        # 选队：按本次实际队伍数计算卡片中心；少队时整组居中，不是固定五列。
        # 2026-11-23 实机根因：游戏进出征页默认选中空闲/满兵队；选队后必须重截，
        # 否则 line 查一键补兵、merit 查路线阻挡、transfer 查自动征兵都会看到默认旧队的画面。
        card_x = self.card_center(policy.slot)
        self.d.tap(card_x, self.CARD_Y, .3)
        image = self.capture()
        if kind == "transfer":
            for attempt in range(2):
                checkbox = self.v.labeled_checkbox(image, ["自动征兵", "自动证兵"], (1230, 780, 1570, 920))
                checked = self.v.checked(image, *checkbox) if checkbox else None
                if checked is not None:
                    break
                if attempt == 0:
                    self.d.control.sleep(.4)
                    image = self.capture()
                    if self.v.page(image) != "deployment":
                        raise RecognitionUnavailable("读取自动征兵时页面发生变化，重新读取队伍")
            if checked is None:
                self.save_image(image, "transfer-checkbox-unreadable", True)
                raise RecognitionUnavailable("调动页自动征兵状态暂未读清，稍后自动重读")
            if checked is False:
                # 用户 2026-11-13：有任务的队伍，脚本负责把“自动征兵”勾上。
                # 只改这一处、只对有任务的队伍；关掉的情况再复读确认一次。
                self.save_image(image, "transfer-checkbox-disabled", True)
                self.d.tap(*checkbox, .35)
                image = self.capture()
                checked_after = self.v.checked(image, *checkbox) if checkbox else None
                if checked_after:
                    self.log(f"{policy.display} 调动页“自动征兵”原为关闭，已勾选", "INFO")
                else:
                    self.log(f"{policy.display} 调动页“自动征兵”未能确认已勾选，继续调动", "WARN")
        if kind == "line" and policy.refill_enabled and (obs.maximum or policy.min_mode == "absolute"):
            # 兵力低于出击下限才补（不无条件补满，省辐重）；按钮不存在＝当前补不动。
            # 少队时卡槽整组居中，必须按真实卡中心读数，不能按固定五列的旧位置。
            current = self.v.deployment_strength(image, policy.slot,
                                                 center_x=self.card_center(policy.slot))
            floor = policy.threshold(obs.maximum)
            if current is not None and current < floor:
                if self.v.refill_button(image) is None:
                    self.save_image(image, "line-refill-missing", True)
                    raise LineRefillUnavailable("出征页没有一键补兵按钮，当前补不动")
                image, _ = self.line_refill(image, policy)
                strength_after = self.v.deployment_strength(image, policy.slot,
                                                            center_x=self.card_center(policy.slot))
                # 2026-11-23 实机根因：必须先判补后兵力是否已达到出击下限；
                # 只要已达标（如 31487/33000 = 95.4% ≥ 90%），即使此次补兵顺手把区块配额
                # 抽干归零，也应直接放行出征，不能误判补不动而撤回主城。
                if strength_after is None or strength_after < floor:
                    if self.v.refill_quota(image) == 0:
                        self.save_image(image, "line-refill-insufficient", True)
                        raise LineRefillUnavailable("一键补兵后区块归零，仍低于出击下限，判定补不动")
                    if strength_after is not None:
                        # 用户 2026-11-13：点了补兵还不到出击线，就回城（下一轮转补兵点/营帐），
                        # 不带残兵出去；回城/撤退/调动需要几分钟，等现场。
                        self.save_image(image, "line-refill-below-floor", True)
                        self.log(f"{policy.display} 拉线：一键补兵后 {strength_after}/{obs.maximum} "
                                 f"仍低于出击线 {floor}，回城补兵", "INFO")
                        raise LineRefillUnavailable("一键补兵后仍低于出击线，回城补兵")
        route = self.v.joined(image, (750, 108, 1250, 174))
        if "阻挡" in route or "无法" in route:
            raise ActionUnavailable(f"{point.name} 的当前路线不可用：{route}", action_key)
        # AGENTS 约定“派兵前核对账号、槽位、实际兵力与重伤”：
        # 观测之后队伍可能已经重伤/撤退/进入战斗，卡片状态是提交前最后一次现场证据。
        # 拉线/调动营帐允许“征兵”卡片：这两种流程就是要把正在征兵的队伍拉出去/调走。
        allow_recruiting = ("recruiting",) if kind in ("line", "transfer") else ()
        assume_recruiting = kind == "line" and "occupy" not in point.actions
        require_known = kind == "line" and not assume_recruiting
        blocked = self.dispatch_card_block(image, policy, allow=allow_recruiting,
                                           assume_recruiting=assume_recruiting,
                                           require_known=require_known)
        if blocked:
            self.save_image(image, "dispatch-team-not-ready", True)
            self.emit("diagnostic", {"stage": "dispatch_blocked", "team": policy.key,
                                     "target": point.name, "status": blocked})
            self.log(f"{policy.display} / {point.name}：出征页卡片显示{blocked}，本轮不派出", "WARN")
            raise DispatchUnconfirmed(f"出征页卡片显示{blocked}，本轮不派出", action=action_key,
                                      busy=True, page="deployment", status=blocked)
        submit_x, submit_y = self.SUBMIT_POINT
        ident = self.j.prepare(policy.key, kind, f"{chosen_action} {point_label(point)}")
        attempts = 0
        late = None
        # 用户 2026-11-13：拉线每轮只点一次「出征」，没确认就交给下一轮重读，不在同一页连点。
        attempt_limit = 1 if kind == "line" else DISPATCH_ATTEMPTS
        try:
            self.emit("team_action", {"team":policy.key,"state":"confirming","reason":f"正在下发{chosen_action}并核对状态变化"})
            # 闭环：发出指令后必须看到角标状态变化（停留/空闲 -> 行军/驻守/调动/战斗…）。
            # 状态没变说明没生效；拉线一轮只提交一次，武勋等仍可按原策略重试。
            while True:
                attempts += 1
                if attempts == attempt_limit and attempts > 1:
                    # 中间一次只重按出征；仍未受理时重新点选队伍，避免上次点击只是取消了选中。
                    self.d.tap(card_x, self.CARD_Y, .35)
                self.d.tap(submit_x, submit_y, 1.6)
                after = self.capture_clear("dispatch-result" if attempts == 1 else f"dispatch-result-{attempts}", True)
                after = self._settle_dispatch_prompts(after, policy, point.name)
                marker_after = self.read_team_marker(after, policy.slot)
                changed = bool(marker_after and obs.marker_state and marker_after["state"] != "unknown"
                               and marker_after["state"] != obs.marker_state)
                if changed:
                    accepted = True
                    result_state = LEGACY_STATE.get(marker_after["state"], "confirmed")
                    reason = f"角标状态 {obs.marker_state}→{marker_after['state']}"
                else:
                    marker_dx = (marker_after or {}).get("hud_dx", 0) if (marker_after or {}).get("state") not in (None, "unknown") else 0
                    accepted, result_state, reason = self.dispatch_verdict(after, policy, marker_dx)
                page = self.v.page(after)
                if page == "deployment" and obs.state == "outside" and result_state == "outside" and not changed:
                    accepted = False  # 提交前就在城外；停留卡片没有变化不证明已出发。
                if accepted or attempts >= attempt_limit:
                    break
                if page != "deployment":
                    break
                if not _vcall(self.v, "deploy_button_enabled", True, after):
                    hint = _vcall(self.v, "deployment_hint", "", after)
                    self.emit("diagnostic", {"stage": "departure_disabled", "team": policy.key,
                                             "target": point.name, "reason": hint})
                    self.log(f"{policy.display} / {point.name}：提交后按钮不可用 —— {hint or '未读到上方说明'}", "WARN")
                    break
                self.emit("status", f"{policy.display} · 出征页未受理，自动重新提交（第 {attempts+1} 次）")
            if not accepted:
                # 角标更新可能慢一帧：再确认一次，避免把已生效的命令误判成失败。
                self.d.control.sleep(1.0)
                late = self.capture("dispatch-late-check")
                late = self._settle_dispatch_prompts(late, policy, point.name)
                marker_late = self.read_team_marker(late, policy.slot)
                if (marker_late and obs.marker_state and marker_late["state"] != "unknown"
                        and marker_late["state"] != obs.marker_state):
                    accepted = True
                    result_state = LEGACY_STATE.get(marker_late["state"], "confirmed")
                    reason = f"角标状态（延迟确认）{obs.marker_state}→{marker_late['state']}"
            self.j.finish(ident, "accepted" if accepted else "rejected", f"{reason}；共提交 {attempts} 次")
            if not accepted:
                # 当场复核“队伍现在到底在干什么”：读得到且已忙起来，就本轮收手；
                # 读得到且仍停在原地，才算“这条指令没成”，允许按顺序换下一条。
                last = late if late is not None else after
                last_page = self.v.page(last)
                last_status = (self.v.deployment_status(last, policy.slot, self.card_center(policy.slot))
                               if last_page == "deployment" else "")
                marker_now = self.read_team_marker(last, policy.slot)
                marker_state = (marker_now or {}).get("state")
                if marker_state and marker_state != "unknown":
                    still_free = can_dispatch(marker_state)
                else:
                    still_free = last_status in ("outside", "home")
                brief = f"{last_page}/{last_status or '-'}" + (f"/角标{marker_state}" if marker_state else "")
                self.log(f"{policy.display} / {point.name}：{chosen_action} 未确认受理（{brief}）"
                         + ("；队伍仍在原地，按顺序换下一条" if still_free else "；队伍已忙起来，本轮不再补发"), "WARN")
                self.emit("team_action", {"team":policy.key,"state":"retrying",
                                          "reason":f"出征未确认受理（{reason}），下一轮读取现场后自动重试"})
                raise DispatchUnconfirmed(f"出征未确认受理（{reason}）", action=action_key,
                                          busy=not still_free, marker=marker_state,
                                          confidence=(marker_now or {}).get("confidence"),
                                          page=last_page, status=last_status)
            phase = ("backup" if kind == "transfer" else "line" if kind == "line"
                     else "guarding" if chosen_action == "驻守" else "merit")
            self.j.set_phase(policy.key, phase)
            self.log(f"{policy.display} 已提交并确认：{chosen_action} {point_label(point)}")
            self.emit("dispatch", {"team":policy.key,"kind":kind,"target":point.name,"state":result_state,"action":chosen_action})
        except DispatchUnconfirmed:
            raise
        except BaseException:
            if self.j.pending(policy.key):
                self.j.finish(ident, "uncertain", "提交过程中停止或异常，下一轮读取现场后自动重试")
                self.emit("team_action", {"team":policy.key,"state":"retrying","reason":"提交中断，下一轮读取现场后自动重试"})
            raise

    def dispatch_verdict(self, image, policy: TeamPolicy, hud_dx=0):
        """提交后一帧的受理判据。只认提交后新出现的行动证据，不看点击本身。

        hud_dx：已读角标给出的本队 HUD 布局（建筑内队伍会左移），只用于颜色扫描；
        没有可信角标时按标准布局读，避免把地图颜色当成受理证据。
        返回 (是否受理, 队伍状态, 说明)。
        """
        page = self.v.page(image)
        if page == "deployment":
            status = self.v.deployment_status(image, policy.slot, self.card_center(policy.slot))
            if status in ("marching", "returning", "guarding", "outside"):
                return True, status, f"结果页 deployment / {status}"
            return False, "confirmed", f"结果页 deployment / {status}"
        if page == "sandbox":
            y = (245, 383, 520, 660, 785)[policy.slot-1]
            activity = self.v.joined(image, (1690 + min(HUD_OFFSETS), y-60, 1915, y+78))
            # 颜色扫描用标准布局：已确认角标时按它的布局读，读不到再退回标准布局，
            # 不拿 -25 去扫地图（会把地图金色/蓝色误当成受理证据）。
            dx = hud_dx if isinstance(hud_dx, (int, float)) and min(HUD_OFFSETS) - SEARCH_DX <= hud_dx <= max(HUD_OFFSETS) + SEARCH_DX else 0
            hsv = cv2.cvtColor(image[y+3:y+42, 1840+dx:1900+dx], cv2.COLOR_BGR2HSV)
            yellow = cv2.countNonZero(cv2.inRange(hsv, (18, 100, 150), (40, 255, 255)))
            blue = cv2.countNonZero(cv2.inRange(hsv, (90, 70, 110), (125, 255, 255)))
            accepted = yellow > 90 or blue > 90 or any(s in activity for s in ("行军", "调动", "驻守"))
            if blue > 90 or any(s in activity for s in ("行军", "调动")):
                state = "marching"
            elif "驻守" in activity:
                state = "guarding"
            else:
                state = "confirmed"
            return accepted, state, f"结果页 sandbox / {activity}"
        return False, "confirmed", f"结果页 {page}"

    def handle_supply_prompt(self, image, policy=None, target=""):
        """已确认的补给提醒沿用今天不再提示的出征口径。"""
        info = _vcall(self.v, "supply_prompt", None, image)
        if info is None:
            return False
        self.save_image(image, "dispatch-supply-prompt", True)
        event = {"text": info["body"]}
        if policy is not None:
            event.update(team=policy.key, target=target)
        self.emit("supply_prompt", event)
        self._confirm_dispatch_reminder(image, info, "补给提醒")
        self.log("补给提醒：已确认出征，继续核对出发", "INFO")
        return True

    def read_auto_refill(self, image, policy):
        """读部队详情页「回城自动补兵」的真实状态：True / False / None（未读到）。

        用 _vcall 容忍测试/旧工具构造的 vision 没有该方法：读不到就是未知，不写不猜。
        """
        box = _vcall(self.v, "refill_checkbox", None, image)
        return _vcall(self.v, "checked", None, image, *box) if box else None

    def sync_auto_refill(self, image, policy):
        """按队同步游戏内「回城自动补兵」（用户 2026-11-12 定的同步模型）。

        游戏→界面：读到状态就写回 policy.auto_refill。
        界面→游戏：界面改动只标“待写入”；下一次访问该队详情页时读当前值，与待写入比，
        不同才点，复读确认后清标记；相同不点，直接清标记。外部改动以游戏为准。
        """
        state = self.read_auto_refill(image, policy)
        if state is None:
            return None
        policy.auto_refill = state
        want = policy.auto_refill_pending
        if want is None:
            return state
        if want == state:
            policy.auto_refill_pending = None
            self.log(f"{policy.display} 回城自动补兵与界面待写入一致，不点击")
            return state
        box = _vcall(self.v, "refill_checkbox", None, image)
        if box is None:
            return state
        self.emit("status", f"{policy.display} · 写入回城自动补兵：{'开' if want else '关'}")
        self.save_image(image, "auto-refill-before", True)
        self.d.tap(*box, .4)
        after = self.capture("auto-refill-after", True)
        confirmed = self.read_auto_refill(after, policy)
        if confirmed == want:
            policy.auto_refill = confirmed
            policy.auto_refill_pending = None
            self.log(f"{policy.display} 回城自动补兵已按界面设置写入并复核：{'开' if want else '关'}")
        else:
            if confirmed is not None:
                policy.auto_refill = confirmed
            self.log(f"{policy.display} 回城自动补兵写入未确认（读到 {confirmed}），保留待写入下次再试", "WARN")
        return policy.auto_refill

    def line_refill(self, image, policy):
        """拉线出征页：点「一键补兵」并读回执，返回 (补兵后帧, 补前区块值)。

        按钮是否存在由调用方先行判断；这里只负责点击与回执（补前/补后兵力与区块值）。
        """
        quota = self.v.refill_quota(image)
        before = self.v.deployment_strength(image, policy.slot,
                                            center_x=self.card_center(policy.slot))
        button = self.v.refill_button(image)
        self.emit("status", f"{policy.display} · 快速征兵（一键补兵）")
        self.save_image(image, "line-refill-before", True)
        self.d.tap(button[0], button[1], 1.2)
        after = self.capture("line-refill-after", True)
        strength = self.v.deployment_strength(after, policy.slot,
                                              center_x=self.card_center(policy.slot))
        quota_after = self.v.refill_quota(after)
        self.emit("refill", {"team": policy.key, "before": before, "after": strength,
                             "quota": quota, "quota_after": quota_after})
        self.log(f"{policy.display} 快速征兵：补前 {before} → 补后 {strength}"
                 f"（区块 {quota} → {quota_after}）")
        return after, quota

    def pave_dispatch(self, policy: TeamPolicy, obs: Observation, target: tuple[int, int], ident: str):
        """按铺路路线派兵。订单由调用方先原子落库，本方法只负责核对与提交。

        成功必须有“提交后的新行动证据”；停留队伍原地不动不算成功。
        """
        if not self.account_checked:
            raise GameError("本次运行尚未核对账号，禁止派兵")
        if observation_expired(self.p, obs):
            raise ObservationExpired("派兵观测已过期，重新读取后再提交")
        if obs.wounded is not False:
            raise GameError(f"{policy.display} 伤情未知或重伤中，暂不铺路")
        if obs.state not in ("home", "outside"):
            raise GameError(f"{policy.display} 当前状态 {obs.state}，暂不铺路")
        name = f"({target[0]},{target[1]})"
        self.emit("status", f"{policy.display} · 正在定位铺路目标 {name}")
        image = self.go_to(Point("铺路目标", target[0], target[1]))
        buttons = _vcall(self.v, "action_buttons", {}, image)
        if "occupy" not in buttons:
            # 目标可能刚被别人铺到：重截一帧再读一次，避免把一次识别抖动当成“没有攻占”。
            image = self.capture()
            buttons = _vcall(self.v, "action_buttons", {}, image)
        if "occupy" in buttons:
            x, y, _score = buttons["occupy"]
            self.d.tap(x, y, .45)
        else:
            button = self.v.find(image, ["攻占"], (1080, 150, 1810, 880), exact=True)
            if not button:
                other_action = bool(buttons) or self.v.find(
                    image, ["行军", "调动", "驻守", "攻城", "征兵", "升级", "扫荡"],
                    (1080, 150, 1810, 880), exact=True)
                if not other_action:
                    self.save_image(image, "pave-point-unconfirmed", True)
                    raise GameError(f"目标 {name} 的地块动作菜单未确认，不能按无攻占跳过")
                self.save_image(image, "pave-point-unavailable", True)
                raise PavePointUnavailable(f"目标 {name} 当前没有攻占指令")
            self.d.tap(button.x, button.y, .45)
        image = self.capture_clear("pave-preflight", True)
        page = self.v.page(image)
        title = self.v.joined(image, (750, 20, 1190, 125)).replace(" ", "")
        if page != "deployment" or "攻占" not in title:
            raise GameError(f"没有进入正确的出征页（{page} / {title or '未识别'}）")
        route = self.v.joined(image, (750, 108, 1250, 180))
        if "阻挡" in route or "无法" in route:
            raise GameError(f"出征路线不可用：{route}")
        if not _vcall(self.v, "deploy_button_enabled", True, image):
            hint = _vcall(self.v, "deployment_hint", "", image)
            self.save_image(image, "pave-disabled", True)
            self.emit("diagnostic", {"stage": "pave_departure_disabled", "team": policy.key,
                                     "target": name, "reason": hint})
            raise GameError(f"出征按钮不可用：{hint or '未读到上方说明'}")
        card_x = self.card_center(policy.slot)
        self.d.tap(card_x, self.CARD_Y, .3)
        image = self.capture()
        # 自动回城一旦勾选，占领结束就会回城，无法继续下一段。
        checkbox = self.v.labeled_checkbox(image, ["自动回城"], (1230, 780, 1570, 900))
        if checkbox is None:
            raise GameError("自动回城复选框未定位，不提交铺路命令")
        checked = self.v.checked(image, *checkbox)
        if checked is None:
            raise GameError("自动回城状态未识别，不点击开关")
        if checked is True:
            self.d.tap(*checkbox, delay=.4)
            image = self.capture()
            if self.v.page(image) != "deployment" or self.v.checked(image, *checkbox) is not False:
                raise GameError("自动回城无法确认取消，不提交铺路命令")
        # 提交前最后一次现场核对（与 _dispatch 同一口径）：观测之后队伍可能已被打，
        # 卡片明确显示撤退/重伤/战斗/行军等状态时本轮停手，绝不能点出征；unknown 不拦。
        blocked = self.dispatch_card_block(image, policy)
        if blocked:
            self.save_image(image, "pave-team-not-ready", True)
            self.emit("diagnostic", {"stage": "pave_dispatch_blocked", "team": policy.key,
                                     "target": name, "status": blocked})
            self.log(f"{policy.display} / {name}：出征页卡片显示{blocked}，本轮不铺路", "WARN")
            raise GameError(f"出征页卡片显示{blocked}，本轮不铺路")
        before = self.read_team_marker(image, policy.slot)
        before_state = before["state"] if before else obs.marker_state
        submit_x, submit_y = self.SUBMIT_POINT
        self.emit("team_action", {"team": policy.key, "state": "confirming",
                                  "reason": "正在下发攻占并核对状态变化"})
        tapped = False
        reason = ""
        accepted = False
        try:
            for attempt in range(DISPATCH_ATTEMPTS):
                if attempt:
                    self.d.tap(card_x, self.CARD_Y, .35)
                tapped = True  # 从发送提交命令开始，失败均保留为未知，不能推定未提交。
                self.d.tap(submit_x, submit_y, 1.8)
                after = self.capture_clear(f"pave-result-{attempt+1}", True)
                after = self._settle_dispatch_prompts(after, policy, name)
                after_marker = self.read_team_marker(after, policy.slot)
                if (after_marker and before_state and after_marker["state"] != "unknown"
                        and after_marker["state"] != before_state):
                    accepted = True
                    reason = f"角标状态 {before_state}→{after_marker['state']}"
                    break
                page = self.v.page(after)
                reason = f"结果页 {page}，角标状态仍为 {before_state or '未知'}"
                if page == "deployment" and not _vcall(self.v, "deploy_button_enabled", True, after):
                    hint = _vcall(self.v, "deployment_hint", "", after)
                    self.emit("diagnostic", {"stage": "pave_departure_disabled", "team": policy.key,
                                             "target": name, "reason": hint})
                    reason = f"出征按钮不可用：{hint or '未读到上方说明'}"
                    break
                if page != "deployment":
                    break
                self.emit("status", f"{policy.display} · 铺路提交未生效，自动重新提交（第 {attempt+2} 次）")
        except Exception as exc:
            if isinstance(exc, (Stopped, TransportUnavailable)):
                raise  # 保留 prepared + dispatching，由下次运行读现场恢复。
            if isinstance(exc, GameError) and not tapped:
                raise
            raise PaveUnknown(f"提交后读取结果失败：{exc}") from exc
        if not accepted:
            raise PaveUnknown(f"铺路出发未确认（{reason}），已保留待核对订单，不自动重发")
        self.j.set_phase(policy.key, "pave")
        self.log(f"{policy.display} 已提交并确认：铺路至 {name}（{reason}）")
        self.emit("dispatch", {"team": policy.key, "kind": "pave", "target": name,
                               "state": "marching", "action": "攻占"})
        return reason

    def recall(self, policy: TeamPolicy, obs: Observation, manual=False, minimum=None, line=False):
        # 用户 2026-11-04：刷武勋/自然征兵只要兵力达到本队出击下限就回城，不要求征到满兵；
        # 未指定 minimum 时仍按满兵（保持手动/旧调用语义）。
        # 用户 2026-11-13：拉线不套用这条“到线才回城”。拉线只要拉不出去（营帐里补不动、
        # 额度耗尽）就立刻回主城免费补兵，补完再调动回营帐继续拉；营帐里队伍是
        # “忙碌（建筑内征兵）”也允许回城。line=True 只放宽这两条，重伤/坐标缺失仍不回城。
        need = obs.maximum if minimum is None else minimum
        states = ("outside", "recruiting", "guarding", "active") if (manual or line) else ("outside", "recruiting")
        if (obs.strength is None or obs.maximum is None or obs.maximum <= 0
                or (not manual and not line and need is not None and obs.strength < need)
                or obs.wounded is not False
                or obs.state not in states):
            raise GameError("备用征兵尚未达到回城兵力或状态不明，不回城")
        if not self.account_checked and not self.verify_account():
            raise GameError("回城前账号未核对，不提交")
        y = (245, 383, 520, 660, 785)[policy.slot-1]
        image = self.locate_team(policy.slot)
        self.capture("return-preflight", True)
        if manual and obs.state == "guarding":
            cancel = self.context_command(image,["中止"])
            if not cancel:
                raise GameError("驻守中止按钮未识别，不撤军")
            self.d.tap(cancel.x, cancel.y, .7)
            image = self.capture()
        # 已到城池后的弯箭头；不点击行军中的“立即”加速按钮。
        body = self.v.joined(image, (790, 580, 1470, 796))
        if "立即" in body:
            raise GameError("该队仍在行军，不执行加速/付费操作")
        button = self.context_command(image,["回城"])
        if button:
            request = (button.x,button.y)
        else:
            # 手动回城或已到备用点时，只点当前队伍实际识别到的普通回城箭头。
            coord = self.v.map_coordinates(image)
            arrow = self.v.stationary_return_point(image, policy.slot)
            if arrow is None or not (manual or coord and any((b.x, b.y) == coord for b in configured_backup_points(self.p))):
                raise GameError("未确认补兵点位置和回城按钮")
            request = arrow
        # 用户可以在游戏里关闭回城提示；首次点回城前就保存意图，不能等弹窗出现后才记录。
        ident = self.j.prepare(policy.key, "return", "返回主城")
        try:
            self.emit("team_action",{"team":policy.key,"state":"return_confirming","reason":"正在核对普通回城请求"})
            result_state = "return_confirmed"
            self.d.tap(*request)
            prompt = self.capture()
            text = self.v.joined(prompt, (500, 430, 1440, 785))
            withdrawing = "部队处于战斗状态" in text and "确认撤退" in text
            if withdrawing and not manual:
                cancel = self.v.find(prompt,["取消"],(540,605,910,745),exact=True)
                if not cancel:
                    raise GameError("战斗撤退提示未识别取消按钮，等待人工核对")
                self.d.tap(cancel.x,cancel.y,.7)
                after = self.capture("return-battle-cancelled",True)
                if self.v.page(after) != "sandbox":
                    raise GameError("取消撤退后页面未确认，保留命令记录")
                self.j.finish(ident,"rejected","仍在战斗，已取消自动回城，等待战斗结束")
                self.emit("team_action",{"team":policy.key,"state":"fighting","reason":"仍在战斗，等待战斗结束后返回"})
                raise GameError("该队仍在战斗，已取消撤退，稍后再核对回城")
            if "是否确认返回" in text or withdrawing:
                confirm = self.v.find(prompt,["确定","确认"],(995,605,1360,745),exact=True)
                if not confirm:
                    raise GameError("回城确认按钮未识别")
                self.d.tap(confirm.x,confirm.y,1.2)
                after = self.capture("return-result",True)
                remaining = self.v.joined(after,(500,430,1440,785))
                accepted = "是否确认返回" not in remaining and "确认撤退" not in remaining
            elif self.v.page(prompt) == "sandbox":
                fresh = self.scan_team(policy)
                accepted = fresh.state in ("home","wounded","marching","returning")
                result_state = fresh.state
                self.capture("return-without-dialog",True)
            else:
                accepted = False
            self.j.finish(ident,"accepted" if accepted else "uncertain")
            if not accepted:
                raise GameError("回城结果待核对，保留记录防止重复撤军")
            self.j.set_phase(policy.key,"returning")
            self.emit("team_action",{"team":policy.key,"state":result_state,"reason":"普通回城请求已确认；继续读取返回和到家状态"})
            self.log(f"{policy.display} {'按界面请求' if manual else '补不动' if line else '满兵'}返回主城")
        except BaseException:
            if self.j.pending(policy.key):
                self.j.finish(ident,"uncertain","回城提交中停止或异常，等待核对")
                self.emit("team_action",{"team":policy.key,"state":"unknown","reason":"回城结果待核对；已保留命令，防止重复撤军"})
            raise

    def context_command(self,image,labels):
        # 部队菜单跟着地图上的选中对象移动，不能只在屏幕中央找。
        for rect in ((300,580,880,815),(750,580,1330,815),(1150,580,1600,815)):
            found = self.v.find(image,labels,rect,exact=True)
            if found:
                return found
        return None

    def open_affairs(self):
        image = self.ensure_sandbox()
        affairs = self.v.find(image, ["城务"], (745, 670, 1668, 948), exact=True)
        for attempt in range(3):
            if not affairs:
                self.d.tap(1695, 1035, delay=.6)
                image = self.capture()
                if _vcall(self.v, "page", "sandbox", image) != "sandbox":
                    image = self.wait_transition(image, expected=("sandbox",))
                affairs = self.v.find(image, ["城务"], (745, 670, 1668, 948), exact=True)
            if affairs:
                break
        if not affairs:
            raise GameError("未识别城务菜单，请关闭展开菜单后再试")
        self.d.tap(affairs.x, affairs.y, .7)
        image = self.capture()
        if self.v.page(image) != "affairs":
            raise GameError("没有进入城务")
        return image

    def open_bandit_list(self):
        image = self.ensure_sandbox()
        # 号角快捷入口：出现时是固定图形，直接进讨贼页，不必绕城务。
        # 号角只在有效奖励次数与可集结部队时出现；没识别到只回退城务，绝不推断次数为0。
        horn = self.v.horn_point(image)
        if horn:
            self.d.tap(horn["x"], horn["y"], .9)
            image = self.capture("bandit-horn-entry", True)
            if self.v.page(image) == "war" and self.v.find(image, ["讨贼"], (115, 197, 437, 336), exact=True):
                return image
            self.log("号角入口未确认到战争讨贼页，回退城务入口", "INFO")
            image = self.ensure_sandbox()
        return self._open_bandit_list_affairs()

    def _open_bandit_list_affairs(self):
        image = self.open_affairs()
        title = self.v.find(image, ["讨伐贼寇", "讨伐", "贼寇"], (455, 120, 992, 762))
        if not title:
            raise GameError("城务讨伐贼寇入口未识别")
        button = self.v.find(image, ["参与"], (485, 820, 970, 965), exact=True)
        if not button:
            if not self.v.find(image, ["已完成"], (485, 820, 970, 965), exact=True):
                self.save_image(image, "bandit-entry-unconfirmed", True)
                raise GameError("讨贼参与按钮未识别")
            # 城务“已完成”会撤掉参与按钮，仍可经同盟战事读取现有集结和真实余数。
            self.save_image(image, "bandit-affairs-completed", True)
            image = self.ensure_sandbox()
            alliance = self.v.find(image, ["同盟"], (975, 965, 1200, 1065), exact=True)
            if not alliance:
                raise GameError("同盟入口未识别，不进入其他菜单")
            self.d.tap(alliance.x, alliance.y, .8)
            image = self.capture()
            if self.v.page(image) != "alliance":
                raise GameError("未确认已加入同盟的主页，不参加讨贼")
            war = self.v.find(image, ["战事", "战争"], (1040, 440, 1320, 630), exact=True)
            if not war:
                raise GameError("同盟战事入口未识别")
            self.d.tap(war.x, war.y, .8)
        else:
            self.d.tap(button.x, button.y, .8)
        image = self.capture("bandits", True)
        if self.v.page(image) != "war":
            raise GameError("没有打开战争列表")
        label = self.v.find(image, ["讨贼"], (115, 197, 437, 336), exact=True)
        if label:
            self.d.tap(label.x, label.y)
            image = self.capture()
        return image

    def bandits(self,commit=False):
        allow_assist = self.p.bandit_allow_assist
        if self.account_checked and self.j.bandits_exhausted() and not allow_assist:
            self.emit("daily", {"task":"bandits", "result":"本7时周期奖励次数已用完，停止新讨贼；已有队伍继续监控"})
            return False
        cycle = cycle_key("bandits")
        image = self.open_bandit_list()
        text = self.v.joined(image, (460, 180, 1725, 904))
        rallies = self.v.bandit_rallies(image)
        open_rallies = [r for r in rallies if r.get("join")]
        mode, rewards = self.v.bandit_availability(image)
        if self.account_checked and mode != "unknown" and rewards is not None:
            self.j.observe_bandit_availability(mode, rewards, cycle)
        self.emit("bandit_availability", {"mode":mode, "remaining":rewards, "cycle":cycle})
        if "暂无战争" in text:
            result = "等待集结（暂无战争）"
        elif open_rallies:
            result = f"发现 {len(open_rallies)} 个当前可加入集结"
        elif rallies:
            result = "当前集结均已参加或不可加入"
        else:
            result = "列表已读取，加入条件待校验"
        self.emit("daily", {"task": "bandits", "result": result})
        self.emit("rallies", rallies)
        self.j.record_daily("bandits", "waiting")
        self.ensure_sandbox()
        exhausted = mode == "assist" or rewards == 0
        if exhausted and not allow_assist:
            self.emit("daily", {"task":"bandits", "result":"奖励次数已用完，停止新讨贼；协助开关关闭"})
            return False
        eligible = mode == "reward" and rewards is not None and rewards > 0 or allow_assist and mode == "assist" and rewards == 0
        if not eligible:
            self.emit("daily",{"task":"bandits","result":"奖励次数不足或未识别，暂不参加"})
            return False
        if not open_rallies or not commit:
            return bool(open_rallies)
        reserved = getattr(self, "reserved_teams", set())
        for policy in self.p.teams:
            if policy.key in reserved:
                continue
            if policy.role != "bandits" or policy.group != "main":
                continue
            if getattr(self, "active_slots", None) is not None and policy.slot not in self.active_slots:
                continue  # 本次部队列表没有这支讨贼队，不读、不派。
            if self.j.pending(policy.key) or self.j.failure_streak(policy.key) >= 3:
                continue
            obs = self.scan_team(policy)
            decision = bandit_decision(self.p,policy,obs,self.grain,
                                       food_gate=getattr(self, "food_gate", True))
            self.emit("decision",{"team":policy.key,"action":decision.action,"reason":decision.reason})
            if decision.action != "bandits":
                continue
            # 扫描队伍期间集结会变化，重新读取当前行，不按先前的行号或倒计时点加入。
            # 某个集结刚满员等已知未消耗失败时，不锁定队伍，继续尝试其他可加入集结。
            attempted = set()
            last_rejection = None
            fresh = []
            for _ in range(10):
                image = self.open_bandit_list()
                fresh = self.v.bandit_rallies(image)
                rally = next(
                    (r for r in fresh if r["text"].split("集结中")[0] not in attempted and r.get("join")),
                    None,
                )
                if rally is None:
                    break
                attempted.add(rally["text"].split("集结中")[0])
                try:
                    self.join_bandits(policy,obs,rally, allow_assist=allow_assist)
                except RallyJoinRejected as exc:
                    last_rejection = str(exc)
                    self.log(f"{policy.display} 加入集结被拒：{exc}", "INFO")
                    continue
                self.ensure_sandbox()
                return True
            self.ensure_sandbox()
            if last_rejection is not None:
                self.emit("daily", {"task":"bandits","result":f"{policy.display}：{last_rejection}；本轮无其他可加入集结"})
            return any(r.get("join") for r in fresh)
        return True

    def join_bandits(self,policy,obs,rally, *, allow_assist=False):
        if not self.account_checked or self.j.pending(policy.key):
            raise GameError("讨贼队伍的账号或上次命令未核对")
        self.d.tap(*rally["join"],delay=.8)
        image = self.capture("bandit-join-preflight",True)
        verified_rally = False
        rewards_before = None
        if self.v.page(image) == "rally":
            if "贼寇" not in rally["text"]:
                raise GameError("集结不是刚核对的贼寇任务，不参加")
            clock = clock_seconds(self.v.joined(image,(115,210,800,285)))
            mode, rewards = self.v.bandit_availability(image, detail=True)
            if mode != "unknown" and rewards is not None:
                self.j.observe_bandit_availability(mode, rewards, cycle_key("bandits"))
            if mode == "reward" and rewards is not None:
                rewards_before = rewards
            button = self.v.find(image,["参与集结"],(800,845,1120,940),exact=True)
            eligible = mode == "reward" and rewards is not None and rewards > 0
            if allow_assist:
                eligible = eligible or mode == "assist"
            if clock is None or clock <= 30 or not eligible or not button:
                raise GameError("当前集结时间、奖励次数或参与按钮不满足条件")
            deadline = time.time() + clock
            self.d.tap(button.x,button.y,.8)
            image = self.capture("bandit-deployment-preflight",True)
            verified_rally = True
        if self.v.page(image) != "deployment":
            text = self.v.joined(image,(300,240,1650,900))
            if any(s in text for s in ("满员","加入失败","参与失败")):
                raise RallyJoinRejected("集结已满或不可加入，未提交出征")
            raise GameError("讨贼加入页需要实机适配；已保存截图，没有提交出征")
        title = self.v.joined(image,(0,0,450,180))+self.v.joined(image,(750,0,1190,180))
        if not verified_rally or not any(s in title for s in ("讨贼","讨伐")):
            raise GameError("加入页任务不是已核对的讨贼，不派兵")
        x = self.card_center(policy.slot)
        self.d.tap(x, self.CARD_Y, .2)
        image = self.capture()
        if self.v.deployment_status(image, policy.slot, x) not in ("home", "unknown", None):
            raise GameError("讨贼出征卡片正在执行其他任务，本次跳过")
        route = self.v.joined(image,(750,108,1250,174))
        if any(s in route for s in ("阻挡","无法","超时")):
            raise GameError("讨贼路线当前不可用")
        submit = self.v.find(image,["出征","出证"],(1540,932,1895,1036))
        if not submit:
            raise GameError("讨贼出征按钮未识别")
        # 详情倒计时在导航/OCR期间持续减少，不能在最后提交时仍沿用旧秒数。
        travel = clock_seconds(self.v.joined(image, (1700, 835, 1900, 900)))
        if travel is None or deadline-time.time() <= travel+10:
            raise GameError("剩余集结时间不足以行军抵达或行军耗时未核对，不提交")
        ident = self.j.prepare(policy.key,"bandits",rally["text"])
        try:
            self.emit("team_action",{"team":policy.key,"state":"confirming","reason":"正在核对讨贼出发结果"})
            result_state = "confirmed"
            self.d.tap(x, self.CARD_Y, .08)
            self.d.tap(submit.x,submit.y,.5)
            # 成功/失败提示是短暂浮层：先抢一帧提示，再等流程关闭后取稳定页。
            toast = self.capture("bandit-join-toast",True)
            self.d.control.sleep(1.1)
            after = self.capture("bandit-join-result",True)
            page = self.v.page(after)
            if page in DISPATCH_PROMPT_PAGES:
                after = self._settle_dispatch_prompts(after, policy, rally["text"])
                page = self.v.page(after)
            elif page != "bandit_assist_confirm" and self.v.page(toast) == "bandit_assist_confirm":
                # 协助确认框可能先出现在提示帧。
                after, page = toast, "bandit_assist_confirm"
            if page == "bandit_assist_confirm":
                buttons = self.v.bandit_assist_prompt(after)
                if buttons is None:
                    raise GameError("协助讨贼确认按钮未核对")
                mode, rewards = "assist", 0
                self.j.observe_bandit_availability("assist", 0, cycle_key("bandits"))
                if not allow_assist or deadline-time.time() <= travel+10 or time.time()-obs.observed_at > 120:
                    self.d.tap(buttons[0].x, buttons[0].y, .7)
                    self.j.finish(ident, "rejected", "奖励已用尽或时效不足，已在明确的协助确认框取消；未确认协助出征")
                    raise GameError("已取消奖励耗尽后的协助；协助未开启或本次时效不足")
                self.d.tap(buttons[1].x, buttons[1].y, 1.6)
                after = self.capture("bandit-assist-result", True)
                after = self._settle_dispatch_prompts(after, policy, rally["text"])
                page = self.v.page(after)
            if page == "rally":
                self.ensure_sandbox()
                after = self.capture("bandit-join-sandbox",True)
                page = self.v.page(after)
            result_text = self.v.joined(toast,(300,240,1650,900)) + self.v.joined(after,(470,240,1510,895))
            acknowledged = any(s in result_text for s in ("加入成功","集结成功"))
            if page == "deployment":
                state = self.v.deployment_status(after, policy.slot, self.card_center(policy.slot))
                acknowledged = acknowledged or state in ("marching","guarding","outside")
                if state in ("marching","guarding","outside"):
                    result_state = state
            elif page == "sandbox":
                y = (245,383,520,660,785)[policy.slot-1]
                hsv = cv2.cvtColor(after[y+3:y+42,1840:1900],cv2.COLOR_BGR2HSV)
                blue = cv2.inRange(hsv,(90,70,110),(125,255,255))
                acknowledged = acknowledged or cv2.countNonZero(blue) > 90
                if cv2.countNonZero(blue) > 90:
                    result_state = "marching"
            if not acknowledged and page != "deployment":
                # 奖励次数在集结出发时才扣减，只能作为已消耗的正向证据。
                # 当前列表明确“已参加”才确认；仍可加入则本次未注册。目标消失可能同时
                # 意味着退出、取消或翻页，不能单独当作成功，保留待核对由队伍现场结清。
                if rewards_before is None:
                    stored = self.j.daily_context("bandit_availability", cycle_key("bandits"))
                    if stored and stored.get("mode") == "reward" and type(stored.get("remaining")) is int:
                        rewards_before = stored["remaining"]
                try:
                    list_image = self.open_bandit_list()
                    fresh = self.v.bandit_rallies(list_image)
                    check_mode, check_rewards = self.v.bandit_availability(list_image)
                except GameError:
                    fresh, check_mode, check_rewards = None, "unknown", None
                target_key = rally["text"].split("集结中")[0]
                target = next((r for r in fresh or [] if r["text"].split("集结中")[0] == target_key), None)
                failed_text = any(s in result_text for s in ("满员","加入失败","参与失败"))
                consumed = (check_mode == "reward" and check_rewards is not None
                            and rewards_before is not None and check_rewards < rewards_before)
                if consumed or target is not None and target.get("joined"):
                    acknowledged = True
                    result_state = "confirmed"
                elif failed_text and fresh is not None:
                    self.j.finish(ident,"rejected","页面提示加入失败（如集结已满员），判定未提交未消耗")
                    raise RallyJoinRejected("集结加入失败且未消耗奖励次数")
                elif target is not None and target.get("join"):
                    self.j.finish(ident,"rejected","提交后目标集结仍可加入，判定未提交未消耗")
                    raise RallyJoinRejected("集结加入未生效且未消耗奖励次数")
            self.j.finish(ident,"accepted" if acknowledged else "uncertain","讨贼加入结果需现场核对" if not acknowledged else "加入状态已确认")
            if not acknowledged:
                raise GameError("讨贼命令结果待核对，已锁定该队防止重复参加")
            self.j.set_phase(policy.key,"bandits")
            self.emit("dispatch",{"team":policy.key,"kind":"bandits","target":rally["text"],"state":result_state,"action":"协助讨贼" if mode == "assist" else "讨贼出征"})
            self.emit("daily",{"task":"bandits","result":f"{policy.display} 已确认加入；奖励结算继续核对"})
        except BaseException:
            if self.j.pending(policy.key):
                self.j.finish(ident,"uncertain","讨贼提交中停止或异常，等待核对")
                self.emit("team_action",{"team":policy.key,"state":"unknown","reason":"讨贼出发结果待核对；已锁定该队，防止重复参加"})
            raise

    def drill(self, commit=True):
        from .rewards import collect
        return collect(self, "drill", commit)

    def alliance_rewards(self, commit=True):
        from .rewards import collect
        return collect(self, "alliance_rewards", commit)

    def monthly_popup(self, commit=True) -> bool:
        image = self.capture()
        if self.v.page(image) != "monthly":
            return False
        body = self.v.joined(image, (500, 250, 1435, 940))
        claim = self.v.find(image, ["一键领取"], (735, 845, 1230, 975), exact=True)
        if not claim or not any(s in body for s in ("月卡", "每日", "领取")):
            raise GameError("月卡领取弹窗未通过校验")
        if not commit:
            self.emit("daily", {"task": "monthly", "result": "已关闭月卡弹窗；执行模式、开关及账号核对通过后由日常领取"})
            self.close_panel(image)
            return False
        if self.j.daily("monthly")[0] == "done":
            self.close_panel(image)
            return False
        if self.j.daily("monthly")[0] == "pending":
            raise GameError("月卡本周期领取结果未确认，请人工核对")
        cycle = self.j.prepare_daily("monthly", {"panel": body})
        self.d.tap(claim.x, claim.y, .8)
        after = self.capture("monthly-claimed", True)
        # 奖励标题位于约 y=220；从250裁剪会截断“恭喜获得”，金额图标没有黄金文字。
        body = self.v.joined(after, (540, 180, 1370, 860))
        if "黄金" in body or "获得" in body:
            self.j.finish_daily("monthly", cycle, {"reward": body})
            self.emit("daily", {"task": "monthly", "result": "月卡奖励已领取"})
            self.dismiss_overlay(after)
            return True
        raise GameError("月卡领取结果待核对，停止继续点击")

    def city_farm(self, inside=False):
        """Locate a current grain field through the game's reclamation list."""
        if not inside:
            image = self.open_affairs()
            if self.v.page(image) != "affairs":
                self.save_image(image, "farming-affairs-unrecognized", True)
                raise GameError("屯田前城务页面未确认，停止导航")
            button = self.v.find(image, ["参与"], (1000, 823, 1250, 958), exact=True)
            title = self.v.find(image, ["屯田"], (990, 120, 1265, 790))
            if not button or not title:
                self.save_image(image, "farming-entry-unavailable", True)
                raise FarmingUnavailable("城务屯田入口暂不可用，暂停新屯粮并继续部队监控")
            self.d.tap(button.x, button.y, 3)
            image = self.capture()
            if self.v.page(image) != "city":
                raise GameError("屯田入口没有进入自己的城内")
        elif self.v.page(self.capture()) != "city":
            raise GameError("屯田后城内画面未确认，停止继续点击")
        self.emit("status", "查找城内农田 · 领土→开垦→核对当前资源类型")
        self.open_city_resources()
        image = self.capture("farming-resource-list", True)
        total = self.v.city_resource_count(image)
        # 同名同级行可能完全一样，不按文字重复提前结束；每次滚动约三行并保留重叠。
        pages = min(30, math.ceil(total / 3) + 2) if total is not None else 12
        attempts = 0
        for page_number in range(pages):
            self.d.control.checkpoint()
            if self.v.page(image) != "territory":
                raise GameError("内城开垦列表页面变化，停止定位")
            rows = self.v.city_resource_rows(image)
            candidates = sorted([row for row in rows if row.resource == "grain"], key=lambda row: (-row.level, row.location.y))
            if candidates:
                candidate = candidates[min(attempts, len(candidates)-1)]
                attempts += 1
                self.emit("farming_search", {"page":page_number+1,"total":total,"name":candidate.name,"source":"territory_reclamation"})
                self.d.tap(candidate.location.x, candidate.location.y, 1.1)
                frame = self.capture("farming-field", True)
                for read_number in range(3):
                    scene = self.v.page(frame)
                    if scene == "blocked":
                        raise GameError("定位农田时登录被阻断，停止操作")
                    if scene == "city":
                        selected = self.v.city_farm_selection(frame)
                        if selected and selected.level == candidate.level:
                            self.emit("farming_field", {"name":selected.name,"remaining":selected.remaining,"source":"territory_reclamation"})
                            return frame, selected.button, selected.remaining
                    if read_number < 2:
                        self.d.control.sleep(.7)
                        frame = self.capture()
                if self.v.page(frame) != "city":
                    raise GameError("定位后未确认本人城内，停止继续点击")
                if attempts >= 3:
                    break
                # 地块类型改变或遮挡时重新读取列表，尝试另一条当前农田，不使用旧行坐标。
                self.open_city_resources()
                image = self.capture("farming-resource-list-retry", True)
                continue
            if page_number + 1 < pages:
                self.d.swipe(1380, 850, 1380, 460, 400)
                image = self.capture("farming-resource-list-scroll", True)
        raise FarmingUnavailable("未在内城开垦列表中确认可屯田的农田，暂停新屯粮并继续部队监控")

    def open_city_resources(self):
        image = self.capture()
        if self.v.page(image) != "city":
            raise GameError("必须在本人城内读取开垦地，停止定位")
        entry = self.v.find(image, ["领地"], (375, 0, 555, 82))
        if not entry:
            raise FarmingUnavailable("城内领土入口未识别，暂停屯粮")
        self.d.tap(entry.x, entry.y, .6)
        image = self.capture()
        if self.v.page(image) != "territory":
            raise GameError("没有进入已识别的领土列表")
        tab = self.v.find(image, ["开垦"], (120, 210, 1790, 305), exact=True)
        if not tab:
            raise FarmingUnavailable("领土开垦分类未识别，暂停屯粮")
        self.d.tap(tab.x, tab.y, .6)

    def check_farming_result(self, inside=False):
        cycle = cycle_key("farming")
        image, _, remaining = self.city_farm(inside=inside)
        self.j.observe_farming_remaining(remaining, cycle)
        if cycle != cycle_key("farming"):
            self.emit("daily", {"task":"farming","result":"屯田核对期间次数周期刷新，保留原记录，不用新周期次数确认旧结果"})
            self.ensure_sandbox()
            return False
        current = self.v.grain(image)
        confirmed = self.j.confirm_farming(remaining, current)
        if confirmed:
            _, count = self.j.daily("farming")
            self.emit("daily", {"task":"farming","result":f"城内屯粮已完成，本周期 {count}/{self.p.farming_budget} 次；游戏剩余 {remaining} 次"})
            self.emit("farming_check", {"confirmed":True,"remaining":remaining,"grain":current,"attempt":self.j.farming_attempt()})
        else:
            self.emit("daily", {"task":"farming","result":"屯田次数变化尚未确认，继续监控；暂停新增屯田"})
            self.emit("farming_check", {"confirmed":False,"remaining":remaining,"grain":current})
        self.ensure_sandbox()
        return confirmed

    def farming(self, commit=True, force=False):
        if not self.p.food_enabled:
            return
        if time.time() < self.farming_retry_at:
            return
        try:
            return self._farming(commit, force)
        except NetworkRecovered:
            raise
        except GameError as exc:
            # 屯粮失败只暂停本项；回到沙盘后仍须监控在途、重伤和备用补兵队伍。
            # 设备错误不会在这里吞掉，异地登录/维护也会由 ensure_sandbox 继续抛出。
            self.capture("farming-unavailable", True)
            self.log(str(exc), "WARN")
            self.emit("daily", {"task":"farming","result":str(exc)})
            self.ensure_sandbox()
            self.farming_retry_at = time.time() + max(180, self.p.retry_seconds)

    def _farming(self, commit=True, force=False):
        if not self.p.food_enabled:
            return
        status, count = self.j.daily("farming")
        # 先核对未完成记录，再检查库存/预算，避免恢复库存后遗留待核对命令。
        if status == "pending":
            if not self.j.farming_attempt():
                self.emit("daily", {"task":"farming","result":"旧屯田记录缺少次数证据，暂停新增屯田；部队继续监控"})
                self.ensure_sandbox()
                return
            self.emit("status", "正在核对上次城内屯田 · 不重复提交")
            if commit:
                self.check_farming_result()
            else:
                self.emit("daily", {"task":"farming","result":"试运行：上次屯田仍待核对，不提交"})
            return
        if self.grain is None or self.grain >= (self.p.food_recover if force else self.p.food_low):
            return
        if count >= self.p.farming_budget:
            self.emit("daily", {"task": "farming", "result": "本周期屯田预算已用完，等待粮食产出"})
            return
        if self.j.farming_exhausted():
            self.emit("daily", {"task":"farming","result":"本周期游戏屯田次数已用完，不重复进入；等待7时刷新或库存自然恢复，继续部队监控"})
            return
        self.emit("status", "正在打开城内农田 · 核对收益与剩余次数")
        cycle = cycle_key("farming")
        image, button, remaining = self.city_farm()
        self.j.observe_farming_remaining(remaining, cycle)
        before = self.v.grain(image)
        if remaining <= 0:
            self.emit("daily",{"task":"farming","result":"屯田次数已用完，等待次数刷新或库存自然恢复"})
            self.ensure_sandbox()
            return
        self.d.tap(button.x, button.y)
        image = self.capture("farming-preflight", True)
        spans = self.v.read(image, (560, 350, 1380, 782))
        text = " | ".join(t.text for t in spans)
        button = next((t for t in spans if t.text.replace(" ","") in ("屯田","确认","确定") and 740 < t.x < 1190 and 650 < t.y < 756),None)
        income = quantity(text.split("预计收入",1)[1]) if "预计收入" in text else None
        # 实机收益弹窗以麦穗图标表示粮食，没有“粮食”文字。前后都核对农田及明确收益。
        if "农田" not in text or income is None or income <= 0 or not button:
            raise GameError("未确认屯田收益是粮食")
        if not commit:
            self.emit("daily", {"task": "farming", "result": "试运行：可屯粮，不提交"})
            self.d.back()
            self.ensure_sandbox()
            return
        if cycle != cycle_key("farming"):
            self.emit("daily", {"task":"farming","result":"屯田预检期间次数周期刷新，取消本次提交，下轮重新核对"})
            self.d.back()
            self.ensure_sandbox()
            return
        self.j.prepare_farming(remaining, before, income)
        self.emit("status", "正在城内屯粮 · 等待结果，不重复点击")
        self.d.tap(button.x, button.y, .5)
        self.d.control.sleep(12)
        self.capture("farming-result", True)
        self.check_farming_result(inside=True)

    def open_activity(self):
        image = self.ensure_sandbox()
        icon = self.v.navigation_icon(image, 'activity')
        if not icon:
            entry = self.v.find(image, ['活动'], (140, 155, 1120, 202), exact=True)
            if entry:
                icon = (entry.x, entry.y)
        if not icon:
            toggle = self.v.navigation_toggle(image)
            if toggle:
                self.d.tap(*toggle, .6)
                image = self.wait_transition(self.capture(), expected=("sandbox",))
                icon = self.v.navigation_icon(image, 'activity')
        if not icon:
            self.capture('activity-menu-unrecognized', True)
            raise GameError('活动图标未识别，停止导航')
        self.d.tap(*icon, .8)
        image = self.capture('activity-open', True)
        if self.v.page(image) != 'activity':
            raise GameError('未进入活动页')
        return image

    def monthly(self, commit=True):
        from .daily_tasks import monthly
        return monthly(self, commit)

    def signin(self, commit=True):
        from .daily_tasks import signin
        return signin(self, commit)

    def event_signin(self, commit=True):
        if self.j.daily("events")[0] in ("done", "unavailable"):
            self.emit("daily", {"task":"events","result":"今日已完成活动检查，不再重复进入"})
            return
        image = self.open_activity()
        # 活动形式会随赛季变化，使用活动名称 + 明确签到领取按钮；不遍历“前往活动/分享”。
        label = self.v.find(image, ["活动签到", "登录有礼", "签到活动"], (65, 89, 320, 995))
        if not label:
            if commit:
                self.j.record_daily("events", "unavailable")
            self.emit("daily", {"task":"events","result":"当前活动列表未找到可识别的活动签到"})
            self.close_panel(image)
            return
        self.d.tap(label.x, label.y, .5)
        image = self.capture("event-signin", True)
        button = self.v.find(image, ["免费领取", "签到领取", "立即签到"], (345, 180, 1865, 1030), exact=True)
        pending = self.j.daily("events")[0] == "pending"
        if pending:
            if button is None:
                body = self.v.joined(image, (345, 180, 1865, 1030))
                if any(token in body for token in ("已领取", "已签到")):
                    self.j.finish_daily("events", cycle_key("events"), {"check": "已领取"})
                    result = "上次活动签到已确认领取"
                else:
                    result = "上次活动领取结果待核对，当前无领取按钮"
            elif commit:
                raise GameError("本周期活动领取结果未确认")
            else:
                result = "试运行：本周期活动领取结果待核对"
        elif button and commit:
            cycle = self.j.prepare_daily("events", {"button":button.text,"activity":label.text})
            self.d.tap(button.x, button.y, .6)
            after = self.capture("event-result", True)
            if "获得" in self.v.joined(after, (400, 180, 1500, 890)):
                self.j.finish_daily("events", cycle, {"reward":"获得"})
                self.dismiss_overlay(after)
                result = "活动签到奖励已领取"
            else:
                result = "活动领取结果待核对"
        else:
            result = "试运行：有可领活动签到" if button else "活动页无明确免费签到按钮"
        self.emit("daily", {"task":"events","result":result})
        self.ensure_sandbox()

    def visit(self, commit=True):
        from .daily_tasks import visit
        return visit(self, commit)

    def read_reserve_rates(self):
        """按已核对的主城征兵所路径只读，绝不切换快速征兵勾选。"""
        image = self.locate_team(1)
        self.check_account(image)
        if not self.account_checked or self.v.map_coordinates(image) != (self.p.main_city.x, self.p.main_city.y):
            raise GameError("第 1 队不在已校验主城，不能读取征兵所")
        self.d.tap(960, 512, 2.5)
        image = self.capture()
        if self.v.page(image) != "city":
            raise GameError("未进入主城内部")
        buildings = self.v.city_building_point(image)
        if buildings is None:
            raise GameError("城内建筑列表控件未核对，不点击旧位置")
        self.d.tap(*buildings, delay=.6)
        image = self.capture()
        if self.v.page(image) != "city_buildings":
            raise GameError("城内建筑列表未打开")
        card = self.v.find(image, ["征兵所"], (300, 730, 1765, 995))
        if not card:
            raise GameError("建筑列表征兵所未识别")
        self.d.tap(card.x, card.y, .8)
        image = self.capture()
        # 旧版连续猜两个地图点关闭列表、选中建筑；现在先重读，必要时只拖动城内地图。
        if self.v.page(image) not in ("city", "city_buildings"):
            raise GameError("征兵所定位后城内页面变化")
        if self.v.page(image) == "city_buildings":
            self.d.swipe(1500, 570, 1380, 570, 250)
            image = self.wait_transition(self.capture(), expected=("city",))
        image = self.clear_map_selection(image, expected_page="city")
        self.save_image(image, "reserve-building-located", True)
        building = self.v.find(image, ["征兵所"], (650, 300, 1300, 650), exact=True)
        if building is None:
            raise GameError("征兵所实际位置未识别，不点击旧建筑坐标")
        # 名称条下面才是建筑本体；从本帧名称锚定，不复用镜头中心或旧城内坐标。
        self.d.tap(building.x, building.box[3]+40, .5)
        image = self.capture("reserve-building-selected", True)
        if self.v.page(image) != "city":
            raise GameError("征兵所选中后页面变化")
        entry = self.v.find(image, ["征兵", "证兵"], (1080, 150, 1810, 880), exact=True)
        if not entry:
            raise GameError("征兵所当前菜单未识别，不继续点击")
        self.d.tap(entry.x, entry.y, .5)
        image = self.capture()
        if self.v.page(image) != "reserve":
            raise GameError("没有打开征兵所")
        pools = []
        for name in ("普通", "武卫一", "武卫二"):
            labels = ["主城", "普通", "普通预备兵"] if name == "普通" else [name, name+"预备兵"]
            tab = self.v.find(image, labels, (180, 180, 690, 850), exact=True)
            if tab is None:
                raise GameError(f"{name} 预备兵页签未识别，不点固定行")
            self.d.tap(tab.x, tab.y, .6)
            image = self.capture("reserve-"+name, True)
            if self.v.page(image) != "reserve":
                raise GameError("读取预备兵时页面变化")
            content = self.v.joined(image, (790, 185, 1810, 978))
            pools.append({"name": name, "text": content})
        self.emit("rates", pools)
        self.ensure_sandbox()

