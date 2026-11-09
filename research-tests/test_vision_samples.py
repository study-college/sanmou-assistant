from pathlib import Path

import cv2
import numpy as np
import pytest

from sanmou_assistant.vision import Vision
from sanmou_assistant.game import Game, GameError
from sanmou_assistant.models import Point


ROOT = Path(__file__).resolve().parents[1]/"artifacts"


@pytest.fixture(scope="module")
def vision():
    return Vision()


@pytest.mark.parametrize("filename,slot,strength,wounded,pool",[("95-second-team-final.png",2,33000,False,"武卫二"),("54-main2-recovery-details.png",2,33000,True,"武卫二"),("96-first-team-final.png",1,33000,False,"普通"),("93-fourth-team-main-city-auto-refill.png",4,26342,False,"普通")])
def test_observed_full_team_images(vision,filename,slot,strength,wounded,pool):
    image = cv2.imread(str(ROOT/"fieldwork-2026-10-08"/filename))
    assert image is not None
    assert Game.selected_full_slot(image,slot)
    obs = vision.full_team(image,f"main:{slot}")
    assert obs.strength == strength and obs.maximum == 33000
    assert obs.wounded is wounded
    assert obs.pool == pool
    assert obs.supply == 100 and obs.stamina is not None
    assert obs.reserve is not None and obs.reserve <= obs.capacity
    if slot == 4:
        assert obs.auto_refill is False


def test_grain_and_map_target_are_read_from_scene(vision):
    image = cv2.imread(str(ROOT/"fieldwork-2026-10-08"/"111-final-sandbox.png"))
    assert vision.grain(image) == 1560000
    target = cv2.imread(str(ROOT/"fieldwork-2026-10-08"/"14-target-current.png"))
    assert vision.map_coordinates(target) == (640,286)


PAVE = ROOT/"research-2026-10-27-pave"/"samples"


@pytest.mark.parametrize("filename,slot,state", [
    ("pave-deploy-9.png", 2, "outside"),
    ("pave-deploy-10.png", 2, "home"),
])
def test_auto_pave_deployment_page_and_card_state(vision, filename, slot, state):
    # 目标不与连接地相邻时，攻占页会切换成“选择部队自动铺路”，仍是出征页。
    image = cv2.imread(str(PAVE/filename))
    assert image is not None
    assert vision.page(image) == "deployment"
    assert vision.deployment_status(image, slot) == state


def test_auto_pave_marker_reads_tiles_and_cost(vision):
    marker = vision.auto_pave(cv2.imread(str(PAVE/"pave-deploy-9.png")))
    assert marker == {"tiles": 9, "seconds": 117, "free_stamina": True}
    wide = vision.auto_pave(cv2.imread(str(PAVE/"pave-deploy-10.png")))
    assert wide["tiles"] == 10 and wide["seconds"] == 486
    assert vision.auto_pave(cv2.imread(str(PAVE/"pave-deploy-plain.png"))) is None


def test_network_lost_dialog_is_reconnect(vision):
    # 断线弹层原文“无网络连接，请检查网络状况后点击重连”，此前被判 unknown。
    image = cv2.imread(str(PAVE/"network-lost.png"))
    assert image is not None
    assert vision.page(image) == "reconnect"
    kind, button = vision.connection_prompt(image)
    assert kind == "reconnect"
    assert button is not None and button.text.replace(" ", "") == "重连"
    assert abs(button.x - 1190) <= 30 and abs(button.y - 656) <= 30


def test_top_bar_land_and_road_slots(vision):
    # 铺路消耗道路额度（34/35 → 35/35），铺路前必须能读到这两个计数。
    before = vision.top_slots(cv2.imread(str(PAVE/"top-road-34.png")))
    after = vision.top_slots(cv2.imread(str(PAVE/"top-road-35.png")))
    assert before == {"land": (60, 60), "road": (34, 35)}
    assert after == {"land": (60, 60), "road": (35, 35)}


