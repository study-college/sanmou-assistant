"""Pure target & backup point operations shared by GUI, RuntimeHub, and CLI."""
from __future__ import annotations

import copy
from dataclasses import asdict
from typing import Iterable, Optional

from .locations import single_address
from .models import ACTION_NAMES, Point, Profile, point_label

LIVE_TARGET_MODES = ("merit", "line")
ACTION_LABEL_TO_KEY = {label: key for key, label in ACTION_NAMES.items()}
DEFAULT_MERIT_ACTIONS = ["attack", "occupy", "guard"]
DEFAULT_LINE_ACTIONS = ["guard", "attack", "march"]


def parse_action_list(actions: str | Iterable[str] | None,
                      default: list[str] | None = None) -> list[str]:
    """解析指令顺序，支持英文键（guard/attack/occupy/march）和中文名（驻守/攻城/攻占/行军）。"""
    if actions is None:
        if default:
            return list(default)
        raise ValueError("请指定至少一个有效指令（attack/occupy/guard/march）")
    if isinstance(actions, str):
        raw = [part.strip() for part in actions.replace("，", ",").replace("、", ",").replace("->", ",").replace("→", ",").split(",")]
        items = [part for part in raw if part]
    else:
        items = [str(part).strip() for part in actions if str(part).strip()]
    if not items:
        if default:
            return list(default)
        raise ValueError("指令顺序不能为空")
    result: list[str] = []
    for item in items:
        key = item if item in ACTION_NAMES else ACTION_LABEL_TO_KEY.get(item)
        if not key:
            allowed = "、".join(f"{k}({v})" for k, v in ACTION_NAMES.items())
            raise ValueError(f"不支持的指令 {item!r}；仅支持：{allowed}")
        if key not in result:
            result.append(key)
    return result


def resolve_coord_input(*, address: str = "", name: str = "",
                        x: Optional[int] = None, y: Optional[int] = None,
                        fallback_prefix: str = "目标") -> tuple[str, int, int, str]:
    """解析分享地址字符串或显式坐标，返回 (name, x, y, expected_name)。"""
    text = (address or "").strip()
    if text:
        parsed = single_address(text)
        resolved_name = (name or "").strip() or parsed.name or f"{fallback_prefix} ({parsed.x},{parsed.y})"
        return resolved_name, parsed.x, parsed.y, parsed.name
    if x is None or y is None:
        raise ValueError("请提供游戏分享地址（例如 涪县(300,300)），或同时提供 x 与 y 坐标")
    if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, int) or not isinstance(y, int):
        raise ValueError("坐标 x 和 y 必须是整数")
    if not (0 <= x <= 3000 and 0 <= y <= 3000):
        raise ValueError(f"坐标 ({x},{y}) 超出 0～3000 范围")
    clean_name = (name or "").strip()
    resolved_name = clean_name or f"{fallback_prefix} ({x},{y})"
    return resolved_name, x, y, clean_name


def _unique_point_name(base_name: str, x: int, y: int, existing_names: set[str]) -> str:
    candidate = (base_name or "").strip() or f"目标 ({x},{y})"
    if candidate not in existing_names:
        return candidate
    with_coord = f"{candidate} ({x},{y})"
    if with_coord not in existing_names:
        return with_coord
    suffix = 2
    while f"{with_coord} #{suffix}" in existing_names:
        suffix += 1
    return f"{with_coord} #{suffix}"


def _point_list_attr(mode: str) -> str:
    if mode == "merit":
        return "targets"
    if mode == "line":
        return "line_targets"
    raise ValueError(f"不支持的目标模式 {mode!r}；仅支持 merit 或 line")


def _default_actions_for(mode: str, existing: list[Point]) -> list[str]:
    enabled = sorted([p for p in existing if p.enabled], key=lambda p: p.priority)
    if enabled and enabled[0].actions:
        return list(enabled[0].actions)
    if existing and existing[0].actions:
        return list(existing[0].actions)
    return list(DEFAULT_MERIT_ACTIONS if mode == "merit" else DEFAULT_LINE_ACTIONS)


