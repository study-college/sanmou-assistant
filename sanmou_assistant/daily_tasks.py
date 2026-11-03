"""Bounded daily flows with durable, independently confirmed claims."""
from .game import GameError
from .journal import cycle_key
import re


def reward_visible(game, image):
    return '恭喜获得' in game.v.joined(image, (480, 175, 1440, 325)).replace(' ', '')


def require_account(game):
    if game.account_checked:
        return
    if not game.p.main_city or not game.p.account:
        raise GameError('请先校验账号与主城，再执行日常')
    image = game.go_to(game.p.main_city)
    game.check_account(image)
    if not game.account_checked:
        raise GameError('本次未确认配置中的账号，暂停日常')
    game.ensure_sandbox()


def monthly(game, commit=True):
    if game.j.daily('monthly')[0] == 'done':
        game.emit('daily', {'task': 'monthly', 'result': '今日已领取，不再进入月卡'})
        return
    require_account(game)
    image = game.ensure_sandbox()
    icon = game.v.navigation_icon(image, 'monthly')
    if not icon:
        toggle = game.v.navigation_toggle(image)
        if toggle:
            game.d.tap(*toggle, .6)
            image = game.wait_transition(game.capture(), expected=('sandbox',))
            icon = game.v.navigation_icon(image, 'monthly')
    if icon:
        game.d.tap(*icon, .8)
    else:
        welfare = game.v.find(image, ['福利'], (140, 90, 1120, 210), exact=True)
        if welfare is None:
            raise GameError('月卡及福利入口均未识别，不点击顶栏货币，稍后重读')
        game.d.tap(welfare.x, welfare.y, .8)
        image = game.capture('welfare-open', True)
        if not game.v.find(image, ['福利'], (180, 0, 330, 75), exact=True):
            raise GameError('福利入口未确认，暂停月卡')
        tab = game.v.find(image, ['月卡'], (60, 95, 320, 750), exact=True)
        if not tab:
            raise GameError('福利月卡页签未识别')
        game.d.tap(tab.x, tab.y, .8)
    image = game.capture('monthly-open', True)
    page = game.v.page(image)
    if page == 'monthly':
        game.monthly_popup(commit)
    elif page == 'monthly_claimed':
        if commit:
            pending = game.j.pending_daily('monthly')
            if pending:
                for (cycle,) in pending:
                    if cycle == cycle_key('monthly'):
                        game.j.finish_daily('monthly', cycle, {'panel': '已领取'})
            else:
                game.j.record_daily('monthly', 'done')
        game.emit('daily', {'task': 'monthly', 'result': '页面确认今日俸禄已领取'})
    elif getattr(game.v, "monthly_no_card", lambda *_: False)(image):
        # 账号没买月卡：页面只有购买/续费入口。自动跳过，不购买也不领取。
        game.save_image(image, 'monthly-no-card', True)
        if commit:
            game.j.record_daily('monthly', 'skipped')
        game.emit('daily', {'task': 'monthly', 'result': '本账号没有可领取的月卡（只有购买入口），已跳过，不购买'})
    else:
        raise GameError('月卡领取状态未确认，未点击购买或续费')
    game.ensure_sandbox()


def open_signin(game):
    image = game.open_activity()
    label = game.v.find(image, ['签到有礼'], (60, 90, 320, 930), exact=True)
    if not label:
        raise GameError('签到有礼页签未识别')
    game.d.tap(label.x, label.y, .6)
    return game.capture('signin', True)


def card_claimed(game, image, card):
    return game.v.reward_claimed(image, (card.x-65, card.y+25, card.x+65, card.y+140))