def test_fuxian_clear_detail_name_and_selected_transfer_rest_status(vision):
    image = cv2.imread(str(ROOT/"fix-2026-10-09-backup/probe-172255/172328_303378-target-595-277.png"))
    assert image is not None
    game = Game.__new__(Game)
    game.v = vision
    game.verify_target(image,Point(name="补兵",x=595,y=277,expected_name="涪县"))
    with pytest.raises(GameError,match="目标名称不符"):
        game.verify_target(image,Point(name="补兵",x=595,y=277,expected_name="其他城"))
    with pytest.raises(GameError,match="坐标不同"):
        game.verify_target(image,Point(name="补兵",x=529,y=277,expected_name="涪县"))
    selected = cv2.imread(str(ROOT/"fix-2026-10-09-backup/probe-172701/172801_395147-transfer-selected.png"))
    assert vision.page(selected) == "deployment"
    assert vision.checked(selected,1330,855) is True
    assert vision.deployment_strength(selected,2) == 15116


@pytest.mark.parametrize('located_file,overview_file,slot,strength,state,coord', [
    ('exercise-174120/174158_694223-main_2-located.png','probe-174429/174507_857901-overview-inspect.png',2,15824,'recruiting',(595,277)),
    ('full-arrived-blue-marker.png','probe-180529/180559_408741-overview-inspect.png',1,33000,'outside',(595,277)),
    ('exercise-173650/173841_862077-dispatch-result.png','probe-174429/174507_857901-overview-inspect.png',2,15824,'marching',None),
])
@pytest.mark.skip(reason="0.7.4 起派兵/批次契约重写（角标闭环、卡位直提交、无颜色计数），旧断言作废；真机验证见 docs/50 与 docs/52")
def test_city_recruitment_is_not_the_city_march_menu(vision,tmp_path,located_file,overview_file,slot,strength,state,coord):
    from sanmou_assistant.journal import Journal
    from sanmou_assistant.models import Profile, TeamPolicy
    located = cv2.imread(str(ROOT/"fix-2026-10-09-backup"/located_file))
    overview = cv2.imread(str(ROOT/"fix-2026-10-09-backup"/overview_file))
    marching = cv2.imread(str(ROOT/"fix-2026-10-09-backup/exercise-173650/173841_862077-dispatch-result.png"))
    assert vision.stationary_return_visible(located,slot) == (state != 'marching')
    assert not vision.stationary_return_visible(marching,2)
    # locate_team已经返回当前定位帧，证据保存复用它，不再额外截同一画面。
    frames = iter([overview])
    class Device:
        def capture(self): return next(frames)
        def tap(self,*args): pass
    p = Profile(account="测试",main_city=Point(x=100,y=200),backups=[Point(name="城池",x=595,y=277)])
    j = Journal(tmp_path/'runtime.sqlite','test')
    j.set_phase(f'main:{slot}','backup')
    game = Game(Device(),vision,p,j,tmp_path,lambda *args:None)
    game.locate_team = lambda slot:located
    obs = game.scan_team(TeamPolicy(slot=slot))
    assert obs.state == state and obs.location == coord
    assert obs.strength == strength and obs.maximum == 33000 and obs.wounded is False
    j.close()


def test_sandbox_with_resource_bar_covered_is_still_recognized(vision):
    path = ROOT/"development-2026-10-09/acceptance-data/sessions/20261009-102425/102508_539953-unknown-page.png"
    image = cv2.imread(str(path))
    assert vision.page(image) == "sandbox"


@pytest.mark.parametrize("relative",[
    "issue-2026-10-10-auction-popup/auction-popup-200050.png",
    "issue-2026-10-10-auction-popup/reward-popup-210129.png",
])
def test_dismissible_overlay_popups_are_recognized(vision, relative):
    # 「拍卖开始」活动开启与「恭喜获得·助威币」奖励到账两类弹层,标题样式不同,
    # 共同特征是底部「点击空白处关闭」;必须识别为可关闭弹层而不是 unknown。
    image = cv2.imread(str(ROOT/relative))
    assert image is not None
    assert vision.page(image) == "dismissible_overlay"


