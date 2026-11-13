from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shlex
import math
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import cv2
import numpy as np


class Stopped(Exception):
    pass


class TransportUnavailable(RuntimeError):
    """连接短暂失效；上层丢弃当前操作上下文，不能重放输入。"""


class Control:
    def __init__(self):
        self.stop = threading.Event()
        self.pause = threading.Event()
        self.on_pause = None

    def passive_checkpoint(self):
        if self.stop.is_set():
            raise Stopped()

    def checkpoint(self):
        if self.stop.is_set():
            raise Stopped()
        while self.pause.is_set():
            if self.on_pause:
                self.on_pause()
            if self.stop.wait(.1):
                raise Stopped()

    def sleep(self, seconds: float):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.checkpoint()
            self.stop.wait(min(.1, max(0, end - time.monotonic())))


def run(args: list[str], timeout: float = 20, binary: bool = False, control=None):
    if control:
        control.passive_checkpoint()
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
        end = time.monotonic() + timeout
        try:
            while True:
                if control:
                    control.passive_checkpoint()
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(args, timeout)
                try:
                    stdout, stderr = process.communicate(timeout=min(.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            process.kill()
            process.communicate()
            raise
        result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    if result.returncode:
        message = (result.stderr or result.stdout).decode("utf-8", errors="replace")
        raise RuntimeError(message.strip()[:600] or f"命令退出 {result.returncode}")
    return result.stdout if binary else result.stdout.decode("utf-8-sig", errors="replace")


def discover_cli(explicit: str = "") -> str:
    if explicit.strip():
        path = Path(explicit.strip().strip('"'))
        if not path.is_file() or path.name.lower() != "mumu-cli.exe":
            raise RuntimeError("指定的 mumu-cli.exe 不存在或文件名不正确，请在连接页重新选择；没有切换到其他安装")
        return str(path.resolve())
    candidates = [explicit, os.environ.get("MUMU_CLI", "")]
    for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        for root in ("Program Files/Netease/MuMu", "Program Files (x86)/Netease/MuMu", "MuMu", "MuMuPlayer-12", "Program Files/Netease/MuMuPlayer-12"):
            candidates.append(f"{drive}:/{root}/nx_main/mumu-cli.exe")
    try:
        import winreg
        for base in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
                try:
                    with winreg.OpenKey(base, sub) as key:
                        for i in range(winreg.QueryInfoKey(key)[0]):
                            with winreg.OpenKey(key, winreg.EnumKey(key, i)) as item:
                                try:
                                    label = winreg.QueryValueEx(item, "DisplayName")[0]
                                    if "mumu" in label.lower():
                                        location = winreg.QueryValueEx(item, "InstallLocation")[0]
                                        candidates.extend([str(Path(location) / "nx_main/mumu-cli.exe"), str(Path(location) / "mumu-cli.exe")])
                                except OSError:
                                    pass
                except OSError:
                    pass
    except ImportError:
        pass
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise RuntimeError("没有找到 MuMu CLI，请在连接页选择 nx_main\\mumu-cli.exe")


def instances(cli: str, control=None) -> list[dict]:
    raw = run([cli, "info", "-v", "all"], control=control)
    obj = json.loads(raw)
    rows = obj if isinstance(obj, list) else [obj] if isinstance(obj, dict) and "index" in obj else list(obj.values()) if isinstance(obj, dict) else []
    if any(not isinstance(row, dict) or not str(row.get("index", "")).isdigit() for row in rows):
        raise RuntimeError("MuMu 实例信息格式不受支持，请使用带 nx_main/mumu-cli.exe 的 Windows MuMu 版本")
    return rows


def discover_adb(cli: str) -> str:
    root = Path(cli).parent
    candidates = [root / "adb.exe", root.parent / "adb.exe"]
    devices = root.parent / "nx_device"
    if devices.is_dir():
        candidates.extend(sorted(devices.glob("*/shell/adb.exe"), reverse=True))
    return str(next((p for p in candidates if p.is_file()), candidates[0]))


def minimize_emulator_windows(info: dict | None = None) -> int:
    """仅最小化所选实例报告的主窗口；句柄与 PID 必须同时吻合。"""
    if os.name != "nt" or not info:
        return 0
    try:
        raw = info.get("main_wnd", "")
        hwnd = int(raw, 16) if isinstance(raw, str) else int(raw)
        expected_pid = int(info.get("pid", 0))
    except (TypeError, ValueError):
        return 0
    if hwnd <= 0 or expected_pid <= 0:
        return 0
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindow.argtypes = user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.IsWindow.restype = user32.IsWindowVisible.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.ShowWindow.restype = ctypes.c_int
    handle = ctypes.c_void_p(hwnd)
    pid = ctypes.c_ulong()
    if not user32.IsWindow(handle):
        return 0
    user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
    if pid.value != expected_pid or not user32.IsWindowVisible(handle):
        return 0
    user32.ShowWindow(handle, 6)  # SW_MINIMIZE；不激活其他实例。
    return 1


class Device:
    def __init__(self, cli: str, index: str, control: Control):
        self.cli, self.index, self.control = cli, str(index), control
        self.adb = discover_adb(cli)
        self.serial = ""
        self.width, self.height = 1920, 1080
        self.info = {}
        self.lease = None
        self.touchscreen = None
        self.minimize_window = False
        self.emit = lambda kind, value: None

    def connect(self, launch: bool = False, minimize: bool = False):
        self.control.checkpoint()
        self.minimize_window = minimize
        self.lease = InstanceLease(self.cli, self.index)
        matches = [i for i in instances(self.cli, self.control) if str(i["index"]) == self.index]
        if not matches:
            raise RuntimeError("所选 MuMu 实例不存在，请重新发现实例")
        info = matches[0]
        if not info.get("is_android_started"):
            if not launch:
                raise RuntimeError("所选模拟器尚未启动")
            run([self.cli, "control", "-v", self.index, "launch"], timeout=30, control=self.control)
            end = time.monotonic() + 150
            while time.monotonic() < end:
                self.control.sleep(3)
                info = next((i for i in instances(self.cli, self.control) if str(i["index"]) == self.index), {})
                if minimize:
                    minimize_emulator_windows(info)
                if info.get("is_android_started"):
                    break
            else:
                raise RuntimeError("等待 Android 启动超时")
        self.info = info
        if minimize:
            minimize_emulator_windows(info)
        host, port = info.get("adb_host_ip", "127.0.0.1"), info.get("adb_port")
        if not port:
            raise RuntimeError("实例未报告 ADB 端口，无法可靠绑定设备")
        self.serial = f"{host}:{port}"
        run([self.adb, "connect", self.serial], control=self.control)
        state = ""
        for attempt in range(15 if launch else 1):
            try:
                state = run([self.adb, "-s", self.serial, "get-state"], timeout=5, control=self.control).strip()
                if state == "device":
                    break
            except RuntimeError as exc:
                state = str(exc)
            if attempt < 14 and launch:
                self.control.sleep(1)
                run([self.adb, "connect", self.serial], timeout=5, control=self.control)
        if state != "device":
            raise RuntimeError(f"ADB 未就绪：{state}")
        self.capture()

    def close(self):
        if self.lease:
            self.lease.close()
            self.lease = None

    def channel_float_center(self, package: str):
        """Read the SDK's actual native control; never infer its position from a game icon."""
        name = f"/sdcard/.sanmou-assistant-ui-{os.getpid()}.xml"
        try:
            self.shell("uiautomator", "dump", name, timeout=20)
            xml = self.shell("cat", name)
            return channel_float_center(xml, package, self.width, self.height)
        finally:
            self.shell("rm", "-f", name)

    def shell(self, *args: str, timeout: float = 15) -> str:
        self.control.checkpoint()
        try:
            return run([self.adb, "-s", self.serial, "shell", *map(str, args)], timeout, control=self.control)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            # 输入可能已被Android接收；绝不重放tap/text/swipe/启动等shell命令。
            message = str(exc).lower()
            transient = (isinstance(exc, subprocess.TimeoutExpired) or any(s in message for s in (
                "error: closed", "offline", "device not found", "no devices", "connection reset"))
                or bool(re.search(r"device ['\"].+['\"] not found", message)))
            error = TransportUnavailable if transient else RuntimeError
            raise error(f"ADB shell {args[0] if args else '?'} 失败（{self.serial}），命令结果未知，不自动重发：{exc}") from exc

    def recover_channel(self):
        """只恢复当前连接并取新画面，失败输入不重放。"""
        self.control.checkpoint()
        run([self.adb, "disconnect", self.serial], timeout=5, control=self.control)
        run([self.adb, "connect", self.serial], timeout=5, control=self.control)
        state = run([self.adb, "-s", self.serial, "get-state"], timeout=5, control=self.control).strip()
        if state != "device":
            raise RuntimeError(f"ADB 重连后未就绪（{self.serial}）：{state}")
        return self.capture()

    def capture(self, passive=False) -> np.ndarray:
        # 暂停期间仅允许截图；shell/tap仍必须经过完整暂停检查。
        (self.control.passive_checkpoint if passive else self.control.checkpoint)()
        args = [self.adb, "-s", self.serial, "exec-out", "screencap", "-p"]
        started = time.monotonic()
        try:
            raw = run(args, timeout=20, binary=True, control=self.control)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            message = str(exc).lower()
            if not isinstance(exc, subprocess.TimeoutExpired) and not any(s in message for s in (
                    "closed", "offline", "device not found", "no devices", "transport", "connection reset")):
                raise RuntimeError(f"ADB 截图失败（{self.serial}）：{exc}") from exc
            self.emit("transport", {"stage":"capture", "serial":self.serial, "error":str(exc),
                                    "seconds":round(time.monotonic()-started,3), "recovery":"target-only-once"})
            (self.control.passive_checkpoint if passive else self.control.checkpoint)()
            # 只重连所选实例，不重启全局ADB、不影响其他设备，也不恢复任何游戏输入。
            # 高负载下 adb connect/截图偶尔超时，给有限重试（3 轮），避免把瞬时卡顿判成永久故障。
            raw = None
            last_error = None
            for attempt in range(3):
                if attempt:
                    self.control.sleep(1.0)
                try:
                    run([self.adb, "disconnect", self.serial], timeout=5, control=self.control)
                    run([self.adb, "connect", self.serial], timeout=10, control=self.control)
                    state = run([self.adb, "-s", self.serial, "get-state"], timeout=10, control=self.control).strip()
                    if state != "device":
                        raise RuntimeError(f"ADB 未就绪：{state}")
                    (self.control.passive_checkpoint if passive else self.control.checkpoint)()
                    raw = run(args, timeout=20, binary=True, control=self.control)
                    break
                except (RuntimeError, subprocess.TimeoutExpired) as retry:
                    last_error = retry
            if raw is None:
                raise RuntimeError(f"ADB 截图通道重连三次后仍失败（{self.serial}），停止操作：{last_error}") from last_error
            self.emit("transport", {"stage":"capture", "serial":self.serial, "recovery":"restored",
                                    "seconds":round(time.monotonic()-started,3)})
        if time.monotonic()-started > 3:
            self.emit("transport", {"stage":"capture-slow", "serial":self.serial,
                                    "seconds":round(time.monotonic()-started,3)})
        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError("ADB 截图不是有效的 PNG")
        self.height, self.width = image.shape[:2]
        if abs(self.width / self.height - 16 / 9) > .015:
            raise RuntimeError(f"画面 {self.width}×{self.height} 不是受支持的横屏 16:9，请调整模拟器分辨率")
        return image if (self.width, self.height) == (1920, 1080) else cv2.resize(image, (1920, 1080))

    def tap(self, x: float, y: float, delay: float = .35):
        if not 0 <= x < 1920 or not 0 <= y < 1080:
            raise ValueError("点击位置超出画面")
        self.shell("input", "tap", round(x * self.width / 1920), round(y * self.height / 1080))
        self.control.sleep(delay)

    def text(self, text: str):
        # 此接口只接受地图数值坐标，不接受任意 shell 文本。
        if not text or any(c not in "0123456789." for c in text):
            raise ValueError("仅接受数字坐标")
        self.shell("input", "text", text)

    def back(self):
        self.shell("input", "keyevent", "4")
        self.control.sleep(.6)

    def finish_coordinate_input(self):
        # 只在Android报告输入法已显示时完成编辑。返回键会取消游戏坐标编辑，不能用于提交文本。
        state = self.shell("dumpsys", "input_method")
        if re.search(r"\b(?:mInputShown|isInputViewShown|mIsInputViewShown)=true\b", state):
            self.shell("input", "keyevent", "66")
            self.control.sleep(.6)
            return True
        return False

    def swipe(self, x1, y1, x2, y2, duration=350):
        self.shell("input", "swipe", round(x1*self.width/1920), round(y1*self.height/1080), round(x2*self.width/1920), round(y2*self.height/1080), duration)
        self.control.sleep(.5)

    def long_press(self, x, y, seconds=1.8):
        """原地按住同一坐标：地图缩放按钮长按 1~2 秒即可连续缩放。

        用户 2026-11-13：连点可能不生效，长按更稳；只按下已识别到的按钮。
        """
        self.shell("input", "swipe", round(x*self.width/1920), round(y*self.height/1080),
                   round(x*self.width/1920), round(y*self.height/1080), int(seconds*1000))
        self.control.sleep(.5)

    def launch_game(self, package: str):
        if not re.fullmatch(r"[A-Za-z0-9_.]+",package):
            raise ValueError("游戏包名格式不正确")
        output = self.shell("pm", "list", "packages", package)
        if f"package:{package}" not in output.splitlines():
            raise RuntimeError("所选实例没有安装该游戏包，请在连接页核对包名")
        resolved = self.shell("cmd","package","resolve-activity","--brief","-a","android.intent.action.MAIN","-c","android.intent.category.LAUNCHER",package)
        component = next((line.strip() for line in reversed(resolved.splitlines()) if re.fullmatch(r"[\w.]+/[\w.$]+",line.strip()) and line.strip().startswith(package+"/")),None)
        if not component:
            raise RuntimeError("未找到游戏启动入口，请核对包名和安装状态")
        self.shell("am","start","-n",component,timeout=30)
        if self.minimize_window:
            minimize_emulator_windows(self.info)
        self.control.sleep(4)
        if self.minimize_window:
            minimize_emulator_windows(self.info)

    def zoom_out(self, steps: int = 22, delta: int = -120):
        """Zoom through Android touch input, including when MuMu is minimized."""
        self.control.checkpoint()
        if steps <= 0 or delta == 0:
            return
        if self.touchscreen is None:
            self.touchscreen = parse_touchscreen(self.shell("getevent","-lp"))
        for _ in range(math.ceil(steps/11)):
            self.control.checkpoint()
            script = pinch_script(self.touchscreen,zoom_in=delta > 0)
            self.shell("sh","-c",shlex.quote(script),timeout=15)
            self.control.sleep(.6)


def channel_float_center(xml: str, package: str, width: int, height: int):
    """Only a unique, visible Xiaomi SDK float in this game's native hierarchy qualifies."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    matches = []
    for node in root.iter("node"):
        if node.get("package") != package or node.get("resource-id") != package + ":id/is_float_icon":
            continue
        bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
        if bounds is None or node.get("enabled") != "true":
            continue
        x1, y1, x2, y2 = map(int, bounds.groups())
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            continue
        if not (8 <= x2-x1 <= width*.2 and 8 <= y2-y1 <= height*.3):
            continue
        matches.append(((x1+x2)/2*1920/width, (y1+y2)/2*1080/height))
    return matches[0] if len(matches) == 1 else None


def parse_touchscreen(capabilities: str):
    candidates = []
    for block in re.split(r"(?=add device \d+:)",capabilities):
        path = re.search(r"add device \d+: (/dev/input/event\d+)",block)
        slot = re.search(r"ABS_MT_SLOT[^\n]*min (-?\d+), max (-?\d+)",block)
        axes = [re.search(rf"{axis}[^\n]*min (-?\d+), max (-?\d+)",block) for axis in ("ABS_MT_POSITION_X","ABS_MT_POSITION_Y")]
        if not path or "INPUT_PROP_DIRECT" not in block or not slot or int(slot[1]) > 0 or int(slot[2]) < 1 or not all(axes):
            continue
        ranges = [tuple(map(int,axis.groups())) for axis in axes]
        if all(high > low for low,high in ranges):
            candidates.append((path[1],*ranges))
    if len(candidates) != 1:
        raise RuntimeError("未找到唯一可用的双指触屏，停止地图缩放；请检查模拟器输入设备")
    return candidates[0]


def pinch_script(touchscreen, zoom_in=False):
    path,(xmin,xmax),(ymin,ymax) = touchscreen
    if not re.fullmatch(r"/dev/input/event\d+",path):
        raise ValueError("触屏设备路径不正确")
    def event(kind,code,value):
        return f"sendevent {path} {kind} {code} {value}"
    release = "; ".join([event(3,47,0),event(3,57,-1),event(3,47,1),event(3,57,-1),event(1,330,0),event(0,0,0)])
    commands = ["set -e",f"trap {shlex.quote(release)} EXIT"]
    start,end = (.05,.3) if zoom_in else (.3,.05)
    for slot,fraction in ((0,.5-start),(1,.5+start)):
        commands.extend([event(3,47,slot),event(3,57,24000+slot),event(3,53,(xmin+xmax)//2),event(3,54,round(ymin+(ymax-ymin)*fraction))])
    commands.extend([event(1,330,1),event(0,0,0),"sleep .08"])
    for step in range(1,13):
        half = start+(end-start)*step/12
        for slot,fraction in ((0,.5-half),(1,.5+half)):
            commands.extend([event(3,47,slot),event(3,54,round(ymin+(ymax-ymin)*fraction))])
        commands.extend([event(0,0,0),"sleep .03"])
    commands.append(release)
    return "\n".join(commands)


class InstanceLease:
    """同一 Windows 会话内跨进程互斥；进程退出时系统自动释放。"""

    def __init__(self, cli: str, index: str):
        self.handle = None
        if os.name != "nt":
            return
        key = hashlib.sha256(f"{str(Path(cli).resolve()).casefold()}|{index}".encode()).hexdigest()[:24]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel = kernel
        handle = kernel.CreateMutexW(None, True, "Local\\SanmouAssistant_"+key)
        if not handle:
            raise RuntimeError("无法建立模拟器操作互斥")
        if ctypes.get_last_error() == 183:
            kernel.CloseHandle(handle)
            raise RuntimeError("该实例正由另一个助手任务控制，请先停止那个任务")
        self.handle = handle

    def close(self):
        if self.handle:
            self.kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
            self.kernel.ReleaseMutex(self.handle)
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def __del__(self):
        self.close()

