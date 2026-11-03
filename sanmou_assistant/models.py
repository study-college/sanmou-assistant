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


@dataclass
class TeamPolicy:
    slot: int = 1
    group: str = "main"
    label: str = ""
    merit: bool = False
    bandits: bool = False
    min_mode: str = "percent"
    minimum: int = 90
    stamina: int = 40
    supply: int = 80
    target_group: str = "默认"
    backup: str = ""
    identity: str = ""
    priority: str = "merit"

    @property
    def key(self) -> str:
        return f"{self.group}:{self.slot}"

    @property
    def display(self) -> str:
        return self.label or f"{'主城' if self.group == 'main' else '亲卫'} {self.slot} 队"

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
    food_low: int = 500000
    food_recover: int = 1500000
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
        keys = []
        for t in self.teams:
            keys.append(t.key)
            if t.group not in ("main", "guard") or not 1 <= t.slot <= (5 if t.group == "main" else 3):
                errors.append("队伍分组或槽位无效")
            if t.min_mode not in ("percent", "absolute") or not 1 <= t.minimum <= (100 if t.min_mode == "percent" else 1000000):
                errors.append(f"{t.display} 的兵力下限无效")
            if not 0 <= t.stamina <= 200 or not 0 <= t.supply <= 100 or t.priority not in ("merit", "bandits"):
                errors.append(f"{t.display} 的体力、补给或任务优先级无效")
            if active and (t.merit or t.bandits) and t.group == "guard":
                errors.append(f"{t.display} 的亲卫自动出征尚未实机验收，请先关闭")
            if active and t.merit and not any(p.enabled and p.group == t.target_group for p in self.targets):
                errors.append(f"{t.display} 未配置启用的武勋目标")
            if t.backup and not any(p.name == t.backup for p in self.backups):
                errors.append(f"{t.display} 指定的备用补兵点不存在")
        if len(keys) != len(set(keys)):
            errors.append("存在重复队伍槽位")
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
            if route.team not in team_keys:
                errors.append(f"铺路路线 {route.name!r} 的队伍不存在")
            if route.enabled and not route.team.startswith("main:"):
                errors.append(f"铺路路线 {route.name!r} 只能使用主城队伍，亲卫尚不支持铺路")
            if not isinstance(route.enabled, bool):
                errors.append(f"铺路路线 {route.name!r} 的启用状态无效")
            if type(route.resume_after_return) is not bool or route.replenish not in ("keep", "auto", "supply"):
                errors.append(f"铺路路线 {route.name!r} 的恢复或补兵策略无效")
            if not 1 <= len(route.points) <= 30:
                errors.append(f"铺路路线 {route.name!r} 需要 1～30 个坐标")
            for point in route.points:
                if (not isinstance(point, (list, tuple)) or not 2 <= len(point) <= 3
                        or any(type(point[i]) is not int or not 0 <= point[i] <= 3000 for i in (0, 1))
                        or (len(point) == 3 and not isinstance(point[2], str))):
                    errors.append(f"铺路路线 {route.name!r} 含无效坐标")
                    break
        if len(route_names) != len(set(route_names)):
            errors.append("铺路路线名称重复")
        enabled_teams = [r.team for r in self.pave_routes if r.enabled]
        if len(enabled_teams) != len(set(enabled_teams)):
            errors.append("同一支队伍不能同时启用多条铺路路线")
        if active and (any(t.merit or t.bandits for t in self.teams) or any(r.enabled for r in self.pave_routes)) and (not self.main_city or not self.account):
            errors.append("请先在游戏主城旁完成账号与主城校验")
        return list(dict.fromkeys(errors))

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
            for team in data.get("teams", []):
                for name in ("slot", "minimum", "stamina", "supply"):
                    if name in team and type(team[name]) is not int:
                        raise ValueError(f"队伍 {name} 必须是整数")
                for name in ("merit", "bandits"):
                    if name in team and type(team[name]) is not bool:
                        raise ValueError(f"队伍 {name} 必须是开关")
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
    if not policy.merit:
        return bandit_decision(profile,policy,obs,grain,now) if policy.bandits else Decision(reason="武勋未启用")
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
        if obs.strength is not None and obs.maximum is not None and obs.strength == obs.maximum:
            return Decision("return", "城外已满兵，返回主城")
        return Decision(reason="城外普通征兵，等真实兵力征满")
    if obs.state not in ("home", "outside"):
        return Decision(reason="队伍尚未确认空闲，保留当前任务")
    if profile.food_enabled and grain is not None and grain < profile.food_low:
        return Decision(reason="全局粮食库存低于下限，等待补粮")
    if obs.strength is not None and obs.maximum is not None and obs.strength < policy.threshold(obs.maximum):
        backups = sorted([p for p in profile.backups if p.enabled and (not policy.backup or p.name == policy.backup)], key=lambda p: p.priority)
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
    if not policy.bandits or policy.group != "main":
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
        return Decision(reason=f"讨贼：兵力低于下限 {policy.threshold(obs.maximum)}")
    if obs.stamina is not None and obs.stamina < policy.stamina:
        return Decision(reason="讨贼：等待体力")
    if obs.supply is not None and obs.supply < policy.supply:
        return Decision(reason="讨贼：等待补给")
    if profile.food_enabled and grain is not None and grain < profile.food_low:
        return Decision(reason="讨贼：等待补粮")
    return Decision("bandits","讨贼：队伍在主城，等可加入集结")