@pytest.mark.parametrize("relative,allowed",[
    # 已领取面板四词齐全:保持原有 monthly_claimed 专属关闭位,弹层识别必须让路。
    ("fix-2026-10-09-farm-discovery/live-one-20261010-000611/000637_823550-unknown-page.png", {"monthly_claimed"}),
    # 领取后结果面板:中央四词 OCR 不全,判为可关闭弹层点空白关闭同样正确(奖励已入账),但不得落到 unknown 停机。
    ("fix-2026-10-09-farm-discovery/live-one-20261010-000347/000406_246765-monthly-claimed.png", {"monthly_claimed", "dismissible_overlay"}),
])
def test_monthly_panels_are_never_an_unknown_stop(vision, relative, allowed):
    image = cv2.imread(str(ROOT/relative))
    assert image is not None
    assert vision.page(image) in allowed


@pytest.mark.parametrize("relative",[
    "fix-2026-10-09-scheduling/acceptance-data/sessions/20261009-131309/131730_982516-unknown-page.png",
    "fix-2026-10-09-scheduling/packaged-failure/132748_464633-dispatch-result.png",
    "fix-2026-10-09-scheduling/packaged-failure/132912_298978-unknown-page.png",
    "fix-2026-10-09-farm-discovery/page-number-obscured.png",
    # 2026-10-17 实机：清晰“1/2”被 OCR 读成“172”（斜杠识为 7），曾判 unknown 卡死。
    "explore-2026-10-17/095105-chat-open.png",
    "explore-2026-10-17/094841-guard-target.png",
])
def test_sandbox_is_recognized_without_team_page_number(vision,relative):
    image = cv2.imread(str(ROOT/relative))
    assert image is not None
    assert vision.page(image) == "sandbox"
    # 没有分页条/页码被行军名或地图盖住时，沙盘页型仍由资源栏与底栏确认。
    covered = image.copy()
    covered[841:889,1770:1910] = 0
    assert vision.page(covered) == "sandbox"


def test_stylized_signin_count_keeps_all_digits_and_requires_matching_reads(vision):
    path = ROOT/"development-2026-10-09/acceptance-data/sessions/20261009-102801/102839_202914-signin.png"
    image = cv2.imread(str(path))
    assert vision.signin_count(image) == 27
    cards = vision.read(image,(782,97,1896,830))
    assert vision.highlighted_signin_card(image,next(t for t in cards if t.text == "第28天"))
    assert not vision.highlighted_signin_card(image,next(t for t in cards if t.text == "第27天"))
    incomplete = image.copy()
    incomplete[907:987,473:520] = 0
    assert vision.signin_count(incomplete) != 27


def test_bandit_join_slot_requires_current_rally_and_plus_icon(vision):
    rallies = cv2.imread(str(ROOT/"development-2026-10-09/acceptance-data/sessions/20261009-101520/101622_971431-bandits.png"))
    rows = vision.bandit_rallies(rallies)
    assert len(rows) == 2 and rows[0]["remaining_seconds"] == 413
    active = cv2.imread(str(ROOT/"development-2026-10-09/manual/102346_680788-bandits.png"))
    assert vision.bandit_rallies(active) == []


def test_spaced_game_button_characters_are_read_together(vision):
    image = cv2.imread(str(ROOT/"development-2026-10-09/acceptance-data/sessions/20261009-105036/105134_121400-farming-preflight.png"))
    button = vision.find(image,["屯田"],(740,651,1190,756),exact=True)
    assert button and button.text == "屯田" and 940 < button.x < 980


def test_city_farming_inventory_and_remaining_uses_from_real_failed_run(vision):
    import re
    folder = ROOT/'fix-2026-10-09-farming'
    result = cv2.imread(str(folder/'failed-result.png'))
    assert vision.page(result) == 'city'
    assert vision.grain(result) == 310000
    selected = cv2.imread(str(folder/'inspect-204612/204656_454431-farm-selected.png'))
    assert vision.find(selected,['农田'],(365,250,1240,612))
    button = vision.find(selected,['屯田'],(850,370,1550,665))
    assert button and int(re.search(r'余\s*(\d+)',button.text)[1]) == 18


