import json
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

from sanmou_assistant.diagnostics import export_diagnostics, write_session_report
from sanmou_assistant.transport import Control, Stopped, discover_adb, discover_cli, run


def test_stop_interrupts_only_owned_command_without_waiting_for_timeout(tmp_path):
    control = Control()
    timer = threading.Timer(.3, control.stop.set)
    started = time.monotonic()
    timer.start()
    try:
        with pytest.raises(Stopped):
            run([sys.executable, "-c", "import time;time.sleep(30)"], timeout=40, control=control)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 3


def test_run_preserves_output_and_exit_failure():
    assert run([sys.executable, "-c", "print('ok')"]).strip() == "ok"
    with pytest.raises(RuntimeError, match="bad"):
        run([sys.executable, "-c", "import sys;sys.stderr.write('bad');sys.exit(2)"])
    with pytest.raises(subprocess.TimeoutExpired):
        run([sys.executable, "-c", "import time;time.sleep(10)"], timeout=.2)


def test_explicit_install_is_never_silently_replaced(tmp_path):
    with pytest.raises(RuntimeError, match="没有切换"):
        discover_cli(str(tmp_path / "mumu-cli.exe"))
    cli = tmp_path / "nx_main" / "mumu-cli.exe"
    cli.parent.mkdir()
    cli.touch()
    adb = tmp_path / "nx_device" / "12.0" / "shell" / "adb.exe"
    adb.parent.mkdir(parents=True)
    adb.touch()
    assert discover_cli(str(cli)) == str(cli.resolve())
    assert Path(discover_adb(str(cli))) == adb


def test_feedback_is_readable_bounded_and_does_not_follow_external_images(tmp_path):
    data = tmp_path / "user data 中文"
    folder = data / "sessions" / "20261027-010000"
    folder.mkdir(parents=True)
    image = data / "sessions" / "_evidence" / "error.png"
    image.parent.mkdir()
    image.write_bytes(b"png-test")
    outside = tmp_path / "unrelated.png"
    outside.write_bytes(b"secret")
    records = [{"event": "error", "value": "测试 <报错>"},
               {"event": "evidence", "value": str(image)},
               {"event": "evidence", "value": str(outside)}]
    (folder / "events.jsonl").write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    write_session_report(folder, "测试")
    output = tmp_path / "feedback.zip"
    report = export_diagnostics(data, output)
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert "images/error.png" in archive.namelist()
        assert all("unrelated" not in name for name in archive.namelist())
        assert "&lt;报错&gt;" in archive.read("请先打开诊断报告.html").decode("utf-8")
        assert "测试" in archive.read("report.json").decode("utf-8")
    assert report["environment"]["version"]


def test_only_paving_also_requires_account_and_rejects_guard():
    from sanmou_assistant.models import Profile, PaveRoute
    profile = Profile(pave_routes=[PaveRoute(name="route", points=[[1, 2]])])
    profile.teams[1].role, profile.teams[1].route = "pave", "route"
    profile.normalize()
    assert any("主城校验" in value for value in profile.validate(active=True))
    profile.teams[5].role, profile.teams[5].route = "pave", "route"
    profile.teams[1].role, profile.teams[1].route = "idle", ""
    profile.normalize()
    assert any("亲卫" in value for value in profile.validate())


def test_land_menu_march_text_is_not_a_moving_team(tmp_path):
    from types import SimpleNamespace
    import numpy as np
    from sanmou_assistant.game import Game
    from sanmou_assistant.journal import Journal
    from sanmou_assistant.models import Profile, TeamPolicy
    vision = SimpleNamespace(joined=lambda *args: "行军", stationary_return_visible=lambda *args: False,
                             returning_team_visible=lambda *args: False)
    journal = Journal(tmp_path / "runtime.sqlite", "test")
    game = Game(None, vision, Profile(), journal, tmp_path, lambda *args: None)
    assert game.read_outside_state(np.zeros((1080, 1920, 3), np.uint8), TeamPolicy(slot=5), None) == "unknown"
    journal.close()


@pytest.mark.skip(reason="0.7.5 起过渡帧由 frame_clear/capture_clear 统一重截，旧的多帧序列假设作废；替代测试 test_frame_clear_rejects_cloud_transition_frames")
def test_clouded_home_menu_is_reread_before_team_state_is_decided():
    from types import SimpleNamespace
    import numpy as np
    from sanmou_assistant.game import Game
    from sanmou_assistant.models import Profile, Point, TeamPolicy, Observation
    cloud = np.zeros((1080,1920,3),np.uint8)
    clear = np.ones_like(cloud)
    full = np.full_like(cloud,2)
    frames = iter([clear,full,full])
    waits = []
    game = Game.__new__(Game)
    game.p = Profile(account="测试",main_city=Point(name="主城",x=607,y=291))
    game.v = SimpleNamespace(navigation_close_point=lambda *a:(138,38), map_coordinates=lambda im:(100,100),
        find=lambda im,*a,**kw:SimpleNamespace() if im is clear else None,
        page=lambda im:"team" if im is full else "sandbox",
        full_team=lambda im,key:Observation(key,wounded=False,strength=33000,maximum=33000))
    game.d = SimpleNamespace(control=SimpleNamespace(sleep=waits.append),tap=lambda *a:None)
    game.locate_team=lambda slot:cloud
    game.capture=lambda *a:next(frames)
    game.wait_transition=lambda im:im
    game.save_image=lambda *a:None
    game.home_matches_account=lambda im:True
    game.selected_full_slot=lambda *a:True
    game.v.full_team_rows=lambda im:{5:SimpleNamespace(x=128,y=674)}
    game.v.home_team_visible=lambda *a:False
    game.emit=lambda *a:None
    result=game.scan_team(TeamPolicy(slot=5))
    assert result.state=="home" and result.location==(100,100)
    assert waits==[.8]
