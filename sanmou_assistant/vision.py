from __future__ import annotations

import re
import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .models import Observation


@dataclass
class Text:
    text: str
    score: float
    x: float
    y: float
    box: tuple[float, float, float, float]


@dataclass(frozen=True)
class CityResourceRow:
    name: str
    resource: str
    level: int
    location: Text


@dataclass(frozen=True)
class CityFarmSelection:
    name: str
    level: int
    button: Text
    remaining: int


CITY_RESOURCE_NAMES = {"农田": "grain", "林场": "wood", "铁矿": "iron", "石矿": "stone"}


def pairs(text: str) -> list[tuple[int, int]]:
    text = text.replace("／", "/").replace(" ", "").replace(",", "")
    return [(int(a), int(b)) for a, b in re.findall(r"(\d{1,7})/(\d{1,7})", text)]


def quantity(text: str) -> int | None:
    match = re.search(r"(\d+(?:\.\d+)?)(万|亿)?", text.replace(",", ""))
    if not match:
        return None
    return round(float(match[1]) * {None: 1, "万": 10000, "亿": 100000000}[match[2]])


def clock_seconds(text: str) -> int | None:
    match = re.search(r"(?<!\d)(\d{1,2})[:：](\d{2})(?:[:：](\d{2}))?", text)
    if not match:
        return None
    a, b, c = match.groups()
    return int(a)*3600 + int(b)*60 + int(c) if c is not None else int(a)*60+int(b)


