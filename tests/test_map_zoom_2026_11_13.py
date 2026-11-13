"""地图缩放按钮与沙盘页型判据回归（2026-11-13 实机问题）。

背景（用户 2026-11-13 21:22 复盘）：
- 顶部资源栏背后是半透明地图，地图标签会压上来，不能拿“繁荣/道路”当判据；
  沙盘上必定存在的是底栏“武将/同盟/职业/征战”和右下角“寻访”徽记。
- 地图缩放改用左下角固定按钮长按，不再用双指/连点。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from sanmou_assistant.vision import Vision, sandbox_hud

ASSETS = Path(__file__).resolve().parents[1] / "sanmou_assistant" / "assets"


# ---------- 沙盘页型判据 ----------

def test_sandbox_hud_uses_bottom_bar_and_xunfang_only():
    bottom = "武将 | 同盟 | 职业 | 证战"
    assert sandbox_hud(bottom, True)


def test_sandbox_hud_ignores_the_top_resource_bar():
    """顶部读到什么都无所谓；看不到寻访徽记就不是沙盘。"""
    bottom = "武将 | 同盟 | 职业"
    assert not sandbox_hud(bottom, False)


def test_sandbox_hud_rejects_non_map_pages():
    assert not sandbox_hud("城池 | 战没 | 同盟 | 个人", True)          # 地图沙盘左侧筛选
    assert not sandbox_hud("武将 | 同盟", True)                        # 缺“职业/征战”
    assert not sandbox_hud("", True)


# ---------- 寻访徽记 ----------

def test_sandbox_badge_matches_the_real_template():
    vision = Vision()
    frame = np.full((1080, 1920, 3), 27, dtype=np.uint8)
    tpl = cv2.imread(str(ASSETS / "xunfang-mask.png"), cv2.IMREAD_GRAYSCALE)
    assert tpl is not None
    frame[930:930+tpl.shape[0], 1735:1735+tpl.shape[1]] = cv2.cvtColor(tpl, cv2.COLOR_GRAY2BGR)
    assert vision.sandbox_badge(frame)
    blank = np.full((1080, 1920, 3), 27, dtype=np.uint8)
    assert not vision.sandbox_badge(blank)


# ---------- 地图缩放按钮 ----------

def _frame_with_zoom_buttons():
    rng = np.random.default_rng(7)
    image = rng.integers(20, 60, size=(1080, 1920, 3), dtype=np.uint8)
    for name, paste in (("map-zoom-out-mask.png", (20, 679)),
                        ("map-zoom-in-mask.png", (20, 812))):
        template = cv2.imread(str(ASSETS / name), cv2.IMREAD_GRAYSCALE)
        assert template is not None
        x, y = paste
        image[y:y+template.shape[0], x:x+template.shape[1]] = cv2.cvtColor(template, cv2.COLOR_GRAY2BGR)
    return image


def test_map_zoom_buttons_are_located_at_fixed_positions():
    vision = Vision()
    image = _frame_with_zoom_buttons()
    assert vision.map_zoom_point(image, zoom_in=False) == pytest.approx((56, 715), abs=2)
    assert vision.map_zoom_point(image, zoom_in=True) == pytest.approx((56, 848), abs=2)


def test_map_zoom_is_not_clicked_when_buttons_are_absent():
    """整屏弹层/遮挡时认不出按钮就返回 None，绝不拿固定坐标盲点。"""
    vision = Vision()
    blank = np.full((1080, 1920, 3), 30, dtype=np.uint8)
    assert vision.map_zoom_point(blank, zoom_in=False) is None
    assert vision.map_zoom_point(blank, zoom_in=True) is None