def switch_primary_target(
    profile: Profile,
    *,
    mode: str,
    address: str = "",
    name: str = "",
    x: Optional[int] = None,
    y: Optional[int] = None,
    actions: str | Iterable[str] | None = None,
    keep_others: bool = False,
    active: bool = False,
) -> tuple[Profile, Point]:
    """一键切换武勋或拉线首选目标（纯函数，返回新 Profile 副本与生效的目标点）。

    - 若同坐标已存在，原地提至优先级 1 并启用（保留旧名/旧动作，显式传入时覆盖）。
    - 默认 keep_others=False：将其余历史目标置为停用但保留在列表中，方便随时切回。
    - keep_others=True：保持其余目标启用状态，仅将新目标置顶并启用。
    """
    attr = _point_list_attr(mode)
    prefix = "武勋目标" if mode == "merit" else "拉线目标"
    parsed_name, px, py, expected = resolve_coord_input(
        address=address, name=name, x=x, y=y, fallback_prefix=prefix)
    updated = copy.deepcopy(profile)
    points: list[Point] = list(getattr(updated, attr))
    parsed_actions = parse_action_list(actions) if actions is not None else None

    existing_index = next((i for i, p in enumerate(points) if p.x == px and p.y == py), None)
    others = [p for i, p in enumerate(points) if i != existing_index]
    other_names = {p.name for p in others}

    if existing_index is not None:
        target = copy.deepcopy(points[existing_index])
        if (name or "").strip() or expected:
            target.name = _unique_point_name(parsed_name, px, py, other_names)
            if expected:
                target.expected_name = expected
        if parsed_actions is not None:
            target.actions = parsed_actions
        target.enabled = True
    else:
        chosen_actions = parsed_actions if parsed_actions is not None else _default_actions_for(mode, points)
        unique_name = _unique_point_name(parsed_name, px, py, other_names)
        target = Point(
            name=unique_name,
            x=px,
            y=py,
            actions=chosen_actions,
            priority=1,
            enabled=True,
            expected_name=expected,
        )

    if not keep_others:
        for point in others:
            point.enabled = False

    reordered = [target] + others
    for idx, point in enumerate(reordered, 1):
        point.priority = idx
    setattr(updated, attr, reordered)
    updated.normalize()
    errors = updated.validate(active=active)
    if errors:
        raise ValueError("；".join(errors))
    return updated, reordered[0]


def _coerce_point(item: Point | dict, *, mode: str, index: int, existing_names: set[str]) -> Point:
    if isinstance(item, Point):
        point = copy.deepcopy(item)
        if not point.actions:
            point.actions = list(DEFAULT_MERIT_ACTIONS if mode == "merit" else DEFAULT_LINE_ACTIONS)
        point.name = _unique_point_name(point.name, point.x, point.y, existing_names)
        existing_names.add(point.name)
        return point
    if not isinstance(item, dict):
        raise ValueError("目标条目必须是对象")
    prefix = "武勋目标" if mode == "merit" else "拉线目标"
    parsed_name, px, py, expected = resolve_coord_input(
        address=str(item.get("address") or ""),
        name=str(item.get("name") or ""),
        x=item.get("x"),
        y=item.get("y"),
        fallback_prefix=prefix,
    )
    default_actions = DEFAULT_MERIT_ACTIONS if mode == "merit" else DEFAULT_LINE_ACTIONS
    actions = parse_action_list(item.get("actions"), default=default_actions)
    enabled = item.get("enabled", True)
    if type(enabled) is not bool:
        raise ValueError("目标 enabled 必须是布尔值")
    priority = item.get("priority", index)
    if type(priority) is not int or not 1 <= priority <= 100:
        raise ValueError("目标 priority 必须是 1～100 的整数")
    unique_name = _unique_point_name(parsed_name, px, py, existing_names)
    existing_names.add(unique_name)
    return Point(
        name=unique_name,
        x=px,
        y=py,
        group=str(item.get("group") or "默认"),
        actions=actions,
        priority=priority,
        enabled=enabled,
        expected_name=str(item.get("expected_name") or expected),
        capacity=int(item.get("capacity") or 0),
    )


