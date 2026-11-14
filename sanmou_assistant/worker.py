from __future__ import annotations

import hashlib
import json
import time
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .game import (DispatchUnconfirmed, Game, GameError, LineRefillUnavailable, NavigationRecovered,
                   NetworkRecovered, ObservationExpired, PavePointUnavailable, PaveUnknown,
                   RecognitionUnavailable, TeamAbsent)
from .evidence import EvidenceStore
from .journal import Journal
from .models import (Decision, Observation, Point, Profile, decide, line_decision, line_target,
                     observation_expired, point_label)
from .team_states import LEGACY_STATE, can_dispatch
from .transport import Control, Device, Stopped, TransportUnavailable, discover_cli, instances
from .vision import Vision


def marker_of(obs):
    """角标状态（用户校准）；旧路径或未读到时为 None。"""
    return getattr(obs, "marker_state", None)


def _can_dispatch(obs):
    marker = marker_of(obs)
    return can_dispatch(marker) if marker is not None else obs.state in ("home", "outside", "recruiting")


def pave_at_point(obs, target):
    """铺路到点判定：必须是"停留在目标点"。

    调动停留（在营帐/城池里）不算铺路到点；旧路径没有角标时才退回旧的 outside 判定。
    """
    if obs.location != target:
        return False
    marker = marker_of(obs)
    if marker is None:
        return obs.state == "outside"
    return marker == "stationary"


def needs_detail(marker, has_pending):
    """快路径：只有"可能派出"或"有待核对订单"的队才值得开详情。

    重伤/待识别一律不派兵，角标本身已经够做决策；为读倒计时去开详情页
    每队要多花二十多秒，属于纯粹的空转，不再做。

    例外由 Worker.recheck_due() 单独处理：久未产出的队按 20 分钟到点读一次详情，
    读的是兵力（和到点后的重伤倒计时），不读征兵剩余时间。
    """
    if not marker:
        return True
    return can_dispatch(marker["state"]) or bool(has_pending)