def signin(game, commit=True):
    started_cycle = cycle_key('signin')
    if game.j.daily('signin')[0] == 'done':
        game.emit('daily', {'task': 'signin', 'result': '今日签到与进度奖励已完成'})
        return
    require_account(game)
    image = open_signin(game)
    count = game.v.signin_count(image)
    if count is None:
        raise GameError('累计签到天数未确认，暂停领取')
    expected = count % 28 + 1
    cards = game.v.signin_cards(image)
    if expected not in cards:
        game.d.swipe(1490, 735, 1490, 300, 420)
        image = game.capture('signin-scroll', True)
        cards = game.v.signin_cards(image)
    pending = game.j.pending_daily('signin_day')
    scrolled_pending = False
    for (cycle,) in pending:
        before = game.j.daily_context('signin_day', cycle) or {}
        card = cards.get(before.get('day'))
        if not card:
            game.d.swipe(1490,735,1490,300,420)
            image = game.capture('signin-pending-scroll',True)
            if game.v.signin_count(image) != count:
                raise GameError('核对期间累计签到发生变化，暂停新领取')
            cards = game.v.signin_cards(image)
            card = cards.get(before.get('day'))
            scrolled_pending = True
        if count == before.get('count', -2) + 1 and card and card_claimed(game,image,card):
            game.j.finish_daily('signin_day', cycle, {'count_after':count, 'card':'已领取'})
        else:
            raise GameError('签到上次提交待核对；只读取，不重复领取')
    day_confirmed = game.j.daily('signin_day',started_cycle)[0] == 'done'
    if scrolled_pending and not day_confirmed:
        game.d.swipe(1490,300,1490,735,420)
        image = game.capture('signin-current-day',True)
        if game.v.signin_count(image) != count:
            raise GameError('核对期间累计签到发生变化，暂停新领取')
        cards = game.v.signin_cards(image)
        if expected not in cards:
            game.d.swipe(1490,735,1490,300,420)
            image = game.capture('signin-scroll',True)
            cards = game.v.signin_cards(image)
    # 用“累计天数 → 本轮第几天”直接定位当轮日卡，不再依赖高亮：
    # 页面上除了当轮日卡，还有“补签”卡和里程碑金卡，用高亮推断会误判。
    expected_card = cards.get(expected)
    if expected_card is not None and card_claimed(game, image, expected_card):
        # 当轮日卡已有绿勾：今天已经签到过，不再点击，也不碰补签卡。
        if not commit:
            game.emit('daily', {'task': 'signin', 'result': f'试运行：今日已签到（本轮第{expected}天），不领取进度奖励'})
            game.ensure_sandbox()
            return
        game.j.record_daily('signin_day', 'done')
        game.emit('daily', {'task': 'signin', 'result': f'今日已签到（本轮第{expected}天绿勾已确认），累计{count}天'})
    elif day_confirmed:
        pass
    elif expected_card is None:
        raise GameError(f'本轮第{expected}天日卡未读到，暂停领取')
    else:
        if not commit:
            game.emit('daily', {'task':'signin','result':f'试运行：累计{count}天，可领取本轮第{expected}天'})
            game.ensure_sandbox()
            return
        cycle = game.j.prepare_daily('signin_day', {'count':count,'day':expected})
        game.d.tap(expected_card.x, expected_card.y+80, .8)
        after = game.capture('signin-result', True)
        if reward_visible(game,after):
            game.dismiss_overlay(after)
            image = game.capture('signin-confirm', True)
        else:
            image = after
        updated = game.v.signin_count(image)
        actual = game.v.signin_cards(image).get(expected)
        if updated != count+1 or not actual or not card_claimed(game,image,actual):
            raise GameError('签到次数/绿勾未确认，保留待核对，不再点击')
        game.j.finish_daily('signin_day',cycle,{'count_after':updated,'card':'已领取'})
        count = updated
        game.emit('daily', {'task':'signin','result':f'已领取本轮第{expected}天，累计{count}天'})
    if not commit:
        game.emit('daily',{'task':'signin','result':'试运行：日卡已核对，不领取进度奖励'})
        game.ensure_sandbox()
        return
    milestones = game.v.signin_milestones(image)
    if not milestones:
        raise GameError('签到进度奖励未识别，暂不标记全部完成')
    for threshold,label in milestones:
        if threshold > count:
            continue
        task = f'signin_milestone_{threshold}'
        if game.j.daily(task)[0] == 'done':
            continue
        claimed = game.v.reward_claimed(image,(label.x-65,875,label.x+65,995))
        if claimed:
            for (cycle,) in game.j.pending_daily(task):
                game.j.finish_daily(task,cycle,{'check':'已领取'})
            game.j.record_daily(task,'done')
            continue
        if game.j.pending_daily(task):
            raise GameError(f'{threshold}天进度奖励待核对，不重复点击')
        cycle = game.j.prepare_daily(task,{'threshold':threshold,'count':count})
        game.d.tap(label.x,label.y-80,.8)
        after = game.capture(f'signin-milestone-{threshold}',True)
        if reward_visible(game,after):
            game.dismiss_overlay(after)
            image = game.capture('signin-progress-confirm',True)
        else:
            image = after
        if not game.v.reward_claimed(image,(label.x-65,875,label.x+65,995)):
            raise GameError(f'{threshold}天奖励绿勾未确认，保留待核对')
        game.j.finish_daily(task,cycle,{'check':'已领取'})
        game.emit('daily',{'task':'signin','result':f'已领取累计{threshold}天进度奖励'})
    game.j.record_daily('signin','done',cycle=started_cycle)
    game.emit('daily',{'task':'signin','result':f'累计{count}天；今日日卡及所有已达标进度奖励已核对'})
    game.ensure_sandbox()


