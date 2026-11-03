from pathlib import Path
from types import SimpleNamespace
import time

import cv2
import numpy as np
import pytest

from sanmou_assistant.game import Game, GameError, FarmingUnavailable
from sanmou_assistant.journal import Journal
from sanmou_assistant.models import Profile
from sanmou_assistant.vision import Vision, Text, CityResourceRow, CityFarmSelection


def text(value,x,y):
    return Text(value,.99,x,y,(x-35,y-14,x+35,y+14))


def row(resource='grain', level=12, y=435):
    names={'grain':'农田','wood':'林场','iron':'铁矿','stone':'石矿'}
    return CityResourceRow(f'{level}级{names[resource]}',resource,level,text('主城内',1280,y))


@pytest.mark.parametrize('resources',[
    ['stone','wood','grain','iron'], ['iron','grain','stone','wood'], ['grain','wood','wood','wood'],
])
def test_current_resource_names_and_same_row_location_determine_candidates(resources):
    spans=[]
    names={'grain':'农田','wood':'林场','iron':'铁矿','stone':'石矿'}
    for index,resource in enumerate(resources):
        y=430+index*110
        spans += [text(f'{8+index}级{names[resource]}',850,y),text('开垦地',1040,y),text('主城内',1280,y)]
    vision=Vision.__new__(Vision)
    vision.read=lambda *a,**k:spans
    result=vision.city_resource_rows(None)
    assert [r.resource for r in result] == resources
    assert [r.location.y for r in result] == [430+i*110 for i in range(4)]


def test_incomplete_or_cross_row_coordinates_are_never_used():
    spans=[text('12级农田',850,435),text('开垦地',1040,435),text('主城内',1280,547),
           text('12级农田',850,660),text('领地',1040,660),text('主城内',1280,660)]
    vision=Vision.__new__(Vision)
    vision.read=lambda *a,**k:spans
    assert vision.city_resource_rows(None) == []


@pytest.mark.parametrize('names',[
    ['12级石矿','12级石矿'],['12级农田','12级林场'],['12级农田'],['11级农田','12级农田'],
])
def test_ambiguous_or_non_grain_selection_is_rejected(names):
    vision=Vision.__new__(Vision)
    vision.read=lambda *a,**k:[text(name,350+i*380,440+i*50) for i,name in enumerate(names)]+[text('屯田(余17)',1200,550)]
    assert vision.city_farm_selection(None) is None


def test_verified_farm_selection_reads_actual_button_and_remaining_count():
    vision=Vision.__new__(Vision)
    vision.read=lambda *a,**k:[text('8级农田',350,500),text('8级农田',735,440),text('屯田',1120,550),text('(余17)',1255,550)]
    selected=vision.city_farm_selection(None)
    assert selected and selected.level == 8 and selected.remaining == 17
    assert (selected.button.x,selected.button.y) == (1120,550)


@pytest.fixture
def discovery(tmp_path):
    events=[]
    class Device:
        image='city'
        page_index=0
        pages=[[row()]]
        remaining=17
        taps=[]
        swipes=0
        selected=None
        rejected=set()
        bad_page=False
        control=SimpleNamespace(checkpoint=lambda:None,sleep=lambda n:None)
        def tap(self,x,y,delay=.35):
            self.taps.append((x,y))
            if (x,y) == (470,20):
                self.image='unknown' if self.bad_page else 'territory'
                self.page_index=0
            elif (x,y) == (1125,891): self.image='city'
            elif self.image=='territory' and (x,y) != (1125,260):
                current=[r for r in self.pages[self.page_index] if (r.location.x,r.location.y)==(x,y)]
                assert len(current)==1
                self.selected=None if (x,y) in self.rejected else current[0]
                self.image='city'
        def swipe(self,*args):
            self.swipes+=1
            self.page_index=min(self.page_index+1,len(self.pages)-1)
    device=Device()
    device.taps=[]
    class FakeVision:
        def page(self,image,verify=True): return image
        def find(self,image,labels,rect,exact=False):
            if labels==['领地']: return text('领地60/60',470,20)
            if labels==['开垦']: return text('开垦',1125,260)
            if labels==['参与']: return text('参与',1125,891)
            if labels==['屯田']: return text('屯田',1100,300)
        def city_resource_count(self,image): return len(device.pages)*3
        def city_resource_rows(self,image): return device.pages[device.page_index]
        def city_farm_selection(self,image):
            selected=device.selected
            if selected and selected.resource=='grain':
                return CityFarmSelection(selected.name,selected.level,text(f'屯田(余{device.remaining})',1200,550),device.remaining)
        def grain(self,image): return 100000
    journal=Journal(tmp_path/'runtime.sqlite','test')
    game=Game(device,FakeVision(),Profile(food_enabled=True),journal,tmp_path,lambda *e:events.append(e))
    game.grain=100000
    game.capture=lambda *a:device.image
    game.open_affairs=lambda:'affairs'
    def sandbox():
        device.image='sandbox'
        return 'sandbox'
    game.ensure_sandbox=sandbox
    yield game,device,journal,events
    journal.close()


