from pathlib import Path

import cv2
import numpy as np
import pytest

from sanmou_assistant.vision import Vision

ROOT=Path(__file__).resolve().parents[1]/'artifacts/reward-0.4.1'


@pytest.fixture(scope='module')
def vision():
    return Vision()


@pytest.mark.parametrize('folder,pattern,page',[
    ('research/sessions/_evidence/actions/20261028-003950','*camp-recruit-panel.png','camp_recruit_list'),
    ('research/sessions/_evidence/actions/20261028-010636','*camp-recruit-detail.png','camp_recruit'),
    ('research/sessions/_evidence/actions/20261028-012546','*camp-supply-panel.png','camp_recruit'),
    ('pave-live-data/sessions/_evidence/faults/20261028-020226','*startup-not-recognized.png','maintenance'),
])
def test_real_camp_and_maintenance_pages(vision,folder,pattern,page):
    path=next((ROOT/folder).glob(pattern))
    image=cv2.imdecode(np.fromfile(path,np.uint8),cv2.IMREAD_COLOR)
    assert vision.page(image)==page


def test_real_chest_and_reward_result(vision):
    path=next((ROOT/'research/sessions/_evidence/actions/20261027-235830').glob('*badge-before.png'))
    image=cv2.imdecode(np.fromfile(path,np.uint8),cv2.IMREAD_COLOR)
    assert vision.reward_chest(image,'drill')
    assert vision.reward_chest(image,'alliance_rewards') is None
    path=next(path.parent.glob('*badge-after.png'))
    image=cv2.imdecode(np.fromfile(path,np.uint8),cv2.IMREAD_COLOR)
    assert '恭喜获得' in vision.joined(image,(480,175,1440,325)).replace(' ','')