def test_channel_popup_covering_team_cards_is_identified_and_closed_without_orders(vision,tmp_path):
    from sanmou_assistant.models import Profile
    popup = cv2.imread(str(ROOT/'fix-2026-10-09-farming/channel-popup.png'))
    deployment = cv2.imread(str(ROOT/'fix-2026-10-09-farming/channel-uncovered-deployment.png'))
    sandbox = cv2.imread(str(ROOT/'fieldwork-2026-10-08/111-final-sandbox.png'))
    assert vision.page(popup) == 'channel_menu'
    assert vision.page(deployment) == 'deployment'
    assert vision.deployment_strength(deployment,1) == 33000
    class Device:
        image = popup
        actions = []
        def capture(self):return self.image
        def channel_float_center(self, package): return (77, 904)
        def tap(self,x,y,*args,**kwargs):
            if self.image is popup:
                assert (x,y) == (77,904), '只能点击原生控件树确认的悬浮球'
                self.actions.append('collapse_channel')
                self.image = deployment
            else:
                assert (x,y) == vision.navigation_close_point(deployment,"deployment")
                self.actions.append('close_deployment')
                self.image = sandbox
    device = Device()
    game = Game(device,vision,Profile(),None,tmp_path,lambda *args:None)
    assert vision.page(game.ensure_sandbox()) == 'sandbox'
    assert device.actions == ['collapse_channel','close_deployment']


def test_army_context_menu_can_move_with_the_map(vision):
    image = cv2.imread(str(ROOT/"development-2026-10-09/acceptance-data/sessions/20261009-103939/104014_062902-main_2-located.png"))
    game = Game.__new__(Game)
    game.v = vision
    button = game.context_command(image,["回城"])
    assert button and 670 < button.x < 730


def test_reserve_page_artistic_title_is_recognized(vision):
    image = cv2.imread(str(ROOT/"development-2026-10-09/manual/112120_073780-ocr.png"))
    assert vision.page(image) == "reserve"


@pytest.mark.parametrize("relative", [
    "fix-2026-10-09-stall/121800_901914-dispatch-preflight.png",
    "fix-2026-10-09-stall/121829_864185-unknown-page.png",
    "development-2026-10-09/acceptance-data/sessions/20261009-113153/113314_469765-dispatch-preflight.png",
    "exploration-2026-10-08/50-attack-deployment.png",
])
def test_deployment_with_map_names_behind_title_is_recognized(vision, relative):
    image = cv2.imread(str(ROOT / relative))
    assert image is not None
    assert vision.page(image) == "deployment"


def test_deployment_requires_selection_prompt_and_submit_button(vision):
    image = cv2.imread(str(ROOT / "fix-2026-10-09-stall/121800_901914-dispatch-preflight.png"))
    covered = image.copy()
    covered[675:760, 475:1265] = 0
    assert vision.page(covered) != "deployment"
    covered = image.copy()
    covered[932:1036, 1540:1895] = 0
    assert vision.page(covered) != "deployment"


def test_deployment_can_be_closed_even_when_map_labels_hide_action_title(vision):
    image = cv2.imread(str(ROOT / "fix-2026-10-09-stall/121800_901914-dispatch-preflight.png"))
    image[0:175, 750:1190] = 0
    assert vision.page(image) == "deployment"


@pytest.mark.parametrize("slot,expected",[(1,33000),(2,33000),(3,33000),(4,26342),(5,33000)])
def test_deployment_strength_reads_only_current_card_digits(vision,slot,expected):
    image = cv2.imread(str(ROOT/"fix-2026-10-09-scheduling/packaged-failure/140007_530992-dispatch-preflight.png"))
    assert vision.deployment_strength(image,slot) == expected
    image[968:1006,145+(slot-1)*249:254+(slot-1)*249] = 0
    assert vision.deployment_strength(image,slot) is None


