from pathlib import Path

import cv2
import pytest

from sanmou_assistant.vision import Vision


@pytest.fixture(scope="module")
def vision():
    return Vision()


@pytest.mark.parametrize("name", ["activity-failed.png", "activity-earlier.png", "after-cancel-point.png"])
def test_actual_activity_has_verified_return_and_title(vision, name):
    root = Path(__file__).resolve().parents[1] / "artifacts/activity-recovery-2026-10-28"
    image = cv2.imread(str(root / name))
    assert vision.page(image) == "navigable_page"
    back = vision.navigation_back_point(image)
    assert back["title"] == "二士相争"
    assert 100 < back["x"] < 180 and 15 < back["y"] < 65
    for rect in [(8, 75, 74, 190), (0, 190, 85, 750)]:
        hidden = image.copy()
        y0, x0, y1, x1 = rect
        hidden[y0:y1, x0:x1] = 0
        assert vision.navigation_back_point(hidden) is None


def test_sandbox_is_not_a_generic_return_page(vision):
    root = Path(__file__).resolve().parents[1] / "artifacts/activity-recovery-2026-10-28"
    image = cv2.imread(str(root / "after-activity-back.png"))
    assert vision.page(image) == "sandbox"
    assert vision.navigation_back_point(image) is None
