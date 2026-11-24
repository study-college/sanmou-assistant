"""2026-11-23 拉线效率与稳健性系统修复回归测试。

覆盖实机 62 分钟挂机排查出的核心问题：
1. 拉线选队后必须重截再核对「一键补兵」按钮，不能拿默认选中满兵队的 preflight 旧图误判；
2. 一键补兵后只要兵力已达到出击下限，即使区块配额归零也应直接放行出征；
3. 出征页「征兵」被 OCR 识别为形近字「证兵」时仍正确判定为 recruiting；
4. MuMu 实例就绪判定兼容 player_state="start_finished" 且 adb_port 已就绪；
5. 拉线等待态（调动/行军/撤退/重伤）无缓存也不飞镜头；回主城只读 1 次；
   城外可派且已知满兵上限时直奔出征页不飞镜头破坏目标视角。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from sanmou_assistant.game import Game, LineRefillUnavailable, NavigationRecovered
from sanmou_assistant.models import Observation, PaveRoute, Point, Profile, TeamPolicy
from sanmou_assistant.transport import _decode_screencap, _instance_ready
from sanmou_assistant.vision import Vision
from sanmou_assistant.worker import Worker


FRAME_PRE = np.zeros((1080, 1920, 3), np.uint8)
FRAME_SELECTED = np.full((1080, 1920, 3), 1, np.uint8)
FRAME_AFTER_REFILL = np.full((1080, 1920, 3), 2, np.uint8)
FRAME_SUBMITTED = np.full((1080, 1920, 3), 3, np.uint8)


def _line_dispatch_game(*, preflight_has_refill=False, selected_has_refill=True,
                        strength_before=20666, strength_after=31487, quota_after=0):
    game = Game.__new__(Game)
    game.active_slots = {1, 2, 3, 4, 5}
    game.account_checked = True
    game.emit = lambda kind, value: None
    game.log = lambda *args, **kwargs: None
    game.save_image = lambda *args, **kwargs: None
    game.go_to = lambda point: FRAME_PRE
    game.reopen_target = lambda point: FRAME_PRE
    game.pick_action = lambda img, point, kind: ("行军", "march", (1317, 460))
    game.d = SimpleNamespace(
        tap=lambda *args, **kwargs: None,
        control=SimpleNamespace(sleep=lambda s: None),
    )
    captures = iter([FRAME_SELECTED, FRAME_AFTER_REFILL, FRAME_SUBMITTED])
    game.capture = lambda *args, **kwargs: next(captures, FRAME_SUBMITTED)
    clear_frames = iter([FRAME_PRE, FRAME_SUBMITTED])
    game.capture_clear = lambda *args, **kwargs: next(clear_frames, FRAME_SUBMITTED)
    game._settle_dispatch_prompts = lambda img, policy, name: img
    game.read_team_marker = lambda img, slot: (
        {"state": "march", "label": "行军", "confidence": 0.99}
        if img is FRAME_SUBMITTED else None
    )
    finished = []
    game.j = SimpleNamespace(
        pending=lambda key: [],
        prepare=lambda *args, **kwargs: "intent-1",
        finish=lambda ident, status, note: finished.append((ident, status, note)),
        set_phase=lambda key, phase: None,
    )
    game.finished_intents = finished

    def refill_button(img):
        if img is FRAME_PRE:
            return (1400, 1004) if preflight_has_refill else None
        if img is FRAME_SELECTED:
            return (1400, 1004) if selected_has_refill else None
        return None

    def deployment_strength(img, slot, center_x=None):
        if img is FRAME_AFTER_REFILL:
            return strength_after
        return strength_before

    def refill_quota(img):
        if img is FRAME_AFTER_REFILL:
            return quota_after
        return 8000 if img is FRAME_SELECTED else None

    game.v = SimpleNamespace(
        page=lambda img, verify=True: "sandbox" if img is FRAME_SUBMITTED else "deployment",
        deploy_button_enabled=lambda img: True,
        deployment_strength=deployment_strength,
        deployment_status=lambda img, slot, center_x=None: "recruiting",
        refill_button=refill_button,
        refill_quota=refill_quota,
        joined=lambda img, rect: "",
    )
    return game


def test_line_dispatch_recaptures_after_selecting_team_and_passes_when_refill_hits_floor():
    """选队前旧图无补兵按钮（满兵队），选队重截后有补兵按钮；
    补兵后 20666→31487（95.4% ≥ 90%），即使区块配额归零也直接出征。"""
    game = _line_dispatch_game(
        preflight_has_refill=False,
        selected_has_refill=True,
        strength_before=20666,
        strength_after=31487,
        quota_after=0,
    )
    policy = TeamPolicy(slot=4, role="line", min_mode="percent", minimum=90, refill_enabled=True)
    obs = Observation("main:4", state="active", marker_state="busy",
                      wounded=False, strength=20666, maximum=33000)
    point = Point(name="空地 (312,218)", x=312, y=218, actions=["march", "attack", "occupy", "guard"])
    game._dispatch(policy, obs, point, "line")
    assert game.finished_intents and game.finished_intents[-1][1] == "accepted"


def test_line_dispatch_still_raises_when_quota_zero_and_strength_below_floor():
    """区块归零且补后兵力仍低于出击线时，仍必须判定补不动并走兜底。"""
    game = _line_dispatch_game(
        preflight_has_refill=False,
        selected_has_refill=True,
        strength_before=15000,
        strength_after=25000,
        quota_after=0,
    )
    policy = TeamPolicy(slot=4, role="line", min_mode="percent", minimum=90, refill_enabled=True)
    obs = Observation("main:4", state="active", marker_state="busy",
                      wounded=False, strength=15000, maximum=33000)
    point = Point(name="空地 (312,218)", x=312, y=218, actions=["march", "attack", "occupy", "guard"])
    with pytest.raises(LineRefillUnavailable, match="区块归零"):
        game._dispatch(policy, obs, point, "line")


def test_card_state_recognises_ocr_variant_zhengbing():
    """出征页金色「征兵」被 OCR 读成形近字「证兵」时必须识别为 recruiting。"""
    assert Vision._card_state("证兵 | 00:05:04") == "recruiting"
    assert Vision._card_state("征兵 | 02:37:48") == "recruiting"


def test_mumu_instance_ready_accepts_start_finished_with_adb_port():
    assert _instance_ready({"is_android_started": True}) is True
    assert _instance_ready({"is_android_started": False, "player_state": "start_finished", "adb_port": 16384}) is True
    assert _instance_ready({"is_android_started": False, "player_state": "start_finished", "adb_port": 0}) is False
    assert _instance_ready({"is_android_started": False, "player_state": "stopped", "adb_port": 16384}) is False


class _FakeJournal:
    def pending(self, key):
        return []

    def phase(self, key):
        return ""

    def set_phase(self, key, value):
        pass

    def failure_streak(self, key):
        return 0


class _LineWorkerGame:
    def __init__(self, hud, scan_obs=None):
        self.hud = hud
        self.scan_obs = scan_obs
        self.scanned = 0
        self.dispatched = []
        self.logs = []
        self.account_checked = True
        self.v = SimpleNamespace(find=lambda *args, **kwargs: None)

    def read_all_marker_states(self):
        return self.hud

    def scan_team(self, policy):
        self.scanned += 1
        return self.scan_obs

    def scan_pave_team(self, policy):
        return Observation(policy.key, state="outside", marker_state="unknown", note="铺路快照")

    def capture(self, *args, **kwargs):
        return FRAME_PRE

    def ensure_sandbox(self):
        pass

    def log(self, message, level="INFO"):
        self.logs.append((level, message))

    def dispatch(self, policy, obs, point, kind):
        self.dispatched.append((policy.key, kind, (point.x, point.y), obs.maximum))


@pytest.mark.parametrize("marker_state", ["transfer", "march", "retreat", "wounded", "fighting", "guard", "entrust"])
def test_line_wait_states_never_fly_camera_without_cache(tmp_path, marker_state):
    """等待态（调动/行军/撤退/重伤等）即使刚派兵清了兵力缓存，也绝不飞镜头读详情。"""
    app = QApplication.instance() or QApplication([])
    policy = TeamPolicy(slot=1, role="line")
    profile = Profile(teams=[policy], line_targets=[Point(name="空地", x=310, y=216, actions=["march"])])
    worker = Worker(profile, tmp_path, command="run", dry_run=False)
    worker.journal = _FakeJournal()
    game = _LineWorkerGame(hud={1: {"state": marker_state, "label": marker_state, "confidence": 0.99}})
    worker.process_line(game)
    assert game.scanned == 0, f"角标 {marker_state} 只需等待，不应飞镜头读队"
    assert game.dispatched == []
    worker.deleteLater()
    app.processEvents()


def test_line_home_transition_reads_once_not_twice(tmp_path):
    """刚回主城时只完整读 1 次即调动，不能重复读第 2 遍。"""
    app = QApplication.instance() or QApplication([])
    policy = TeamPolicy(slot=1, role="line", minimum=95)
    backup = Point(name="测试营地", x=320, y=225, actions=["attack", "occupy", "guard"])
    profile = Profile(
        teams=[policy],
        line_targets=[Point(name="空地", x=310, y=216, actions=["march"])],
        line_backups={"main:1": backup},
    )
    worker = Worker(profile, tmp_path, command="run", dry_run=False)
    worker.journal = _FakeJournal()
    worker.line_last_marker["main:1"] = "retreat"
    home_obs = Observation("main:1", state="home", marker_state="idle",
                           strength=33000, maximum=33000, wounded=False, note="兵力已交叉核对")
    game = _LineWorkerGame(
        hud={1: {"state": "idle", "label": "空闲", "confidence": 0.99}},
        scan_obs=home_obs,
    )
    worker.process_line(game)
    assert game.scanned == 1, "回主城核对兵力只应完整读 1 次，不能连读 2 遍"
    assert game.dispatched == [("main:1", "transfer", (320, 225), 33000)]
    worker.deleteLater()
    app.processEvents()


def test_line_outside_ready_skips_camera_fly_when_max_known(tmp_path):
    """城外调动停留且已知满兵上限（即使缓存已清/过期）时，不先飞镜头读队，直接进出征页。"""
    app = QApplication.instance() or QApplication([])
    policy = TeamPolicy(slot=2, role="line", refill_enabled=True)
    profile = Profile(teams=[policy], line_targets=[Point(name="空地", x=310, y=216, actions=["march"])])
    worker = Worker(profile, tmp_path, command="run", dry_run=False)
    worker.journal = _FakeJournal()
    worker.line_max_strength["main:2"] = 33000  # 已知上限，line_snapshot 为空
    game = _LineWorkerGame(
        hud={2: {"state": "transfer_stay", "label": "调动停留", "confidence": 0.99}},
    )
    worker.process_line(game)
    assert game.scanned == 0, "城外可派且已知上限时应直奔目标出征页，不飞镜头破坏目标视角"
    assert game.dispatched == [("main:2", "line", (310, 216), 33000)]
    worker.deleteLater()
    app.processEvents()


def test_decode_screencap_handles_raw_12_and_16_byte_headers_and_png():
    import struct
    import cv2

    bgr = np.zeros((4, 8, 3), np.uint8)
    bgr[:, :] = (10, 20, 30)  # B=10, G=20, R=30
    rgba = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA)
    for hdr in (struct.pack("<III", 8, 4, 1), struct.pack("<IIII", 8, 4, 1, 0)):
        out = _decode_screencap(hdr + rgba.tobytes())
        assert out is not None and out.shape == (4, 8, 3)
        assert np.array_equal(out, bgr)
    ok, png = cv2.imencode(".png", bgr)
    assert ok
    out_png = _decode_screencap(png.tobytes())
    assert out_png is not None and np.array_equal(out_png, bgr)
    assert _decode_screencap(b"corrupted") is None


def test_pave_wait_states_skip_camera_fly_via_hud(tmp_path):
    """铺路进行中只要整帧角标明确在行军/战斗/占领/撤退/重伤，直接原地等待不飞镜头。"""
    app = QApplication.instance() or QApplication([])
    policy = TeamPolicy(slot=1, role="pave")
    route = PaveRoute(name="铺路 1", team="main:1", points=[(330, 240, "空地")], enabled=True)
    profile = Profile(teams=[policy], pave_routes=[route])
    worker = Worker(profile, tmp_path, command="run", dry_run=False)

    class _PaveJournal(_FakeJournal):
        def pave_progress(self, name):
            return {"idx": 0, "status": "running", "note": "", "fingerprint": worker.pave_fingerprint(route),
                    "origin": "", "intent": ""}

        def set_pave(self, *args, **kwargs):
            pass

    worker.journal = _PaveJournal()
    game = _LineWorkerGame(hud={1: {"state": "march", "label": "行军", "confidence": 0.99}})
    worker.process_pave(game)
    assert game.scanned == 0, "铺路行军途中不应飞镜头定位"
    worker.deleteLater()
    app.processEvents()


def test_reopen_target_guardrails_block_main_city_expired_and_recover_misclick():
    """点屏幕中心护栏：禁止主城走快路、超时自动作废、误触非沙盘自动自愈回落 go_to。"""
    game = Game.__new__(Game)
    main_city = Point(name="主城", x=300, y=200)
    target = Point(name="空地", x=310, y=216)
    game.p = Profile(main_city=main_city)
    game.note_view(main_city)
    assert game.view_on(main_city) is False, "禁止对自己主城走点击屏幕中心快路"

    game.note_view(target)
    assert game.view_on(target) is True
    game.view_at = time.monotonic() - game.VIEW_TTL - 1
    assert game.view_on(target) is False, "超过有效期的镜头结论必须自动作废"

    # 模拟误触中心进入内城（page="city"）：应先调用 ensure_sandbox(recover_navigation=True) 再回落 go_to。
    recovered = []
    goto_calls = []
    game.note_view(target)
    game.ensure_sandbox = lambda recover_navigation=False, **kw: (
        recovered.append(recover_navigation) or (_ for _ in ()).throw(NavigationRecovered("ok"))
        if recover_navigation else FRAME_PRE
    )
    game.tap_target_center = lambda label: FRAME_SELECTED
    game.save_image = lambda *args, **kwargs: None
    game.go_to = lambda pt: (goto_calls.append((pt.x, pt.y)) or FRAME_SUBMITTED)
    game.v = SimpleNamespace(
        map_coordinates=lambda img: None,
        page=lambda img, verify=True: "city",
    )
    res = game.reopen_target(target)
    assert res is FRAME_SUBMITTED
    assert recovered == [True], "误触非沙盘后必须先安全恢复回沙盘"
    assert goto_calls == [(310, 216)], "恢复沙盘后应无缝回落到 go_to"
    assert game.view_point is None


def test_outside_state_recognises_zhengbing_variant():
    game = Game.__new__(Game)
    game.j = None
    game.v = SimpleNamespace(
        returning_team_visible=lambda img, slot: False,
        stationary_return_visible=lambda img, slot: False,
        joined=lambda img, rect: "证兵 00:12:00",
    )
    assert game.read_outside_state(FRAME_PRE, TeamPolicy(slot=1), (320, 225)) == "recruiting"