def test_bandit_rally_details_have_a_verified_close_path(vision, tmp_path):
    from sanmou_assistant.models import Profile
    image = cv2.imread(str(ROOT/"fix-2026-10-09-scheduling/packaged-failure/140259_869669-bandit-join-preflight.png"))
    assert vision.page(image) == "rally"
    calls = []
    class Device:
        def tap(self,x,y,*args): calls.append((x,y))
    game = Game(Device(), vision, Profile(), None, tmp_path, lambda *args: None)
    frames = iter([image,cv2.imread(str(ROOT/"fieldwork-2026-10-08/111-final-sandbox.png"))])
    game.capture = lambda *args:next(frames)
    game.emit = lambda *args:None
    assert game.ensure_sandbox() is not None
    assert calls == [vision.navigation_close_point(image,"rally")]
    image[845:940,800:1120] = 0
    assert vision.page(image) == "rally"  # 已加入或结束后参与按钮可能消失，详情仍应能关闭。


@pytest.mark.parametrize("filename,expected", [
    ("attack-result-with-resource-overlap.png", 3660000),
    ("resource-current.png", 3670000),
])
def test_grain_digits_are_not_merged_with_frontline_player_names(vision, filename, expected):
    import numpy as np
    # 此页旧裁剪识别为“银66万/600万”，漏了首位 3。
    path = ROOT / "fix-2026-10-09-stall" / filename
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    assert vision.grain(image) == expected


def test_grain_conflicting_or_incomplete_reads_remain_unknown(monkeypatch):
    from sanmou_assistant.vision import Text
    import numpy as np
    readings = iter(["366万/600万", "66万/600万"])
    detector = Vision.__new__(Vision)
    monkeypatch.setattr(detector, "read", lambda *args: [Text(next(readings), .99, 0, 0, (0, 0, 1, 1))])
    assert detector.grain(np.zeros((1080, 1920, 3), np.uint8)) is None
    monkeypatch.setattr(detector, "read", lambda *args: [Text("银66万/600万", .99, 0, 0, (0, 0, 1, 1))])
    assert detector.grain(np.zeros((1080, 1920, 3), np.uint8)) is None


def test_stalled_deployment_can_return_to_sandbox_without_submitting(vision, tmp_path):
    from sanmou_assistant.models import Profile
    deployment = cv2.imread(str(ROOT / "fix-2026-10-09-stall/121829_864185-unknown-page.png"))
    sandbox = cv2.imread(str(ROOT / "fieldwork-2026-10-08/111-final-sandbox.png"))
    class Device:
        def __init__(self):
            self.image = deployment
            self.taps = []
        def capture(self):
            return self.image
        def tap(self, x, y, *args):
            self.taps.append((x, y))
            assert abs(x-1840)<6 and abs(y-547)<6, "只允许退出，不点击出征"
            self.image = sandbox
    device = Device()
    game = Game(device, vision, Profile(), None, tmp_path, lambda *args: None)
    assert vision.page(game.ensure_sandbox()) == "sandbox"
    assert device.taps == [vision.navigation_close_point(deployment,"deployment")]


def test_return_intent_is_saved_before_first_return_click(vision,tmp_path):
    from sanmou_assistant.journal import Journal
    from sanmou_assistant.models import Observation,Profile,TeamPolicy
    from sanmou_assistant.transport import Stopped
    image = cv2.imread(str(ROOT/"development-2026-10-09/acceptance-data/sessions/20261009-103939/104014_062902-main_2-located.png"))
    journal = Journal(tmp_path/"runtime.sqlite","test")
    class InterruptedDevice:
        def capture(self):
            return image
        def tap(self,*args,**kwargs):
            assert journal.pending("main:2"), "提交前必须已有持久记录"
            raise Stopped()
    game = Game(InterruptedDevice(),vision,Profile(),journal,tmp_path,lambda *args:None)
    game.account_checked = True
    game.locate_team = lambda slot:image
    obs = Observation("main:2",state="outside",strength=33000,maximum=33000,wounded=False)
    with pytest.raises(Stopped):
        game.recall(TeamPolicy(slot=2),obs,manual=True)
    pending = journal.pending("main:2")
    assert len(pending) == 1 and pending[0][1] == "return" and pending[0][3] == "uncertain"
    # 回城仍按现场核对（不重复撤军），记录保留为 uncertain 等下一轮观察。
    assert journal.recent_orders("main:2")[0][1] == "return"
    journal.close()


