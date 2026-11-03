"""主队五队状态定义（由用户校准，2026-11-02）。

角标含义只能来自用户明确给出的标准答案；本文件是那份答案的代码化。
新增状态必须先在 `artifacts/team-marker-calibration/` 留下带真值的帧，
再用 `scripts/build_team_marker_templates.py` 生成模板，不得凭空添加。
"""

from __future__ import annotations

# state key -> 游戏内状态名
STATES = {
    "idle": "空闲",
    "transfer": "调动",
    "transfer_stay": "调动停留",
    "march": "行军",
    "guard": "驻守",
    "stationary": "停留",
    "retreat": "撤退",
    "wounded": "重伤恢复",
    "queued_battle": "排队进入战斗",
    "fighting": "战斗",
    "busy": "忙碌",
    "unknown": "待识别",
}

# 用户原话整理；改变这些规则前必须重新向用户确认。
RULES = {
    "idle": (
        "部队在主城中且不在其他状态时的默认状态；可执行任何指令；进入空闲时恢复满补给。"
        "主队头像没有角标。"
    ),
    "transfer_stay": (
        "部队被调动进城外建筑（营帐、城池等）后停留在建筑里的状态，用户称“调动停留”。"
        "是建筑内的停留，不是行军、也不是驻守。没有自然征兵或征兵中止时，角标是蓝色旗形。"
    ),
    "transfer": (
        "用户明确：指调动这个过程本身——正在被调动、还没在建筑里停留下来。"
        "角标是蓝色右箭头，与“调动停留”的旗形不同。"
    ),
    "retreat": (
        "部队在返回出发点的过程中处于撤退状态；撤退状态下不会发生战斗。"
        "角标是白色左箭头（旧资产名 returning-team-mask 是历史误名，不得再写成回城/返回）。"
    ),
    "march": (
        "对非阻挡土地发布行军指令后的状态；不受连接地规则限制；可中止；不消耗武将体力；"
        "途中遇敌即战斗，胜方继续、败方撤回出发地；每格约9秒。"
    ),
    "guard": (
        "对己方或盟友领土发布驻守指令后的状态；戍卫目标及其相邻1格的己方/盟友领土；"
        "中止后转为停留；不与目标土地守军战斗，敌军进入范围才战斗。"
    ),
    "stationary": (
        "部队在大世界土地上不执行其他指令时的状态；停留中可以发生战斗。"
    ),
    "wounded": (
        "用户 2026-11-02 实机确认：重伤不以角标表示，头像正中间压白色“重伤”字样。"
        "重伤队伍不派出；倒计时从详情页或出征页读一次后本地倒数。"
    ),
    "queued_battle": (
        "用户明确：排队进入战斗——该地块上正在发生战斗，本队正在排队等待进入。"
        "是动态画面，沙漏在旋转。"
    ),
    "fighting": (
        "部队正在交战。角标是两把粉红色交叉双剑，动态交叉、分开。"
    ),
    "busy": (
        "用户明确：金色圆盘时钟不是“占领”专用，而是这支部队“在忙”。"
        "已知覆盖：占领（攻占并战胜守军后的持续状态，土地等级越高越久，中止转停留，"
        "结束时连接地消失则占领失败）和建筑内自然征兵（2026-11-02 17:43 在营帐征兵实机确认）。"
        "角标无法区分具体是哪一种，需要结合任务记录或所在建筑判断。时钟静止，指针不走。"
    ),
}

# 与旧 Observation.state 的临时对应，仅用于显示兼容；生产决策接入前需逐条核对。
# transfer_stay 是建筑内停留，旧体系没有对应值，先按 outside 显示，不能据此判定可出击。
LEGACY_STATE = {
    "idle": "home",
    "transfer": "marching",
    "transfer_stay": "outside",
    "march": "marching",
    "guard": "guarding",
    "stationary": "outside",
    "retreat": "returning",
    "wounded": "wounded",
    "queued_battle": "fighting",
    "fighting": "fighting",
    "busy": "active",
    "unknown": "unknown",
}

# 用户 2026-11-02 明确的出兵口径：
# 停留（城外任意地上）、空闲（主城）、调动停留（营帐/城池里）都可以拉出去；
# 忙碌（占领、建筑内自然征兵等）不派；在路上、驻守、撤退、战斗、排队、未识别都不派。
DISPATCHABLE = ("idle", "stationary", "transfer_stay")
ENGAGED = ("queued_battle", "fighting", "guard")
MOVING = ("march", "transfer", "retreat")


def can_dispatch(state: str) -> bool:
    return state in DISPATCHABLE


def is_busy(state: str) -> bool:
    return state == "busy"


def is_in_battle(state: str) -> bool:
    return state in ("queued_battle", "fighting")


def is_moving(state: str) -> bool:
    return state in MOVING


def label(state: str) -> str:
    return STATES.get(state, STATES["unknown"])


def display_label(marker_label: str | None, legacy_state: str, legacy_labels: dict) -> str:
    """总览状态列：优先显示角标状态，没有角标结果时回落到旧词表。"""
    return marker_label or legacy_labels.get(legacy_state, STATES["unknown"])
