"""铺路到达判定、快路径与角标状态的单元测试（不连接设备）。"""

from sanmou_assistant.models import Observation, Point, Profile, decide
from sanmou_assistant.team_states import can_dispatch, display_label, is_busy, is_in_battle, is_moving
from sanmou_assistant.worker import needs_detail, pave_at_point


def test_pave_arrival_requires_stationary_at_the_target():
    obs = Observation("main:1", state="outside", location=(10, 20), marker_state="stationary")
    assert pave_at_point(obs, (10, 20))
    assert not pave_at_point(obs, (10, 21))
    # 调动停留是建筑里的停留，不算铺路到点
    obs.marker_state = "transfer_stay"
    assert not pave_at_point(obs, (10, 20))
    # 没有角标时退回旧判定，保证旧路径不中断
    obs.marker_state = None
    assert pave_at_point(obs, (10, 20))


def test_fast_path_only_reads_details_for_teams_we_might_dispatch():
    assert needs_detail({"state": "stationary"}, False)          # 停留：要读兵力才能派
    assert needs_detail({"state": "idle"}, False)                # 空闲：要读兵力才能派
    assert needs_detail({"state": "transfer_stay"}, False)       # 营帐里：可能要派
    assert not needs_detail({"state": "busy"}, False)            # 忙碌：不派，省掉详情
    assert not needs_detail({"state": "march"}, False)           # 行军：不派
    assert not needs_detail({"state": "guard"}, False)           # 驻守：不派
    assert needs_detail({"state": "busy"}, True)                 # 有待核对订单：仍要读现场
    assert not needs_detail({"state": "unknown"}, False)         # 未识别：不派，省掉详情
    assert needs_detail(None, False)                             # 没有整帧结果：退回逐队


def test_dispatch_rules_follow_the_learned_states():
    assert can_dispatch("idle") and can_dispatch("stationary") and can_dispatch("transfer_stay")
    for state in ("busy", "march", "transfer", "guard", "retreat", "fighting", "queued_battle", "unknown"):
        assert not can_dispatch(state)
    assert is_busy("busy")
    assert is_in_battle("fighting") and is_in_battle("queued_battle")
    assert is_moving("march") and is_moving("retreat") and is_moving("transfer")


def test_overview_prefers_marker_label_and_falls_back_to_legacy():
    legacy = {"home": "主城待命", "active": "任务进行中"}
    assert display_label("忙碌", "active", legacy) == "忙碌"
    assert display_label("调动停留", "outside", legacy) == "调动停留"
    assert display_label(None, "home", legacy) == "主城待命"
    assert display_label(None, "别的词", legacy) == "待识别"


def test_team_standing_on_target_keeps_the_configured_action_order():
    """用户 2026-11-03：脚本不按“队伍站在哪”裁剪指令，顺序永远以配置为准。"""
    p = Profile(account="测试", main_city=Point(x=100, y=200),
                targets=[Point(name="山道", x=636, y=286, actions=["attack", "guard", "occupy", "march"])])
    t = p.teams[0]
    t.role = "merit"
    obs = Observation(t.key, state="outside", strength=33000, maximum=33000, stamina=100, supply=100,
                      wounded=False, location=(636, 286), marker_state="stationary")
    # 站在目标格上照样要下指令，而且候选动作就是配置里那一串，不能被改写成“驻守/行军”。
    assert decide(p, t, obs, 1000000).action == "merit"
    assert p.targets[0].actions == ["attack", "guard", "occupy", "march"]
    # 不在目标点上时同样保留原顺序（这条以后不再有第二条分支）。
    obs.location = (630, 280)
    assert p.targets[0].actions == ["attack", "guard", "occupy", "march"]


def test_hide_mode_reads_the_eye_button_state():
    import cv2
    from pathlib import Path
    from sanmou_assistant.vision import Vision

    root = Path(__file__).resolve().parents[1]
    vision = Vision()
    open_frame = cv2.imread(str(root / "artifacts/hide-toggle/probe2/before.png"))
    hidden_frame = cv2.imread(str(root / "artifacts/hide-toggle/probe2/after-tap.png"))
    assert vision.hide_mode(open_frame)[0] == "open"
    assert vision.hide_mode(hidden_frame)[0] == "hidden"


def test_hide_setting_defaults_to_on_and_survives_round_trip():
    p = Profile(account="测试")
    assert p.hide_before_run is True
    restored = Profile.from_dict(p.to_dict())
    assert restored.hide_before_run is True
    restored.hide_before_run = False
    assert Profile.from_dict(restored.to_dict()).hide_before_run is False


def test_needs_detail_skips_wounded_and_busy():
    # 重伤/忙碌都不派兵，角标足够决策；为读倒计时开详情页每队多花二十多秒，不再做。
    assert not needs_detail({"state": "wounded"}, False)
    assert not needs_detail({"state": "busy"}, False)
    assert needs_detail({"state": "wounded"}, True)   # 有待核对订单仍要读现场


def test_old_hide_setting_name_is_migrated():
    legacy = Profile(account="测试").to_dict()
    legacy.pop("hide_before_run")
    legacy["close_hide_before_run"] = False
    assert Profile.from_dict(legacy).hide_before_run is False


def test_action_icons_match_at_native_scale():
    import cv2
    from pathlib import Path
    from sanmou_assistant.vision import Vision

    root = Path(__file__).resolve().parents[1]
    vision = Vision()
    land = cv2.imread(str(root / "artifacts/icon-samples/live/land-581-236.png"))
    buttons = vision.action_buttons(land)
    assert "occupy" in buttons and "march" in buttons
    assert "siege" not in buttons
    tent = cv2.imread(str(root / "artifacts/icon-samples/live/tent-634-266.png"))
    assert {"march", "transfer", "guard"} <= set(vision.action_buttons(tent))
    enemy = cv2.imread(str(root / "artifacts/icon-samples/live/go-644-285-select.png"))
    assert "siege" in vision.action_buttons(enemy)


def test_frame_clear_rejects_cloud_transition_frames():
    import cv2
    from pathlib import Path
    from sanmou_assistant.vision import Vision

    root = Path(__file__).resolve().parents[1]
    vision = Vision()
    normal = cv2.imread(str(root / "artifacts/verify-0.7.4/state-final/frame.png"))
    fog = cv2.imread(str(root / "data/sessions/_evidence/routine/20261102-232205/20261102-233547_139661-4e948a84-main_4-located.png"))
    assert vision.frame_clear(normal) is True
    assert vision.frame_clear(fog) is False
    # 出征页没有底栏，不能因为顶部是暗色标题就误判成过渡
    deployment = cv2.imread(str(root / "artifacts/icon-samples/live/deployment-live.png"))
    assert vision.frame_clear(deployment) is True


def test_departure_button_state():
    import cv2
    from pathlib import Path
    from sanmou_assistant.vision import Vision

    root = Path(__file__).resolve().parents[1]
    vision = Vision()
    page = cv2.imread(str(root / "artifacts/icon-samples/live/deployment-live.png"))
    assert vision.deploy_button_enabled(page) is True
    grey = page.copy()
    grey[947:1020, 1580:1860] = (60, 60, 60)
    assert vision.deploy_button_enabled(grey) is False