@pytest.mark.parametrize("manual",[False,True])
def test_battle_return_prompt_cancels_automatic_and_confirms_manual(vision,tmp_path,manual):
    from sanmou_assistant.journal import Journal
    from sanmou_assistant.models import Observation,Profile,TeamPolicy
    from sanmou_assistant.game import GameError
    initial = cv2.imread(str(ROOT/"development-2026-10-09/acceptance-data/sessions/20261009-103939/104014_062902-main_2-located.png"))
    prompt = cv2.imread(str(ROOT/"fix-2026-10-09-scheduling/packaged-failure/battle-withdraw-prompt.png"))
    sandbox = cv2.imread(str(ROOT/"fieldwork-2026-10-08/111-final-sandbox.png"))
    journal = Journal(tmp_path/"runtime.sqlite","test")
    calls = []
    class Device:
        def tap(self,x,y,*args):
            assert journal.pending("main:2"), "弹窗确认前保留已有回城意图"
            calls.append((x,y))
    game = Game(Device(),vision,Profile(),journal,tmp_path,lambda *args:None)
    game.account_checked = True
    game.locate_team = lambda slot:initial
    frames = iter([initial,prompt,sandbox])
    game.capture = lambda *args:next(frames)
    obs = Observation("main:2",state="outside",strength=33000,maximum=33000,wounded=False)
    if manual:
        game.recall(TeamPolicy(slot=2),obs,manual=True)
        assert journal.phase("main:2") == "returning"
        assert calls[-1][0] > 995
        expected = "accepted"
    else:
        with pytest.raises(GameError,match="仍在战斗"):
            game.recall(TeamPolicy(slot=2),obs)
        assert journal.phase("main:2") == ""
        assert 540 < calls[-1][0] < 910
        expected = "rejected"
    assert len(calls) == 2 and not journal.pending("main:2")
    assert journal.db.execute("SELECT status FROM intents").fetchone() == (expected,)
    journal.close()


@pytest.mark.parametrize("target,expected_home",[("fix-2026-10-09-scheduling/packaged-failure/home-with-context.png",True),("fieldwork-2026-10-08/14-target-current.png",False)])
def test_locating_an_unselected_city_still_requires_its_coordinates_and_owner(vision,tmp_path,target,expected_home):
    from sanmou_assistant.models import Point,Profile
    initial = cv2.imread(str(ROOT/"fix-2026-10-09-scheduling/packaged-failure/home-without-context.png"))
    selected = cv2.imread(str(ROOT/target))
    assert vision.page(initial) == "sandbox" and vision.map_coordinates(initial) is None
    class Device:
        image = initial
        def tap(self,x,y,*args):
            if (x,y) == (960,539):
                self.image = selected
        def swipe(self,*args): pass
        def capture(self): return self.image
    profile = Profile(account="测试主公",main_city=Point(x=607,y=291,expected_name="测试主公"))
    game = Game(Device(),vision,profile,None,tmp_path,lambda *args:None)
    image = game.locate_team(1)
    game.check_account(image)
    assert game.account_checked is expected_home
    assert vision.map_coordinates(image) is not None


def test_main_city_title_confirms_owner_when_owner_row_is_unreadable(vision,tmp_path):
    from sanmou_assistant.models import Point,Profile
    from sanmou_assistant.game import GameError
    image = cv2.imread(str(ROOT/"fix-2026-10-09-scheduling/packaged-failure/home-owner-row-missed.png"))
    image[370:438,320:745] = 0
    game = Game(None,vision,Profile(account="测试主公",main_city=Point(x=607,y=291)),None,tmp_path,lambda *args:None)
    game.check_account(image)
    assert game.account_checked
    game.p.account = "其他账号"
    with pytest.raises(GameError,match="账号与配置档不符"):
        game.check_account(image)
    game.p.account = "测试主公"
    image[238:345,700:1180] = 0
    with pytest.raises(GameError,match="账号与配置档不符"):
        game.check_account(image)

