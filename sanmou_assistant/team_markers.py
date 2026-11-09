"""主队头像角标：按槽位单帧读取游戏状态（支持少队）。

规则来源：用户在 2026-11-02 校准的角标含义，见 `team_states.py` 与
`artifacts/team-marker-calibration/`。本模块只做三件事：

1. 用已标注样本生成的字形模板做遮罩归一化相关，认出是哪个状态（含平移搜索，
   所以同一状态出现在不同槽位、不同头像上也能认）；
2. 认不出时，判断"这一格到底有没有角标"：角标是黑色菱形底板上的紧凑亮色字形，
   有底板但不认识 => unknown；没有底板 => 空闲（用户确认：空闲无角标）；
3. 绝不把没学过的角标降级成空闲。

不加载 OCR，不连接设备，不读写配置，不做任何点击或导航。
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from .team_states import STATES, label

SLOT_Y = (245, 383, 520, 660, 785)
BADGE_SIZE = 58
BADGE_X = 1840
BADGE_DY = -8
SEARCH_DX = 10
SEARCH_DY = 18

# 阈值来自 2026-11-02 多个不同地图、不同头像（含换将）的实测：
# 已标注角标相关度 0.89~1.00；空闲槽位 ≤0.60；未学角标（白色左箭头）0.44。
MATCH_MIN = .85
BEST_IDLE_MAX = .80

# 角标字形：黑色菱形底板上的紧凑亮块。亮块要居中、够紧凑，并且紧邻一圈是暗的，
# 否则就是头像本身（皮肤/发丝/衣饰），不能当角标。
GLYPH_BRIGHT = 165
GLYPH_AREA = 90
# 战斗中双剑在“分开”相位时竖排约 28×42，外框上限必须容纳它，否则会漏成“无角标”。
GLYPH_BOX = 48
GLYPH_MIN_SIDE = 20
GLYPH_CENTRE = 16
GLYPH_RING_DARK = 110

_MATCH_MASK = None


def _diamond(radius):
    size = BADGE_SIZE
    centre = size // 2
    mask = np.zeros((size, size), np.uint8)
    cv2.fillPoly(mask, [np.array([
        [centre + radius, centre], [centre, centre - radius],
        [centre - radius, centre], [centre, centre + radius],
    ], np.int32)], 255)
    return mask.astype(bool)


def _match_mask():
    global _MATCH_MASK
    if _MATCH_MASK is None:
        _MATCH_MASK = _diamond(22)
    return _MATCH_MASK


class TemplateError(RuntimeError):
    pass


class TeamMarkerReader:
    """从 1920×1080 单帧按槽位读出队伍状态。"""

    def __init__(self, template_dir: Path | None = None):
        self.template_dir = template_dir or Path(__file__).with_name("assets") / "team-markers"
        self.templates = self._load_templates()

    def _load_templates(self):
        manifest = self.template_dir / "manifest.json"
        if not manifest.exists():
            raise TemplateError(f"角标模板清单不存在：{manifest}")
        entries = json.loads(manifest.read_text(encoding="utf-8"))["templates"]
        loaded: dict[str, list[dict]] = {}
        for entry in entries:
            path = self.template_dir / entry["file"]
            image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None or image.shape[:2] != (BADGE_SIZE, BADGE_SIZE):
                raise TemplateError(f"角标模板损坏或尺寸不符：{path}")
            loaded.setdefault(entry["state"], []).append({
                "image": image,
                "file": entry["file"],
                "source": entry.get("source", ""),
                "slot": entry.get("slot"),
            })
        unknown = set(loaded) - set(STATES)
        if unknown:
            raise TemplateError(f"模板里出现未定义状态：{sorted(unknown)}")
        if not loaded:
            raise TemplateError("角标模板为空")
        return loaded

    # ---------- 画面与几何 ----------

    @staticmethod
    def avatars_visible(image: np.ndarray) -> bool:
        """右侧 HUD 是否已经出现（只作画面可用性判断，不用于数队伍）。

        2026-11-08 用户明确：队伍数量以游戏部队列表为准，不看头像。
        这里只要求右侧至少有一支队伍的血条，少队/空槽位不影响。
        """
        if image is None or image.shape[:2] != (1080, 1920):
            return False
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        s, v = hsv[:, :, 1], hsv[:, :, 2]
        for y in SLOT_Y:
            bar = (s[y + 51:y + 57, 1780:1900] > 90) & (v[y + 51:y + 57, 1780:1900] > 120)
            if bar.mean() >= .2:
                return True
        return False

    @staticmethod
    def _window(image, slot):
        y = SLOT_Y[slot - 1] + BADGE_DY
        return image[y:y + BADGE_SIZE, BADGE_X:BADGE_X + BADGE_SIZE]

    @staticmethod
    def _search_roi(image, slot):
        y = SLOT_Y[slot - 1]
        return image[y - 40:y + 70, 1820:1920], 20, 32

    # ---------- 判定 ----------

    def glyph_candidates(self, image, slot) -> list[dict]:
        """窗内"像角标字形"的亮块：紧凑、居中、紧邻一圈偏暗。"""
        window = self._window(image, slot)
        value = cv2.cvtColor(window, cv2.COLOR_BGR2HSV)[:, :, 2]
        bright = (value >= GLYPH_BRIGHT).astype(np.uint8)
        count, labels, stats, centres = cv2.connectedComponentsWithStats(bright, 8)
        found = []
        for index in range(1, count):
            x, y, w, h, area = stats[index]
            if area < GLYPH_AREA or w > GLYPH_BOX or h > GLYPH_BOX:
                continue
            if max(w, h) < GLYPH_MIN_SIDE:
                # 地图上的白字（如“伤”）会碎成很小的亮块，不能当角标字形。
                continue
            cx, cy = centres[index]
            if abs(cx - BADGE_SIZE / 2) > GLYPH_CENTRE or abs(cy - BADGE_SIZE / 2) > GLYPH_CENTRE:
                continue
            ring = (cv2.dilate((labels == index).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0) & (labels != index)
            if ring.sum() < 20:
                continue
            ring_dark = float(np.median(value[ring]))
            if ring_dark > GLYPH_RING_DARK:
                continue
            found.append({"area": int(area), "box": [int(w), int(h)], "ring": round(ring_dark, 1)})
        return found

    def plaque_score(self, image, slot):
        """诊断用：菱形区域内近黑像素占比。不能单独当"有没有角标"的判据。"""
        window = self._window(image, slot)
        value = cv2.cvtColor(window, cv2.COLOR_BGR2HSV)[:, :, 2]
        return float((value[_match_mask()] < 40).mean())

    def _scores(self, image, slot):
        mask = _match_mask()
        roi, ox0, oy0 = self._search_roi(image, slot)
        windows = np.lib.stride_tricks.sliding_window_view(roi, (BADGE_SIZE, BADGE_SIZE), axis=(0, 1))
        windows = windows.transpose(0, 1, 3, 4, 2)  # (y, x, 58, 58, 3)
        # 只取允许的偏移范围，避免在空地/头像上"找"出假角标。
        ys = sorted({oy0 + d for d in range(-SEARCH_DY, SEARCH_DY + 1) if 0 <= oy0 + d < windows.shape[0]})
        xs = sorted({ox0 + d for d in range(-SEARCH_DX, SEARCH_DX + 1) if 0 <= ox0 + d < windows.shape[1]})
        patches = windows[np.ix_(ys, xs)][..., mask, :].astype(np.float32)
        patches = patches - patches.mean(axis=-2, keepdims=True)
        norm = np.sqrt((patches * patches).sum(axis=(-2, -1)))
        results = {}
        for state, group in self.templates.items():
            best = (-2.0, 0, 0)
            for template in group:
                base = template["image"][mask].astype(np.float32)
                base = base - base.mean(axis=0, keepdims=True)
                denom = np.sqrt((base * base).sum())
                if denom == 0:
                    continue
                scores = (patches * base).sum(axis=(-2, -1)) / np.maximum(norm * denom, 1e-6)
                index = int(np.argmax(scores))
                iy, ix = np.unravel_index(index, scores.shape)
                score = float(scores[iy, ix])
                if score > best[0]:
                    best = (score, xs[ix] - ox0, ys[iy] - oy0)
            results[state] = best
        return results

    def read_slot(self, image: np.ndarray, slot: int) -> dict:
        if not 1 <= slot <= 5:
            raise ValueError("主队槽位必须为1～5")
        scores = self._scores(image, slot)
        ranked = sorted(scores.items(), key=lambda kv: kv[1][0], reverse=True)
        best_state, (best_score, dx, dy) = ranked[0]
        second_state, (second_score, *_rest) = ranked[1] if len(ranked) > 1 else ("unknown", (-2.0, 0, 0))
        plaque = self.plaque_score(image, slot)
        glyphs = self.glyph_candidates(image, slot)
        if best_score >= MATCH_MIN:
            state, reason = best_state, "模板匹配"
        elif glyphs:
            state, reason = "unknown", "角标未识别"
        elif best_score < BEST_IDLE_MAX:
            state, reason = "idle", "无角标"
        else:
            state, reason = "unknown", "角标未识别"
        idle = state == "idle"
        return {
            "slot": slot,
            "state": state,
            "label": label(state),
            "reason": reason,
            "confidence": round(1 - min(best_score, .99), 3) if idle else round(best_score, 3),
            "plaque": round(plaque, 3),
            "glyph": glyphs[0] if glyphs else None,
            "matched": None if idle else best_state,
            "matched_label": None if idle else label(best_state),
            "matched_score": None if idle else round(best_score, 3),
            "second": None if idle else second_state,
            "second_score": None if idle else round(second_score, 3),
            "shift": None if idle else [dx, dy],
        }

    def read(self, image: np.ndarray) -> list[dict]:
        if image.shape[:2] != (1080, 1920):
            raise ValueError(f"队伍角标读取需要1920×1080画面，实际为{image.shape[1]}×{image.shape[0]}")
        return [self.read_slot(image, slot) for slot in range(1, 6)]
