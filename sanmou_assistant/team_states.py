"""主队状态定义（由用户校准，2026-11-02；支持 1～5 队）。

角标含义只能来自用户明确给出的标准答案；本文件是那份答案的代码化。
新增状态必须先在 `artifacts/team-marker-calibration/` 留下带真值的帧，
再用 `scripts/build_team_marker_templates.py` 生成模板，不得凭空添加。
"""

from __future__ import annotations

# state key -> 游戏内状态名
STATES = {
    "idle": "空闲",
    "training": "练兵（可出击）",
    "transfer": "调动",
    "siege_transfer": "攻城调动",
    "siege_transfer_stay": "攻城调动停留",
    "transfer_stay": "调动停留",
    "march": "行军",
    "guard": "驻守",
    "stationary": "停留",
    "retreat": "撤退",
    "wounded": "重伤恢复",
    "queued_battle": "排队进入战斗",
    "fighting": "战斗",
    "busy": "忙碌",
    "entrust": "委托",
    "unknown": "待识别",
}

# 用户原话整理；改变这些规则前必须重新向用户确认。
RULES = {
    "idle": (
        "部队在主城中且不在其他状态时的默认状态；可执行任何指令；进入空闲时恢复满补给。"
        "主队头像没有角标。"
    ),
    "training": (
        "智能练兵为绿色持枪人影，武将可持续获得经验，体力未满自动取消。"
        "用户 2026-11-16 确认：主城显示在右下，按空闲的派兵规则；"
        "出城后显示在左下，右下仍显示行动状态。"
    ),
    "transfer_stay": (
        "部队被调动进城外建筑（营帐、城池等）后停留在建筑里的状态，用户称“调动停留”。"
        "是建筑内的停留，不是行军、也不是驻守。没有自然征兵或征兵中止时，角标是蓝色旗形。"
    ),
    "transfer": (
        "用户明确：指调动这个过程本身——正在被调动、还没在建筑里停留下来。"
        "角标是蓝色右箭头，与“调动停留”的旗形不同。"
    ),
    "siege_transfer": (
        "用户 2026-11-16 确认：向攻城大营移动，角标为蓝色旗和右箭头。"
        "与普通调动共用移动期间的等待规则。"
    ),
    "siege_transfer_stay": (
        "用户 2026-11-16 确认：攻城调动到位后，部队停留在攻城大营。"
        "按城外建筑的调动停留处理，出征仍走该队原任务条件。"
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
        "重伤在头像正中显示白字，可与撤退、调动停留同时出现；回主城仍显示重伤。"
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
    "entrust": (
        "【委托】指令：点击我方同盟已任命城主的城池，可对其使用委托指令。执行委托指令的部队"
        "将行军至目标城池，到达后该部队将委托给城主管理，城主可将其编入军团并出征作战；"
        "委托至城池时的行军速度是普通行军的 2 倍。"
        "用户 2026-11-18 确认：角标是浅蓝色拳套（护手），压在黑色菱形底板上；"
        "委托期间队伍交给他人管理，除非离开委托，我方无法派出和管理。"
        "用户同次明确：不需要考虑离开委托，识别到委托就当作“不管这队”，不派兵、不回城、不补兵。"
    ),
}

# 与旧 Observation.state 的临时对应，仅用于显示兼容；生产决策接入前需逐条核对。
# transfer_stay 是建筑内停留，旧体系没有对应值，先按 outside 显示，不能据此判定可出击。
LEGACY_STATE = {
    "idle": "home",
    "training": "home",
    "transfer": "marching",
    "siege_transfer": "marching",
    "siege_transfer_stay": "outside",
    "transfer_stay": "outside",
    "march": "marching",
    "guard": "guarding",
    "stationary": "outside",
    "retreat": "returning",
    "wounded": "wounded",
    "queued_battle": "fighting",
    "fighting": "fighting",
    "busy": "active",
    # 委托期间队伍在别人手里（先是 2 倍速度行军到目标城池，到达后由城主管理）。
    # 旧词表没有“委托”，按行军显示会让总览只剩角标标签；这里仍用 marching 表达
    # “不在我方手上、不能把它当空闲派兵”，实际展示优先用 marker_label=委托。
    "entrust": "marching",
    "unknown": "unknown",
}

# 用户 2026-11-02 明确的出兵口径：
# 停留（城外任意地上）、空闲（主城）、调动停留（营帐/城池里）都可以拉出去；
# 忙碌（占领、建筑内自然征兵等）不派；在路上、驻守、撤退、战斗、排队、未识别都不派。
# 委托（2026-11-18）是队伍交给城主管理：用户明确不考虑离开委托，所以不派、不回城、不补兵。
DISPATCHABLE = ("idle", "training", "stationary", "transfer_stay", "siege_transfer_stay")
ENGAGED = ("queued_battle", "fighting", "guard")
MOVING = ("march", "transfer", "siege_transfer", "retreat")


def marker_with_home_evidence(marker, home: bool):
    """右下角的空闲/练兵表示在主城；用已读到的位置排除矛盾。"""
    if marker and marker.get("state") in ("idle", "training") and not home:
        return {**marker, "state": "unknown", "label": label("unknown"),
                "reason": "主城状态与位置矛盾"}
    return marker


def can_dispatch(state: str) -> bool:
    return state in DISPATCHABLE


def is_busy(state: str) -> bool:
    return state == "busy"


def is_in_battle(state: str) -> bool:
    return state in ("queued_battle", "fighting")


def is_moving(state: str) -> bool:
    return state in MOVING


def is_entrusted(state: str) -> bool:
    """委托给城主的队伍：我方不派兵、不回城、不补兵，也不用开详情复核。

    用户 2026-11-18：不考虑离开委托，识别到委托就当作“不管这队”。
    """
    return state == "entrust"


def label(state: str) -> str:
    return STATES.get(state, STATES["unknown"])


def display_label(marker_label: str | None, legacy_state: str, legacy_labels: dict) -> str:
    """总览状态列：优先显示角标状态，没有角标结果时回落到旧词表。"""
    return marker_label or legacy_labels.get(legacy_state, STATES["unknown"])
