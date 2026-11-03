from pathlib import Path

import cv2
import numpy as np
import pytest

from sanmou_assistant.team_markers import BADGE_DY, SLOT_Y, TeamMarkerReader


ROOT = Path(__file__).resolve().parents[1]


def scene(path):
    image = cv2.imdecode(np.fromfile(ROOT / path, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    return image


@pytest.fixture(scope="module")
def reader():
    return TeamMarkerReader()


def states(reader, path):
    return [r["state"] for r in reader.read(scene(path))]


def test_user_labeled_frame_reads_all_five_states(reader):
    # 2026-11-02 16:47:18 用户校准帧：调动停留 / 空闲 / 行军 / 驻守 / 停留
    assert states(reader, "artifacts/team-marker-calibration/frame-01/frame.png") == [
        "transfer_stay", "idle", "march", "guard", "stationary"]


def test_second_labeled_frame_reads_transfer_and_retreat(reader):
    # 2026-11-02 17:1x 用户校准帧：调动停留 / 调动 / 撤退 / 驻守 / 停留
    assert states(reader, "artifacts/team-marker-calibration/frame-04/frame.png") == [
        "transfer_stay", "transfer", "retreat", "guard", "stationary"]


def test_battle_and_busy_states_are_recognised(reader):
    # frame-05：排队进入战斗 / 行军 / 行军 / 空闲 / 忙碌（金色时钟）
    assert states(reader, "artifacts/team-marker-calibration/frame-05/frame.png") == [
        "queued_battle", "march", "march", "idle", "busy"]
    # frame-06：撤退 / 战斗 / 行军 / 空闲 / 忙碌
    assert states(reader, "artifacts/team-marker-calibration/frame-06/frame.png") == [
        "retreat", "fighting", "march", "idle", "busy"]
    # frame-08：用户在营帐里自然征兵，1/2 队同样显示金色时钟 => 忙碌，不是占领专用
    assert states(reader, "artifacts/team-marker-calibration/frame-08/frame.png") == [
        "busy", "busy", "idle", "idle", "idle"]


def test_white_left_arrow_is_retreat_not_returning(reader):
    # 旧资产 returning-team-mask.png 的白色左箭头已由用户纠正为撤退，不得读成回城/返回。
    result = reader.read(scene("artifacts/stability-2026-10-28/samples/returning-team.png"))
    assert result[1]["state"] == "retreat"
    assert result[1]["label"] == "撤退"


def test_dark_hair_under_badge_area_is_not_mistaken_for_a_marker(reader):
    # 2026-11-02 第二次实机帧：2 队头像长黑发铺满角标区，仍必须读成空闲。
    result = reader.read(scene("artifacts/team-marker-calibration/frame-02/frame.png"))
    assert result[1]["state"] == "idle"
    assert result[1]["glyph"] is None


def test_same_gold_flag_reads_as_guard_on_different_avatars(reader):
    # 同一套黄色旗形角标出现在不同槽位、不同头像上，仍然认作驻守。
    result = reader.read(scene("artifacts/stability-2026-10-29/samples/active.png"))
    assert [r["state"] for r in result[:3]] == ["guard", "guard", "guard"]
    assert [r["state"] for r in result[3:]] == ["idle", "idle"]


def synthetic_frame(reader):
    """造一个只有角标底板、字形没学过的画面。"""
    image = np.full((1080, 1920, 3), 40, np.uint8)
    for y in SLOT_Y:
        cv2.rectangle(image, (1780, y + 51), (1900, y + 56), (60, 220, 60), -1)
    centre_x, centre_y = 1869, SLOT_Y[3] + BADGE_DY + 29
    diamond = np.array([
        [centre_x + 29, centre_y], [centre_x, centre_y - 29],
        [centre_x - 29, centre_y], [centre_x, centre_y + 29],
    ], np.int32)
    cv2.fillPoly(image, [diamond], (20, 20, 20))
    cv2.circle(image, (centre_x, centre_y), 11, (255, 255, 255), -1)
    return image


def test_unrecognized_glyph_stays_unknown_instead_of_idle(reader):
    # 有角标底板、有居中亮色字形，但没有任何模板匹配 => unknown，不能降级成空闲。
    result = reader.read_slot(synthetic_frame(reader), 4)
    assert result["state"] == "unknown"
    assert result["glyph"] is not None


def test_every_state_has_a_legacy_mapping_for_the_worker(reader):
    from sanmou_assistant.team_states import LEGACY_STATE, STATES

    assert set(LEGACY_STATE) == set(STATES)
    assert LEGACY_STATE["idle"] == "home"
    assert LEGACY_STATE["busy"] == "active"
    assert LEGACY_STATE["unknown"] == "unknown"


def test_avatars_visible_requires_the_right_edge_column(reader):
    assert reader.avatars_visible(scene("artifacts/team-marker-calibration/frame-01/frame.png"))
    assert not reader.avatars_visible(np.zeros((1080, 1920, 3), dtype=np.uint8))


def test_marker_reader_rejects_unexpected_frame_size(reader):
    with pytest.raises(ValueError, match="1920×1080"):
        reader.read(np.zeros((100, 100, 3), dtype=np.uint8))
