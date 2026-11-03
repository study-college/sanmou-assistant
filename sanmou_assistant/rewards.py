"""Recurring free chests: positive icon + account verification + durable intent."""
from .daily_tasks import require_account, reward_visible


LABELS = {"drill": "军演挂机奖励", "alliance_rewards": "同盟奖励宝箱"}


def status(game, task, message):
    previous = getattr(game, "reward_messages", {})
    if previous.get(task) != message:
        game.emit("daily", {"task": task, "result": message})
        previous[task] = message
        game.reward_messages = previous


def collect(game, task, commit=True):
    label = LABELS[task]
    key = "reward:" + task
    image = game.ensure_sandbox()
    chest = game.v.reward_chest(image, task)
    pending = game.j.pending(key)
    if chest is None and not pending:
        status(game, task, "主页面当前无宝箱，继续监控")
        return False
    if not commit:
        status(game, task, "只检查：发现可领取宝箱" if chest else "上次领取待核对，只读取")
        return False
    require_account(game)
    image = game.ensure_sandbox()
    chest = game.v.reward_chest(image, task)
    if pending:
        if chest is None:
            for ident, *_ in pending:
                game.j.finish(ident, "accepted", "重启后核对：已回沙盘，原宝箱已消失")
            status(game, task, "上次领取已按主页面核对，继续监控新宝箱")
        else:
            status(game, task, "上次领取结果待核对，保留记录，不重复点击")
        return False
    if chest is None:
        return False
    # 领取可在同一天重复发生，不用每日done/午夜/7时推测宝箱是否可领。
    ident = game.j.prepare(key, "reward", label)
    game.capture("reward-"+task+"-before", True)
    try:
        game.d.tap(chest["x"], chest["y"], .9)
        image = game.capture("reward-"+task+"-result", True)
        if reward_visible(game, image):
            game.j.finish(ident, "accepted", "看到本次恭喜获得奖励弹层")
            status(game, task, label + "已领取，继续监控下次出现")
            game.ensure_sandbox()
            return True
        if game.v.page(image) == "sandbox" and game.v.reward_chest(image, task) is None:
            game.j.finish(ident, "accepted", "提交后沙盘宝箱已消失")
            status(game, task, label + "已领取，宝箱已消失")
            return True
        game.j.finish(ident, "uncertain", "点击后奖励/宝箱消失证据不足，等待现场核对")
        status(game, task, "领取结果待核对，保留记录，不重复点击")
        game.ensure_sandbox()
        return True
    except BaseException:
        game.j.finish(ident, "uncertain", "领取点击或读取中断，重启先核对，不重放")
        raise