class Worker(QThread):
    event = Signal(str, object)
    completed = Signal()

    # 2026-11-04：久未产出就按 20 分钟看一次兵力；武勋每 30 分钟读一次只作展示。
    RECHECK_SECONDS = 1200
    MERIT_INTERVAL = 1800
    # 拉线兵力缓存的刷新间隔：忙/可派且要按出击线判断时，超过这个秒数就完整读一次。
    LINE_STRENGTH_TTL = 180
    EVENT_HISTORY_LIMIT = 2000

    def __init__(self, profile: Profile, folder: Path, command="scan", dry_run=True, max_orders=0, duration=0):
        super().__init__()
        self.profile, self.folder, self.command, self.dry_run = profile, folder, command, dry_run
        self.control = Control()
        self.max_orders = max_orders
        self.duration = duration
        self.orders = 0
        self.retry_at = {}
        self.retry_reason = {}
        self.journal = None
        self.bandits_available = False
        self.next_bandit_check = 0.0
        self.next_daily_check = {}
        self.initial_pending = set()
        self.unreadable_batches = 0
        self.unreadable_batch_counts = {}
        self.unreadable_teams = {}
        self.idle_rounds = {}
        self.refresh_requested = threading.Event()
        self.transport_recoveries = []
        # 2026-11-04：状态只用角标，久了就按时间开一次详情读兵力；武勋只做展示。
        self.started_at = time.time()
        self.last_progress = {}      # 该队最近一次“有新动作”的时间
        self.progress_marker = {}    # 上次读到的角标状态，用于判断“有没有变化”
        self.last_recheck = {}       # 上次强制开详情的时间
        self.wounded_until = {}      # 重伤本地倒计时截止（从详情页读到的秒数）
        self.wounded_probed = {}     # 刚看到重伤时先立刻读一次倒计时，避免干等 10 分钟
        self.merit_value = None      # 最近一次武勋读数
        self.merit_base = None       # 本次任务开始时的武勋
        # 铺路快照沿用最近一次完整读数（兵力/伤情）；到点后不再为读数值多开一次总览。
        self.pave_strength = {}
        # 拉线队的兵力缓存（完整读数，含读取时间；之后快照复用，必要时刷新）。
        self.line_snapshot = {}
        # 拉线补不动回城后，等待下一轮调动到补兵点（营帐）。
        self.line_backup = set()
        # 已回到主城、做过一次完整兵力复核的拉线队（避免整帧缓存把回城补兵误判成没补）。
        self.line_home_seen = set()
        # 已完成“最后一次调动”的拉线队；所有拉线队都完成才停程序（用户 2026-11-13）。
        self.line_finished = set()
        self.merit_prev = None       # 上一次读数，用于单次增量
        self.next_merit_at = 0.0
        self.merit_failures = 0
        self.wait_log_at = {}        # “可派/不可派原因”限频日志
        self.published = []
        # 本赛季实际拥有的主队槽位；None 表示尚未读取。启动时确认一次，运行期间固定。
        self.active_slots = None
        self.roster_checked = False
        # 每次任务启动都重新核实主城坐标；不是每轮巡检都做。
        self.home_checked = False
        # 本次是否有实际刷武勋队；粮食检查和讨贼补粮门槛只跟随它。
        self.merit_active = False

    def request_refresh(self):
        self.refresh_requested.set()

    def publish(self, kind, value):
        if kind == "dispatch":
            self.orders += 1
        if kind != "preview":
            with (self.folder / "events.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"at":datetime.now().isoformat(), "event":kind, "value":value}, ensure_ascii=False)+"\n")
            self.published.append({"event": kind, "value": value})
            if len(self.published) > self.EVENT_HISTORY_LIMIT:
                del self.published[:-self.EVENT_HISTORY_LIMIT]
        self.event.emit(kind, value)

    def pause(self, value: bool):
        if value:
            self.control.pause.set()
        else:
            self.control.pause.clear()

    def stop(self):
        self.control.stop.set()

    def _scope(self):
        city = self.profile.main_city
        return (f"{self.profile.package}|{self.profile.account}|"
                f"{city.x if city else '?'}|{city.y if city else '?'}")

    def ensure_home(self, game):
        """每次任务启动核实一次主城坐标。

        用户 2026-11-08：主城位置以后会变，不能固定沿用上次坐标。启动后先回城、
        出城（若回城进入城内）、点主城中心，读坐标和归属名；位置变了就更新并保存。
        只做一次，不是每轮巡检；读不到就重试，仍读不到才停止，绝不拿旧坐标继续派兵。
        """
        if self.home_checked:
            return self.profile.main_city
        last_error = ""
        for attempt in range(3):
            self.control.checkpoint()
            try:
                game.calibrate_home()
            except NetworkRecovered:
                raise
            except (GameError, RecognitionUnavailable) as exc:
                last_error = str(exc)
                try:
                    game.ensure_sandbox()
                except (GameError, RecognitionUnavailable) as sandbox_error:
                    last_error = f"{last_error}；回到沙盘也失败：{sandbox_error}"
                self.control.sleep(1)
                continue
            self.home_checked = True
            return self.profile.main_city
        raise GameError(f"启动时未能核实主城坐标：{last_error or '未读到主城'}")

    def ensure_roster(self, game):
        """启动时读一次游戏部队列表，确认本次运行实际有几支主队。

        用户 2026-11-08：队伍数量是启动时的事实，运行期间不变；以游戏左侧
        “部队一/二/…”为准，不看右侧头像。读不到就重试几次，仍读不到才停止，
        绝不按配置里的 5 队去点空槽位。
        """
        if self.active_slots is not None:
            return self.active_slots
        team1 = next((t for t in self.profile.teams if t.group == "main" and t.slot == 1), None)
        if team1 is None:
            raise GameError("配置中没有主城第 1 队，无法读取部队列表")
        # 优先用非第 1 队做只读探针，避免去点正在集结/出征的第 1 队；
        # 该队不存在时再退回第 1 队。探针只点头像和部队总览，不派兵。
        probe = next((t for t in sorted(self.profile.teams, key=lambda t: t.slot)
                      if t.group == "main" and t.slot != 1), team1)
        last_error = ""
        for attempt in range(3):
            self.control.checkpoint()
            try:
                game.read_roster(probe.slot)
            except TeamAbsent as exc:
                last_error = str(exc)
                if probe is not team1:
                    probe = team1
                    continue
            except NetworkRecovered:
                raise
            except (GameError, RecognitionUnavailable) as exc:
                last_error = str(exc)
                if probe is not team1 and attempt == 0:
                    probe = team1
                    continue
                game.ensure_sandbox()
            if game.active_slots:
                self.active_slots = set(game.active_slots)
                break
            if not last_error:
                last_error = "部队总览没有读到有效行"
            self.control.sleep(1)
        if self.active_slots is None:
            raise GameError(f"未能从游戏部队列表确认本次队伍：{last_error or '未读到行'}")
        configured = sorted(t.slot for t in self.profile.teams
                            if t.group == "main" and t.participating)
        missing = [slot for slot in configured if slot not in self.active_slots]
        message = (f"本次启动确认 {len(self.active_slots)} 支主队"
                   f"（槽位 {sorted(self.active_slots)}）；按实际队伍工作，运行期间不再重读")
        if missing:
            message += f"；配置中的 {missing} 队当前没有，已跳过"
        game.log(message, "INFO")
        self.merit_active = any(t.role == "merit" and self.team_present(t.key)
                                for t in self.profile.teams)
        game.food_gate = self.merit_active
        return self.active_slots

    def team_present(self, key):
        """配置里的队伍键是否在当前实际队伍中；只对 main:N 生效。"""
        text = str(key)
        if not text.startswith("main:"):
            return True
        try:
            slot = int(text.split(":", 1)[1])
        except ValueError:
            return True
        # None 只出现在启动确认之前；正式批次开始前 ensure_roster 一定已经写入集合。
        return self.active_slots is None or slot in self.active_slots

    def active_policies(self, policies):
        """只保留主城队伍里本次部队列表实际存在的那几支。

        启动确认之前返回空列表，绝不回退到“配置里有 5 队”。
        """
        if self.active_slots is None:
            return []
        return [t for t in policies if t.group == "main" and self.team_present(t.key)]

    @staticmethod
    def has_merit_team(policies):
        """本次是否有队伍选择刷武勋；粮食检查只跟随刷武勋。"""
        return any(t.role == "merit" for t in policies)

    def food_recovery_state(self, policies, grain, stored=False):
        """粮食恢复标记：只有存在刷武勋队、补粮开关打开时才可能为真。

        用户 2026-11-09：没有队伍刷武勋时，粮食多少都无所谓，不触发屯田；
        其他讨贼/铺路/日常照常。grain 未读到时保留已有恢复标记，避免误放行。
        """
        if not self.has_merit_team(policies) or not self.profile.food_enabled:
            return False
        if grain is None:
            return bool(stored)
        return (stored or grain < self.profile.food_low) and grain < self.profile.food_recover

    def run(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        device = None
        game = None
        failure = ""
        try:
            from .diagnostics import environment
            (self.folder / "session.json").write_text(json.dumps({
                "environment": environment(), "command": self.command, "dry_run": self.dry_run,
                "profile": self.profile.to_dict()}, ensure_ascii=False, indent=2), encoding="utf-8")
            if self.command == "pave-reset":
                self.journal = Journal(self.folder.parent / "runtime.sqlite", self._scope())
                for route in self.profile.pave_routes:
                    if not route.team:
                        continue
                    if self.journal.pending(route.team):
                        self.publish("log", {"time": datetime.now().strftime("%H:%M:%S"), "level": "WARN",
                                             "message": f"路线 {route.name} 的队伍有待核对命令，先核对再重置"})
                        continue
                    self.journal.set_pave(route.name, 0, "idle", "用户重置铺路进度")
                self.publish("status", "铺路进度已重置；再次开始执行会从第一段重新核对")
                return
            if self.command == "runtime":
                import cv2
                import numpy as np
                canvas = np.full((110,600,3),255,dtype=np.uint8)
                cv2.putText(canvas,"33000/33000",(30,75),cv2.FONT_HERSHEY_SIMPLEX,1.6,(0,0,0),3,cv2.LINE_AA)
                text = Vision(self.control).joined(canvas,(0,0,600,110))
                if "33000/33000" not in text.replace(" ",""):
                    raise RuntimeError("OCR 运行环境检测未通过："+text)
                self.publish("log",{"time":datetime.now().strftime("%H:%M:%S"),"level":"INFO","message":"Qt、OpenCV、ONNX 与 OCR 模型已加载，数值样本识别通过"})
                self.publish("status","运行环境检测通过")
                return
            if self.command not in ("preview", "discover"):
                EvidenceStore(self.folder).check_capacity()
            self.publish("status", "连接中")
            cli = discover_cli(self.profile.cli_path)
            self.publish("instances", {"cli":cli, "instances":instances(cli)})
            if self.command == "discover":
                self.publish("status", "已发现实例")
                return
            device = Device(cli, self.profile.instance, self.control)
            device.emit = self.publish
            device.connect(launch=self.command in ("run", "launch"), minimize=self.profile.minimize_emulator)
            from .transport import run
            try:
                emulator_version = run([cli, "version"], timeout=5, control=self.control).strip()
            except (RuntimeError, OSError):
                emulator_version = "未读取到版本，请在 MuMu 设置中查看"
            self.publish("emulator", {"version": emulator_version, "adb": device.adb if hasattr(device, "adb") else ""})
            if self.profile.minimize_emulator:
                from .transport import minimize_emulator_windows
                minimized = minimize_emulator_windows(getattr(device, "info", {}))
                if minimized:
                    self.publish("status", f"模拟器窗口已最小化（{minimized} 个）· 截图与操作全部走后台输入")
            self.publish("connection", {"serial":device.serial,"resolution":f"{device.width}×{device.height}","instance":self.profile.instance,"name":getattr(device,"info",{}).get("name","MuMu")})
            self.publish("preview", device.capture())
            if self.command == "packages":
                packages = device.shell("pm", "list", "packages", "-3")
                games = sorted({line.split(":", 1)[1].strip() for line in packages.splitlines()
                                if line.startswith("package:") and "nslg" in line.lower()})
                self.publish("packages", games)
                self.publish("status", f"发现 {len(games)} 个三谋渠道包" if games else "没有发现三谋渠道包，请确认游戏已安装，或手动填写包名")
                return
            if self.command == "preview":
                image = device.capture()
                vision = Vision(self.control)
                page = vision.page(image)
                if page in ("sandbox", "city"):
                    value = vision.grain(image)
                    self.publish("grain", value)
                    self.publish("resource_status", {"visible": value is not None,
                                 "observed_at": time.time() if value is not None else None})
                self.publish("status", "画面已读取")
                return
            vision = Vision(self.control)
            vision.emit = self.publish
            # 主城尚未核实前先不建 Journal；核实后按新主城坐标建立 scope，避免沿用旧城键。
            game = Game(device, vision, self.profile, None, self.folder, self.publish)
            game.live_resources = True
            game.resource_refresh_requested = self.refresh_requested
            self.control.on_pause = lambda: game.passive_refresh(paused=True)
            game.claim_login_rewards = self.command in ("run", "dailies") and not self.dry_run
            if self.command not in ("preview", "discover", "runtime"):
                self.publish("status", "模拟器已连接 · 正在启动游戏")
                device.launch_game(self.profile.package)
                if self.profile.minimize_emulator:
                    # 启动游戏可能把模拟器窗口带回前台，再压回最小化。
                    minimize_emulator_windows(getattr(device, "info", {}))
                self.publish("status", "正在识别游戏登录状态")
                for _ in range(15):
                    page = vision.page(game.capture())
                    if page == "monthly":
                        # 启动弹窗先关闭，部队/主城核对账号以后再由日常主动领取。
                        game.monthly_popup(False)
                    elif page == "reconnect":
                        game.reconnect_game(game.last_image)
                    elif page == "sandbox":
                        break
                    elif page in ("worldmap", "city_buildings", "team", "overview", "deployment", "city", "affairs", "war", "rally", "reserve", "drill", "campaign", "activity", "welfare", "visit", "visit_result", "channel_menu", "territory", "monthly_claimed", "dismissible_overlay", "war_summary", "chat", "guide", "alliance", "bandit_assist_confirm", "camp_recruit_list", "camp_recruit", "navigable_page"):
                        try:
                            game.ensure_sandbox()
                        except NetworkRecovered as exc:
                            game.log(str(exc), "WARN")
                            continue
                        break
                    elif page == "blocked":
                        game.capture("login-blocked",True)
                        raise GameError("账号已在其他设备登录，停止操作")
                    elif page == "maintenance":
                        game.capture("server-maintenance", True)
                        raise GameError("游戏服务器维护，已停止任务；维护结束后手动开始")
                    else:
                        # 启动阶段：整屏活动/礼包弹层有已校准的右上叉号才关；其余未知页只等待。
                        try:
                            game.ensure_sandbox()
                        except (RecognitionUnavailable, NavigationRecovered) as exc:
                            game.log(str(exc), "WARN")
                            continue
                        break
                else:
                    if game.last_image is not None:
                        game.save_image(game.last_image, "startup-not-recognized", True)
                    raise GameError("游戏未进入已登录沙盘，请完成登录后再连接")
                if self.command == "launch":
                    self.publish("status", "游戏已就绪")
                    return
            for _ in range(3):
                try:
                    game.ensure_sandbox()
                    break
                except NetworkRecovered:
                    continue
            else:
                raise GameError("游戏网络反复断开，停止操作")
            # 每次任务启动都重新核实主城；位置变了就在这里更新，后面所有定位都用新坐标。
            self.ensure_home(game)
            scope = (f"{self.profile.package}|{self.profile.account}|"
                     f"{self.profile.main_city.x if self.profile.main_city else '?'}|"
                     f"{self.profile.main_city.y if self.profile.main_city else '?'}")
            self.journal = Journal(self.folder.parent / "runtime.sqlite", scope)
            game.j = self.journal
            self.initial_pending = {row[0] for policy in self.profile.teams
                                    for row in self.journal.pending(policy.key)}
            game.reserved_teams = self.pave_team_keys()
            if self.command == "calibrate":
                self.publish("status", "账号与主城已校验")
                return
            if self.command == "rates":
                game.read_reserve_rates()
                self.publish("status", "征兵所已读取")
                return
            if self.command == "farming":
                # 屯田只跟随刷武勋；没有刷武勋队时不执行，即使手动点屯田检查。
                if not any(t.role == "merit" for t in self.profile.teams):
                    self.publish("status", "没有刷武勋队；屯田只跟随刷武勋，本次不屯田")
                    return
                game.farming(not self.dry_run)
                self.publish("status","屯粮检查结束")
                return
            if self.command == "review":
                self.ensure_roster(game)
                for policy in self.profile.teams:
                    if policy.group != "main" or not self.team_present(policy.key):
                        continue
                    obs = game.scan_team(policy)
                    if obs.state in ("home", "wounded") and obs.strength is not None:
                        self.journal.review_team(policy.key)
                self.publish("status", "已核对在主城的队伍；城外未知命令继续保留")
                return
            if self.command == "dailies":
                self.ensure_roster(game)
                self.do_dailies(game)
                self.publish("status", "日常检查结束")
                return
            if self.command == "return":
                self.ensure_roster(game)
                for policy in self.profile.teams:
                    if policy.group != "main" or not self.team_present(policy.key) or not self.journal.phase(policy.key):
                        continue
                    obs = self.observe_team(game, policy)
                    if obs is None:
                        continue
                    if self.journal.pending(policy.key):
                        self.publish("decision", {"team":policy.key,"action":"wait","reason":"上一笔回城或出征尚未结清，先继续核对，不重复回城"})
                        continue
                    if obs.state in ("guarding", "outside", "recruiting") and obs.wounded is False:
                        if self.dry_run:
                            game.log(f"试运行：{policy.display} 可以请求普通回城")
                        else:
                            game.recall(policy,obs,manual=True)
                self.publish("status","回城检查完成，正在行军的队伍继续原任务")
                return
            food_recovery = self.journal.flag("food_recovery") if self.profile.food_enabled else False
            hide_checked = False
            started = time.monotonic()
            while True:
                self.control.checkpoint()
                if (not hide_checked and self.command == "run" and not self.dry_run
                        and self.profile.hide_before_run):
                    try:
                        state = game.ensure_hide_on()
                    except GameError as exc:
                        game.log(f"打开游戏隐藏显示未完成：{exc}；本轮继续", "WARN")
                    else:
                        if state is None:
                            # 启动瞬间画面还没稳定：不记“已检查”，下一轮再试。
                            game.log("游戏隐藏状态暂未读清，下一轮再试", "INFO")
                        else:
                            hide_checked = True
                # F2：游戏进出主城会自己关掉隐藏；回到沙盘后立刻重核一次（用户确认“够用”）。
                if (hide_checked and game.city_visited and self.command == "run" and not self.dry_run
                        and self.profile.hide_before_run):
                    game.city_visited = False
                    try:
                        game.ensure_hide_on()
                    except GameError as exc:
                        game.log(f"回沙盘后重核游戏隐藏显示未完成：{exc}；本轮继续", "WARN")
                if self.duration and time.monotonic()-started >= self.duration:
                    self.publish("status", "本次验收时限已到，停止新动作")
                    return
                try:
                    self.publish("status", "监控中 · 正在读取队伍" if self.command == "scan" else "只监控" if self.dry_run else "运行中")
                    if not self.roster_checked:
                        self.ensure_roster(game)
                        self.roster_checked = True
                    policies = ([t for t in self.profile.teams if not t.role == "hidden"] if self.command == "scan"
                                else [t for t in self.profile.teams if t.role in ("merit", "bandits")])
                    # 只处理主城队伍里本次部队列表实际存在的那几支；亲卫不参与这三个功能。
                    policies = self.active_policies(policies)
                    # 粮食只跟随“刷武勋”：没有刷武勋队时，粮食多少都不屯田，
                    # 也不因粮食恢复标记拦住讨贼/铺路/日常。
                    merit_active = self.has_merit_team(policies)
                    self.merit_active = merit_active
                    game.food_gate = merit_active
                    if merit_active and self.command == "run" and self.profile.food_enabled:
                        game.ensure_sandbox()
                        food_recovery = self.food_recovery_state(policies, game.grain, food_recovery)
                        self.journal.set_flag("food_recovery", food_recovery)
                        if food_recovery:
                            game.farming(not self.dry_run, force=True)
                            food_recovery = self.food_recovery_state(policies, game.grain, food_recovery)
                            self.journal.set_flag("food_recovery", food_recovery)
                    else:
                        food_recovery = False
                    game.reserved_teams = self.pave_team_keys()
                    # 2026-11-04：武勋只作展示——本次任务第一次读数作基线，之后每 30 分钟一次。
                    # 读不到就跳过一次，不影响任何派兵。
                    if self.command == "run" and not self.dry_run:
                        self.check_merit(game)
                    # 讨贼独立成队，按周期检查；武勋批次不因讨贼插入而中断。
                    if self.command == "run" and self.bandits_due() and any(t.role == "bandits" for t in policies):
                        self.check_bandits(game,allow_execute=not food_recovery)
                    limited = self.order_limit_reached()
                    # 粮食恢复标记按已读库存重算：只有刷武勋时才可能置位；
                    # 没有刷武勋队时保持 false，讨贼/铺路/日常不受粮食影响。
                    if merit_active and self.profile.food_enabled and game.grain is not None:
                        fresh_recovery = self.food_recovery_state(policies, game.grain, food_recovery)
                        if fresh_recovery != food_recovery:
                            food_recovery = fresh_recovery
                            if self.command == "run" and not self.dry_run:
                                self.journal.set_flag("food_recovery", food_recovery)
                    pave_keys = self.pave_team_keys() if self.command == "run" and not self.dry_run else set()
                    batch_policies = policies if self.command == "scan" or self.dry_run else [t for t in policies if (t.role == "merit" or self.journal.pending(t.key)) and t.key not in pave_keys]
                    if not limited:
                        limited = self.process_team_batch(game, batch_policies, food_recovery)
                    if self.command == "run" and not self.dry_run:
                        # 铺路路线独立推进：到达当前段才派下一段，不占武勋批次。
                        self.process_pave(game, food_recovery)
                        # 拉线独立推进：单坐标反复拉，独立于刷武勋与铺路。
                        self.process_line(game, food_recovery)
                    if self.command == "run" and self.bandits_due():
                        # 验收达到出征上限以后仍可只读列表，绝不再提交新出征。
                        self.check_bandits(game,allow_execute=not food_recovery and not limited)
                        limited = limited or self.order_limit_reached()
                    if limited:
                        game.ensure_sandbox()
                        self.publish("preview", game.capture())
                        self.order_limit_reached()
                        return
                    if self.command == "run" and not self.dry_run:
                        # 讨贼专用队的状态读取放在武勋连续派出之后。
                        reserved = self.pave_team_keys()
                        self.process_team_batch(game, [t for t in policies if t.role == "bandits" and t.key not in reserved
                            and (self.bandits_enabled_today() or self.journal.pending(t.key) or self.journal.phase(t.key) == "bandits")], food_recovery)
                    if self.command == "run":
                        # 部队先行动；每轮最多一个有界日常流程，不占住沙盘反复签到。
                        self.do_dailies(game, max_tasks=1)
                    if self.command == "scan":
                        game.ensure_sandbox()
                        self.publish("preview", game.capture())
                        self.publish("status", "部队读取完成")
                        return
                    image = game.ensure_sandbox()
                    self.publish("preview", image)
                    self.publish("status", f"等待下一次检查 · {self.profile.poll_seconds} 秒")
                    self.wait_for_next_check(game)
                except TransportUnavailable as exc:
                    self.recover_transport(game, device, exc)
                    continue
                except NetworkRecovered as exc:
                    game.log(str(exc), "WARN")
                    continue
                except RecognitionUnavailable as exc:
                    if self.command != "run":
                        raise
                    # 2026-11-10 用户：跨天/活动等已识别弹层要自行关闭后继续铺路，不靠人工。
                    try:
                        game.ensure_sandbox(recover_navigation=True)
                        game.log(f"{exc}；已关闭已识别弹层并继续监控", "INFO")
                    except (RecognitionUnavailable, NavigationRecovered) as recover_exc:
                        # 用户 2026-11-13：战斗/攻城特效把建筑详情留在屏幕上时，页面会判未知；
                        # 左右滑动取消选中后就能回到沙盘，不必干等到特效结束。
                        try:
                            recovered = game.deselect_unknown_page()
                        except GameError:
                            raise
                        if recovered:
                            game.log(f"{exc}；左右滑动取消选中后回到沙盘，继续监控", "INFO")
                            continue
                        game.log(f"{exc}；弹层暂未读清（{recover_exc}），等待重读", "WARN")
                    except GameError:
                        raise
                    self.publish("status", f"等待重新识别 · {self.profile.poll_seconds} 秒后重试")
                    self.wait_for_next_check(game)
        except Stopped:
            self.publish("status", "已停止新动作；游戏中的行军/征兵继续")
        except Exception as exc:
            import traceback
            failure = str(exc)
            if game is not None and getattr(game, "last_image", None) is not None:
                try:
                    game.save_image(game.last_image, "worker-failed", True)
                except Exception:
                    pass
            self.publish("diagnostic", {"stage":"worker", "exception":type(exc).__name__,
                                        "traceback":traceback.format_exc()})
            self.publish("log", {"time":datetime.now().strftime("%H:%M:%S"),"level":"ERROR","message":str(exc)})
            self.publish("status", "已停止 · " + str(exc))
            self.publish("error", str(exc))
        finally:
            self.control.on_pause = None
            try:
                if self.journal:
                    self.journal.close()
            finally:
                try:
                    if device:
                        device.close()
                finally:
                    try:
                        # 手动停止也留一份本次会话的事件摘要，方便和历史会话对比；
                        # 以前只有命令行入口会写，界面里停止后没有 result.json。
                        (self.folder / "result.json").write_text(
                            json.dumps(self.published, ensure_ascii=False, indent=2), encoding="utf-8")
                    except OSError:
                        pass
                    try:
                        from .diagnostics import write_session_report
                        write_session_report(self.folder, failure)
                    except Exception:
                        pass  # 诊断保存失败不能妨碍释放控制权和退出。
                    self.completed.emit()

    def wait_for_next_check(self, game):
        end = time.monotonic() + self.profile.poll_seconds
        while time.monotonic() < end:
            self.control.checkpoint()
            game.passive_refresh()
            self.control.sleep(min(.2, max(0, end-time.monotonic())))

    def recover_transport(self, game, device, error):
        now = time.monotonic()
        self.transport_recoveries = [at for at in self.transport_recoveries if now - at < 600]
        if len(self.transport_recoveries) >= 3:
            raise RuntimeError("10 分钟内 ADB 输入连接已恢复 3 次仍反复失败，停止操作") from error
        self.transport_recoveries.append(now)
        game.account_checked = False
        game.grain = None
        self.publish("status", "设备短暂断连 · 正在恢复连接，随后重新读取队伍")
        game.log(f"{error}；仅恢复连接，保留待核对订单", "WARN")
        self.control.sleep(2)
        image = device.recover_channel()
        self.publish("preview", image)
        self.publish("transport", {"stage": "input", "recovery": "restored", "replayed": False})
        # 新循环从现场重新导航和读取；不继续之前的选队/派兵流程。

    def observe_team(self, game, policy):
        try:
            obs = game.scan_team(policy)
        except TeamAbsent as exc:
            # 本赛季尚未拥有的槽位不是故障：不读详情、不派兵、不写 WARN。
            self.publish("observation", Observation(policy.key, note=str(exc)).__dict__)
            return None
        except NetworkRecovered:
            raise
        except GameError as exc:
            game.log(f"{policy.display} 读取未完成：{exc}", "WARN")
            # 只恢复已识别页面；异地登录、未知页面会再次抛出并停止整机。
            game.ensure_sandbox()
            obs = Observation(policy.key, note=str(exc))
            self.publish("observation", obs.__dict__)
        if obs.state == "unknown":
            failures = self.unreadable_teams.get(policy.key, 0) + 1
            self.unreadable_teams[policy.key] = failures
            if failures == 2 or failures % 3 == 0:
                game.log(f"{policy.display} 连续 {failures} 次状态未识别；本队等待重新读取，其他队继续", "WARN")
                self.publish("team_action", {"team":policy.key,"state":"unknown",
                             "reason":f"连续 {failures} 次未读到状态，等待重新读取"})
        else:
            self.unreadable_teams.pop(policy.key, None)
        for ident,kind,target,status,created in self.journal.pending(policy.key):
            if kind == "pave":
                # 专用恢复还必须核对原地、目标和路线进度，通用观测不能提前结清。
                continue
            if kind == "return" and obs.state == "home" and obs.strength is not None:
                self.journal.finish(ident,"accepted","实时观测确认已经回到主城")
                self.journal.set_phase(policy.key,"")
            elif obs.state in ("marching", "returning", "guarding", "wounded"):
                self.journal.finish(ident,"accepted","后续实时观测确认队伍已行动/重伤返回")
                self.journal.set_phase(policy.key,"backup" if kind == "transfer" else "returning" if kind == "return" else "bandits" if kind == "bandits" else "line" if kind == "line" else "guarding" if obs.state == "guarding" and kind == "merit" else "merit")
            elif kind in ("merit", "transfer", "bandits", "line") and obs.state in ("outside", "recruiting") and obs.strength is not None and obs.wounded is False:
                self.journal.finish(ident,"accepted","实时队伍详情确认已在城外到达或征兵")
                self.journal.set_phase(policy.key,"backup" if kind == "transfer" else "bandits" if kind == "bandits" else "line" if kind == "line" else "merit")
            elif kind != "pave" and obs.state == "home" and obs.strength is not None and obs.wounded is False and time.time()-created >= self.profile.retry_seconds:
                # 讨贼加入成功会让队伍立刻出城；冷却后仍在主城说明未提交（如满员未消耗），
                # 结清为 rejected 允许重试，不再永久锁定该队。
                self.journal.finish(ident,"rejected","冷却后现场确认在主城，没有未结束的出征")
                game.log(f"{policy.display} 的未确认命令已核对：队伍仍在主城，下一轮按策略自动重试","INFO")
        if obs.state == "home" and self.journal.phase(policy.key) in ("backup", "returning", "bandits", "guarding", "line"):
            self.journal.set_phase(policy.key, "")
        return obs

    def observe_pave_team(self, game, policy, full=False):
        """铺路专用观测：等待途中只读角标/坐标/重伤，到点才用完整读数派兵。

        用户 2026-11-10：阶段点之间要尽快衔接，等待期间不要做无意义操作。
        完整观测给出本段出发时的兵力与伤情；之后的行军/占领期沿用该读数。
        """
        cached = self.pave_strength.get(policy.key)
        if full or cached is None:
            obs = self.observe_team(game, policy)
            if obs is not None and obs.strength is not None:
                self.pave_strength[policy.key] = (obs.strength, obs.wounded)
            return obs
        obs = (game.scan_pave_team(policy) if hasattr(game, "scan_pave_team")
               else self.observe_team(game, policy))
        if obs.strength is None:
            obs.strength = cached[0]
        if obs.wounded is None:
            obs.wounded = cached[1]
        return obs

    def team_decision(self, game, policy, obs, food_recovery):
        if self.command == "run" and not self.dry_run and policy.key in self.pave_team_keys():
            return Decision(reason="该队保留给未完成的铺路路线")
        decision = decide(self.profile, policy, obs, game.grain, self.journal.phase(policy.key),
                          food_gate=self.merit_active)
        if food_recovery and decision.action in ("merit", "transfer"):
            decision = Decision(reason="粮食恢复中，等待达到恢复库存")
        if self.journal.pending(policy.key):
            return Decision(reason="上一笔命令正在结清，本轮先等现场核对")
        if decision.action not in ("wait", "bandits") and time.time() < self.retry_at.get(policy.key, 0):
            return Decision(reason=self.retry_reason.get(policy.key, "等待操作间隔") + "；稍后自动重读并重试")
        return decision

    def defer_team(self, key, error):
        delay = (self.unconfirmed_delay(key) if isinstance(error, DispatchUnconfirmed)
                 else self.profile.poll_seconds if isinstance(error, (RecognitionUnavailable, ObservationExpired))
                 else self.profile.retry_seconds)
        self.retry_at[key] = time.time() + delay
        self.retry_reason[key] = str(error)

    def note_progress(self, policy, obs):
        """记录“这队最近什么时候有变化”，并维护重伤本地倒计时。

        2026-11-04 用户口径：不从角标推断忙碌在做什么（黄色时钟覆盖征兵/占领/铺路），
        只看“这队多久没动”；到点开一次详情读兵力就够了。重伤例外：读到倒计时后本地倒数。
        """
        key = policy.key
        state = obs.marker_state or obs.state
        now = time.time()
        if self.progress_marker.get(key) != state:
            self.progress_marker[key] = state
            self.last_progress[key] = now
        else:
            self.last_progress.setdefault(key, now)
        if obs.wounded:
            if obs.injury_seconds is not None:
                self.wounded_until[key] = now + int(obs.injury_seconds)
                self.wounded_probed.pop(key, None)
        else:
            self.wounded_until.pop(key, None)
            self.wounded_probed.pop(key, None)

    def take_recheck(self, key, now, gap):
        if now - self.last_recheck.get(key, 0.0) < gap:
            return False
        self.last_recheck[key] = now
        return True

    def recheck_due(self, key, marker):
        """角标不可派、且久未产出 → 到点开一次详情（读兵力；重伤读到点后的倒计时）。

        既不猜“忙碌=征兵/占领/铺路”，也不读征兵剩余时间。
        """
        state = (marker or {}).get("state")
        if not state or state == "unknown" or can_dispatch(state):
            return False
        now = time.time()
        until = self.wounded_until.get(key)
        if state == "wounded":
            if until is None:
                # 刚看到重伤：下一轮立刻读一次倒计时；读不到就 10 分钟再试，不在重伤期间空转。
                if not self.wounded_probed.get(key):
                    self.wounded_probed[key] = True
                    return self.take_recheck(key, now, 30)
                return self.take_recheck(key, now, 600)
            return now >= until and self.take_recheck(key, now, 60)
        if until is not None and now < until:
            return False
        last = max(self.last_progress.get(key, 0.0), self.started_at)
        if now - last < self.RECHECK_SECONDS:
            return False
        return self.take_recheck(key, now, self.RECHECK_SECONDS)

    def log_recheck(self, policy, obs):
        """到点复核后写一行带数字的日志：多久没动、在什么角标、兵力多少。"""
        minutes = int((time.time() - self.last_progress.get(policy.key, self.started_at)) / 60)
        label = obs.marker_label or obs.state or "未知"
        extra = ""
        if obs.wounded:
            until = self.wounded_until.get(policy.key)
            if until:
                extra = f"，重伤剩余约 {max(0, int(until - time.time()))} 秒"
        troops = f"兵力 {obs.strength}/{obs.maximum}" if obs.strength is not None else "兵力未读到"
        when = f"已 {minutes} 分钟没有新动作" if minutes >= 1 else "刚刚变为"
        self.publish("log", {"time": datetime.now().strftime("%H:%M:%S"), "level": "INFO",
                             "message": f"{policy.display} {when}（{label}{extra}）；详情：{troops}"})

    def log_once(self, key, message, level="INFO", every=3600):
        now = time.time()
        if now - self.wait_log_at.get(key, 0.0) < every:
            return
        self.wait_log_at[key] = now
        self.publish("log", {"time": datetime.now().strftime("%H:%M:%S"), "level": level, "message": message})

    def check_merit(self, game):
        """每 30 分钟读一次个人信息页武勋；只做展示，失败不影响任何派兵。"""
        now = time.time()
        if now < self.next_merit_at:
            return
        self.next_merit_at = now + self.MERIT_INTERVAL
        try:
            value, text = game.read_merit()
        except (GameError, RecognitionUnavailable) as exc:
            self.merit_failures += 1
            if self.merit_failures <= 2:
                game.log(f"武勋读数未取到：{exc}；本轮跳过，不影响派兵", "WARN")
            return
        self.merit_failures = 0
        previous = self.merit_prev
        if self.merit_base is None:
            self.merit_base = value
        self.merit_value = value
        self.merit_prev = value
        delta = value - self.merit_base
        step = value - previous if previous is not None else 0
        # 游戏按“x.x万”显示时读数只能精确到 0.1 万；展示时标“约”，不做跨精度换算。
        unit = 10000000 if "亿" in text else 1000 if "万" in text else 1
        self.publish("merit", {"value": value, "text": text, "base": self.merit_base,
                               "delta": delta, "step": step, "unit": unit})
        game.log(f"武勋 {text}；本次任务累计 {delta:+d}", "INFO")

    def process_team_batch(self, game, policies, food_recovery=False, refresh_stale=True):
        if policies and self.command == "run" and not self.dry_run and getattr(game, "account_checked", None) is False:
            if not game.verify_account():
                raise RecognitionUnavailable("账号与主城尚未核对通过，等待下一轮重读")
        observations = []
        # 快路径（0.7.2+）：先一帧读出本次部队列表里各队状态，只对"可能派出/有待核对订单"的队读详情。
        # 忙碌、行军、驻守等队伍本轮不派兵，没必要再逐队开详情，省掉分钟级扫描。
        markers = {}
        fast = self.command == "run" and not self.dry_run and hasattr(game, "read_all_marker_states")
        if fast:
            markers = game.read_all_marker_states()
            if not markers:
                self.publish("diagnostic", {"stage": "marker_batch", "result": "整帧角标不可用，退回逐队读取"})

        forced = set()

        def needs_scan(policy):
            if not markers:
                return True
            marker = markers.get(policy.slot)
            if needs_detail(marker, self.journal.pending(policy.key)):
                return True
            if self.recheck_due(policy.key, marker):
                forced.add(policy.key)
                return True
            return False

        def read_batch():
            for policy in policies:
                self.control.checkpoint()
                if fast and not needs_scan(policy):
                    marker = markers[policy.slot]
                    obs = Observation(policy.key, state=LEGACY_STATE.get(marker["state"], "unknown"),
                                      wounded=(marker["state"] == "wounded") if marker["state"] == "wounded" else None,
                                      note="仅按单帧角标判定，未读详情",
                                      marker_state=marker["state"], marker_label=marker["label"],
                                      marker_confidence=marker["confidence"], marker_at=time.time())
                    self.note_progress(policy, obs)
                    self.publish("observation", obs.__dict__)
                    observations.append((policy, obs))
                    continue
                obs = self.observe_team(game, policy)
                if obs is None:
                    continue
                self.note_progress(policy, obs)
                observations.append((policy, obs))
        batch = getattr(game, "team_reading_batch", None)
        if batch is None:
            read_batch()
        else:
            with batch(policies):
                read_batch()
        if observations:
            group = tuple(sorted(p.key for p, _ in observations))
            unreadable = all(o.state == "unknown" for _,o in observations)
            self.unreadable_batches = self.unreadable_batch_counts.get(group, 0) + 1 if unreadable else 0
            self.unreadable_batch_counts[group] = self.unreadable_batches
            if self.unreadable_batches == 3:
                self.publish("diagnostic", {"stage":"team_batch", "unreadable_batches":self.unreadable_batches,
                                            "teams":[p.key for p,_ in observations]})
        for policy, obs in observations:
            if policy.key in forced:
                self.log_recheck(policy, obs)
        plans = [(p,o,self.team_decision(game,p,o,food_recovery)) for p,o in observations]
        def group_key(item):
            decision = item[2]
            point = decision.target
            return (decision.action != "merit", decision.action, point.x if point else -1, point.y if point else -1, point.name if point else "")
        plans.sort(key=group_key)
        count = sum(d.action == "merit" for p,o,d in plans)
        if count and self.command == "run" and not self.dry_run:
            self.publish("status",f"本批 {count} 队可尝试出击 · 正在连续处理")
        previous_target = None
        dispatched = set()
        for policy, obs, planned in plans:
            self.control.checkpoint()
            decision = self.team_decision(game,policy,obs,food_recovery)
            self.publish("decision", {"team":policy.key,"action":decision.action,"reason":decision.reason})
            # 讨贼队的补兵开关归使用者自己管：不达标就跳过，但一小时最多提醒一次原因。
            if (policy.role == "bandits" and decision.action == "wait"
                    and "自动补兵未开启" in decision.reason):
                self.log_once(policy.key, f"{policy.display} 跳过讨贼：{decision.reason}；"
                                          f"请在游戏里打开该队主城自动补兵，脚本不改游戏设置",
                              level="WARN", every=3600)
            if self.command != "run" or self.dry_run or decision.action in ("wait", "bandits"):
                continue
            failure = None
            try:
                if decision.action in ("merit", "transfer"):
                    if decision.action == "merit":
                        points = sorted([p for p in self.profile.targets
                                         if p.enabled and p.group == policy.target_group],
                                        key=lambda p: p.priority)
                    else:
                        order = {name: i for i, name in enumerate(policy.backup_points)}
                        candidates = [p for p in self.profile.backups if p.enabled
                                      and (not order or p.name in order)]
                        points = sorted(candidates, key=lambda p: (order.get(p.name, 10 ** 6), p.priority))
                    for point in points:
                        key = (point.x,point.y,point.name,tuple(point.actions),point.expected_name,decision.action)
                        try:
                            game.dispatch(policy,obs,point,decision.action,reuse_target=previous_target == key)
                            previous_target = key
                            failure = None
                            dispatched.add(policy.key)
                            self.last_progress[policy.key] = time.time()
                            break
                        except NetworkRecovered:
                            raise
                        except DispatchUnconfirmed as exc:
                            previous_target = None
                            game.log(f"{policy.display} / {point.name}：{exc}","INFO")
                            game.ensure_sandbox()
                            failure = exc
                            break
                        except GameError as exc:
                            failure = exc
                            previous_target = None
                            game.log(f"{policy.display} / {point.name}：{exc}","WARN")
                            game.ensure_sandbox()
                            if self.journal.pending(policy.key):
                                break
                elif decision.action == "return":
                    previous_target = None
                    if obs.location is None:
                        # 坐标没读到就不点回城：宁可下一轮重读，也不在不确定位置点箭头。
                        self.log_once(policy.key + ":return",
                                      f"{policy.display} 兵力已够回城，但队伍坐标未读到；本轮不点回城，等现场重读",
                                      every=600)
                    else:
                        # 自然征满/够兵回城：只要达到本队出击下限即可，不要求满兵。
                        game.recall(policy, obs, minimum=policy.threshold(obs.maximum) if obs.maximum else None)
                        dispatched.add(policy.key)
                        self.last_progress[policy.key] = time.time()
                if failure is not None:
                    self.defer_team(policy.key, failure)
                else:
                    self.retry_at.pop(policy.key, None)
                    self.retry_reason.pop(policy.key, None)
            except NetworkRecovered:
                raise
            except DispatchUnconfirmed as exc:
                previous_target = None
                game.log(f"{policy.display}：{exc}","INFO")
                game.ensure_sandbox()
                self.defer_team(policy.key, exc)
            except GameError as exc:
                previous_target = None
                game.log(f"{policy.display}：{exc}","WARN")
                self.defer_team(policy.key, exc)
                game.ensure_sandbox()
            if self.order_limit_reached():
                return True
        self.round_self_check(game, plans, dispatched)
        return False

    def round_self_check(self, game, plans, dispatched):
        """每轮自检：可派却一直没动，必须看得见，而且必须是暂时的。

        用户 2026-11-03 底线：队伍不能闲死。所以这里只做三件事：
        1. 打一行“谁是什么状态、谁可派、本轮派了谁、谁可派没动”；
        2. 可派但没动连续 2 轮 → 解除该队冷却，下一轮强制重新尝试；
        3. 永远不停队、不加黑名单；跨轮不记忆“失败过”。
        """
        if self.command != "run" or self.dry_run or not plans:
            return
        if not any(policy.role == "merit" for policy, _obs, _planned in plans):
            return
        labels = {"home": "主城", "outside": "城外停留", "guarding": "驻守", "wounded": "重伤恢复",
                  "marching": "行军", "returning": "返回", "fighting": "战斗", "recruiting": "征兵"}
        states, idle = [], []
        for policy, obs, planned in plans:
            label = obs.marker_label or labels.get(obs.state, obs.state or "未知")
            states.append(f"{policy.key}={label}")
            if policy.role != "merit" or policy.key in dispatched or not _can_dispatch(obs):
                self.idle_rounds.pop(policy.key, None)
                continue
            rounds = self.idle_rounds.get(policy.key, 0) + 1
            self.idle_rounds[policy.key] = rounds
            idle.append(f"{policy.key}（{planned.reason}）")
            if rounds >= 2:
                if planned.action in ("merit", "transfer", "bandits"):
                    # 想派却没派出去：解除冷却，下一轮一定重新尝试；不停止该队。
                    self.retry_at.pop(policy.key, None)
                    self.retry_reason.pop(policy.key, None)
                    advice = "已解除冷却，下一轮强制重试"
                else:
                    advice = f"等待条件：{planned.reason}"
                self.idle_rounds[policy.key] = 0
                game.log(f"{policy.display} 可派但连续 {rounds} 轮未动作：{planned.reason}；{advice}", "WARN")
                self.publish("diagnostic", {"stage": "liveness", "team": policy.key,
                                            "rounds": rounds, "reason": planned.reason})
        message = "监控：" + " ".join(states)
        if idle:
            message += f" → 可派未动作 {len(idle)} 队：" + "，".join(idle)
        else:
            message += " → 可派未动作 0"
        self.publish("log", {"time": datetime.now().strftime("%H:%M:%S"), "level": "INFO", "message": message})

    def pave_team_keys(self):
        """仍有未完成铺路路线的队伍；含 blocked（故障暂停也要保留归属）。

        本赛季尚未拥有的队伍不占用保留位，也不阻断其他队伍。
        """
        keys = set()
        for route in self.profile.pave_routes:
            if not route.team or not route.points:
                continue
            if not self.team_present(route.team):
                continue
            progress = self.journal.pave_progress(route.name)
            if progress["fingerprint"] and progress["fingerprint"] != self.pave_fingerprint(route):
                keys.add(route.team)
                continue
            if progress["status"] == "done" or progress["idx"] >= len(route.points):
                continue
            keys.add(route.team)
        return keys

    @staticmethod
    def pave_fingerprint(route):
        payload = json.dumps({"team": route.team, "points": route.points},
                             ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def pave_event(route, team, idx, status):
        """铺路进度事件：给界面提供“?/?”和下一个目标，日志仍记录完整过程。"""
        nxt = list(route.points[idx][:2]) if 0 <= idx < len(route.points) else None
        return {"route": route.name, "team": team, "done": idx,
                "total": len(route.points), "status": status, "next": nxt}

    def _recover_pave(self, game, policy, route, progress):
        """dispatching：订单已写但结果未知。只按现场核对，不重发。"""
        idx, intent, origin = progress["idx"], progress["intent"], progress["origin"]
        target = tuple(route.points[idx][:2]) if idx < len(route.points) else None
        obs = self.observe_team(game, policy)
        marker = marker_of(obs)
        origin_point = None
        if origin and "," in origin:
            try:
                origin_point = tuple(int(v) for v in origin.split(",", 1))
            except ValueError:
                origin_point = None
        if target and pave_at_point(obs, target):
            new_idx = idx + 1
            status = "done" if new_idx >= len(route.points) else "idle"
            self.journal.settle_pave(intent, "accepted", route.name, new_idx, status,
                                     "恢复核对：已在目标点停留")
            game.log(f"{policy.display} 恢复核对：已到达铺路目标，路线 {status}")
            self.publish("pave", self.pave_event(route, policy.key, new_idx, status))
        elif marker == "busy":
            # 占领、建筑内自然征兵等：队伍确实已经动了，只是还没停在目标点。
            self.journal.settle_pave(intent, "accepted", route.name, idx, "running",
                                     "恢复核对：队伍处于忙碌（占领/征兵等），按现场等待")
        elif obs.state in ("marching", "returning", "guarding", "wounded", "active", "paving", "occupying", "fighting"):
            self.journal.settle_pave(intent, "accepted", route.name, idx, "running",
                                     "恢复核对：队伍已离开原地")
        elif origin_point and obs.state in ("outside", "recruiting") and obs.location == origin_point and obs.wounded is False:
            if time.time() - progress.get("updated", 0) >= self.profile.retry_seconds:
                self.journal.settle_pave(intent, "rejected", route.name, idx, "idle",
                                         "冷却后仍在出发点，判定未出发")
                game.log(f"{policy.display} 铺路订单冷却后核对为未出发，下一轮重试", "INFO")
        elif (_can_dispatch(obs) and obs.wounded is False
              and obs.strength is not None
              and time.time() - progress.get("updated", 0) >= self.profile.retry_seconds):
            # 可能整段行军/击退发生在停机期间。先解除未知提交，下一轮从现场重新派当前段。
            self.journal.settle_pave(intent, "accepted", route.name, idx, "recovering",
                                     "现场已停下，提交周期结束；下一轮恢复当前段")
        elif obs.state == "home" and obs.strength is not None:
            # 已经回到主城说明这一段没有在走：自动放行，下一轮核对路线后重试，不长期锁队。
            if time.time() - progress.get("updated", 0) >= min(self.profile.retry_seconds, 60):
                self.journal.settle_pave(intent, "rejected", route.name, idx, "idle",
                                         "现场确认队伍在主城，判定未出发")
                game.log(f"{policy.display} 铺路订单核对为未出发，下一轮重试", "INFO")
        # 其余情况保持待核对，不自动重发。

    def process_pave(self, game, food_recovery=False):
        """每轮最多为每条路线推进一段；订单与进度原子落盘，结果未知不重发。"""
        if self.command != "run" or self.dry_run:
            return
        # 只执行被某支队伍选中的路线；未分配的路线只是草稿，不参与执行。
        routes = [r for r in self.profile.pave_routes if r.team and r.points]
        if not routes:
            return
        game.reserved_teams = self.pave_team_keys()
        for route in routes:
            self.control.checkpoint()
            policy = next((t for t in self.profile.teams if t.key == route.team), None)
            if policy is None or policy.group != "main":
                continue
            if not self.team_present(policy.key):
                self.log_once(f"pave-absent:{route.name}",
                              f"铺路路线 {route.name} 所属的 {policy.display} 尚未拥有，暂不推进", every=3600)
                continue
            progress = self.journal.pave_progress(route.name)
            fingerprint = self.pave_fingerprint(route)
            if progress["fingerprint"] and progress["fingerprint"] != fingerprint:
                old_intent = progress.get("intent")
                old_pending = self.journal.db.execute(
                    "SELECT 1 FROM intents WHERE scope=? AND id=? AND status IN ('prepared','uncertain')",
                    (self.journal.scope, old_intent)).fetchone() if old_intent else None
                if self.journal.pending(policy.key) or old_pending:
                    game.log(f"铺路路线 {route.name} 已修改，但该队有未确认命令；保持暂停", "WARN")
                    continue
                self.journal.set_pave(route.name, 0, "idle", "路线已修改，进度重置",
                                      fingerprint=fingerprint, origin="", intent="")
                progress = self.journal.pave_progress(route.name)
                game.log(f"铺路路线 {route.name} 已修改，进度重置")
            if progress["status"] == "blocked":
                self.journal.set_pave(route.name, progress["idx"], "recovering", "恢复当前段")
                progress = self.journal.pave_progress(route.name)
            if progress["status"] in ("blocked", "done") or progress["idx"] >= len(route.points):
                continue
            if progress["status"] == "dispatching" and progress["intent"]:
                self._recover_pave(game, policy, route, progress)
                continue
            if time.time() < self.retry_at.get(f"pave:{route.name}", 0):
                continue
            self.journal.set_phase(policy.key, "pave")
            obs = self.observe_pave_team(
                game, policy, full=progress["status"] not in ("running", "recovering"))
            if obs is None:
                continue
            if obs.state == "wounded" or obs.wounded is True:
                self.journal.set_pave(route.name, progress["idx"], "recovering", "重伤中，等待现场确认恢复")
                self.publish("decision", {"team": policy.key, "action": "wait", "reason": "铺路：等待重伤恢复"})
                continue
            idx = progress["idx"]
            marker = marker_of(obs)
            if marker in ("queued_battle", "fighting", "retreat"):
                # 2026-11-10 用户：攻占空地时与守军战斗是铺路的正常过程，不当异常；
                # 战斗结束后自动继续下一段。只有战斗后出现重伤才需要另行处理（下一阶段）。
                name = {"queued_battle": "排队进入战斗", "fighting": "战斗中", "retreat": "撤退中"}[marker]
                if marker == "retreat":
                    game.log(f"{policy.display} 铺路：角标显示{name}，暂停推进下一段，等现场恢复", "WARN")
                else:
                    self.log_once(f"pave-fight:{route.name}",
                                  f"{policy.display} 铺路：与守军战斗中，正常等待结束后继续下一段",
                                  every=600)
                self.publish("pave", self.pave_event(route, policy.key, idx, "waiting"))
                continue
            if progress["status"] in ("running", "recovering"):
                target = tuple(route.points[idx][:2])
                if pave_at_point(obs, target):
                    idx += 1
                    status = "done" if idx >= len(route.points) else "idle"
                    self.journal.set_pave(route.name, idx, status, "", fingerprint=fingerprint)
                    game.log(f"{policy.display} 已铺到 ({target[0]},{target[1]})"
                             + ("，路线完成" if status == "done" else "，继续下一段"))
                    self.publish("pave", self.pave_event(route, policy.key, idx, status))
                    if status == "done":
                        continue
                    progress = self.journal.pave_progress(route.name)
                elif _can_dispatch(obs) and obs.wounded is False:
                    # 队伍已经停下（在主城或任意停留点）：恢复当前未完成段，不额外做补兵/回城判断。
                    self.journal.set_pave(route.name, idx, "idle", "队伍已停下，恢复当前未完成段", fingerprint=fingerprint)
                    progress = self.journal.pave_progress(route.name)
                    # 刚才可能导航核对目标，派出前必须重新读取本队，不能沿用过期观测。
                    obs = self.observe_team(game, policy)
                    if obs is None:
                        continue
                else:
                    self.publish("decision", {"team": policy.key, "action": "wait", "reason": "铺路仍在进行或状态待确认；按现场等待"})
                    continue
            if self.journal.pending(policy.key):
                continue
            if food_recovery:
                self.publish("decision", {"team": policy.key, "action": "wait", "reason": "铺路：粮食恢复中，继续核对已有进度，暂停新出征"})
                continue
            if not _can_dispatch(obs):
                continue
            if obs.wounded is not False or obs.strength is None or obs.strength <= 0:
                continue
            point = route.points[idx]
            target = (int(point[0]), int(point[1]))
            if self.order_limit_reached():
                return
            if not game.account_checked:
                try:
                    if not game.verify_account():
                        game.log("账号与主城未核对通过，铺路暂停", "WARN")
                        return
                except GameError as exc:
                    game.log(f"铺路前账号核对失败：{exc}", "WARN")
                    return
            origin = f"{obs.location[0]},{obs.location[1]}" if obs.location else ""
            ident = self.journal.begin_pave(policy.key, route.name, idx, f"攻占 {target}",
                                            origin, fingerprint)
            try:
                game.pave_dispatch(policy, obs, target, ident)
            except PaveUnknown as exc:
                self.journal.mark_pave_unknown(ident, route.name, idx, str(exc))
                self.retry_at[f"pave:{route.name}"] = time.time() + self.profile.retry_seconds
                game.ensure_sandbox()
                continue
            except PavePointUnavailable as exc:
                # 用户 2026-11-10：目标已被别人铺到/没有攻占时跳过该点，继续下一个点。
                new_idx = idx + 1
                status = "done" if new_idx >= len(route.points) else "idle"
                self.journal.settle_pave(ident, "rejected", route.name, new_idx, status,
                                         f"目标没有攻占指令，跳过该点：{exc}")
                game.log(f"{policy.display} 铺路第 {idx+1} 段 {target} 没有攻占指令"
                         "（可能已被别人铺到），按规则跳过并继续下一点", "INFO")
                self.publish("pave", self.pave_event(route, policy.key, new_idx, status))
                self.retry_at.pop(f"pave:{route.name}", None)
                try:
                    game.ensure_sandbox()
                except GameError:
                    pass
                continue
            except ObservationExpired:
                self.journal.settle_pave(ident, "rejected", route.name, idx, "idle", "读数过期，未提交")
                continue
            except NetworkRecovered:
                self.journal.settle_pave(ident, "rejected", route.name, idx, "idle", "网络重连，未提交")
                raise
            except GameError as exc:
                self.journal.settle_pave(ident, "rejected", route.name, idx, "idle", f"未提交：{exc}")
                game.log(f"{policy.display} 铺路段 {idx+1} 未派出：{exc}", "WARN")
                self.retry_at[f"pave:{route.name}"] = time.time() + self.profile.retry_seconds
                game.ensure_sandbox()
                continue
            self.journal.settle_pave(ident, "accepted", route.name, idx, "running",
                                     f"({target[0]},{target[1]})")
            self.retry_at[policy.key] = time.time() + self.profile.retry_seconds
            self.publish("decision", {"team": policy.key, "action": "pave",
                                      "reason": f"铺路路线 {route.name} 第 {idx+1} 段 → ({target[0]},{target[1]})"})

    def line_team_keys(self):
        return {t.key for t in self.profile.teams
                if t.role == "line" and t.group == "main" and self.team_present(t.key)}

    def observe_line_team(self, game, policy):
        """拉线读队：轻量快照（坐标/角标/重伤），不走刷武勋那套主城核对重扫描。

        用户 2026-11-13：一队在城外时旧的重读取会反复失败。第一轮顺手读一次完整数据
        缓存兵力；之后只用快照，读不到完整数据也不拦派兵（能拉就拉）。
        """
        if policy.key not in self.line_snapshot:
            full = self.observe_team(game, policy)
            if full is not None and full.strength is not None and full.maximum is not None:
                self.line_snapshot[policy.key] = (full.strength, full.maximum, time.time())
            if full is not None and full.marker_state is not None:
                return full
        scanner = getattr(game, "scan_pave_team", None)
        if not callable(scanner):
            return self.observe_team(game, policy)
        obs = scanner(policy)
        cached = self.line_snapshot.get(policy.key)
        if obs is not None and cached and obs.strength is None:
            obs.strength, obs.maximum = cached[0], cached[1]
        return obs

    def line_backup_point(self, policy):
        """拉线的备用征兵地点（补兵点/营帐）：按本队指定顺序，否则全部启用点。"""
        order = {name: i for i, name in enumerate(policy.backup_points)}
        candidates = [p for p in self.profile.backups if p.enabled
                      and (not order or p.name in order)]
        candidates.sort(key=lambda p: (order.get(p.name, 10 ** 6), p.priority))
        return candidates[0] if candidates else None

    def process_line(self, game, food_recovery=False):
        """拉线模块（用户 2026-11-13 MVP 口径）。

        - 目标动作按拉线目标自己的指令顺序（和武勋目标一样，由使用者排列）；
        - 每轮每队最多点一次「出征」，没确认就交给下一轮重读；
        - 忙碌（占领/自然征兵）但兵力已到出击线：去出征页核对，是征兵就拉、是占领就放弃；
        - 补不动：回城 → 按补兵点调动到营帐 → 到营帐后够线就拉，不够就自然征兵到线再拉；
        - 一轮只截一帧读所有槽位状态；只在要派兵/回城/调动时才导航。
        """
        if self.command != "run" or self.dry_run:
            return
        policies = [t for t in self.profile.teams if t.role == "line"]
        if not policies:
            return
        # 一轮只截一帧读右侧所有槽位：角标 + 重伤。读不到才退回逐队导航。
        self.control.checkpoint()
        hud = {}
        reader = getattr(game, "read_all_marker_states", None)
        if callable(reader):
            try:
                hud = reader() or {}
            except GameError as exc:
                game.log(f"拉线整帧角标读取失败：{exc}；退回逐队读取", "WARN")
        for policy in policies:
            self.control.checkpoint()
            if policy.group != "main" or not self.team_present(policy.key):
                continue
            if self.journal.pending(policy.key):
                # 有未结清订单先按真实现场结清（observe_team 是通用结清入口）；
                # 拉线的整帧角标/轻量快照会绕过它，不能直接跳过把订单永久锁住。
                # 这一步放在“无目标/已完成/冷却跳过”之前：无目标时也必须先恢复 pending。
                settled = self.observe_team(game, policy)
                if settled is not None and settled.strength is not None and settled.maximum is not None:
                    self.line_snapshot[policy.key] = (settled.strength, settled.maximum, time.time())
                else:
                    # 没读到新兵力就作废旧缓存，避免拿过期读数做出击线判定。
                    self.line_snapshot.pop(policy.key, None)
                if self.journal.pending(policy.key):
                    continue
            if policy.key in self.line_finished:
                continue
            target = line_target(self.profile, policy)
            if target is None:
                self.log_once(f"line-target:{policy.key}",
                              f"{policy.display} 未配置启用的拉线目标（目标组 {policy.target_group}）；"
                              f"请在「目标与铺路 → 拉线目标」添加坐标并勾选启用，本队跳过",
                              every=1800)
                continue
            if time.time() < self.retry_at.get(f"line:{policy.key}", 0):
                continue
            if food_recovery:
                self.publish("decision", {"team": policy.key, "action": "wait",
                                          "reason": "拉线：粮食恢复中，暂停新出征"})
                continue
            if not game.account_checked:
                try:
                    if not game.verify_account():
                        continue
                except GameError as exc:
                    game.log(f"拉线前账号核对失败：{exc}", "WARN")
                    continue
            obs = None
            has_backup = self.line_backup_point(policy) is not None
            marker = hud.get(policy.slot)
            cached = self.line_snapshot.get(policy.key)
            # 缓存太久、而这一刻的决策要看兵力（忙/可派）时完整读一次；其他状态不值当导航。
            stale = bool(cached) and time.time() - cached[2] > self.LINE_STRENGTH_TTL
            need_strength = bool(marker) and marker.get("state") in (
                "busy", "idle", "training", "stationary", "transfer_stay")
            # 有补兵点时，忙也要完整读一次：确认是“在主城（先转补兵点）”还是建筑里。
            force_home_check = has_backup and bool(marker) and marker.get("state") == "busy"
            if (marker and marker.get("state") != "unknown" and cached
                    and not (stale and need_strength) and not force_home_check):
                obs = Observation(
                    policy.key,
                    state=LEGACY_STATE.get(marker["state"], "unknown"),
                    strength=cached[0], maximum=cached[1],
                    wounded=True if marker["state"] == "wounded" else None,
                    note="拉线整帧快照", marker_state=marker["state"],
                    marker_label=marker["label"], marker_confidence=marker["confidence"],
                    marker_at=time.time())
                self.publish("observation", obs.__dict__)
            if obs is None:
                try:
                    obs = self.observe_line_team(game, policy)
                except TeamAbsent as exc:
                    self.log_once(f"line-absent:{policy.key}", f"{policy.display} 拉线：{exc}", every=1800)
                    continue
            if obs is None:
                continue
            if obs.strength is not None and obs.maximum is not None:
                self.line_snapshot[policy.key] = (obs.strength, obs.maximum, time.time())
            # 用户 2026-11-13：有补兵点的拉线队，只要在主城（含自然征兵）就先补到出击线，
            # 再调动到补兵点。
            # 主城只认角标（空闲/练兵；用户 2026-11-13：不搞坐标判据，角标才作数）。
            # 用户 2026-11-13 补充：回主城的一瞬间就完成了给队伍补兵，之后征兵只补兵池、
            # 不会再给这支队补兵（除非在游戏里主动设置）。所以到主城后读到仍低于出击线，
            # 就是补不上，直接做最后一次调动并结束该队，不能等 8 分钟再说。
            at_home = obs.state == "home"
            if not at_home:
                self.line_home_seen.discard(policy.key)
            if has_backup and at_home and not obs.wounded:
                if policy.key not in self.line_home_seen:
                    # 刚回主城：角标缓存里的兵力可能是路上读数，必须完整读一次再判定，
                    # 否则会把“回城瞬间补上的兵”当成没补上。读不到就下一轮再复核。
                    try:
                        full = self.observe_team(game, policy)
                    except TeamAbsent:
                        full = None
                    if full is None or full.strength is None:
                        self.log_once(f"line-home-blind:{policy.key}",
                                      f"{policy.display} 拉线：已回主城但兵力未读到，下一轮再复核",
                                      every=600)
                        continue
                    obs = full
                    self.line_home_seen.add(policy.key)
                need = policy.threshold(obs.maximum) if obs.maximum else None
                if obs.strength is None:
                    self.log_once(f"line-home-blind:{policy.key}",
                                  f"{policy.display} 拉线：已回主城但兵力未读到，等下一次完整读取",
                                  every=600)
                    continue
                if need is not None and obs.strength < need:
                    self._line_finish(game, policy, obs)
                    continue
                self._line_transfer_backup(game, policy, obs)
                continue
            decision = line_decision(policy, obs, target=target,
                                     stopped=self.control.stop.is_set())
            self.publish("decision", {"team": policy.key, "action": decision.action, "reason": decision.reason})
            if decision.action not in ("line", "refill"):
                continue
            # 动作顺序完全按目标自己的配置（和刷武勋目标一样），不再写死攻占/行军。
            point = replace(target, expected_name="")
            try:
                game.dispatch(policy, obs, point, "line", reuse_target=True)
            except LineRefillUnavailable as exc:
                game.log(f"{policy.display} 拉线：{exc}；按兜底处理", "WARN")
                self.retry_at[f"line:{policy.key}"] = time.time() + self.profile.retry_seconds
                game.ensure_sandbox()
                self._line_fallback(game, policy, obs)
            except DispatchUnconfirmed as exc:
                self.retry_at[f"line:{policy.key}"] = time.time() + self.unconfirmed_delay(policy.key)
                game.log(f"{policy.display} / 拉线：{exc}", "INFO")
                game.ensure_sandbox()
            except NetworkRecovered:
                raise
            except GameError as exc:
                game.log(f"{policy.display} / 拉线：{exc}", "WARN")
                self.retry_at[f"line:{policy.key}"] = time.time() + self.profile.retry_seconds
                game.ensure_sandbox()
            else:
                self.retry_at.pop(f"line:{policy.key}", None)
                # 状态即将变化：清一次缓存，下一轮重新读完整数据。
                self.line_snapshot.pop(policy.key, None)
                self.publish("decision", {"team": policy.key, "action": "line",
                                          "reason": f"拉线已提交 {point_label(target)}"})

    def _line_finish(self, game, policy, obs):
        """主城也补不上兵：最后一次调动到补兵点；所有拉线队都完成后才停程序。

        用户 2026-11-13：一支队补不上不代表任务结束——只要还有拉线队能补能拉，
        任务就继续；等所有被选为拉线的队伍都完成“最后一次调动”，任务才算结束。
        """
        point = self.line_backup_point(policy)
        submitted = False
        if point is None:
            game.log(f"{policy.display} 拉线任务结束：主城补不上兵，且没有配置补兵点；"
                     f"留在主城", "WARN")
        else:
            try:
                game.dispatch(policy, obs, point, "transfer")
                submitted = True
            except GameError as exc:
                game.log(f"{policy.display} 拉线任务结束前调动未提交：{exc}", "WARN")
        self.line_backup.discard(policy.key)
        self.line_finished.add(policy.key)
        if submitted:
            game.log(f"{policy.display} 已进入调动（{point.name}）；该队拉线任务结束，"
                     f"等待兵力恢复后再启动", "WARN")
            self.publish("decision", {"team": policy.key, "action": "transfer",
                                      "reason": f"拉线任务结束：最后一次调动到 {point.name}"})
        # 未拥有的拉线队（本赛季没有该槽位）不参与“还有谁没完成”的判定：
        # 配置 5 队实际 2 队时，实际两队都完成就允许停止，不能被不存在的槽位拖住。
        remaining = [p.display for p in self.profile.teams
                     if p.role == "line" and p.group == "main"
                     and self.team_present(p.key) and p.key not in self.line_finished]
        if remaining:
            self.publish("status", f"{policy.display} 拉线结束；继续 {'、'.join(remaining)}")
            return
        self.publish("status", "拉线任务结束 · 所有拉线队已发出调动令（补兵点）")
        if submitted:
            game.log("所有拉线队伍都已发出调动令（补兵点）；拉线任务结束，程序停止", "WARN")
        self.control.stop.set()

    def _line_transfer_backup(self, game, policy, obs):
        """回城后按补兵点调动到营帐；到达营帐后再按状态继续拉。"""
        point = self.line_backup_point(policy)
        if point is None:
            self.line_backup.discard(policy.key)
            self.log_once(f"line-backup:{policy.key}",
                          f"{policy.display} 已回主城，但没有启用的补兵点；留在主城继续", every=1800)
            return
        try:
            game.dispatch(policy, obs, point, "transfer")
        except DispatchUnconfirmed as exc:
            self.retry_at[f"line:{policy.key}"] = time.time() + self.unconfirmed_delay(policy.key)
            game.log(f"{policy.display} / 拉线：回城后调动到 {point.name} 未确认：{exc}", "INFO")
            game.ensure_sandbox()
        except NetworkRecovered:
            raise
        except GameError as exc:
            self.retry_at[f"line:{policy.key}"] = time.time() + self.profile.retry_seconds
            game.log(f"{policy.display} 拉线：回城后调动到 {point.name} 未完成：{exc}", "WARN")
            game.ensure_sandbox()
        else:
            self.line_backup.discard(policy.key)
            self.retry_at.pop(f"line:{policy.key}", None)
            game.log(f"{policy.display} 已回主城，按补兵点调动到 {point.name}；到达后继续拉线", "INFO")
            self.publish("decision", {"team": policy.key, "action": "transfer",
                                      "reason": f"拉线：回城后调动到 {point.name}"})

    def _line_fallback(self, game, policy, obs):
        """补不动时的兜底：回城（下一轮再调动到补兵点/营帐）或停手。"""
        if policy.refill_fallback == "stop":
            self.publish("decision", {"team": policy.key, "action": "wait",
                                      "reason": "拉线：补不动且设为停手，记录并等下一轮重读"})
            return
        if obs.location is None or obs.wounded is not False:
            # 整帧快照没有坐标/伤情：只在这一步完整读一次，再决定回城。
            try:
                full = self.observe_team(game, policy)
            except TeamAbsent:
                full = None
            if full is not None and full.location is not None and full.wounded is False:
                obs = full
            else:
                self.log_once(f"line-recall:{policy.key}",
                              f"{policy.display} 拉线：补不动需回城，但坐标/伤情未读到，等现场重读", every=600)
                return
        try:
            # 用户 2026-11-13：拉线不套用刷武勋的“到出击线才回城”——拉不出去就回城补兵。
            game.recall(policy, obs, line=True)
        except GameError as exc:
            game.log(f"{policy.display} 拉线回城未完成：{exc}", "WARN")
        else:
            self.line_backup.add(policy.key)
            game.log(f"{policy.display} 拉线：补不动，已回主城；下一轮按补兵点调动到营帐", "INFO")

    def unconfirmed_delay(self, key):
        """出征未确认后的冷却：只做短退避，绝不锁队。

        用户 2026-11-03：每轮 30 秒、队伍必须动起来。所以连续未确认最多退到 60 秒，
        到点就重试；真正的“别重复发”由单轮内的指令顺序和“队伍已忙则不补发”保证。
        """
        streak = self.journal.failure_streak(key) if self.journal else 0
        return min(30 * (1 + streak), min(60, self.profile.retry_seconds))

    def order_limit_reached(self):
        if not self.max_orders:
            return False
        # 验收上限只因本次新命令未知而停止；旧命令仍锁定原队，并允许先用现场状态核对。
        if any(row[0] not in self.initial_pending for t in self.profile.teams for row in self.journal.pending(t.key)):
            self.publish("status","验收命令结果待确认，停止新动作")
            return True
        if self.orders >= self.max_orders:
            self.publish("status",f"验收出征 {self.orders} 次，已停止新命令")
            return True
        return False

    def bandits_enabled_today(self):
        teams = [t for t in self.profile.teams if t.role == "bandits" and self.team_present(t.key)]
        return bool(self.profile.dailies.get("bandits") and teams
                    and (self.profile.bandit_allow_assist or self.journal is None or not self.journal.bandits_exhausted()))

    def bandits_due(self):
        return self.bandits_enabled_today() and time.monotonic() >= self.next_bandit_check

    def do_dailies(self, game: Game, max_tasks=0):
        daily = self.profile.dailies
        processed = 0
        from .journal import cycle_key
        tasks = [("drill", game.drill)]
        if daily.get("alliance_rewards"):
            tasks.append(("alliance_rewards", game.alliance_rewards))
        tasks.extend((("monthly", game.monthly), ("signin", game.signin), ("events", game.event_signin), ("visit", game.visit)))
        for task, function in tasks:
            if not daily.get(task):
                continue
            if task == "visit":
                complete = self.journal.daily("visit_free")[0] == "done" and (not daily.get("half_price") or self.journal.daily("visit_half")[0] == "done")
            else:
                complete = self.journal.daily(task)[0] in ("done", "unavailable")
            recurring = task in ("drill", "alliance_rewards")
            if complete and not recurring:
                self.publish("daily", {"task":task,"result":"今日已完成，跳过页面和点击"})
                continue
            retry_key = (task, cycle_key(task))
            if max_tasks and not recurring and time.monotonic() < self.next_daily_check.get(retry_key, 0):
                continue
            self.control.checkpoint()
            result = True
            try:
                result = function(not self.dry_run)
            except NavigationRecovered as exc:
                # 误入页面被关回沙盘属于可恢复：跳过本任务，下一项会自己重新确认画面。
                game.log(str(exc), "WARN")
                self.publish("daily", {"task": task, "result": "误入页面已返回沙盘，本轮跳过该任务"})
            except NetworkRecovered:
                raise
            except GameError as exc:
                game.log(f"日常 {task}：{exc}", "WARN")
                self.publish("daily", {"task":task,"result":str(exc)})
                game.ensure_sandbox()
            self.next_daily_check[retry_key] = time.monotonic() + self.profile.daily_interval
            processed += int(not recurring or result is not False)
            if max_tasks and processed >= max_tasks:
                break
        if self.command == "dailies" and daily.get("bandits") and any(
                t.role == "bandits" and self.team_present(t.key) for t in self.profile.teams):
            self.check_bandits(game)
        if daily.get("career"):
            self.publish("daily", {"task":"career","result":"职业技能的消耗与入口待校验，本版保留设置但不执行"})

    def check_bandits(self,game,allow_execute=True):
        if not self.bandits_enabled_today():
            self.bandits_available = False
            self.publish("daily", {"task":"bandits", "result":"本周期奖励次数已用完，停止新讨贼；已有队伍继续监控"})
            return
        try:
            self.bandits_available = game.bandits(not self.dry_run and allow_execute)
        except NetworkRecovered:
            raise
        except GameError as exc:
            self.bandits_available = False
            game.log(f"讨贼：{exc}","WARN")
            self.publish("daily",{"task":"bandits","result":str(exc)})
        finally:
            # 停止后不补发点击；正常、空列表、加入失败都归还沙盘及预览。
            if not self.control.stop.is_set():
                self.control.checkpoint()
                image = game.ensure_sandbox()
                self.publish("preview",image)
            self.next_bandit_check = time.monotonic() + self.profile.bandit_interval

