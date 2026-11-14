"""主队头像角标：按槽位单帧读取游戏状态（支持少队）。

规则来源：用户在 2026-11-02 校准的角标含义，见 `team_states.py` 与
`artifacts/team-marker-calibration/`。本模块只做三件事：

1. 用已标注样本生成的字形模板做遮罩归一化相关，认出是哪个状态（含平移搜索，
   所以同一状态出现在不同槽位、不同头像上也能认）；
2. 认不出时，判断"这一格到底有没有角标"：角标是黑色菱形底板上的紧凑亮色字形，
   有底板但不认识 => unknown；没有底板 => 空闲（用户确认：空闲无角标），
   但空闲还以整帧通过 `hud_frame_confirmed` 为前提：全黑（切页动画/黑屏）、
   随机噪声（采集花屏）等无法确认有效 HUD 的帧一律 unknown，绝不报空闲；
3. 绝不把没学过的角标降级成空闲。

`read()`、`read_slots()` 与 `read_slot()` 是同一契约：`read()` 委托
`read_slots(image, range(1, 6))`。批量读取共享同一帧级上下文（HUD 有效性确认、
可共享的图像转换、加载期预处理好的模板数据），单槽读取每次新建上下文，
结果与批量路径逐槽一致。

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

# 右侧队伍头像不是固定 x。2026-11-13 用户截图 + 本机真帧实测：
# 选中建筑物（如寒沙寨）后，**驻扎在该建筑里的队伍**整行左移 25px（头像/血条/角标一起），
# 同一帧里不在该建筑的队伍仍在原位；旧代码只按固定 x=1840 ±10 找角标，
# 建筑内队伍因此被读成“未识别”。这里改成“每个槽位在已知布局里各试一次，
# 取匹配最强的那套”，不要求整列一起移动。新增布局必须先有真帧实测再写进来。
# 候选之间用 SEARCH_DX 的残差连成连续区间：0±6、-12±6、-25±6 → 覆盖 -31..+6。
HUD_OFFSETS = (0, -12, -25)
SEARCH_DX = 6
SEARCH_DY = 10

# 阈值来自 2026-11-02 多个不同地图、不同头像（含换将）的实测：
# 已标注角标相关度 0.89~1.00；空闲槽位 ≤0.60；未学角标（白色左箭头）0.44。
MATCH_MIN = .85
BEST_IDLE_MAX = .80

# 帧级“有效 HUD”门槛（2026-11-14 实测，报空闲前必须整帧通过）：
# 右侧 HUD 整列（x1695..1920, y180..900）真实帧灰度标准差 33~55、相邻像素平均差
# 与标准差之比 0.14~0.19；均匀/高斯随机噪声 ≈1.15；全黑/纯灰标准差为 0 且无血条。
# 少队/合成帧（仅血条+模板）std≥7、比值≤0.03，同样通过，不影响既有空闲判定。
HUD_FRAME_MIN_STD = 5.0
HUD_FRAME_MAX_NOISE_RATIO = .5

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


class _FrameContext:
    """一帧的共享只读上下文：HUD 有效性确认每帧只算一次。

    `read_slots()` 整帧共享一个上下文，避免每个空闲槽重复做整帧 HSV/灰度确认；
    `read_slot()` 单独调用时每次新建上下文，与批量路径逐槽结果一致。
    黑帧/噪声帧的判定标准不变，只是从"每槽一次"变成"每帧一次"。
    """

    def __init__(self, reader: "TeamMarkerReader", image: np.ndarray):
        self.reader = reader
        self.image = image
        self._avatars: bool | None = None
        self._hud_confirmed: bool | None = None

    def avatars_visible(self) -> bool:
        if self._avatars is None:
            self._avatars = self.reader.avatars_visible(self.image)
        return self._avatars

    def hud_frame_confirmed(self) -> bool:
        if self._hud_confirmed is None:
            # 与 reader.hud_frame_confirmed 完全相同的判定，只是血条检查走帧级缓存。
            self._hud_confirmed = (self.avatars_visible()
                                   and self.reader._hud_structure_confirmed(self.image))
        return self._hud_confirmed


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
        mask = _match_mask()
        loaded: dict[str, list[dict]] = {}
        for entry in entries:
            path = self.template_dir / entry["file"]
            image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None or image.shape[:2] != (BADGE_SIZE, BADGE_SIZE):
                raise TemplateError(f"角标模板损坏或尺寸不符：{path}")
            # 加载期一次性算好掩罩内减均值的向量与范数（有界小内存），
            # _scores 里只做点积，不再逐槽逐偏移重复预处理。
            base = image[mask].astype(np.float32)
            base -= base.mean(axis=0, keepdims=True)
            loaded.setdefault(entry["state"], []).append({
                "image": image,
                "vector": base.reshape(-1),
                "norm": float(np.sqrt((base * base).sum())),
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
        按所有已知布局的并集找血条，建筑内队伍左移时同样成立。
        """
        if image is None or image.shape[:2] != (1080, 1920):
            return False
        left = 1720 + min(HUD_OFFSETS)
        right = 1920 + max(HUD_OFFSETS)
        # 只转换血条所在的纵向条带：cvtColor 是逐像素运算，结果与整帧转换后切片完全一致。
        top = SLOT_Y[0] + 51
        strip = image[top:SLOT_Y[-1] + 57, left:right]
        hsv = cv2.cvtColor(strip, cv2.COLOR_BGR2HSV)
        s, v = hsv[:, :, 1], hsv[:, :, 2]
        for y in SLOT_Y:
            row = y + 51 - top
            bar = (s[row:row + 6, :] > 90) & (v[row:row + 6, :] > 120)
            if bar.mean() >= .15:
                return True
        return False

    def hud_frame_confirmed(self, image: np.ndarray) -> bool:
        """这一帧能不能当作“右侧队伍 HUD 真实存在”的画面。

        空闲＝无角标是相对真实画面的判断。全黑（切页动画/黑屏采集）和随机噪声
        （采集花屏）既不是“有底板不认识”，也不是“没有底板”，三个条件同时成立
        才确认有效 HUD：至少一条队伍血条、HUD 整列有内容（排除纯黑/纯灰平帧）、
        相邻像素强相关（排除纯噪声，真实画面 ≈0.2，均匀噪声 ≈1.15）。
        确认不了的帧宁可报 unknown，也不能读成“空闲可派”。
        """
        if not self.avatars_visible(image):
            return False
        return self._hud_structure_confirmed(image)

    @staticmethod
    def _hud_structure_confirmed(image: np.ndarray) -> bool:
        """血条已确认后的整列内容/噪声结构门槛（不含 avatars_visible）。"""
        left = 1720 + min(HUD_OFFSETS)
        right = 1920 + max(HUD_OFFSETS)
        roi = image[SLOT_Y[0] - 65:SLOT_Y[-1] + 115, left:right]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY).astype(np.float32)
        std = float(gray.std())
        if std < HUD_FRAME_MIN_STD:
            return False
        return float(np.abs(np.diff(gray, axis=1)).mean()) / std <= HUD_FRAME_MAX_NOISE_RATIO

    @staticmethod
    def _window(image, slot, dx=0):
        y = SLOT_Y[slot - 1] + BADGE_DY
        x = BADGE_X + dx
        return image[y:y + BADGE_SIZE, x:x + BADGE_SIZE]

    @staticmethod
    def _search_roi(image, slot, dx=0):
        y = SLOT_Y[slot - 1]
        x0 = 1820 + dx
        return image[y - 40:y + 70, x0:x0 + 100], 20, 32

    # ---------- 判定 ----------

    @staticmethod
    def _glyph_like(value, labels, stats, index, centre=None):
        """一个亮块是否像角标字形：紧凑、够大、紧邻一圈偏暗。

        centre 给了就还要求它落在窗口中心附近（旧“固定位置”判据）；
        不给则只按形状判断，用于“这一行任意 x 有没有角标”。
        """
        x, y, w, h, area = stats[index]
        if area < GLYPH_AREA or w > GLYPH_BOX or h > GLYPH_BOX:
            return False
        if max(w, h) < GLYPH_MIN_SIDE:
            # 地图上的白字（如“伤”）会碎成很小的亮块，不能当角标字形。
            return False
        if centre is not None:
            cx, cy = centre
            if abs(cx - BADGE_SIZE / 2) > GLYPH_CENTRE or abs(cy - BADGE_SIZE / 2) > GLYPH_CENTRE:
                return False
        ring = (cv2.dilate((labels == index).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0) & (labels != index)
        if ring.sum() < 20:
            return False
        return float(np.median(value[ring])) <= GLYPH_RING_DARK

    def glyph_candidates(self, image, slot, dx=0) -> list[dict]:
        """窗内"像角标字形"的亮块：紧凑、居中、紧邻一圈偏暗。"""
        window = self._window(image, slot, dx)
        value = cv2.cvtColor(window, cv2.COLOR_BGR2HSV)[:, :, 2]
        bright = (value >= GLYPH_BRIGHT).astype(np.uint8)
        count, labels, stats, centres = cv2.connectedComponentsWithStats(bright, 8)
        found = []
        for index in range(1, count):
            if not self._glyph_like(value, labels, stats, index, centre=centres[index]):
                continue
            x, y, w, h, area = stats[index]
            ring = (cv2.dilate((labels == index).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0) & (labels != index)
            found.append({"area": int(area), "box": [int(w), int(h)],
                          "ring": round(float(np.median(value[ring])), 1)})
        return found

    def badge_like_anywhere(self, image, slot) -> bool:
        """本行头像区域任意 x 上有没有“像角标”的形状。

        用途只有一个：第一套布局没匹配上时，判断还要不要再试左移布局。
        没有这种形状就当空闲；有但认不出，后面会保守报“未识别”，不降级成空闲。
        """
        y = SLOT_Y[slot - 1]
        row = image[y - 22:y + 32, 1740 + min(HUD_OFFSETS):1920 + max(HUD_OFFSETS)]
        if row.size == 0:
            return False
        value = cv2.cvtColor(row, cv2.COLOR_BGR2HSV)[:, :, 2]
        bright = (value >= GLYPH_BRIGHT).astype(np.uint8)
        count, labels, stats, centres = cv2.connectedComponentsWithStats(bright, 8)
        return any(self._glyph_like(value, labels, stats, index)
                   for index in range(1, count))

    def plaque_score(self, image, slot, dx=0):
        """诊断用：菱形区域内近黑像素占比。不能单独当"有没有角标"的判据。"""
        window = self._window(image, slot, dx)
        value = cv2.cvtColor(window, cv2.COLOR_BGR2HSV)[:, :, 2]
        return float((value[_match_mask()] < 40).mean())

    def _scores(self, image, slot, dx=0):
        mask = _match_mask()
        roi, ox0, oy0 = self._search_roi(image, slot, dx)
        windows = np.lib.stride_tricks.sliding_window_view(roi, (BADGE_SIZE, BADGE_SIZE), axis=(0, 1))
        windows = windows.transpose(0, 1, 3, 4, 2)  # (y, x, 58, 58, 3)
        # 只取允许的偏移范围，避免在空地/头像上"找"出假角标。
        ys = sorted({oy0 + d for d in range(-SEARCH_DY, SEARCH_DY + 1) if 0 <= oy0 + d < windows.shape[0]})
        xs = sorted({ox0 + d for d in range(-SEARCH_DX, SEARCH_DX + 1) if 0 <= ox0 + d < windows.shape[1]})
        patches = windows[np.ix_(ys, xs)][..., mask, :].astype(np.float32)
        patches = patches - patches.mean(axis=-2, keepdims=True)
        flat = patches.reshape(len(ys), len(xs), -1)
        norm = np.sqrt((flat * flat).sum(axis=-1))
        results = {}
        for state, group in self.templates.items():
            best = (-2.0, 0, 0)
            for template in group:
                denom = template["norm"]
                if denom == 0:
                    continue
                scores = (flat @ template["vector"]) / np.maximum(norm * denom, 1e-6)
                index = int(np.argmax(scores))
                iy, ix = np.unravel_index(index, scores.shape)
                score = float(scores[iy, ix])
                if score > best[0]:
                    best = (score, xs[ix] - ox0, ys[iy] - oy0)
            results[state] = best
        return results

    def read_slot(self, image: np.ndarray, slot: int, offset: int | None = None) -> dict:
        if not 1 <= slot <= 5:
            raise ValueError("主队槽位必须为1～5")
        return self._read_slot(_FrameContext(self, image), slot, offset)

    def read_slots(self, image: np.ndarray, slots) -> list[dict]:
        """一帧批量读取指定槽位；整帧共享 HUD 确认，结果与逐槽 read_slot 一致。"""
        if image.shape[:2] != (1080, 1920):
            raise ValueError(f"队伍角标读取需要1920×1080画面，实际为{image.shape[1]}×{image.shape[0]}")
        ctx = _FrameContext(self, image)
        return [self._read_slot(ctx, slot) for slot in slots]

    def _read_slot(self, ctx: _FrameContext, slot: int, offset: int | None = None) -> dict:
        if not 1 <= slot <= 5:
            raise ValueError("主队槽位必须为1～5")
        # 已知布局逐个试，取匹配最强的一套：建筑内队伍左移、其他队不动，不能假定整列一致。
        # 为控制耗时：第一套布局匹配到可信状态就结束；第一套连“角标形状”都没有就当空闲，
        # 只有“有角标但没认出”时才继续试左移布局（通常只有建筑内那 1～2 支队）。
        candidates = (int(offset),) if offset is not None else HUD_OFFSETS
        image = ctx.image
        head = None
        all_glyphs = []
        for index, dx in enumerate(candidates):
            scores = self._scores(image, slot, dx)
            ranked = sorted(scores.items(), key=lambda kv: kv[1][0], reverse=True)
            best_state, (best_score, local_dx, local_dy) = ranked[0]
            second_state, (second_score, *_rest) = ranked[1] if len(ranked) > 1 else ("unknown", (-2.0, 0, 0))
            glyphs = self.glyph_candidates(image, slot, dx)
            all_glyphs.extend(glyphs)
            entry = {
                "rank": (best_score, bool(glyphs)),
                "dx": dx,
                "state": best_state,
                "score": best_score,
                "local": (local_dx, local_dy),
                "second": (second_state, second_score),
                "glyphs": glyphs,
                "plaque": self.plaque_score(image, slot, dx),
            }
            if head is None or entry["rank"] > head["rank"]:
                head = entry
            if head["score"] >= MATCH_MIN:
                break
            if index == 0 and not self.badge_like_anywhere(image, slot):
                # 第一套布局没有任何角标形状：空闲，不必再为其他布局重复整窗匹配。
                break
        best_score = head["score"]
        if best_score >= MATCH_MIN:
            state, reason = head["state"], "模板匹配"
        elif all_glyphs:
            state, reason = "unknown", "角标未识别"
        elif best_score < BEST_IDLE_MAX:
            # “无角标＝空闲”只在整帧确认是有效 HUD 时成立：全黑（切页/黑屏）、随机
            # 噪声（采集花屏）等画面无法确认 HUD，一律 unknown，绝不读成空闲。
            if ctx.hud_frame_confirmed():
                state, reason = "idle", "无角标"
            else:
                state, reason = "unknown", "画面未确认"
        else:
            state, reason = "unknown", "角标未识别"
        idle = state == "idle"
        glyph = head["glyphs"][0] if head["glyphs"] else (all_glyphs[0] if all_glyphs else None)
        local_dx, local_dy = head["local"]
        second_state, second_score = head["second"]
        return {
            "slot": slot,
            "state": state,
            "label": label(state),
            "reason": reason,
            "confidence": round(1 - min(best_score, .99), 3) if idle else round(best_score, 3),
            "plaque": round(head["plaque"], 3),
            "glyph": glyph,
            "matched": None if idle else head["state"],
            "matched_label": None if idle else label(head["state"]),
            "matched_score": None if idle else round(best_score, 3),
            "second": None if idle else second_state,
            "second_score": None if idle else round(second_score, 3),
            "hud_dx": head["dx"],
            "shift": None if idle else [local_dx, local_dy],
        }

    def read(self, image: np.ndarray) -> list[dict]:
        return self.read_slots(image, range(1, 6))
