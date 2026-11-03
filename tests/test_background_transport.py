import shlex

import pytest

import sanmou_assistant.transport as module
from sanmou_assistant.transport import Control, Device, parse_touchscreen, pinch_script


CAPABILITIES = """add device 1: /dev/input/event8
  name: "virtual touchscreen"
  events:
    ABS_MT_SLOT : value 0, min 0, max 31, fuzz 0
    ABS_MT_POSITION_X : value 0, min 0, max 1080, fuzz 0
    ABS_MT_POSITION_Y : value 0, min 0, max 1920, fuzz 0
  input props:
    INPUT_PROP_DIRECT
"""


def test_background_start_and_zoom_use_android_without_window_activation(monkeypatch):
    calls = []
    def run(args, *unused, **kwargs):
        calls.append(args)
        if "getevent" in args:
            return CAPABILITIES
        if "resolve-activity" in args:
            return "priority=0\ncom.example.game/.MainActivity\n"
        if "packages" in args:
            return "package:com.example.game\n"
        return ""
    monkeypatch.setattr(module, "run", run)
    device = Device("C:/MuMu/nx_main/mumu-cli.exe", "0", Control())
    device.serial = "test:123"
    device.control.sleep = lambda seconds: None
    device.launch_game("com.example.game")
    device.zoom_out()
    device.zoom_out(delta=120)
    assert all(args[0].endswith("adb.exe") for args in calls)
    assert all(args[1:3] == ["-s", "test:123"] for args in calls)
    assert sum("getevent" in args for args in calls) == 1
    assert any(args[4:] == ["am", "start", "-n", "com.example.game/.MainActivity"] for args in calls)
    gestures = [shlex.split(args[-1])[0] for args in calls if "sh" in args]
    assert len(gestures) == 4
    assert all("trap " in script and " EXIT" in script for script in gestures)
    assert all(script.rstrip().endswith("sendevent /dev/input/event8 0 0 0") for script in gestures)


def test_background_zoom_rejects_ambiguous_or_single_contact_devices():
    assert parse_touchscreen(CAPABILITIES) == ("/dev/input/event8", (0,1080), (0,1920))
    for invalid in (CAPABILITIES.replace("max 31", "max 0"), CAPABILITIES.replace("INPUT_PROP_DIRECT", "INPUT_PROP_POINTER"), CAPABILITIES + CAPABILITIES.replace("event8", "event9")):
        with pytest.raises(RuntimeError, match="唯一可用"):
            parse_touchscreen(invalid)
    with pytest.raises(ValueError, match="设备路径"):
        pinch_script(("/tmp/bad; command", (0,1080), (0,1920)))


def test_background_launch_does_not_guess_an_unresolved_entry(monkeypatch):
    calls = []
    def run(args, *unused, **kwargs):
        calls.append(args)
        return "package:com.example.game\n" if "packages" in args else "No activity found\n"
    monkeypatch.setattr(module, "run", run)
    device = Device("C:/MuMu/nx_main/mumu-cli.exe", "0", Control())
    with pytest.raises(RuntimeError, match="启动入口"):
        device.launch_game("com.example.game")
    assert not any("am" in args for args in calls)


def test_selected_window_is_minimized_during_android_boot(monkeypatch):
    started = {"index":"2", "is_android_started":True, "adb_port":16384, "pid":42, "main_wnd":"ABC"}
    booting = {**started, "is_android_started":False}
    snapshots = iter([[booting],[booting],[started]])
    calls = []
    monkeypatch.setattr(module,"instances",lambda *args:next(snapshots))
    monkeypatch.setattr(module,"InstanceLease",lambda *a:None)
    monkeypatch.setattr(module,"minimize_emulator_windows",lambda info:calls.append(("minimize",info["index"],info["is_android_started"])) or 1)
    monkeypatch.setattr(module,"run",lambda args,**kw:"device" if "get-state" in args else "")
    d = Device("C:/MuMu/mumu-cli.exe","2",Control())
    d.control.sleep = lambda *a:None
    d.capture = lambda:None
    d.connect(launch=True,minimize=True)
    assert calls[0] == ("minimize","2",False)
    assert calls[-1] == ("minimize","2",True)


def test_launch_minimizes_selected_window_before_and_after_wait(monkeypatch):
    events = []
    d = Device("C:/MuMu/mumu-cli.exe","2",Control())
    d.info = {"index":"2"}
    d.minimize_window = True
    d.control.sleep = lambda n:events.append(("wait",n))
    def shell(*args,**kw):
        if args[0] == "pm":return "package:com.example.game\n"
        if args[0] == "cmd":return "com.example.game/.MainActivity\n"
        events.append(("launch",))
    d.shell = shell
    monkeypatch.setattr(module,"minimize_emulator_windows",lambda info:events.append(("minimize",info["index"])))
    d.launch_game("com.example.game")
    assert events == [("launch",),("minimize","2"),("wait",4),("minimize","2")]