def visit_offer(game, image):
    if game.v.page(image) not in ('visit', 'visit_result'):
        raise GameError('当前不是已确认的寻访单次页面')
    single = game.v.find(image,['寻访一次'],(440,870,1080,985),exact=True)
    if not single:
        raise GameError('单次寻访按钮未确认')
    rect = (int(single.x)-200,int(single.y)-65,int(single.x)+200,int(single.y)+45)
    free = game.v.find(image,['免费'],rect,exact=True)
    half = game.v.find(image,['半价'],rect,exact=True)
    if free and half:
        raise GameError('免费/半价状态冲突，暂停寻访')
    if free:
        return 'free',0,single
    values = [int(t.text) for t in game.v.read(image,(int(single.x)-140,int(single.y)-125,int(single.x)+140,int(single.y)-55),minimum=.85)
              if re.fullmatch(r'\d{1,6}',t.text)]
    if half:
        if len(values) != 1 or values[0] <= 0:
            raise GameError('半价寻访的实际黄金价格未确认')
        return 'half',values[0],single
    if values == [1] and game.v.single_visit_ticket(image,single):
        return 'other',1,single
    raise GameError('未确认免费、半价或普通寻访券价格；暂停日常，不猜测优惠已用完')


def visit_balances(game, image):
    result = []
    for rect in ((1510,8,1645,68),(1110,8,1220,68)):
        values = [int(t.text) for t in game.v.read(image,rect,minimum=.85) if re.fullmatch(r'\d{1,8}',t.text)]
        result.append(values[0] if len(values) == 1 else None)
    return tuple(result)


def quick_visit(game, image, commit):
    label = game.v.find(image,['快速寻访'],(1560,960,1885,1055),exact=True)
    if not label:
        raise GameError('快速寻访勾选项未确认')
    # OCR有时把绿勾并入文字左边界；右边界稳定，复核窗口覆盖完整复选框。
    x,y = label.box[2]-190,label.y
    if game.v.reward_claimed(image,(x-40,y-35,x+40,y+35)):
        return image
    if not commit:
        return image
    game.d.tap(x,y,.5)
    image = game.capture('visit-quick-checked',True)
    if not game.v.reward_claimed(image,(x-40,y-35,x+40,y+35)):
        raise GameError('快速寻访未确认勾选，未提交寻访')
    return image


