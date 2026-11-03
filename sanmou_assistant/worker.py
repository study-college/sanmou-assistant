from __future__ import annotations

import hashlib
import json
import time
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .game import DispatchUnconfirmed, Game, GameError, NavigationRecovered, NetworkRecovered, ObservationExpired, PaveUnknown, RecognitionUnavailable
from .evidence import EvidenceStore
from .journal import Journal
from .models import Decision, Observation, Profile, decide, observation_expired
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
    """
    if not marker:
        return True
    return can_dispatch(marker["state"]) or bool(has_pending)


class Worker(QThread):
    event = Signal(str, object)
    completed = Signal()

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

    def request_refresh(self):
        self.refresh_requested.set()

    def publish(self, kind, value):
        if kind == "dispatch":
            self.orders += 1
        if kind != "preview":
            with (self.folder / "events.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"at":datetime.now().isoformat(), "event":kind, "value":value}, ensure_ascii=False)+"\n")
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
            scope = f"{self.profile.package}|{self.profile.account}|{self.profile.main_city.x if self.profile.main_city else '?'}|{self.profile.main_city.y if self.profile.main_city else '?'}"
            self.journal = Journal(self.folder.parent / "runtime.sqlite", scope)
            self.initial_pending = {row[0] for policy in self.profile.teams for row in self.journal.pending(policy.key)}
            game = Game(device, vision, self.profile, self.journal, self.folder, self.publish)
            game.reserved_teams = self.pave_team_keys()
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
            if self.command == "calibrate":
                game.calibrate_home()
                self.publish("status", "账号与主城已校验")
                return
            if self.command == "rates":
                game.read_reserve_rates()
                self.publish("status", "征兵所已读取")
                return
            if self.command == "farming":
                game.farming(not self.dry_run)
                self.publish("status","屯粮检查结束")
                return
            if self.command == "review":
                for policy in self.profile.teams:
                    if policy.group != "main":
                        continue
                    obs = game.scan_team(policy)
                    if obs.state in ("home", "wounded") and obs.strength is not None:
                        self.journal.review_team(policy.key)
                self.publish("status", "已核对在主城的队伍；城外未知命令继续保留")
                return
            if self.command == "dailies":
                self.do_dailies(game)
                self.publish("status", "日常检查结束")
                return
            if self.command == "return":
                for policy in self.profile.teams:
                    if policy.group != "main" or not self.journal.phase(policy.key):
                        continue
                    obs = self.observe_team(game, policy)
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
                    hide_checked = True
                    try:
                        game.ensure_hide_on()
                    except GameError as exc:
                        game.log(f"打开游戏隐藏显示未完成：{exc}；本轮继续", "WARN")
                if self.duration and time.monotonic()-started >= self.duration:
                    self.publish("status", "本次验收时限已到，停止新动作")
                    return
                try:
                    self.publish("status", "监控中 · 正在读取队伍" if self.command == "scan" else "只监控" if self.dry_run else "运行中")
                    if not self.profile.account or not self.profile.main_city:
                        game.calibrate_home()
                    policies = self.profile.teams if self.command == "scan" else [t for t in self.profile.teams if t.merit or t.bandits]
                    if self.command == "run" and self.profile.food_enabled:
                        game.ensure_sandbox()
                        if game.grain is not None:
                            food_recovery = (food_recovery or game.grain < self.profile.food_low) and game.grain < self.profile.food_recover
                            self.journal.set_flag("food_recovery",food_recovery)
                        if food_recovery:
                            game.farming(not self.dry_run, force=True)
                            food_recovery = game.grain is None or game.grain < self.profile.food_recover
                            self.journal.set_flag("food_recovery",food_recovery)
                    game.reserved_teams = self.pave_team_keys()
                    # 同一队兼做讨贼且设为讨贼优先时，先尝试当前集结；独立讨贼队不打断武勋批次。
                    if self.command == "run" and self.bandits_due() and any(t.merit and t.bandits and t.priority == "bandits" for t in policies):
                        self.check_bandits(game,allow_execute=not food_recovery)
                    limited = self.order_limit_reached()
                    # 粮食恢复标记按已读库存重算：低于触发值才置位，达到恢复值就解除。
                    # 只读扫描也按事实重算（不写盘），避免上一轮残留标记把这一轮判成"恢复中"。
                    if self.profile.food_enabled and game.grain is not None:
                        fresh_recovery = (food_recovery or game.grain < self.profile.food_low) and game.grain < self.profile.food_recover
                        if fresh_recovery != food_recovery:
                            food_recovery = fresh_recovery
                            if self.command == "run" and not self.dry_run:
                                self.journal.set_flag("food_recovery", food_recovery)
                    pave_keys = self.pave_team_keys() if self.command == "run" and not self.dry_run else set()
                    batch_policies = policies if self.command == "scan" or self.dry_run else [t for t in policies if (t.merit or self.journal.pending(t.key)) and t.key not in pave_keys]
                    if not limited:
                        limited = self.process_team_batch(game, batch_policies, food_recovery)
                    if self.command == "run" and not self.dry_run:
                        # 铺路路线独立推进：到达当前段才派下一段，不占武勋批次。
                        self.process_pave(game, food_recovery)
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
                        self.process_team_batch(game, [t for t in policies if t.bandits and not t.merit and t.key not in reserved
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
                    game.log(f"{exc}；本轮暂停输入，{self.profile.poll_seconds} 秒后自动重读", "WARN")
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
                self.journal.set_phase(policy.key,"backup" if kind == "transfer" else "returning" if kind == "return" else "bandits" if kind == "bandits" else "guarding" if obs.state == "guarding" and kind == "merit" else "merit")
            elif kind in ("merit", "transfer", "bandits") and obs.state in ("outside", "recruiting") and obs.strength is not None and obs.wounded is False:
                self.journal.finish(ident,"accepted","实时队伍详情确认已在城外到达或征兵")
                self.journal.set_phase(policy.key,"backup" if kind == "transfer" else "bandits" if kind == "bandits" else "merit")
            elif kind != "pave" and obs.state == "home" and obs.strength is not None and obs.wounded is False and time.time()-created >= self.profile.retry_seconds:
                # 讨贼加入成功会让队伍立刻出城；冷却后仍在主城说明未提交（如满员未消耗），
                # 结清为 rejected 允许重试，不再永久锁定该队。
                self.journal.finish(ident,"rejected","冷却后现场确认在主城，没有未结束的出征")
                game.log(f"{policy.display} 的未确认命令已核对：队伍仍在主城，下一轮按策略自动重试","INFO")
        if obs.state == "home" and self.journal.phase(policy.key) in ("backup", "returning", "bandits", "guarding"):
            self.journal.set_phase(policy.key, "")
        return obs

    def team_decision(self, game, policy, obs, food_recovery):
        if self.command == "run" and not self.dry_run and policy.key in self.pave_team_keys():
            return Decision(reason="该队保留给未完成的铺路路线")
        decision = decide(self.profile, policy, obs, game.grain, self.journal.phase(policy.key))
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

    def process_team_batch(self, game, policies, food_recovery=False, refresh_stale=True):
        if policies and self.command == "run" and not self.dry_run and getattr(game, "account_checked", None) is False:
            if not game.verify_account():
                raise RecognitionUnavailable("账号与主城尚未核对通过，等待下一轮重读")
        observations = []
        # 快路径（0.7.2+）：先一帧读出五队状态，只对"可能派出/有待核对订单"的队读详情。
        # 忙碌、行军、驻守等队伍本轮不派兵，没必要再逐队开详情，省掉分钟级扫描。
        markers = {}
        fast = self.command == "run" and not self.dry_run and hasattr(game, "read_all_marker_states")
        if fast:
            markers = game.read_all_marker_states()
            if not markers:
                self.publish("diagnostic", {"stage": "marker_batch", "result": "整帧角标不可用，退回逐队读取"})

        def needs_scan(policy):
            if not markers:
                return True
            return needs_detail(markers.get(policy.slot), self.journal.pending(policy.key))

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
                    self.publish("observation", obs.__dict__)
                    observations.append((policy, obs))
                    continue
                observations.append((policy, self.observe_team(game, policy)))
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
            if self.command != "run" or self.dry_run or decision.action in ("wait", "bandits"):
                continue
            failure = None
            try:
                if decision.action in ("merit", "transfer"):
                    points = sorted([p for p in (self.profile.targets if decision.action == "merit" else self.profile.backups) if p.enabled and (p.group == policy.target_group if decision.action == "merit" else not policy.backup or p.name == policy.backup)], key=lambda p:p.priority)
                    for point in points:
                        key = (point.x,point.y,point.name,tuple(point.actions),point.expected_name,decision.action)
                        try:
                            game.dispatch(policy,obs,point,decision.action,reuse_target=previous_target == key)
                            previous_target = key
                            failure = None
                            dispatched.add(policy.key)
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
                    game.recall(policy,obs)
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
        if not any(policy.merit for policy, _obs, _planned in plans):
            return
        labels = {"home": "主城", "outside": "城外停留", "guarding": "驻守", "wounded": "重伤恢复",
                  "marching": "行军", "returning": "返回", "fighting": "战斗", "recruiting": "征兵"}
        states, idle = [], []
        for policy, obs, planned in plans:
            label = obs.marker_label or labels.get(obs.state, obs.state or "未知")
            states.append(f"{policy.key}={label}")
            if not policy.merit or policy.key in dispatched or not _can_dispatch(obs):
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
        """仍有未完成铺路路线的队伍；含 blocked（故障暂停也要保留归属）。"""
        keys = set()
        for route in self.profile.pave_routes:
            if not route.enabled or not route.points:
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
            self.publish("pave", {"route": route.name, "team": policy.key, "done": new_idx,
                                  "total": len(route.points), "status": status})
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
        routes = [r for r in self.profile.pave_routes if r.enabled and r.points]
        if not routes:
            return
        game.reserved_teams = self.pave_team_keys()
        for route in routes:
            self.control.checkpoint()
            policy = next((t for t in self.profile.teams if t.key == route.team), None)
            if policy is None or policy.group != "main":
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
            obs = self.observe_team(game, policy)
            if obs.state == "wounded" or obs.wounded is True:
                self.journal.set_pave(route.name, progress["idx"], "recovering", "重伤中，等待现场确认恢复")
                self.publish("decision", {"team": policy.key, "action": "wait", "reason": "铺路：等待重伤恢复"})
                continue
            idx = progress["idx"]
            marker = marker_of(obs)
            if marker in ("queued_battle", "fighting", "retreat"):
                # 实战预留：铺路途中遇敌、战斗、被击退时不推进新段，按现场重新读取。
                # 补给下降、被击退后的恢复策略尚未接入（用户要求先预留，不做）。
                name = {"queued_battle": "排队进入战斗", "fighting": "战斗中", "retreat": "撤退中"}[marker]
                game.log(f"{policy.display} 铺路：角标显示{name}，暂停推进下一段，等现场恢复", "WARN")
                self.publish("pave", {"route": route.name, "team": policy.key, "done": idx,
                                      "total": len(route.points), "status": "waiting"})
                continue
            if progress["status"] in ("running", "recovering"):
                target = tuple(route.points[idx][:2])
                if pave_at_point(obs, target):
                    idx += 1
                    status = "done" if idx >= len(route.points) else "idle"
                    self.journal.set_pave(route.name, idx, status, "", fingerprint=fingerprint)
                    game.log(f"{policy.display} 已铺到 ({target[0]},{target[1]})"
                             + ("，路线完成" if status == "done" else "，继续下一段"))
                    self.publish("pave", {"route": route.name, "team": policy.key, "done": idx,
                                          "total": len(route.points), "status": status})
                    if status == "done":
                        continue
                    progress = self.journal.pave_progress(route.name)
                elif _can_dispatch(obs) and obs.wounded is False:
                    # 队伍已经停下（在主城或任意停留点）：恢复当前未完成段，不额外做补兵/回城判断。
                    self.journal.set_pave(route.name, idx, "idle", "队伍已停下，恢复当前未完成段", fingerprint=fingerprint)
                    progress = self.journal.pave_progress(route.name)
                    # 刚才可能导航核对目标，派出前必须重新读取本队，不能沿用过期观测。
                    obs = self.observe_team(game, policy)
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
        return bool(self.profile.dailies.get("bandits") and any(t.bandits for t in self.profile.teams)
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
        if self.command == "dailies" and daily.get("bandits") and any(t.bandits for t in self.profile.teams):
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