@pytest.mark.parametrize('fields',[
    [row('stone',12,435),row('grain',8,770)],
    [row('grain',6,547),row('wood',12,660)],
    [row('iron',10,435),row('wood',9,547),row('grain',12,880)],
])
def test_different_accounts_resource_order_and_levels_use_current_links(discovery,fields):
    game,device,journal,events=discovery
    device.pages=[fields]
    image,button,remaining=game.city_farm(inside=True)
    grain=next(r for r in fields if r.resource=='grain')
    assert image=='city' and remaining==17
    assert (grain.location.x,grain.location.y) in device.taps
    assert not any((r.location.x,r.location.y) in device.taps for r in fields if r.resource!='grain')
    assert (740,602) not in device.taps


def test_identical_non_grain_pages_do_not_end_search_before_offscreen_farm(discovery):
    game,device,journal,events=discovery
    device.pages=[[row('wood')],[row('wood')],[row('wood')],[row('grain',9,660)]]
    game.city_farm(inside=True)
    assert device.swipes==3
    assert device.selected.resource=='grain'


def test_location_mismatch_reopens_list_and_uses_another_current_farm(discovery):
    game,device,journal,events=discovery
    device.pages=[[row(y=435),row(y=660)]]
    device.rejected={(1280,435)}
    game.city_farm(inside=True)
    assert device.selected.location.y==660
    assert device.taps.count((470,20))==2


def test_no_confirmed_farm_waits_without_spending_or_stopping_monitoring(discovery):
    game,device,journal,events=discovery
    device.pages=[[row('stone')],[row('iron')],[row('wood')]]
    game.farming()
    assert device.image=='sandbox'
    assert journal.daily('farming')==('',0)
    assert game.farming_retry_at > time.time()
    assert any('继续部队监控' in v.get('result','') for k,v in events if k=='daily')
    taps=list(device.taps)
    game.farming()
    assert taps==device.taps


def test_pending_farming_lookup_failure_keeps_atomic_attempt_and_never_resubmits(discovery):
    game,device,journal,events=discovery
    journal.prepare_farming(18,100000,180000)
    device.pages=[[row('stone')]]
    game.farming()
    assert journal.daily('farming')==('pending',0)
    assert journal.farming_attempt()['remaining_before']==18
    assert not any(k=='farming_check' and v.get('confirmed') for k,v in events)


def test_unknown_page_is_still_fatal_and_never_treated_as_safe_farm_absence(discovery):
    game,device,journal,events=discovery
    device.bad_page=True
    with pytest.raises(GameError,match='领土列表'):
        game.city_farm(inside=True)
    assert journal.daily('farming')==('',0)


@pytest.mark.parametrize('missing',[['参与'],['屯田']])
def test_missing_entry_on_verified_affairs_recovers_without_spending(discovery,missing):
    game,device,journal,events=discovery
    original=game.v.find
    game.v.find=lambda image,labels,rect,exact=False: None if labels==missing else original(image,labels,rect,exact)
    saved=[]
    game.save_image=lambda image,name,publish:saved.append((image,name))
    game.farming()
    assert ('affairs','farming-entry-unavailable') in saved
    assert device.image=='sandbox' and not device.taps
    assert game.farming_retry_at > time.time()
    assert journal.daily('farming')==('',0)
    assert not journal.farming_exhausted()  # 缺少入口不能推断游戏余0。


@pytest.mark.parametrize('page',['unknown','blocked'])
def test_unverified_affairs_refuses_navigation_with_evidence(discovery,page):
    game,device,journal,events=discovery
    game.open_affairs=lambda:page
    saved=[]
    game.save_image=lambda image,name,publish:saved.append((image,name))
    with pytest.raises(GameError,match='城务页面未确认'):
        # 导航必须拒绝未知页；外层 farming 按现有恢复契约交给 ensure_sandbox，
        # 不能用总会返回沙盘的测试替身要求整个 Worker 永久停止。
        game.city_farm()
    assert saved==[(page,'farming-affairs-unrecognized')]
    assert not device.taps


@pytest.fixture(scope='module')
def actual_vision():
    return Vision()


def read_image(path):
    return cv2.imdecode(np.fromfile(path,dtype=np.uint8),cv2.IMREAD_COLOR)


def test_actual_reclamation_list_and_field_types(actual_vision):
    folder=Path(__file__).resolve().parents[1]/'artifacts/fix-2026-10-09-farm-discovery/research-230702'
    image=read_image(folder/'231736_365600-research.png')
    assert actual_vision.page(image)=='territory'
    assert actual_vision.city_resource_count(image)==24
    rows=actual_vision.city_resource_rows(image)
    assert [r.resource for r in rows]==['grain','grain','grain','grain','wood']
    selected=actual_vision.city_farm_selection(read_image(folder/'232220_163606-research.png'))
    assert selected and selected.level==12 and selected.remaining==17
    stone=folder.parent/'selected-stone-failure.png'
    assert actual_vision.city_farm_selection(read_image(stone)) is None