def replace_targets(
    profile: Profile,
    *,
    mode: str,
    points: list[Point | dict],
    active: bool = False,
) -> Profile:
    """整体替换武勋目标表或拉线目标表，自动规范优先级与校验。"""
    attr = _point_list_attr(mode)
    if not isinstance(points, list):
        raise ValueError("目标列表必须是数组")
    updated = copy.deepcopy(profile)
    seen_names: set[str] = set()
    coerced = [_coerce_point(item, mode=mode, index=i, existing_names=seen_names)
               for i, item in enumerate(points, 1)]
    setattr(updated, attr, coerced)
    updated.normalize()
    errors = updated.validate(active=active)
    if errors:
        raise ValueError("；".join(errors))
    return updated


def set_backup_point(
    profile: Profile,
    *,
    mode: str,
    team_key: str = "",
    address: str = "",
    name: str = "",
    x: Optional[int] = None,
    y: Optional[int] = None,
    clear: bool = False,
    enabled: Optional[bool] = None,
    active: bool = False,
) -> Profile:
    """更新武勋备用征兵点或拉线按队征兵点。"""
    if mode not in LIVE_TARGET_MODES:
        raise ValueError(f"不支持的征兵点模式 {mode!r}；仅支持 merit 或 line")
    updated = copy.deepcopy(profile)
    if mode == "merit":
        if enabled is not None:
            if type(enabled) is not bool:
                raise ValueError("backup_enabled 必须是布尔值")
            updated.backup_enabled = enabled
        if clear:
            updated.merit_backup = None
        elif address or x is not None or y is not None:
            parsed_name, px, py, expected = resolve_coord_input(
                address=address, name=name, x=x, y=y, fallback_prefix="备用征兵点")
            existing = updated.merit_backup
            if (existing is not None and existing.x == px and existing.y == py
                    and (not expected or expected in (existing.name, existing.expected_name))
                    and not (name or "").strip()):
                updated.merit_backup = existing
            else:
                updated.merit_backup = Point(
                    name=parsed_name, x=px, y=py, expected_name=expected, enabled=True)
        elif enabled is None:
            raise ValueError("请提供征兵点地址/坐标，或指定 clear=True / enabled 开关")
    else:
        key = (team_key or "").strip()
        if key == "all":
            target_keys = [t.key for t in updated.teams if t.group == "main" and t.role == "line"]
            if not target_keys:
                raise ValueError("当前没有配置为拉线的队伍，请指定具体队伍键（如 main:1）")
        else:
            valid_keys = {t.key for t in updated.teams if t.group == "main"}
            if key not in valid_keys:
                raise ValueError(f"请指定有效的主城队伍键（例如 main:1～main:5），收到：{key!r}")
            target_keys = [key]
        if clear:
            for k in target_keys:
                updated.line_backups.pop(k, None)
        else:
            parsed_name, px, py, expected = resolve_coord_input(
                address=address, name=name, x=x, y=y, fallback_prefix="拉线征兵点")
            for k in target_keys:
                existing = updated.line_backups.get(k)
                if (existing is not None and existing.x == px and existing.y == py
                        and (not expected or expected in (existing.name, existing.expected_name))
                        and not (name or "").strip()):
                    updated.line_backups[k] = existing
                else:
                    updated.line_backups[k] = Point(
                        name=parsed_name, x=px, y=py, expected_name=expected, enabled=True)
    updated.normalize()
    errors = updated.validate(active=active)
    if errors:
        raise ValueError("；".join(errors))
    return updated


