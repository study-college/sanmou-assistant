"""主队头像角标：按槽位单帧读取游戏状态（支持少队）。

规则来源：用户在 2026-11-02 校准的角标含义，见 `team_states.py` 与
`artifacts/team-marker-calibration/`。本模块只做三件事：

1. 用已标注样本生成的字形模板做遮罩归一化相关，认出是哪个状态（含平移搜索，
   所以同一状态出现在不同槽位、不同头像上也能认）；
2. 认不出时，在已定位的队伍行上区分真角标与头像背景：模板近失，或“窗口里有底板
   且底板上有字形” => unknown；确认没有角标 => 空闲（用户确认：空闲无角标）。
   底板是角标自己的画面：黑色菱形底板（近黑占比）或金色时钟那种整块亮色底板；
   头像内部的亮块没有底板，只是人物画面，不能据此把队伍卡成 unknown；
   空闲还以整帧通过 `hud_frame_confirmed` 为前提：全黑（切页动画/黑屏）、
   随机噪声（采集花屏）等无法确认有效 HUD 的帧一律 unknown，绝不报空闲；
3. 绝不把没学过的角标降级成空闲：认不出的角标一定带底板，底板缺失才按空闲处理。

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

# 选中建筑物后，驻扎队伍整行可能左移（头像/血条/角标一起），且各行独立。
# 这些实测锚点只限定血条位置测量范围，不再逐套猜角标布局；
# hud_offset 在 -31..+6 内连续测量，模板搜索只补偿小残差。
HUD_OFFSETS = (0, -12, -25)
SEARCH_DX = 6
SEARCH_DY = 10

# 阈值来自 2026-11-02 多个不同地图、不同头像（含换将）的实测：
# 已标注角标相关度 0.89~1.00；空闲槽位 ≤0.60；未学角标（白色左箭头）0.44。
MATCH_MIN = .85
BEST_IDLE_MAX = .80

# 2026-11-18 实机：一队（周泰/邓艾/太史慈）在主城空闲，头像左下的披膊+浅色背景
# 是“紧凑亮块+暗环”，被当成字形后整局读成“待识别”，既不派兵也不复核。
# 亮块本身分不出角标与人物，能分的是角标底板：
# - 黑色菱形底板（`plaque_score`）：除金色时钟外 13 个已学状态实测 0.12~0.59，
#   个别没有亮字形的帧低到 0.084（那些帧本来就靠模板命中，不走这条分支）；
# - 金色时钟的亮色底板（`plate_bright_score`）：实测 0.41~0.51，没有黑底板；
# - 同一语料里空闲头像区：近黑占比 0.00~0.098、亮色底板 0.00~0.244。
# 因此“有底板 + 有字形”才是未学角标，没有底板就按无角标读空闲。
# 分布与逐帧对照见 artifacts/diag-2026-11-18-team1-marker/。
PLAQUE_MIN = .12
PLATE_BRIGHT_MIN = .30

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

# 2026-11-15 实机：掩罩半径 22 会越过角标黑色菱形底盘、伸进头像（亮肤色/发丝），
# 减均值后的相关系数被背景拉低——同样的"停留"图标，深色头像下 0.93、亮色头像下只有
# 0.828，低于 MATCH_MIN 就被判成"角标未识别"，铺路整段卡住。
# 收到 17 只覆盖底盘内部：停留 0.828→0.946、停留真机 0.931→0.992，其他状态串判分数
# 仍在 0.5 以下（见 artifacts/analysis-2026-11-15-pave-stall/）。
MATCH_RADIUS = 17
PLAQUE_RADIUS = 25

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
        _MATCH_MASK = _diamond(MATCH_RADIUS)
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

    @staticmethod
    def hud_offset(image: np.ndarray, slot: int) -> int | None:
        """由本队血条独立测量水平位置；测量不到就不能正向确认空闲。"""
        if not 1 <= slot <= 5 or image is None or image.shape[:2] != (1080, 1920):
            return None
        y = SLOT_Y[slot - 1]
        minimum, maximum = min(HUD_OFFSETS) - SEARCH_DX, max(HUD_OFFSETS) + SEARCH_DX
        left, right = 1800 + minimum, 1884 + maximum
        hsv = cv2.cvtColor(image[y + 45:y + 70, left:right], cv2.COLOR_BGR2HSV)
        colored = (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 120)
        scores = []
        for dy in range(colored.shape[0] - 5):
            rows = colored[dy:dy + 6]
            for dx in range(minimum, maximum + 1):
                start = dx - minimum
                scores.append((float(rows[:, start:start + 84].mean()), dx))
        score, dx = max(scores)
        return dx if score >= .15 else None

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

    def plaque_score(self, image, slot, dx=0):
        """完整菱形区域的近黑占比，用于区分大块亮脸与字形。"""
        window = self._window(image, slot, dx)
        value = cv2.cvtColor(window, cv2.COLOR_BGR2HSV)[:, :, 2]
        return float((value[_diamond(PLAQUE_RADIUS)] < 40).mean())

    def plate_bright_score(self, image, slot, dx=0):
        """菱形区域里高亮高饱和像素的占比：金色时钟那种亮色底板的证据。

        金色时钟没有黑色底板（`plaque_score` 只有 0.00~0.15），整块亮黄压满窗口；
        空闲头像区的暖色高光远达不到这个面积，所以两者用同一个阈值就能分开。
        """
        window = self._window(image, slot, dx)
        hsv = cv2.cvtColor(window, cv2.COLOR_BGR2HSV)
        bright = (hsv[:, :, 2] >= 200) & (hsv[:, :, 1] >= 120)
        return float(bright[_diamond(PLAQUE_RADIUS)].mean())

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
        image = ctx.image
        dx = int(offset) if offset is not None else self.hud_offset(image, slot)
        if dx is None:
            reason = "队伍行未确认" if ctx.hud_frame_confirmed() else "画面未确认"
            return {
                "slot": slot, "state": "unknown", "label": label("unknown"),
                "reason": reason, "confidence": 0.0, "plaque": 0.0, "plate_bright": 0.0,
                "glyph": None, "matched": None, "matched_label": None,
                "matched_score": None, "second": None, "second_score": None,
                "hud_dx": None, "shift": None,
            }
        scores = self._scores(image, slot, dx)
        ranked = sorted(scores.items(), key=lambda kv: kv[1][0], reverse=True)
        # 建筑边缘可能把血条测偏。本行窗口漏掉原位时，只额外接受原位的强匹配。
        if offset is None and abs(dx) > SEARCH_DX and ranked[0][1][0] < MATCH_MIN:
            nominal = sorted(self._scores(image, slot).items(), key=lambda kv: kv[1][0], reverse=True)
            if nominal[0][1][0] >= MATCH_MIN:
                dx, ranked = 0, nominal
        best_state, (best_score, local_dx, local_dy) = ranked[0]
        second_state, (second_score, *_rest) = ranked[1] if len(ranked) > 1 else ("unknown", (-2.0, 0, 0))
        glyphs = self.glyph_candidates(image, slot, dx)
        plaque = self.plaque_score(image, slot, dx)
        plate_bright = self.plate_bright_score(image, slot, dx)
        # 底板是角标自己的画面；头像里的亮块没有底板。见文件顶部 2026-11-18 的说明。
        has_plate = plaque >= PLAQUE_MIN or plate_bright >= PLATE_BRIGHT_MIN
        if best_score >= MATCH_MIN:
            state, reason = best_state, "模板匹配"
        elif not ctx.hud_frame_confirmed():
            state, reason = "unknown", "画面未确认"
        # 有底板且底板上有字形 => 没学过的角标，保持 unknown；
        # 没有底板 => 窗口里的亮块只是头像画面，按无角标读空闲，不能把队伍卡死。
        elif best_score >= BEST_IDLE_MAX or (has_plate and glyphs):
            state, reason = "unknown", "角标未识别"
        else:
            state, reason = "idle", "无角标"
        idle = state == "idle"
        return {
            "slot": slot,
            "state": state,
            "label": label(state),
            "reason": reason,
            "confidence": round(1 - min(best_score, .99), 3) if idle else round(best_score, 3),
            "plaque": round(plaque, 3),
            "plate_bright": round(plate_bright, 3),
            "glyph": glyphs[0] if glyphs else None,
            "matched": None if idle else best_state,
            "matched_label": None if idle else label(best_state),
            "matched_score": None if idle else round(best_score, 3),
            "second": None if idle else second_state,
            "second_score": None if idle else round(second_score, 3),
            "hud_dx": dx,
            "shift": None if idle else [local_dx, local_dy],
        }

    def read(self, image: np.ndarray) -> list[dict]:
        return self.read_slots(image, range(1, 6))
