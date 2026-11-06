from __future__ import annotations

import copy
import json
import math
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .team_states import can_dispatch, label as marker_label


ACTION_NAMES = {"attack": "攻城", "occupy": "攻占", "guard": "驻守", "march": "行军"}
STATES = {"home": "主城待命", "wounded": "重伤恢复", "marching": "行军", "returning": "返回", "guarding": "驻守", "active": "任务进行中", "recruiting": "征兵", "outside": "城外停留", "unknown": "待识别"}


def data_directory() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
        default = (base.parent if (base.parent / "release-manifest.json").is_file() else base) / "data"
    else:
        default = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SanmouAssistant"
    path = Path(os.environ.get("SANMOU_DATA_DIR", str(default)))
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # MSIX 文件虚拟化可把 C 盘 AppData 映射到 D 盘；临时文件和目标必须使用同一真实目录。
    path = path.parent.resolve()/path.name
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@dataclass
class Point:
    name: str = "新地点"
    x: int = 0
    y: int = 0
    group: str = "默认"
    actions: list[str] = field(default_factory=lambda: ["attack", "occupy", "guard"])
    priority: int = 1
    enabled: bool = True
    expected_name: str = ""
    capacity: int = 0


# 每支队伍只有一个任务角色；隐藏=不参与任何任务，也不被读取。
TEAM_ROLES = ("merit", "bandits", "pave", "idle", "hidden")
TEAM_ROLE_LABELS = {"merit": "刷武勋", "bandits": "讨贼", "pave": "铺路", "idle": "空闲", "hidden": "隐藏（不参与）"}


@dataclass
class TeamPolicy:
    slot: int = 1
    group: str = "main"
    label: str = ""
    # 唯一任务角色：刷武勋 / 讨贼 / 铺路 / 空闲 / 隐藏。
    role: str = "idle"
    # 隐藏前的角色，取消隐藏时还原；只记录非隐藏角色。
    saved_role: str = ""
    min_mode: str = "percent"
    minimum: int = 90
    stamina: int = 40
    supply: int = 80
    target_group: str = "默认"
    # 本队专用补兵点，按列表顺序尝试；为空时使用全部已启用补兵点。
    backup_points: list = field(default_factory=list)
    # role == "pave" 时使用的路线名称。
    route: str = ""
    identity: str = ""

    @property
    def key(self) -> str:
        return f"{self.group}:{self.slot}"

    @property
    def display(self) -> str:
        return self.label or f"{'主城' if self.group == 'main' else '亲卫'} {self.slot} 队"

    @property
    def participating(self) -> bool:
        """是否会参与派兵或铺路；隐藏和空闲都不参与。"""
        return self.role in ("merit", "bandits", "pave")

    def threshold(self, maximum: int) -> int:
        return math.ceil(maximum * self.minimum / 100) if self.min_mode == "percent" else self.minimum


@dataclass
class PaveRoute:
    """一条铺路路线：按顺序把队伍铺到各个目标坐标。

    points 每项为 [x, y] 或 [x, y, 名称]；名称只用于显示，不参与坐标核对。
    """
    name: str = "铺路路线"
    team: str = "main:2"
    points: list = field(default_factory=list)
    enabled: bool = False
    resume_after_return: bool = False
    replenish: str = "keep"


def pave_target(point) -> tuple[int, int]:
    return int(point[0]), int(point[1])


def pave_label(point) -> str:
    name = point[2].strip() if len(point) > 2 and isinstance(point[2], str) else ""
    return f"{name}({point[0]},{point[1]})" if name else f"({point[0]},{point[1]})"


def pave_summary(points) -> str:
    return " → ".join(pave_label(point) for point in points)