def reconcile_visit(game, image):
    offer,cost,_ = visit_offer(game,image)
    gold,tickets = visit_balances(game,image)
    for task in ('visit_free','visit_half'):
        for (cycle,) in game.j.pending_daily(task):
            if cycle != cycle_key(task):
                raise GameError('有跨日寻访待核对记录，暂停新增寻访，不重复消费')
            before = game.j.daily_context(task,cycle) or {}
            confirmed = (task == 'visit_free' and offer in ('half','other') and gold == before.get('gold_before') and tickets == before.get('tickets_before'))
            if task == 'visit_half':
                confirmed = (offer == 'other' and gold is not None and tickets is not None and
                             before.get('gold_before') is not None and before.get('cost') is not None and
                             before['gold_before']-gold == before['cost'] and tickets == before.get('tickets_before'))
            if not confirmed:
                raise GameError('寻访上次结果未确认；只读取，不重复寻访')
            game.j.finish_daily(task,cycle,{'offer_after':offer,'gold_after':gold,'tickets_after':tickets})
            game.emit('daily',{'task':'visit','result':'已根据优惠状态和实际余额核对上次寻访'})


def visit(game, commit=True):
    allow_half = game.p.dailies.get('half_price',False)
    if game.j.daily('visit_free')[0] == 'done' and (not allow_half or game.j.daily('visit_half')[0] == 'done'):
        game.emit('daily',{'task':'visit','result':'今日已完成所选寻访，跳过入口与消费'})
        return
    require_account(game)
    # 崩溃留在结果页时先只读核对，再回到主界面；绝不点击结果页的“再寻访”。
    image = game.capture()
    if game.v.page(image) in ('visit','visit_result'):
        reconcile_visit(game,image)
    image = game.open_visit()
    if game.v.page(image) != 'visit' or not game.v.find(image,['招贤纳士'],(140,130,380,250),exact=True):
        raise GameError('未进入招贤纳士，不尝试其他卡池')
    image = quick_visit(game,image,commit)
    reconcile_visit(game,image)
    for _ in range(2):
        offer,cost,single = visit_offer(game,image)
        gold,tickets = visit_balances(game,image)
        if offer == 'other':
            if commit:
                game.j.record_daily('visit_free','done')
                game.j.record_daily('visit_half','done')
            break
        task = 'visit_free' if offer == 'free' else 'visit_half'
        if offer == 'half':
            if commit and game.j.daily('visit_free')[0] != 'done':
                game.j.record_daily('visit_free','done')
            if not allow_half:
                break
        if game.j.daily(task)[0] == 'done':
            raise GameError('游戏优惠与今日完成记录不一致，保留记录，不重复寻访')
        if not commit:
            game.emit('daily',{'task':'visit','result':f'试运行：{offer}单次寻访，未勾选或消费'})
            break
        if game.j.pending_daily('visit'):
            raise GameError('旧版寻访有待核对记录，暂停新消费')
        if gold is None or tickets is None or offer == 'half' and gold < cost:
            raise GameError('寻访余额未确认或不足，暂停消费')
        cycle = game.j.prepare_daily(task,{'offer':offer,'cost':cost,'gold_before':gold,'tickets_before':tickets})
        game.d.tap(single.x,single.y,1.2)
        after = game.capture(f'{task}-result',True)
        for attempt in range(5):
            if game.v.page(after) == 'visit_result':
                break
            if game.v.page(after) not in ('unknown','visit'):
                raise GameError('寻访提交后页面未确认，保留待核对')
            game.d.control.sleep(.6)
            after = game.capture()
        else:
            raise GameError('寻访结果未出现，保留待核对，不重试提交')
        reconcile_visit(game,after)
        if game.j.daily(task,cycle)[0] != 'done':
            raise GameError('寻访结果待核对')
        game.emit('daily',{'task':'visit','result':'免费单次寻访完成' if offer == 'free' else f'半价单次寻访完成，实际消耗{cost}黄金'})
        game.ensure_sandbox()
        if offer == 'half' or not allow_half:
            break
        image = quick_visit(game,game.open_visit(),commit)
    if commit and game.j.daily('visit_free')[0] == 'done' and (not allow_half or game.j.daily('visit_half')[0] == 'done'):
        game.j.record_daily('visit','done')
    game.ensure_sandbox()