class Vision:
    # 页型第一次识别为 unknown 时，第二次复核用的检测边长（模型默认 736，越慢但越细）。
    FALLBACK_DETECT_LIMIT = 480
    # 地块动作菜单的搜索范围与图标命中阈值（1920×1080 实机标定）。
    ACTION_MENU_BOX = (1050, 120, 1750, 950)
    ACTION_ICON_MIN = .85

    def __init__(self, control=None):
        self.checkpoint = control.checkpoint if control is not None else lambda: None
        self.checkpoint()
        from rapidocr_onnxruntime import RapidOCR
        self.ocr = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=2)
        self.precise_detector_limit = getattr(getattr(self.ocr, "text_det", None), "limit_side_len", None)
        self.return_arrow = cv2.imdecode(np.fromfile(Path(__file__).with_name("assets") / "return-arrow-mask.png", dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if self.return_arrow is None:
            raise RuntimeError("回城按钮识别资源无法读取")
        current_arrow = cv2.imdecode(np.fromfile(Path(__file__).with_name("assets") / "return-arrow-current-mask.png", dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if current_arrow is None:
            raise RuntimeError("回城按钮新版识别资源无法读取")
        self.return_arrows = (self.return_arrow, current_arrow)
        self.team_stop = cv2.imdecode(np.fromfile(Path(__file__).with_name("assets") / "team-stop-mask.png", dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if self.team_stop is None:
            raise RuntimeError("队伍任务中止图标识别资源无法读取")
        self.navigation_back = cv2.imdecode(np.fromfile(Path(__file__).with_name("assets") / "navigation-back-mask.png", dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if self.navigation_back is None:
            raise RuntimeError("页面返回箭头识别资源无法读取")
        self.home_marker = cv2.imdecode(np.fromfile(Path(__file__).with_name("assets") / "home-team-marker-mask.png", dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if self.home_marker is None:
            raise RuntimeError("主城队伍标记识别资源无法读取")
        self.returning_marker = cv2.imdecode(np.fromfile(Path(__file__).with_name("assets") / "returning-team-mask.png", dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if self.returning_marker is None:
            raise RuntimeError("返回队伍标记识别资源无法读取")
        self.cache = OrderedDict()
        self.page_cache = OrderedDict()
        self.line_cache = OrderedDict()
        self.saw_text = False
        self.emit = lambda kind, value: None
        # 右下角“隐藏”小眼睛：眼睛=可见，带斜杠=隐藏已开启（实机 2026-11-02 校准）。
        self.hide_eye_box = (1548, 108, 1618, 176)
        self.hide_eye = {}
        for name, key in (("eyeball-open.png", "open"), ("eyeball-hidden.png", "hidden")):
            path = Path(__file__).with_name("assets") / "hide-toggle" / name
            image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is not None:
                self.hide_eye[key] = image
        self.reward_templates = []
        # 活动/礼包整屏弹层右上角的叉号（实机 2026-11-03 首登 BLG 活动页校准）。
        self.close_x = None
        close_path = Path(__file__).with_name("assets") / "close-x.png"
        if close_path.is_file():
            close_image = cv2.imdecode(np.fromfile(close_path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if close_image is not None:
                close_hsv = cv2.cvtColor(close_image, cv2.COLOR_BGR2HSV)
                self.close_x = (((close_hsv[:, :, 2] >= 150) & (close_hsv[:, :, 1] <= 140)).astype(np.uint8)) * 255
        # 地块动作菜单图标（1920×1080 原生）：行军/调动/驻守/攻城/攻占/征兵/升级。
        self.action_icons = {}
        icon_dir = Path(__file__).with_name("assets") / "action-icons"
        for key in ("march", "transfer", "guard", "siege", "occupy", "recruit", "upgrade"):
            path = icon_dir / f"{key}.png"
            image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR) if path.is_file() else None
            if image is not None:
                self.action_icons[key] = image
        for name in ("reward-chest.png", "reward-chest-live.png"):
            path = Path(__file__).with_name("assets") / name
            if path.is_file():
                template = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
                if template is not None:
                    self.reward_templates.append(template)

    def reward_chest(self, image, task):
        """只认已核对底栏文字右上方的宝箱；金色圆点、文字和其他位置不算。"""
        label = {"drill": "征战", "alliance_rewards": "同盟"}.get(task)
        if not label:
            return None
        anchor = self.find(image, [label, "证战"] if task == "drill" else [label], (850, 970, 1770, 1065), exact=True)
        if anchor is None:
            return None
        x0, x1 = max(0, round(anchor.x-20)), min(1920, round(anchor.x+150))
        crop = image[920:1008, x0:x1]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        best = None
        for template in self.reward_templates:
            for scale in (.75, .85, .95, 1., 1.1, 1.2):
                small = cv2.resize(template, None, fx=scale, fy=scale)
                h, w = small.shape
                if h > gray.shape[0] or w > gray.shape[1]:
                    continue
                _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(gray, small, cv2.TM_CCOEFF_NORMED))
                if score < .76 or best and score <= best["score"]:
                    continue
                x, y = point
                hsv = cv2.cvtColor(crop[y:y+h, x:x+w], cv2.COLOR_BGR2HSV)
                gold = cv2.inRange(hsv, (10, 80, 180), (43, 255, 255))
                white = cv2.inRange(hsv, (0, 0, 210), (179, 100, 255))
                if cv2.countNonZero(gold) < w*h*.2 or cv2.countNonZero(white) < w*h*.07:
                    continue
                best = {"x": x0+x+w/2, "y": 920+y+h/2, "score": round(score,4), "task": task}
        return best
        self.checkpoint()

    def read(self, image: np.ndarray, rect: tuple = (0, 0, 1920, 1080), minimum: float = .62, scale: float = 1) -> list[Text]:
        self.checkpoint()
        x0, y0, x1, y1 = rect
        crop = image[y0:y1, x0:x1]
        if not crop.size:
            return []
        # 只复用像素完全相同的裁剪；兵力、倒计时等像素变化立即重新识别。
        fingerprint = hashlib.blake2b(crop.tobytes(), digest_size=16).digest()
        detector = getattr(self.ocr, "text_det", None)
        key = (rect, crop.shape, minimum, scale, getattr(detector, "limit_side_len", None), fingerprint)
        if key in self.cache:
            self.cache.move_to_end(key)
            # 命中缓存也表示本帧读到了文字，否则局部像素变化时会错误跳过高精度页型复核。
            if self.cache[key]:
                self.saw_text = True
            return list(self.cache[key])
        if scale != 1:
            crop = cv2.resize(crop, None, fx=scale, fy=scale)
        results, _ = self.ocr(crop, use_cls=False)
        self.checkpoint()
        spans = []
        for box, label, score in results or []:
            if score < minimum:
                continue
            b = np.asarray(box) / scale
            left, top, right, bottom = float(b[:, 0].min()+x0), float(b[:, 1].min()+y0), float(b[:, 0].max()+x0), float(b[:, 1].max()+y0)
            spans.append(Text(label.strip(), float(score), (left+right)/2, (top+bottom)/2, (left, top, right, bottom)))
        if spans:
            self.saw_text = True
        self.cache[key] = tuple(spans)
        if len(self.cache) > 96:
            self.cache.popitem(last=False)
        return spans

    def joined(self, image, rect=(0, 0, 1920, 1080)) -> str:
        return " | ".join(t.text for t in self.read(image, rect))

    def read_line(self, image, minimum=.9):
        """已隔离的单行数字使用同一识别模型，跳过文字位置检测。"""
        self.checkpoint()
        key = (image.shape, minimum, hashlib.blake2b(image.tobytes(), digest_size=16).digest())
        if key in self.line_cache:
            self.line_cache.move_to_end(key)
            return self.line_cache[key]
        results, _ = self.ocr(image, use_det=False, use_cls=False)
        self.checkpoint()
        value = results[0][0].strip() if results and len(results) == 1 and results[0][1] >= minimum else None
        self.line_cache[key] = value
        if len(self.line_cache) > 64:
            self.line_cache.popitem(last=False)
        return value

    def find(self, image, labels: list[str], rect=(0, 0, 1920, 1080), exact=False) -> Text | None:
        return self.find_in(self.read(image, rect), labels, exact)

    @staticmethod
    def find_in(spans: list[Text], labels: list[str], exact=False) -> Text | None:
        """在已识别的一批文字里找按钮。按钮会把两个字拉开时只合并同一行相邻文字。"""
        for label in labels:
            hits = [t for t in spans if (t.text.replace(" ", "") == label if exact else label in t.text.replace(" ", ""))]
            if hits:
                return max(hits, key=lambda t: t.score)
            ordered = sorted(spans,key=lambda t:t.x)
            for i,first in enumerate(ordered):
                group = [first]
                for second in ordered[i+1:i+3]:
                    previous = group[-1]
                    if abs(second.y-first.y) > 12 or not -8 <= second.box[0]-previous.box[2] <= 75 or min(first.score,second.score) < .75:
                        break
                    group.append(second)
                    value = "".join(t.text.replace(" ","") for t in group)
                    if value == label if exact else label in value:
                        left,top = min(t.box[0] for t in group),min(t.box[1] for t in group)
                        right,bottom = max(t.box[2] for t in group),max(t.box[3] for t in group)
                        return Text(value,min(t.score for t in group),(left+right)/2,(top+bottom)/2,(left,top,right,bottom))
        return None

    @staticmethod
    def band_text(spans: list[Text], rect) -> str:
        x0, y0, x1, y1 = rect
        return " | ".join(t.text for t in spans if x0 <= t.x <= x1 and y0 <= t.y <= y1)

    def signin_count(self, image) -> int | None:
        # 累计计数的描边艺术字容易漏检，先读原图，再提取金色数字。
        spans = self.read(image, (417,916,520,986))
        digits_spans = sorted([t for t in spans if t.text.isdigit()], key=lambda t:t.x)
        # 检测器可能把同一数字重复框成“3”和“31”，不能拼成331当作反证。
        overlap = any(a.box[2] > b.box[0] for a,b in zip(digits_spans,digits_spans[1:]))
        original = "" if overlap else "".join(t.text for t in digits_spans)
        crop = image[925:972,443:500]
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        gold = cv2.inRange(hsv,(15,80,100),(40,255,255))
        digits = cv2.cvtColor(255-gold,cv2.COLOR_GRAY2BGR)
        digits = cv2.copyMakeBorder(digits,15,15,15,15,cv2.BORDER_CONSTANT,value=(255,255,255))
        spans = self.read(digits,(0,0,digits.shape[1],digits.shape[0]))
        counts = [int(t.text) for t in spans if t.text.isdigit() and t.score >= .9]
        # 例如原图分别读成“2”和“7”，不能把置信度较低的“7”丢掉后误报为 2 天。
        if len(counts) == 1 and original == str(counts[0]):
            return counts[0]
        # 30的0比旧57px窗口更宽；两个完整小窗直接识别一致才接受，禁止把3C猜为30。
        readings = []
        for rect in ((439,922,511,976),(440,920,512,977)):
            x0,y0,x1,y1 = rect
            hsv = cv2.cvtColor(image[y0:y1,x0:x1],cv2.COLOR_BGR2HSV)
            canvas = cv2.cvtColor(255-cv2.inRange(hsv,(15,80,100),(40,255,255)),cv2.COLOR_GRAY2BGR)
            canvas = cv2.copyMakeBorder(canvas,12,12,12,12,cv2.BORDER_CONSTANT,value=(255,255,255))
            self.checkpoint()
            results,_ = self.ocr(canvas,use_det=False,use_cls=False)
            self.checkpoint()
            if not results or len(results) != 1:
                return None
            label,score = results[0]
            # 单字识别偶尔会带标点（"52."），先剥掉非数字再判断，两窗仍须一致。
            digits_text = "".join(ch for ch in label if ch.isdigit())
            if not digits_text or score < .8:
                return None
            readings.append(int(digits_text))
        if len(set(readings)) == 1 and (not original or str(readings[0]).startswith(original)):
            return readings[0]
        return None

    @staticmethod
    def highlighted_signin_card(image, card: Text) -> bool:
        left,right = int(card.x-75),int(card.x+75)
        top,bottom = int(card.y-31),int(card.y+166)
        if left < 0 or right > image.shape[1] or bottom > image.shape[0]:
            return False
        hsv = cv2.cvtColor(image[top:bottom,left:right],cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv,(15,35,165),(40,255,255))
        edges = np.zeros(mask.shape,np.uint8)
        edges[:6,:] = edges[-6:,:] = 255
        edges[:,:6] = edges[:,-6:] = 255
        return cv2.countNonZero(cv2.bitwise_and(mask,edges)) > cv2.countNonZero(edges)*.22

    @staticmethod
    def reward_claimed(image, rect) -> bool:
        """Match the green check shape, rather than treating every green reward as claimed."""
        x0,y0,x1,y1 = map(int, rect)
        crop = image[max(0,y0):min(image.shape[0],y1),max(0,x0):min(image.shape[1],x1)]
        if not crop.size:
            return False
        mask = cv2.inRange(cv2.cvtColor(crop,cv2.COLOR_BGR2HSV),(40,130,130),(90,255,255))
        template = cv2.imdecode(np.fromfile(Path(__file__).with_name('assets')/'reward-check-mask.png',dtype=np.uint8),0)
        for scale in (.65,.75,.85,1,1.1,1.2):
            sample = cv2.resize(template,None,fx=scale,fy=scale,interpolation=cv2.INTER_NEAREST)
            if sample.shape[0] <= mask.shape[0] and sample.shape[1] <= mask.shape[1]:
                if cv2.minMaxLoc(cv2.matchTemplate(mask,sample,cv2.TM_CCOEFF_NORMED))[1] >= .78:
                    return True
        return False

    def signin_cards(self, image):
        cards = {}
        for text in self.read(image,(782,97,1896,830),minimum=.8):
            match = re.fullmatch(r'第\s*(\d{1,2})\s*天',text.text)
            if match and 1 <= int(match[1]) <= 28 and text.y + 155 < 830:
                cards[int(match[1])] = text
        return cards

    def signin_milestones(self, image):
        return sorted([(int(match[1]),text) for text in self.read(image,(545,991,1910,1058),minimum=.8)
                       if (match := re.fullmatch(r'(\d{1,4})天',text.text.replace(' ','')))],key=lambda row:row[0])

    @staticmethod
    def navigation_icon(image, name):
        # 地图上的彩色队名会遮住活动/月卡文字；只匹配已采集的图标白色笔画。
        template = cv2.imdecode(np.fromfile(Path(__file__).with_name('assets')/f'nav-{name}.png',dtype=np.uint8),0)
        region = cv2.inRange(cv2.cvtColor(image[95:167,140:1120],cv2.COLOR_BGR2HSV),(0,0,170),(179,65,255))
        _,score,_,(x,y) = cv2.minMaxLoc(cv2.matchTemplate(region,template,cv2.TM_CCOEFF_NORMED))
        if score < .82:
            return None
        return (140+x+template.shape[1]/2,95+y+template.shape[0]/2)

    @staticmethod
    def navigation_toggle(image):
        region = cv2.inRange(cv2.cvtColor(image[95:170,140:1150],cv2.COLOR_BGR2HSV),(15,50,150),(45,255,255))
        candidates = []
        for filename, minimum in (("nav-toggle.png", .85), ("nav-toggle-compact.png", .86)):
            template = cv2.imdecode(np.fromfile(Path(__file__).with_name('assets')/filename,dtype=np.uint8),0)
            for sample in (template,cv2.flip(template,1)):
                _,score,_,(x,y) = cv2.minMaxLoc(cv2.matchTemplate(region,sample,cv2.TM_CCOEFF_NORMED))
                if score >= minimum:
                    candidates.append((score,140+x+sample.shape[1]/2,95+y+sample.shape[0]/2))
        if not candidates:
            return None
        _,x,y = max(candidates)
        return (x,y)

    @staticmethod
    def single_visit_ticket(image, single):
        x,y = int(single.x),int(single.y)
        hsv = cv2.cvtColor(image[y-115:y-65,x-110:x-50],cv2.COLOR_BGR2HSV)
        red = cv2.inRange(hsv,(0,120,85),(12,255,255)) | cv2.inRange(hsv,(170,120,85),(179,255,255))
        return cv2.countNonZero(red) >= 150

    def bandit_rallies(self, image) -> list[dict]:
        """当前可见行：集结文字与倒计时必须有；有加入加号=可加入，已参加/无加号=本号已在集结。"""
        import time
        observed_at = time.time()
        rows = []
        for offset in (0,215,430):
            y = 375+offset
            if y+34 > 902:
                continue
            status = self.joined(image,(960,285+offset,1300,337+offset))
            seconds = clock_seconds(status)
            if "集结中" not in status or seconds is None or seconds <= 30:
                continue
            crop = image[y-25:y+25,1265:1315]
            hsv = cv2.cvtColor(crop,cv2.COLOR_BGR2HSV)
            white = cv2.inRange(hsv,(0,0,160),(179,65,255))
            # 空槽加号的十字笔画；已占用兵种图标和旁边金色旗帜不能作为加入。
            center = white[13:38,16:35]
            horizontal = cv2.countNonZero(center[10:17,:])
            vertical = cv2.countNonZero(center[:,7:12])
            corners = sum(cv2.countNonZero(c) for c in (white[3:12,3:12],white[3:12,38:47],white[38:47,3:12],white[38:47,38:47]))
            row_text = self.joined(image,(480,230+offset,1780,338+offset))
            if "贼寇" not in row_text:
                continue
            captain = self.joined(image, (640, 235+offset, 960, 290+offset))
            joined = "已参加" in row_text.replace(" ", "")
            if horizontal < 45 or vertical < 35 or corners > 45:
                # 加号消失不等于集结消失：识别“已参加”状态行，本号已在该集结。
                if not joined and "已参加" in self.joined(image,(1210,250+offset,1350,345+offset)).replace(" ", ""):
                    joined = True
                if joined:
                    rows.append({"join":None, "joined":True, "remaining_seconds":seconds, "text":row_text,
                                 "captain":captain, "observed_at":observed_at, "deadline":observed_at+seconds})
                continue
            rows.append({"join":(1290,y), "joined":False, "remaining_seconds":seconds, "text":row_text,
                         "captain":captain, "observed_at":observed_at, "deadline":observed_at+seconds})
        return rows

    def horn_point(self, image):
        """邮件/演武图标同行最右、世界聊天上方的固定号角；未识别不代表次数为零。"""
        if image is None or not isinstance(image, np.ndarray) or image.shape[:2] != (1080, 1920):
            return None
        template = cv2.imdecode(np.fromfile(Path(__file__).with_name("assets") / "horn-mask.png",
                                            dtype=np.uint8), cv2.IMREAD_COLOR)
        if template is None or template.shape[:2] != (70, 70):
            raise RuntimeError("号角识别资源无法读取")
        x0, y0, x1, y1 = (580, 800, 740, 950)
        crop = image[y0:y1, x0:x1]
        mask = np.zeros((70, 70), np.uint8)
        cv2.circle(mask, (35, 35), 34, 255, -1)
        scores = cv2.matchTemplate(crop, template, cv2.TM_CCOEFF_NORMED, mask=mask)
        _, score, _, (x, y) = cv2.minMaxLoc(scores)
        if not np.isfinite(score) or score < .82:
            return None
        return {"x": x0 + x + 35, "y": y0 + y + 35, "score": round(float(score), 3)}

    def bandit_availability(self, image, detail=False):
        """Reward eligibility and assistance are distinct, agreed complete readings."""
        rects = ((800, 785, 1145, 845), (785, 780, 1160, 850)) if detail else (
            (135, 880, 445, 951), (120, 875, 455, 956))
        results = []
        for rect in rects:
            labels = [t.text.replace(" ", "") for t in self.read(image, rect, minimum=.88)]
            label = next((text for text in labels if "讨贼奖励次数" in text or text == "当前可协助讨贼"), "")
            if label == "当前可协助讨贼":
                results.append(("assist", 0))
                continue
            if re.fullmatch(r"讨贼奖励次数[:：]?", label):
                label += next((text for text in labels if re.fullmatch(r"\d{1,2}", text)), "")
            match = re.fullmatch(r"讨贼奖励次数[:：]?(\d{1,2})", label)
            results.append(("reward", int(match[1])) if match else ("unknown", None))
        return results[0] if results[0] == results[1] else ("unknown", None)

    @staticmethod
    def checked(image, x, y) -> bool | None:
        roi = image[y-19:y+19, x-19:x+19]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        green = cv2.inRange(hsv, (35, 65, 90), (95, 255, 255))
        if cv2.countNonZero(green) > 70:
            return True
        # 已校验的复选框：未勾选时内部是灰色，不以全黑的未知页面判 false。
        if float(np.mean(roi)) > 20 and float(np.mean(hsv[:, :, 1])) < 80:
            return False
        return None

    def page(self, image, verify=True) -> str:
        """识别页型。verify=False 用于“没看清就再截一帧”的等待循环，省掉昂贵的复核。"""
        import time
        started = time.monotonic()
        self.checkpoint()
        key = (image.shape, hashlib.blake2b(image.tobytes(), digest_size=16).digest())
        cache = self.page_cache
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
        # 默认检测器把几十像素高的导航裁剪放大到736像素，造成大量无效计算。
        # 只给页面识别使用较小的检测尺寸；兵力、次数和金额仍按原精度读取。
        detector = getattr(self.ocr, "text_det", None)
        original = getattr(detector, "limit_side_len", None)
        try:
            if original is not None:
                detector.limit_side_len = 128
            self.saw_text = False
            result = self._page(image)
        finally:
            if original is not None:
                detector.limit_side_len = original
        # 一帧里连一个字都没读到（切页动画、黑屏）时原精度复核也读不出页型，
        # 只白花几秒；只有读到了文字但页型仍不明时才用更高精度再核一次。
        # 复核用 480 而不是模型默认的 736：战事批复页在 400 以下会漏检，480 起正常，
        # 耗时约为 736 的六成。
        if result == "unknown" and verify and original is not None and self.saw_text:
            try:
                detector.limit_side_len = min(int(original), self.FALLBACK_DETECT_LIMIT)
                result = self._page(image)
            finally:
                detector.limit_side_len = original
        self.checkpoint()
        # 未复核的 unknown 不进缓存：同一帧之后若需要严格识别，还要能真正复核一次。
        if result != "unknown" or verify:
            cache[key] = result
            if len(cache) > 8:
                cache.popitem(last=False)
        seconds = time.monotonic()-started
        if seconds > 3 or result == "unknown":
            self.emit("vision", {"stage":"page", "page":result, "seconds":round(seconds,3)})
        return result

    def _page(self, image) -> str:
        prompt, _ = self.connection_prompt(image)
        if prompt:
            return prompt
        if self.bandit_assist_prompt(image) is not None:
            return "bandit_assist_confirm"
        # 聊天面板的“前往好友”不是地图前往；先核对两个专有控件。
        if self.chat_close_point(image) is not None:
            return "chat"
        if self.channel_menu_panel(image) is not None:
            return "channel_menu"
        # 左侧竖排艺术字无法稳定 OCR；使用右侧两处横排栏目与批复按钮共同核对。
        if self.war_summary_button(image):
            return "war_summary"
        # 大横条会触发 OCR 的单行模式，把标题与右侧场景混在一起；分开读两处固定标题区。
        header = self.joined(image, (0, 0, 450, 175)) + " | " + self.joined(image, (750, 0, 1190, 175))
        if "征兵" in header or "证兵" in header:
            settings = self.joined(image, (1230, 220, 1800, 290)).replace(" ", "")
            if "部队征兵设置" in settings or "部队证兵设置" in settings:
                return "camp_recruit"
        if ("征兵" in header or "证兵" in header) and self.find(image, ["请选择征兵部队"], (560,575,1320,667)):
            return "camp_recruit_list"
        if any(s in header for s in ("其他地方", "其他设备", "异地", "重新登录")):
            return "blocked"
        if "乔妹助手" in header and self.find(image, ["乔妹问答"], (0, 800, 260, 935), exact=True):
            return "guide"
        if "同盟" in header and self.find(image, ["战事", "战争"], (1040, 440, 1320, 630), exact=True):
            if self.find(image, ["成员"], (1260, 970, 1475, 1060), exact=True):
                return "alliance"
        if "领土" in header and self.find(image, ["开垦"], (120, 210, 1790, 305), exact=True):
            return "territory"
        if "部队总览" in header:
            return "overview"
        # 个人信息页（点头像进入）：左上标题 + 个人数据/武勋栏共同确认，避免把聊天里
        # 的同名词当页面。武勋读数只在这一个已确认页面上取。
        if "个人信息" in header and ("个人数据" in self.joined(image, (1150, 640, 1900, 790))
                                     or "武勋" in self.joined(image, (1500, 430, 1720, 520))):
            return "profile"
        if "部队" in header and ("阵" in header or "交换" in self.joined(image, (310, 95, 410, 270))):
            return "team"
        if "城务" in header:
            return "affairs"
        if "战争" in header:
            return "war"
        if "集结" in header and self.find(image,["已集结玩家"],(115,210,800,285)):
            return "rally"
        if "征兵所" in header or "证兵所" in header:
            return "reserve"
        if "军演" in header:
            return "drill"
        if "征战" in header or "证战" in header:
            return "campaign"
        if "活动" in header:
            return "activity"
        if "福利" in header:
            return "welfare"
        if "寻访" in header and self.find(image,["寻访一次"],(440,870,1080,985),exact=True):
            if '再寻访' in self.joined(image,(570,140,1370,230)) and self.find(image,['招贤纳士'],(140,130,380,250),exact=True) is None:
                return "visit_result"
            return "visit"
        if "地图沙盘" in header or "地图染色" in header:
            return "worldmap"
        if self.find(image, ["请选择建筑"], (0, 610, 450, 750), exact=True):
            cards = self.joined(image, (0, 710, 1160, 800))
            if all(name in cards for name in ("君王殿", "民居", "征兵所")):
                return "city_buildings"
        # 动态地图会把“攻城”与玩家名合并。改用选队提示、提交按钮和队伍卡片共同确认。
        # 目标不与连接地相邻时，攻占页会切换成“选择部队自动铺路”，它仍是出征页。
        selection = self.find(image, ["请选择出征部队", "选择部队自动铺路"], (475, 675, 1265, 760))
        # 选中有休整层数的队伍后，选择提示会被休整说明替换。
        rested_selection = self.find(image, ["休整状态"], (475, 675, 1265, 760)) and self.find(image, ["调动", "攻城", "攻占", "驻守", "行军"], (750, 20, 1190, 125), exact=True)
        if selection or rested_selection:
            if self.find(image, ["出征", "出证"], (1540, 932, 1895, 1036), exact=True):
                if any(self.deployment_status(image, slot) != "unknown" for slot in range(1, 6)):
                    return "deployment"
        # 土地详情能遮住资源栏；沙盘还需用底部导航及右侧队伍页码交叉确认。
        bottom = " | ".join(self.joined(image, r) for r in ((20,930,430,1075),(700,940,1000,1075),(1000,940,1450,1075),(1450,940,1915,1075)))
        if "前往" in bottom:
            return "worldmap"
        # 出城与聊天文字相邻；独立核对按钮，避免拥挤文字漏检或聊天中同词误判。
        if self.find(image, ["出城"], (15, 1000, 180, 1075), exact=True):
            return "city"
        if "武将" in bottom and ("同盟" in bottom or "职业" in bottom):
            # 沙盘页型只由底栏 + 顶部资源栏确认，不读队伍页码。
            # 2026-11-09：页码属于右侧头像列表的分页状态，与“这是不是沙盘”无关；
            # 把它当页型条件会让地图底色影响页面判断。
            top = self.joined(image, (0, 30, 900, 115)).replace(" ", "")
            if "繁荣" in top and "道路" in top:
                return "sandbox"
        center = self.joined(image, (450, 250, 1470, 800))
        if any(s in center for s in ("其他地方登录", "其他设备登录", "重新登录", "连接已断开")):
            return "blocked"
        if "一键领取" in self.joined(image, (700, 825, 1250, 1000)):
            return "monthly"
        claimed = self.joined(image, (350, 170, 1550, 850))
        if all(s in claimed for s in ("千石禄", "万石禄", "今日俸禄", "已领取")):
            return "monthly_claimed"
        # 兜底:「点击空白处关闭」类活动/奖励弹层压暗底层页面,具体页型全部识别失败后才落到这里;
        # 以底部通用关闭文案为准,不写死弹层标题。月卡等带实体按钮的面板已在上方先行识别,不会被点掉。
        if "点击空白处关闭" in self.joined(image, (600, 990, 1350, 1080)).replace(" ", ""):
            return "dismissible_overlay"
        if self.navigation_back_point(image) is not None:
            return "navigable_page"
        return "unknown"

    def navigation_back_point(self, image, require_title=True):
        """只认游戏左上金色返回箭头和独立标题；不猜未知页面的关闭位置。"""
        template = getattr(self, "navigation_back", None)
        if template is None or image is None or image.shape[:2] != (1080, 1920):
            return None
        roi = image[8:74, 75:190]
        mask = cv2.inRange(cv2.cvtColor(roi, cv2.COLOR_BGR2HSV), (10, 30, 110), (45, 255, 255))
        _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(mask, template, cv2.TM_CCOEFF_NORMED))
        if score < .92:
            return None
        if not require_title:
            h, w = template.shape
            return {"x": round(75+point[0]+w/2), "y": round(8+point[1]+h/2)}
        labels = self.read(image, (190, 0, 750, 85), minimum=.88)
        titles = [t.text.replace(" ", "") for t in labels if re.fullmatch(r"[\u4e00-\u9fff]{2,16}", t.text.replace(" ", "")) and t.x < 650]
        if len(titles) != 1 or any(s in titles[0] for s in ("登录", "账号", "充值", "购买", "支付", "维护")):
            return None
        h, w = template.shape
        return {"x": round(75+point[0]+w/2), "y": round(8+point[1]+h/2),
                "title": titles[0], "score": round(score, 4)}

    @staticmethod
    def gold_control(image, filename, rect, minimum=.92, scales=(1.,), outline=False):
        """固定区域仅用于查找真实字形，不把区域中心当作按钮。冲突时拒绝。"""
        if image is None or not isinstance(image, np.ndarray) or image.shape[:2] != (1080, 1920):
            return None
        template = cv2.imdecode(np.fromfile(Path(__file__).with_name("assets") / filename, dtype=np.uint8), 0)
        x0, y0, x1, y1 = rect
        mask = cv2.inRange(cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV), (10, 30, 110), (45, 255, 255))
        candidates = []
        for scale in scales:
            sample = cv2.resize(template, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
            h, w = sample.shape
            if h > mask.shape[0] or w > mask.shape[1]:
                continue
            if outline:
                # 半透明地图上的金色背景不属于按钮。只比较叉号及紧邻轮廓，
                # 保留外缘负样本，纯金色块仍不能匹配成关闭按钮。
                support = cv2.dilate(sample, np.ones((3, 3), np.uint8))
                errors = cv2.matchTemplate(mask, sample, cv2.TM_SQDIFF_NORMED, mask=support)
                scores = 1 - np.nan_to_num(errors, nan=1., posinf=1., neginf=1.)
            else:
                scores = cv2.matchTemplate(mask, sample, cv2.TM_CCOEFF_NORMED)
            for _ in range(2):
                _, score, _, (x, y) = cv2.minMaxLoc(scores)
                if score < minimum:
                    break
                candidates.append((score, x0+x+w/2, y0+y+h/2))
                scores[max(0,y-h//2):y+h//2+1, max(0,x-w//2):x+w//2+1] = -1
        if not candidates:
            return None
        _, x, y = max(candidates)
        if any(abs(cx-x) > 25 or abs(cy-y) > 25 for _, cx, cy in candidates):
            return None
        return round(x), round(y)

    def navigation_close_point(self, image, page):
        if self.page(image) != page:
            return None
        if page in ("team", "guide", "affairs", "drill", "campaign", "activity", "welfare", "visit", "visit_result", "alliance", "profile"):
            point = self.navigation_back_point(image, require_title=False)
            return (point["x"], point["y"]) if point else None
        rects = {"overview": (1680, 55, 1860, 190), "reserve": (1680, 55, 1860, 190),
                 "war": (1680, 85, 1860, 230), "rally": (1680, 85, 1860, 230),
                 "territory": (1680, 85, 1860, 230), "camp_recruit": (1680, 85, 1860, 230),
                 "deployment": (1740, 450, 1915, 645), "camp_recruit_list": (1740, 530, 1915, 720),
                 "monthly": (1580, 35, 1810, 210), "monthly_claimed": (1580, 35, 1810, 210)}
        rect = rects.get(page)
        return self.gold_control(image, "panel-close-mask.png", rect, minimum=.85,
                                 scales=(.8, .9, 1., 1.1, 1.2), outline=True) if rect else None

    def dismiss_point(self, image):
        label = self.find(image, ["点击空白处关闭"], (400, 940, 1550, 1080), exact=True)
        return (label.x, label.y) if label else None

    def sandbox_menu_point(self, image):
        if self.page(image) != "sandbox":
            return None
        return self.gold_control(image, "sandbox-menu-mask.png", (1580, 930, 1750, 1065))

    def city_building_point(self, image):
        if self.page(image) != "city":
            return None
        for filename in ("city-building-current-mask.png", "city-building-mask.png"):
            point = self.gold_control(image, filename, (15, 650, 190, 860), scales=(.85, 1., 1.15))
            if point:
                return point
        return None

    def checkbox_before(self, image, label, gap=80):
        """定位一行文字左侧的复选框方框中心；与 labeled_checkbox 同一套几何规则。"""
        x0, y0 = max(0, round(label.box[0])-gap), max(0, round(label.y)-28)
        x1, y1 = max(0, round(label.box[0])-12), round(label.y)+28
        # 定位方框本身，不把伸出边框的绿色勾当成方框轮廓。
        # 勾选后原 Canny 轮廓被切断且宽度变成 55px，反而只认得未勾选框。
        hsv = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
        border = cv2.inRange(hsv, (0, 0, 65), (179, 65, 255))
        border = cv2.morphologyEx(border, cv2.MORPH_CLOSE, np.ones((9, 1), np.uint8))
        contours, _ = cv2.findContours(border, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        centers = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if 32 <= w <= 54 and 32 <= h <= 50 and .75 < w/h < 1.3:
                center = (round(x0+x+w/2), round(y0+y+h/2))
                if abs(center[1]-label.y) <= 10 and not any(abs(cx-center[0]) < 8 and abs(cy-center[1]) < 8 for cx, cy in centers):
                    centers.append(center)
        return centers[0] if len(centers) == 1 else None

    def labeled_checkbox(self, image, labels, rect):
        label = self.find(image, labels, rect, exact=True)
        if label is None:
            return None
        return self.checkbox_before(image, label)

    def active_task_visible(self, image):
        # 已定位自己队伍的沙盘菜单；仅用于等待，不推断作战种类或提交成功。
        if not self.find(image, ["中止"], (920, 600, 1180, 745), exact=True):
            return False
        clocks = []
        spans = sorted(self.read(image, (850, 430, 1100, 515), minimum=.88), key=lambda t: t.x)
        for span in spans:
            label = span.text.replace(" ", "").replace("：", ":")
            # OCR 会把完整时钟分成相邻的“05:”和“30”；只拼同一行紧邻的秒字段。
            if re.fullmatch(r"\d{1,2}:", label):
                tails = [t.text.replace(" ", "").replace("：", ":") for t in spans
                         if t is not span and abs(t.y-span.y) <= 10 and -8 <= t.box[0]-span.box[2] <= 20
                         and re.fullmatch(r"\d{2}(?::\d{2})?", t.text.replace(" ", "").replace("：", ":"))]
                if len(tails) == 1:
                    label += tails[0]
            if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", label):
                parts = [int(p) for p in label.split(":")]
                if all(p < 60 for p in parts[1:]):
                    clocks.append(clock_seconds(label))
        return len(clocks) == 1 and clocks[0] is not None and clocks[0] > 0

    def channel_menu_panel(self, image):
        # 浮球可拖到其他位置。先找白色面板，再核对四个专有入口；不把普通游戏白窗当渠道菜单。
        white = np.all(image > 225, axis=2).astype(np.uint8)
        _, _, stats, _ = cv2.connectedComponentsWithStats(white)
        for x, y, w, h, area in stats[1:]:
            if not (450 <= w <= 1000 and 230 <= h <= 650 and area >= w*h*.55):
                continue
            labels = self.joined(image, (int(x), int(y), int(x+w), int(y+h))).replace(" ", "")
            if all(label in labels for label in ("优惠券", "积分商城", "会员卡", "福利活动")):
                return (int(x), int(y), int(x+w), int(y+h))
        return None

    def bandit_assist_prompt(self, image):
        # 仅处理奖励耗尽的这一种二次确认，不能把任意“提示/确定”当可接受订单。
        text = self.joined(image, (560, 480, 1370, 590)).replace(" ", "")
        if not all(s in text for s in ("讨伐贼寇奖励次数已用尽", "仍可帮助其他主", "是否确认")):
            return None
        cancel = self.find(image, ["取消"], (570, 610, 895, 707), exact=True)
        confirm = self.find(image, ["确定"], (1020, 610, 1355, 707), exact=True)
        return (cancel, confirm) if cancel and confirm else None

    def chat_close_point(self, image):
        if not self.find(image, ["前往好友"], (0, 885, 250, 972), exact=True):
            return None
        if not self.find(image, ["发送"], (960, 970, 1660, 1065), exact=True):
            return None
        # 普通聊天与乔妹问答各有明确收起箭头；仅收起，不触碰输入/发送。
        if self.find(image, ["对话中"], (0, 975, 250, 1070), exact=True):
            return (1698, 540)
        return (1242, 540)

    def war_summary_button(self, image):
        button = self.find(image, ["批复"], (1000, 780, 1460, 950), exact=True)
        if not button or button.score < .9:
            return None
        resources = self.find(image, ["资源获取"], (820, 230, 1740, 300), exact=True)
        activities = self.find(image, ["今日可进行事项"], (820, 535, 1740, 610), exact=True)
        return button if resources and activities and min(resources.score, activities.score) >= .85 else None

    def connection_prompt(self, image):
        """断线正文和独立重连按钮共同成立；异地登录优先停止。"""
        # 正文与按钮分区；普通沙盘没有断线正文时不扫描整片动态地图。
        spans = self.read(image, (550,350,1370,600), minimum=.85)
        body = "".join(t.text.replace(" ", "") for t in sorted(spans,key=lambda t:(t.y,t.x)))
        if "服务器维护" in body and self.find(image, ["回到登录"], (650, 600, 1300, 820), exact=True):
            return "maintenance", None
        if any(s in body for s in ("其他地方登录", "其他设备登录", "异地登录", "重新登录")):
            return "blocked", None
        network = any(s in body for s in ("网络连接已断开", "连接已断开", "与服务器断开连接",
                                           "无网络连接", "无网络", "网络连接异常", "网络异常",
                                           "连接服务器失败", "网络连接失败"))
        if not network:
            return None, None
        buttons = [t for t in self.read(image,(600,600,1320,820),minimum=.9) if t.score >= .9 and 600 <= t.y <= 820
                   and t.text.replace(" ", "") in ("重连", "重新连接")]
        if network and len(buttons) == 1:
            return "reconnect", buttons[0]
        if network:
            return "blocked", None
        return None, None

    def city_resource_rows(self, image) -> list[CityResourceRow]:
        """Match current field name/type/location on the same reclamation-list row."""
        spans = self.read(image, (700, 385, 1450, 945), minimum=.8)
        result = []
        for title in spans:
            match = re.fullmatch(r"(\d{1,2})级(农田|林场|铁矿|石矿)", title.text.replace(" ", ""))
            if not match or not 700 <= title.x < 960 or not 1 <= int(match[1]) <= 30:
                continue
            types = [t for t in spans if 960 <= t.x < 1115 and abs(t.y-title.y) <= 22
                     and t.text.replace(" ", "") == "开垦地"]
            locations = [t for t in spans if 1115 <= t.x < 1450 and abs(t.y-title.y) <= 22
                         and t.text.replace(" ", "") == "主城内"]
            if len(types) != 1 or len(locations) != 1:
                continue
            result.append(CityResourceRow(title.text, CITY_RESOURCE_NAMES[match[2]], int(match[1]), locations[0]))
        return sorted(result, key=lambda row: row.location.y)

    def city_resource_count(self, image) -> int | None:
        spans = self.read(image, (120, 315, 680, 377), minimum=.8)
        if not any(t.text.replace(" ", "") == "开垦数" for t in spans):
            return None
        values = [int(t.text) for t in spans if t.x > 550 and re.fullmatch(r"\d{1,3}", t.text)]
        return values[0] if len(values) == 1 else None

    def city_farm_selection(self, image) -> CityFarmSelection | None:
        """Two selected-field labels must agree; never use a tile's old appearance."""
        spans = self.read(image, (90, 215, 1700, 860), minimum=.8)
        names = []
        for span in spans:
            match = re.fullmatch(r"(\d{1,2})级(农田|林场|铁矿|石矿)", span.text.replace(" ", ""))
            if match:
                names.append((int(match[1]), match[2]))
        if len(names) < 2 or len(set(names)) != 1:
            return None
        level, name = names[0]
        if name != "农田" or not 1 <= level <= 30:
            return None
        for first in spans:
            if not 300 < first.x < 1650 or "屯田" not in first.text.replace(" ", ""):
                continue
            line = sorted([t for t in spans if abs(t.y-first.y) <= 14 and first.box[0]-5 <= t.x <= first.box[2]+180], key=lambda t: t.x)
            text = "".join(t.text for t in line).replace(" ", "").replace("（", "(").replace("）", ")")
            match = re.fullmatch(r"屯田\(?余(\d{1,2})\)?", text)
            if match:
                return CityFarmSelection(f"{level}级农田", level, first, int(match[1]))
        return None

    def deployment_strength(self, image, slot):
        if not 1 <= slot <= 5:
            return None
        offset = (slot-1)*249
        hsv = cv2.cvtColor(image[968:1006,145+offset:254+offset],cv2.COLOR_BGR2HSV)
        readings = []
        for saturation,value in ((65,150),(80,140)):
            white = cv2.inRange(hsv,(0,0,value),(179,saturation,255))
            canvas = cv2.cvtColor(255-white,cv2.COLOR_GRAY2BGR)
            canvas = cv2.copyMakeBorder(canvas,15,15,15,15,cv2.BORDER_CONSTANT,value=(255,255,255))
            spans = self.read(canvas,(0,0,canvas.shape[1],canvas.shape[0]),minimum=.8)
            # 只接受一个完整数字；不拼接被切碎的 330、3000，也不从卡片其它文字找兵力。
            if len(spans) != 1 or not re.fullmatch(r"\d{1,7}",spans[0].text):
                return None
            readings.append(int(spans[0].text))
        return readings[0] if readings[0] == readings[1] else None

    def action_buttons(self, image):
        """在地块菜单区域用图标模板找可用指令，返回 {key: (x, y, score)}。

        key 与 assets/action-icons 一致：march/transfer/guard/siege/occupy/recruit/upgrade。
        图标匹配比文字识别快一个数量级，命中即可直接点；全不中时调用方再退回 OCR。
        """
        if not self.action_icons:
            return {}
        x1, y1, x2, y2 = self.ACTION_MENU_BOX
        roi = image[y1:y2, x1:x2]
        if roi.size == 0:
            return {}
        found = {}
        for key, template in self.action_icons.items():
            if template.shape[0] > roi.shape[0] or template.shape[1] > roi.shape[1]:
                continue
            result = cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED)
            _, score, _, point = cv2.minMaxLoc(result)
            if score >= self.ACTION_ICON_MIN:
                found[key] = (x1 + point[0] + template.shape[1] // 2,
                              y1 + point[1] + template.shape[0] // 2,
                              float(score))
        return found

    def close_x_point(self, image):
        """整屏活动/礼包/月卡页右上角的叉号。

        颜色随页面不同（白色、金色），所以用"亮色掩码形状"匹配；只有高度匹配才返回。
        """
        if self.close_x is None:
            return None
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        bright = (((hsv[:, :, 2] >= 150) & (hsv[:, :, 1] <= 140)).astype(np.uint8)) * 255
        template = self.close_x
        x1, y1, x2, y2 = 1500, 60, 1800, 260
        roi = bright[y1:y2, x1:x2]
        if roi.shape[0] < template.shape[0] or roi.shape[1] < template.shape[1]:
            return None
        result = cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, point = cv2.minMaxLoc(result)
        if score < .80:
            return None
        return (x1 + point[0] + template.shape[1] // 2,
                y1 + point[1] + template.shape[0] // 2)

    def monthly_no_card(self, image):
        """月卡页只有购买/续费入口、没有可领取的俸禄：账号未开通或已过期。

        只用于"跳过"，绝不用于点击购买。判定同时要求：出现月卡档位（千石禄/万石禄等）、
        出现购买类字样，且全屏没有"一键领取"。
        """
        body = self.joined(image, (200, 80, 1700, 1000)).replace(" ", "")
        if "一键领取" in body:
            return False
        if "今日俸禄" in body and "已领取" in body:
            return False
        tiers = any(s in body for s in ("千石禄", "万石禄", "月卡", "俸禄", "返利"))
        buying = any(s in body for s in ("购买", "续费", "开通", "立即获得", "可回溯", "￥", "¥"))
        return tiers and buying

    def frame_clear(self, image):
        """画面是否已从过渡/云雾中恢复。

        移镜头的云雾会把整屏洗成灰白：底部导航的饱和度从正常 30–60 掉到 10 以下。
        以底栏饱和度为主判据；顶部资源条作为辅助，避免没有底栏的页面误判。
        """
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        value, saturation = hsv[:, :, 2], hsv[:, :, 1]
        nav = saturation[1012:1072, 300:1820]
        top = saturation[8:62, 640:1700]
        top_value = value[8:62, 640:1700]
        if nav.size == 0 or top.size == 0:
            return True
        washed = float(nav.mean()) < 22 and (
            float(top.mean()) < 18 or float(top_value.mean()) > 110)
        return not washed

    def team_wounded(self, image, slot):
        """头像正中间是否压着“重伤”白字。

        只有没有角标的队伍才需要查（重伤队在主城，角标区为空）；一次小区域 OCR，
        文字必须正好是“重伤”，地图上的单个“伤”字或白色撤退箭头不会被当成重伤。
        """
        if not 1 <= slot <= 5:
            return False
        y = (245, 383, 520, 660, 785)[slot - 1]
        text = self.joined(image, (1808, y - 19, 1880, y + 19)).replace(" ", "")
        return "重伤" in text

    def deploy_button_enabled(self, image):
        """出征按钮是否可点：按钮底色为黄色才算可用（灰色＝条件不满足）。"""
        roi = image[947:1020, 1580:1860]
        if roi.size == 0:
            return False
        mask = cv2.inRange(cv2.cvtColor(roi, cv2.COLOR_BGR2HSV), (12, 30, 130), (40, 255, 255))
        return cv2.countNonZero(mask) >= mask.size * .3

    def deployment_hint(self, image):
        """出征页标题下方的一行说明，例如“地块超出可占范围”“被攻击的建筑物没有连接地”。"""
        for region in ((430, 108, 1520, 200), (430, 40, 1520, 200)):
            text = self.joined(image, region).replace(" ", "")
            if len(text) >= 4:
                return text
        return ""

    def hide_mode(self, image):
        """右下角小眼睛当前状态：'hidden'=隐藏已开（带斜杠），'open'=元素可见。

        两个模板互相比较取高分；分不清时返回 (None, scores)，调用方不得乱点。
        """
        if len(self.hide_eye) < 2:
            return None, {}
        x1, y1, x2, y2 = self.hide_eye_box
        roi = image[y1:y2, x1:x2]
        if roi.shape[:2] != self.hide_eye["open"].shape[:2]:
            return None, {}
        scores = {key: float(cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED).max())
                  for key, template in self.hide_eye.items()}
        best = max(scores, key=scores.get)
        if scores[best] - min(scores.values()) < .05:
            return None, scores
        return ("hidden" if best == "hidden" else "open"), scores

    def stationary_return_visible(self, image, slot):
        return self.stationary_return_point(image, slot) is not None

    def team_task_stop_visible(self, image, slot):
        """只证明对应队伍有可中止任务，不用作点击位置或订单受理证据。"""
        if not 1 <= slot <= 5:
            return False
        y = (245,383,520,660,785)[slot-1]
        roi = image[y-38:y+53,1667:1785]
        if roi.shape[:2] != (91,118):
            return False
        gold = cv2.inRange(cv2.cvtColor(roi,cv2.COLOR_BGR2HSV),(10,35,110),(43,255,255))
        return cv2.minMaxLoc(cv2.matchTemplate(gold,self.team_stop,cv2.TM_CCOEFF_NORMED))[1] >= .90

    def stationary_return_point(self, image, slot):
        if not 1 <= slot <= 5:
            return None
        y = (245,383,520,660,785)[slot-1]
        roi = image[y-38:y+53,1667:1768]
        if roi.shape[:2] != (91,101):
            return None
        gold = cv2.inRange(cv2.cvtColor(roi,cv2.COLOR_BGR2HSV),(10,35,110),(43,255,255))
        # 已抵达后的弯箭头；不把行军中的“立即”加速按钮当作回城。
        for source in self.return_arrows:
            for scale in (1, .96, 1.04):
                template = source if scale == 1 else cv2.resize(
                    source, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
                _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(gold,template,cv2.TM_CCOEFF_NORMED))
                if score >= .90:
                    return (1667 + point[0] + template.shape[1] / 2,
                            y - 38 + point[1] + template.shape[0] / 2)
        return None

    def full_team_rows(self, image):
        # 2026-11-08 新赛季左侧列表整体上移：部队一在 y≈149，五队在 y≈601。
        # 旧赛季在 y≈221..674；取 90..810 的并集，兼容两种布局。
        rows = {}
        for text in self.read(image, (0, 90, 430, 810), minimum=.75):
            match = re.fullmatch(r"部队([一二三四五])", text.text.replace(" ", ""))
            if match:
                slot = "一二三四五".index(match[1]) + 1
                if slot in rows:
                    return {}
                rows[slot] = text
        return rows

    def home_team_visible(self, image, slot):
        if not 1 <= slot <= 5:
            return False
        y = (245,383,520,660,785)[slot-1]
        roi = image[y-40:y+53,1700:1790]
        if roi.shape[:2] != (93,90):
            return False
        gold = cv2.inRange(cv2.cvtColor(roi,cv2.COLOR_BGR2HSV),(10,35,110),(43,255,255))
        return float(cv2.matchTemplate(gold,self.home_marker,cv2.TM_CCOEFF_NORMED).max()) >= .92

    def returning_team_visible(self, image, slot):
        if not 1 <= slot <= 5:
            return False
        y = (245,383,520,660,785)[slot-1]
        roi = image[y:y+56,1840:1905]
        if roi.shape[:2] != (56,65):
            return False
        white = cv2.inRange(cv2.cvtColor(roi,cv2.COLOR_BGR2HSV),(0,0,150),(179,65,255))
        return float(cv2.matchTemplate(white,self.returning_marker,cv2.TM_CCOEFF_NORMED).max()) >= .92

    def refill_checkbox(self, image):
        label = self.find(image, ["回城自动补兵"], (1640, 875, 1840, 965), exact=True)
        return (1857, round(label.y)) if label else None

    def full_team(self, image: np.ndarray, key: str) -> Observation:
        obs = Observation(key)
        if self.page(image) != "team":
            obs.note = "不是完整部队页面"
            return obs
        right = self.read(image, (1360, 385, 1905, 565))
        army = [p for t in right if t.y < 448 for p in pairs(t.text) if 1000 <= p[1] <= 1000000 and 0 <= p[0] <= p[1]]
        supply = [p for t in right if t.y > 485 for p in pairs(t.text) if p[1] == 100 and 0 <= p[0] <= 100]
        heroes = self.read(image, (410, 365, 1345, 657))
        hero_strength = [p for t in heroes if 554 < t.y < 603 for p in pairs(t.text) if 100 <= p[1] <= 100000 and p[0] <= p[1]]
        if len(army) == 1 and len(hero_strength) == 3 and sum(p[0] for p in hero_strength) == army[0][0] and sum(p[1] for p in hero_strength) == army[0][1]:
            obs.strength, obs.maximum = army[0]
        if len(supply) == 1:
            obs.supply = supply[0][0]
        stamina = []
        for t in heroes:
            if 606 < t.y < 647:
                match = re.fullmatch(r"(?:体力\s*)?(\d{1,3})(?:/200)?", t.text.replace(" ", ""))
                if match and 0 <= int(match[1]) <= 200:
                    stamina.append(int(match[1]))
        if len(stamina) == 3:
            obs.stamina = min(stamina)
        injury = [t for t in heroes if t.y < 440 and ("重伤" in t.text or "重份" in t.text)]
        red_suspect = False
        for x in (465, 782, 1100):
            roi = cv2.cvtColor(image[390:426, x:x+195], cv2.COLOR_BGR2HSV)
            red = cv2.inRange(roi, (0, 120, 80), (2, 255, 130)) | cv2.inRange(roi, (177, 120, 80), (179, 255, 130))
            # 重伤横条的暗红字；疑似红字但 OCR 没读到时保持未知。
            if cv2.countNonZero(red) > 250:
                # 立绘亮红衣物不属于这种暗红字；疑似横条还须有字形高度的连通笔画。
                # 任何这样的红字仍保持未知，不能仅因OCR漏读“重伤”就认作恢复。
                _, _, components, _ = cv2.connectedComponentsWithStats(red)
                if any(2 <= w <= 40 and 18 <= h <= 36 and area >= 20
                       for _, _, w, h, area in components[1:]):
                    red_suspect = True
        obs.wounded = True if injury else (None if red_suspect or obs.strength is None else False)
        obs.injury_seconds = max((clock_seconds(t.text) or 0 for t in injury), default=None)
        names = sorted([t for t in heroes if 488 < t.y < 544 and re.search(r"\d+\s*[^\d\W]", t.text)], key=lambda t: t.x)
        obs.identity = "/".join(re.sub(r"^[\d\s]+", "", t.text) for t in names)
        pools = self.read(image, (1399, 833, 1624, 949))
        pool_label = " ".join(t.text for t in pools)
        obs.pool = "武卫一" if any(s in pool_label for s in ("武卫一", "武卫营一")) else "武卫二" if any(s in pool_label for s in ("武卫二", "武卫营二")) else "普通"
        reserves = [p for t in pools for p in pairs(t.text) if p[1] > 1000 and p[0] <= p[1]]
        if len(reserves) == 1:
            obs.reserve, obs.capacity = reserves[0]
        checkbox = self.refill_checkbox(image)
        obs.auto_refill = self.checked(image, *checkbox) if checkbox else None
        obs.note = "兵力已交叉核对" if obs.strength is not None else "兵力总数与三名武将数值未通过交叉核对"
        return obs

    def profile_merit(self, image) -> tuple[int | None, str]:
        """个人信息页右上角的“武勋”数值（实机样本 501.5万）。

        读数只用于展示“这轮挂机大概涨了多少武勋”，不参与派兵判断；读不到就返回
        (None, "")，调用方记一条日志后继续，不重试成环。
        """
        def pick(spans):
            found = []
            for t in spans:
                text = t.text.replace(",", "").replace(" ", "")
                if re.fullmatch(r"\d+(?:\.\d+)?[万亿]?", text):
                    value = quantity(text)
                    if value is not None:
                        found.append((value, text))
            return found[0] if len(found) == 1 else (None, "")

        value, text = pick(self.read(image, (1675, 432, 1880, 516)))
        if value is not None:
            return value, text
        # 字号或描边变化时整条数字可能左移；扩一圈仍要求只有一个纯数字，避免读到繁荣值。
        spans = self.read(image, (1690, 425, 1900, 520))
        return pick(spans)

    def grain(self, image) -> int | None:
        # 资源数字为浅色，前线玩家名为彩色描边；先移除背景文字，再核对库存与容量。
        def reading(spans):
            candidates = []
            for span in spans:
                text = span.text.replace("／", "/").replace(" ", "").replace(",", "")
                if re.fullmatch(r"\d+(?:\.\d+)?[万亿]?/\d+(?:\.\d+)?[万亿]?", text):
                    current, capacity = map(quantity, text.split("/"))
                    # 游戏奖励可令库存溢出仓库上限，现场显示红字。容量不是库存的上界；
                    # 仍须两路完整库存/容量一致，拒绝截断、负数和无效容量。
                    if current is not None and current >= 0 and capacity is not None and capacity > 0:
                        candidates.append((current, capacity))
            return candidates[0] if len(candidates) == 1 else None
        hsv = cv2.cvtColor(image[37:88, 1100:1288], cv2.COLOR_BGR2HSV)
        cleaned_images = []
        for saturation, brightness in ((65, 150), (90, 170)):
            mask = cv2.inRange(hsv, (0, 0, brightness), (179, saturation, 255))
            # 满仓后的库存是红字，容量仍是白字；同一行一起读取，
            # 否则只剩容量，原图检测又常把库存和容量拆成两段。
            mask |= cv2.inRange(hsv, (0, 100, brightness), (10, 255, 255))
            mask |= cv2.inRange(hsv, (170, 100, brightness), (179, 255, 255))
            cleaned_images.append(cv2.cvtColor(255-mask, cv2.COLOR_GRAY2BGR))
        # 两种独立滤色的完整库存/容量必须一致；不完整、低置信度或冲突走原检测兜底。
        if callable(getattr(self, "ocr", None)):
            direct = []
            for cleaned in cleaned_images:
                label = self.read_line(cleaned)
                direct.append(reading([Text(label, 1, 0, 0, (0,0,0,0))]) if label else None)
            if direct[0] is not None and direct[0] == direct[1]:
                return direct[0][0]
        results = []
        for cleaned in cleaned_images:
            cleaned = cv2.copyMakeBorder(cleaned, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=(255, 255, 255))
            results.append(reading(self.read(cleaned, (0, 0, cleaned.shape[1], cleaned.shape[0]))))
        if all(value is not None for value in results):
            return results[0][0] if results[0] == results[1] else None
        # 满仓红字等样式仍需两个原图完整读数一致；不能从带玩家名前缀的残缺数字取值。
        raw = [reading(self.read(image, rect)) for rect in ((1100, 35, 1288, 87), (1050, 32, 1290, 93))]
        return raw[0][0] if raw[0] is not None and raw[0] == raw[1] else None

    def map_coordinates(self, image, rect=(630, 550, 1290, 825)) -> tuple[int, int] | None:
        values = []
        for t in self.read(image, rect):
            found = re.search(r"[（(\[]?\s*(\d{1,4})\s*[,，·.]\s*(\d{1,4})\s*[）)\]]?", t.text)
            if found:
                values.append((int(found[1]), int(found[2])))
        return values[0] if len(set(values)) == 1 else None

    def auto_pave(self, image) -> dict | None:
        """攻占页的自动铺路标记：格数、整条链耗时、体力是否不消耗；普通攻占页返回 None。"""
        # 标签左右位置会随“编队”等按钮和地图宽度变化，用整行宽区域。
        if not self.find(image, ["自动铺路"], (1280, 590, 1920, 670)):
            return None
        line = self.joined(image, (1280, 590, 1920, 670)).replace(" ", "")
        tiles = re.search(r"(\d{1,2})格", line)
        cost = self.joined(image, (1450, 690, 1920, 800)).replace(" ", "")
        clock = re.search(r"(\d{1,2}):(\d{2}):(\d{2})", cost)
        stamina = self.joined(image, (1450, 690, 1920, 760)).replace(" ", "")
        seconds = None
        if clock:
            seconds = int(clock[1]) * 3600 + int(clock[2]) * 60 + int(clock[3])
        return {
            "tiles": int(tiles[1]) if tiles else None,
            "seconds": seconds,
            "free_stamina": "不消耗" in stamina,
        }

    def top_slots(self, image) -> dict:
        """顶栏“领地 x/y”“道路 x/y”；铺路前用道路额度判断是否还允许继续。"""
        text = self.joined(image, (280, 0, 1400, 115)).replace(" ", "")
        slots = {}
        for key, label in (("land", "领地"), ("road", "道路")):
            match = re.search(label + r"(\d{1,3})/(\d{1,3})", text)
            if match:
                slots[key] = (int(match[1]), int(match[2]))
        return slots

    def deployment_status(self, image, slot: int, center_x=None) -> str:
        # 少队时卡片整组居中；调用方给 center_x 就按卡片左边缘读，没给才退回旧五列。
        x = (round(center_x) - 125) if center_x is not None else 25 + (slot-1)*249
        value = self.joined(image, (x, 750, x+235, 821))
        if "重伤" in value:
            return "wounded"
        # 铺路/攻占过程中卡片会显示占领倒计时；队伍参战时会显示“战斗”。
        for label, state in (("集结", "outside"), ("战斗", "fighting"),
                             ("占领", "occupying"), ("返回", "returning"),
                             ("征兵", "recruiting"), ("驻守", "guarding"), ("行军", "marching"),
                             ("调动", "outside"), ("停留", "outside"), ("空闲", "home")):
            if label in value:
                return state
        return "unknown"