def merge_live_target_fields(
    base: Profile,
    updated: Profile,
    *,
    active: bool = False,
) -> tuple[Profile, set[str]]:
    """只将允许运行中热更新的白名单字段合并到 base，返回 (新 Profile, 变更模块集合)。

    白名单：targets、line_targets、backup_enabled、merit_backup、line_backups。
    不碰实例、包名、账号、主城、队伍角色、铺路路线等需停机生效的字段。
    """
    merged = copy.deepcopy(base)
    before_merit = (
        [asdict(p) for p in merged.targets],
        bool(merged.backup_enabled),
        asdict(merged.merit_backup) if merged.merit_backup is not None else None,
    )
    before_line = (
        [asdict(p) for p in merged.line_targets],
        {k: asdict(v) for k, v in sorted(merged.line_backups.items())},
    )

    merged.targets = copy.deepcopy(updated.targets)
    merged.line_targets = copy.deepcopy(updated.line_targets)
    merged.backup_enabled = bool(updated.backup_enabled)
    merged.merit_backup = copy.deepcopy(updated.merit_backup)
    merged.line_backups = copy.deepcopy(updated.line_backups)
    merged.normalize()
    errors = merged.validate(active=active)
    if errors:
        raise ValueError("；".join(errors))

    after_merit = (
        [asdict(p) for p in merged.targets],
        bool(merged.backup_enabled),
        asdict(merged.merit_backup) if merged.merit_backup is not None else None,
    )
    after_line = (
        [asdict(p) for p in merged.line_targets],
        {k: asdict(v) for k, v in sorted(merged.line_backups.items())},
    )
    changed: set[str] = set()
    if after_merit != before_merit:
        changed.add("merit")
    if after_line != before_line:
        changed.add("line")
    return merged, changed


def point_to_summary(point: Optional[Point]) -> Optional[dict]:
    if point is None:
        return None
    return {
        "name": point.name,
        "label": point_label(point),
        "x": point.x,
        "y": point.y,
        "actions": list(point.actions),
        "action_labels": [ACTION_NAMES.get(a, a) for a in point.actions],
        "priority": point.priority,
        "enabled": bool(point.enabled),
        "expected_name": point.expected_name,
    }


def summarize_targets(profile: Profile) -> dict:
    """生成给 GUI 摘要条与 Agent JSON 接口共用的目标与征兵点结构化摘要。"""
    merit_enabled = sorted([p for p in profile.targets if p.enabled], key=lambda p: p.priority)
    line_enabled = sorted([p for p in profile.line_targets if p.enabled], key=lambda p: p.priority)
    return {
        "merit": {
            "primary": point_to_summary(merit_enabled[0]) if merit_enabled else None,
            "enabled": [point_to_summary(p) for p in merit_enabled],
            "all": [point_to_summary(p) for p in profile.targets],
            "backup_enabled": bool(profile.backup_enabled),
            "backup": point_to_summary(profile.merit_backup),
        },
        "line": {
            "primary": point_to_summary(line_enabled[0]) if line_enabled else None,
            "enabled": [point_to_summary(p) for p in line_enabled],
            "all": [point_to_summary(p) for p in profile.line_targets],
            "backups": {k: point_to_summary(v) for k, v in sorted(profile.line_backups.items())},
        },
    }


def format_targets_brief(profile: Profile) -> str:
    """用于 UI 顶部/日志的一行简明目标摘要。"""
    summary = summarize_targets(profile)
    parts: list[str] = []
    for label, key in (("拉线", "line"), ("武勋", "merit")):
        primary = summary[key]["primary"]
        if primary:
            acts = "→".join(primary["action_labels"])
            extra = len(summary[key]["enabled"]) - 1
            tail = f" (+{extra}备选)" if extra > 0 else ""
            parts.append(f"{label}：{primary['label']} [{acts}]{tail}")
        else:
            parts.append(f"{label}：未启用")
    return "  |  ".join(parts)