@dataclass
class Profile:
    schema: int = 1
    name: str = "我的配置"
    account: str = ""
    main_city: Optional[Point] = None
    teams: list[TeamPolicy] = field(default_factory=lambda: [TeamPolicy(slot=i) for i in range(1, 6)] + [TeamPolicy(slot=i, group="guard") for i in range(1, 4)])
    targets: list[Point] = field(default_factory=list)
    backups: list[Point] = field(default_factory=list)
    pave_routes: list[PaveRoute] = field(default_factory=list)
    cli_path: str = ""
    instance: str = "0"
    package: str = "com.bilibili.nslg.mi"
    poll_seconds: int = 30
    retry_seconds: int = 180
    backup_enabled: bool = False
    food_enabled: bool = False
    # 启动时把 MuMu 设备窗口压到最小化；截图/点击全部走 Android 后台输入。
    minimize_emulator: bool = True
    # 用户 2026-11-04：默认触发库存 50 万、恢复库存 100 万；粮仓容量 600 万，不会溢出。
    food_low: int = 500000
    food_recover: int = 1000000
    farming_budget: int = 3
    dailies: dict = field(default_factory=lambda: {"monthly": True, "signin": False, "events": False, "drill": True, "alliance_rewards": False, "visit": False, "half_price": False, "bandits": False, "career": False})
    daily_interval: int = 900
    bandit_interval: int = 180
    bandit_allow_assist: bool = False
    # 开始执行任务前，打开游戏里的“隐藏”显示（小眼睛带斜杠）。游戏默认就是开启，这里只做兜底。
    hide_before_run: bool = True

    def validate(self, active: bool = False) -> list[str]:
        errors = []
        if self.schema != 1:
            errors.append("不支持的配置版本")
        if not self.name.strip() or len(self.name) > 80:
            errors.append("配置名称须为 1～80 个字符")
        if not isinstance(self.package, str) or not self.package or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._" for c in self.package):
            errors.append("游戏包名格式不正确")
        if not str(self.instance).isdigit():
            errors.append("请选择 MuMu 实例编号")
        if not 15 <= self.poll_seconds <= 3600 or not 30 <= self.retry_seconds <= 7200:
            errors.append("监控间隔须为 15～3600 秒，重试间隔须为 30～7200 秒")
        if not 30 <= self.bandit_interval <= 3600:
            errors.append("讨贼检查间隔须为 30～3600 秒")
        if type(self.bandit_allow_assist) is not bool:
            errors.append("次数用完后协助讨贼必须是开关")
        if self.food_low < 0 or self.food_recover <= self.food_low or not 0 <= self.farming_budget <= 30:
            errors.append("补粮恢复库存应高于触发库存，单周期屯田预算须为 0～30 次")
        route_names = [r.name for r in self.pave_routes]
        route_set = set(route_names)
        backup_names = {p.name for p in self.backups}
        keys = []
        pave_used = []
        for t in self.teams:
            keys.append(t.key)
            if t.group not in ("main", "guard") or not 1 <= t.slot <= (5 if t.group == "main" else 3):
                errors.append("队伍分组或槽位无效")
            if t.role not in TEAM_ROLES:
                errors.append(f"{t.display} 的任务无效")
            if t.saved_role and t.saved_role not in ("merit", "bandits", "pave", "idle"):
                errors.append(f"{t.display} 的隐藏前任务无效")
            if t.min_mode not in ("percent", "absolute") or not 1 <= t.minimum <= (100 if t.min_mode == "percent" else 1000000):
                errors.append(f"{t.display} 的兵力下限无效")
            if not 0 <= t.stamina <= 200 or not 0 <= t.supply <= 100:
                errors.append(f"{t.display} 的体力或补给无效")
            if active and t.role in ("merit", "bandits") and t.group == "guard":
                errors.append(f"{t.display} 的亲卫自动出征尚未实机验收，请先设为空闲")
            if t.role == "pave":
                if t.group != "main":
                    errors.append(f"{t.display} 的铺路只能由主城队伍执行")
                if not t.route:
                    errors.append(f"{t.display} 已设为铺路，但还没有选择路线")
                elif t.route not in route_set:
                    errors.append(f"{t.display} 选择的铺路路线不存在")
                else:
                    pave_used.append(t.route)
            elif t.route:
                errors.append(f"{t.display} 只有铺路任务才能指定路线")
            if t.role == "merit" and active and not any(p.enabled and p.group == t.target_group for p in self.targets):
                errors.append(f"{t.display} 未配置启用的武勋目标")
            for name in t.backup_points:
                if name not in backup_names:
                    errors.append(f"{t.display} 指定的补兵点 {name!r} 不存在")
        if len(keys) != len(set(keys)):
            errors.append("存在重复队伍槽位")
        if len(pave_used) != len(set(pave_used)):
            errors.append("同一条铺路路线只能分配给一支队伍")
        for points in (self.targets, self.backups, [self.main_city] if self.main_city else []):
            names = []
            for p in points:
                names.append(p.name)
                if not p.name.strip() or not 0 <= p.x <= 3000 or not 0 <= p.y <= 3000 or not 1 <= p.priority <= 100:
                    errors.append(f"地点 {p.name!r} 的名称、坐标或优先级无效")
                if not p.actions or any(a not in ACTION_NAMES for a in p.actions):
                    errors.append(f"地点 {p.name!r} 的允许动作无效")
            if len(names) != len(set(names)):
                errors.append("地点名称重复，请为每个地点取不同的名称")
        team_keys = {t.key for t in self.teams}
        route_names = []
        for route in self.pave_routes:
            route_names.append(route.name)
            if not route.name.strip() or len(route.name) > 40:
                errors.append("铺路路线名称须为 1～40 个字符")
            if route.team and route.team not in team_keys:
                errors.append(f"铺路路线 {route.name!r} 的队伍不存在")
            if route.team and not route.team.startswith("main:"):
                errors.append(f"铺路路线 {route.name!r} 只能使用主城队伍，亲卫尚不支持铺路")
            # enabled 仅为旧配置兼容字段；是否执行只看有没有队伍选中这条路线。
            if not isinstance(route.enabled, bool):
                errors.append(f"铺路路线 {route.name!r} 的启用状态无效")
            if type(route.resume_after_return) is not bool or route.replenish not in ("keep", "auto", "supply"):
                errors.append(f"铺路路线 {route.name!r} 的恢复或补兵策略无效")
            if route.team and not 1 <= len(route.points) <= 30:
                errors.append(f"铺路路线 {route.name!r} 已分配给队伍，需要 1～30 个坐标")
            elif len(route.points) > 30:
                errors.append(f"铺路路线 {route.name!r} 最多 30 个坐标")
            for point in route.points:
                if (not isinstance(point, (list, tuple)) or not 2 <= len(point) <= 3
                        or any(type(point[i]) is not int or not 0 <= point[i] <= 3000 for i in (0, 1))
                        or (len(point) == 3 and not isinstance(point[2], str))):
                    errors.append(f"铺路路线 {route.name!r} 含无效坐标")
                    break
        if len(route_names) != len(set(route_names)):
            errors.append("铺路路线名称重复")
        if active and any(t.participating for t in self.teams) and (not self.main_city or not self.account):
            errors.append("请先在游戏主城旁完成账号与主城校验")
        return list(dict.fromkeys(errors))

    def normalize(self):
        """把队伍的铺路路线归属同步到路线，并让讨贼日常开关跟随队伍任务。

        路线自身只保留“由哪支队执行”的存储字段，用户界面永远以队伍的任务为准，
        避免两个页面各写一份归属。
        """
        owners = {}
        for team in self.teams:
            if team.role == "pave" and team.route:
                owners[team.route] = team.key
        for route in self.pave_routes:
            route.team = owners.get(route.name, "")
            route.enabled = bool(route.team)
        self.dailies["bandits"] = any(t.role == "bandits" for t in self.teams)
        return self

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "Profile":
        if not isinstance(value, dict) or value.get("schema") != 1:
            raise ValueError("配置文件格式或版本不正确")
        data = copy.deepcopy(value)
        # 0.1.4 曾提供地图农田地址；城内屯田不使用游戏地图坐标。
        data.pop("grain_point", None)
        # 固定农田点击无法跨布局使用；按当前开垦列表自动发现。
        data.pop("grain_field", None)
        # 0.7.2 曾叫 close_hide_before_run（任务前关闭隐藏）；0.7.4 反转为任务前打开隐藏。
        if "close_hide_before_run" in data:
            data.setdefault("hide_before_run", data.pop("close_hide_before_run"))
        allowed = set(cls.__dataclass_fields__)
        if set(data) - allowed:
            raise ValueError("配置包含未知字段")
        try:
            for name in ("poll_seconds", "retry_seconds", "food_low", "food_recover", "farming_budget", "daily_interval", "bandit_interval"):
                if name in data and type(data[name]) is not int:
                    raise ValueError(f"{name} 必须是整数")
            for name in ("backup_enabled", "food_enabled", "minimize_emulator", "bandit_allow_assist", "hide_before_run"):
                if name in data and type(data[name]) is not bool:
                    raise ValueError(f"{name} 必须是开关")
            # 旧配置的 merit/bandits/priority/backup 只做一次性兜底：转换为单一任务角色。
            legacy_owner = {}
            for route in data.get("pave_routes", []):
                if isinstance(route, dict) and route.get("team") and route.get("enabled"):
                    legacy_owner.setdefault(route["team"], route.get("name", ""))
            for team in data.get("teams", []):
                if not isinstance(team, dict):
                    continue
                if "role" not in team:
                    merit = bool(team.pop("merit", False))
                    bandits = bool(team.pop("bandits", False))
                    team["role"] = "merit" if merit else "bandits" if bandits else "idle"
                else:
                    team.pop("merit", None)
                    team.pop("bandits", None)
                team.pop("priority", None)
                if "backup" in team:
                    backup = team.pop("backup")
                    team.setdefault("backup_points", [backup] if backup else [])
                if team.get("role") == "idle":
                    owner = legacy_owner.get(f"{team.get('group', 'main')}:{team.get('slot', 1)}")
                    if owner:
                        team["role"], team["route"] = "pave", owner
            for team in data.get("teams", []):
                for name in ("slot", "minimum", "stamina", "supply"):
                    if name in team and type(team[name]) is not int:
                        raise ValueError(f"队伍 {name} 必须是整数")
                if "role" in team and not isinstance(team["role"], str):
                    raise ValueError("队伍任务必须是文字")
                if "backup_points" in team and not isinstance(team["backup_points"], list):
                    raise ValueError("队伍补兵点必须是列表")
            for point in data.get("targets", []) + data.get("backups", []) + ([data["main_city"]] if data.get("main_city") else []):
                for name in ("x", "y", "priority", "capacity"):
                    if name in point and type(point[name]) is not int:
                        raise ValueError(f"地点 {name} 必须是整数")
                if "enabled" in point and type(point["enabled"]) is not bool:
                    raise ValueError("地点启用状态必须是开关")
            if not isinstance(data.get("dailies", {}), dict) or any(type(v) is not bool for v in data.get("dailies", {}).values()):
                raise ValueError("日常设置必须是开关")
            for route in data.get("pave_routes", []):
                if not isinstance(route, dict):
                    raise ValueError("铺路路线格式不正确")
                if "enabled" in route and type(route["enabled"]) is not bool:
                    raise ValueError("铺路路线启用状态必须是开关")
            data["teams"] = [TeamPolicy(**t) for t in data.get("teams", [])]
            data["targets"] = [Point(**t) for t in data.get("targets", [])]
            data["backups"] = [Point(**t) for t in data.get("backups", [])]
            data["pave_routes"] = [PaveRoute(**r) for r in data.get("pave_routes", [])]
            data["main_city"] = Point(**data["main_city"]) if data.get("main_city") else None
            obj = cls(**data)
            obj.normalize()
            errors = obj.validate()
        except (TypeError, KeyError, IndexError, AttributeError) as exc:
            raise ValueError("配置字段类型不正确") from exc
        if errors:
            raise ValueError("；".join(errors))
        return obj


@dataclass
class Observation:
    key: str
    observed_at: float = field(default_factory=time.time)
    state: str = "unknown"
    strength: Optional[int] = None
    maximum: Optional[int] = None
    stamina: Optional[int] = None
    supply: Optional[int] = None
    wounded: Optional[bool] = None
    injury_seconds: Optional[int] = None
    auto_refill: Optional[bool] = None
    pool: str = "未知"
    reserve: Optional[int] = None
    capacity: Optional[int] = None
    identity: str = ""
    location: Optional[tuple[int, int]] = None
    note: str = ""
    # 单帧角标读出的真实状态（用户校准）；与 state 分开保存，state 仍按旧词表驱动调度。
    marker_state: Optional[str] = None
    marker_label: Optional[str] = None
    marker_confidence: Optional[float] = None
    marker_at: Optional[float] = None


@dataclass
class Decision:
    action: str = "wait"
    reason: str = "等待读取"
    target: Optional[Point] = None


def observation_expired(profile: Profile, obs: Observation, now: Optional[float] = None) -> bool:
    return (time.time() if now is None else now) - obs.observed_at > max(120,profile.poll_seconds*2)


def decide(profile: Profile, policy: TeamPolicy, obs: Observation, grain: Optional[int], phase: str = "", now: Optional[float] = None) -> Decision:
    if policy.role == "bandits":
        return bandit_decision(profile,policy,obs,grain,now)
    if policy.role == "pave":
        return Decision(reason="铺路任务，由铺路路线推进")
    if policy.role != "merit":
        return Decision(reason="空闲，未安排任务")
    # 角标是当前状态的第一证据：停留/空闲/调动停留才派出；忙碌、未识别、在路上/驻守/战斗一律等现场。
    marker = getattr(obs, "marker_state", None)
    if marker == "unknown":
        return Decision(reason="角标状态未识别，等下一次读取再派兵")
    if marker is not None and not can_dispatch(marker):
        return Decision(reason=f"角标显示{marker_label(marker)}，本轮不派兵")
    if obs.state in ("marching", "returning", "guarding", "active", "fighting", "occupying", "paving"):
        return Decision(reason=STATES.get(obs.state, "任务进行中") + "，继续监控")
    if obs.wounded:
        return Decision(reason="等待重伤结束" + (f"（约 {obs.injury_seconds} 秒）" if obs.injury_seconds is not None else ""))
    if policy.identity and obs.identity and obs.identity != policy.identity:
        return Decision(reason="阵容与保存的校验信息不同，请重新确认队伍")
    if phase == "backup" and obs.state in ("recruiting", "outside"):
        # 用户 2026-11-04：自然征兵只要兵力达到本队出击下限就回城，不要求征到满、
        # 也不读征兵剩余时间；回主城以后才允许再拉线刷武勋。
        if obs.strength is not None and obs.maximum is not None and obs.strength >= policy.threshold(obs.maximum):
            return Decision("return", f"城外兵力 {obs.strength}/{obs.maximum} 已达出击下限，返回主城")
        return Decision(reason="城外自然征兵中，等兵力达到出击下限再回城")
    if obs.state not in ("home", "outside"):
        return Decision(reason="队伍尚未确认空闲，保留当前任务")
    if profile.food_enabled and grain is not None and grain < profile.food_low:
        return Decision(reason="全局粮食库存低于下限，等待补粮")
    if obs.strength is not None and obs.maximum is not None and obs.strength < policy.threshold(obs.maximum):
        order = {name: i for i, name in enumerate(policy.backup_points)}
        backups = sorted([p for p in profile.backups if p.enabled and (not order or p.name in order)],
                         key=lambda p: (order.get(p.name, 10 ** 6), p.priority))
        if profile.backup_enabled and backups and grain is not None and grain >= profile.food_low:
            return Decision("transfer", "兵力不足，调动到备用地点征满", backups[0])
        return Decision(reason=f"兵力 {obs.strength} / 出击下限 {policy.threshold(obs.maximum)}，等待补兵")
    if obs.state == "home" and obs.auto_refill is False:
        return Decision(reason="主城回城自动补兵显示关闭，保留游戏设置")
    if obs.stamina is not None and obs.stamina < policy.stamina:
        return Decision(reason="等待体力达到设置下限")
    if obs.supply is not None and obs.supply < policy.supply:
        return Decision(reason="等待补给达到设置下限")
    targets = sorted([p for p in profile.targets if p.enabled and p.group == policy.target_group], key=lambda p: p.priority)
    return Decision("merit", "当前队伍可尝试出击", targets[0]) if targets else Decision(reason="没有启用的战场目标")


def bandit_decision(profile: Profile, policy: TeamPolicy, obs: Observation, grain: Optional[int], now: Optional[float] = None) -> Decision:
    if policy.role != "bandits" or policy.group != "main":
        return Decision(reason="该队未启用可执行的讨贼")
    # 用户 2026-11-02：停留在外面或调动到营帐里，同样可以拉出去；忙碌/未识别不派。
    marker = getattr(obs, "marker_state", None)
    if marker is not None:
        if marker == "unknown":
            return Decision(reason="讨贼：角标状态未识别，等下一次读取")
        if not can_dispatch(marker):
            return Decision(reason=f"讨贼：角标显示{marker_label(marker)}，暂不派出")
    elif obs.state != "home":
        return Decision(reason="讨贼：队伍未确认在主城空闲")
    if obs.wounded:
        return Decision(reason="讨贼：等待重伤结束")
    if policy.identity and obs.identity and policy.identity != obs.identity:
        return Decision(reason="讨贼：阵容校验不符")
    if obs.strength is not None and obs.maximum is not None and obs.strength < policy.threshold(obs.maximum):
        # 用户 2026-11-04：讨贼队不自动补兵、不越权改游戏设置；兵力不达标就跳过，
        # 但日志必须说清是"游戏内自动补兵没开"，不能让使用者以为脚本坏了。
        if obs.auto_refill is False:
            return Decision(reason=f"讨贼：兵力 {obs.strength}/{obs.maximum} 低于下限 "
                                   f"{policy.threshold(obs.maximum)}，游戏内主城自动补兵未开启")
        return Decision(reason=f"讨贼：兵力低于下限 {policy.threshold(obs.maximum)}")
    if obs.stamina is not None and obs.stamina < policy.stamina:
        return Decision(reason="讨贼：等待体力")
    if obs.supply is not None and obs.supply < policy.supply:
        return Decision(reason="讨贼：等待补给")
    if profile.food_enabled and grain is not None and grain < profile.food_low:
        return Decision(reason="讨贼：等待补粮")
    return Decision("bandits","讨贼：队伍在主城，等可加入集结")

