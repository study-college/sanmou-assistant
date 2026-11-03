import numpy as np
import pytest
import rapidocr_onnxruntime

from sanmou_assistant.transport import Control, Stopped
from sanmou_assistant.vision import Vision


def test_ocr_reuse_requires_unchanged_pixels_and_can_be_stopped(monkeypatch):
    calls = []
    def recognize(image, **kwargs):
        calls.append(int(image[0, 0, 0]))
        value = "33000" if image[0, 0, 0] == 0 else "26342"
        return [([[0, 0], [50, 0], [50, 20], [0, 20]], value, .99)], None
    monkeypatch.setattr(rapidocr_onnxruntime, "RapidOCR", lambda **kwargs: recognize)
    control = Control()
    vision = Vision(control)
    image = np.zeros((30, 100, 3), dtype=np.uint8)
    assert vision.read(image)[0].text == "33000"
    assert vision.read(image.copy())[0].text == "33000"
    assert calls == [0]
    image[0, 0] = 255
    assert vision.read(image)[0].text == "26342"
    assert calls == [0, 255]
    control.stop.set()
    with pytest.raises(Stopped):
        vision.read(image)
    assert calls == [0, 255]


def test_stop_during_ocr_prevents_following_read_and_action(monkeypatch):
    control = Control()
    def recognize(image, **kwargs):
        control.stop.set()
        return [], None
    monkeypatch.setattr(rapidocr_onnxruntime, "RapidOCR", lambda **kwargs: recognize)
    vision = Vision(control)
    with pytest.raises(Stopped):
        vision.read(np.zeros((30, 100, 3), dtype=np.uint8))
    assert not vision.cache


def test_page_fast_mode_keeps_precise_reads_and_pixel_changes_invalidate_cache(monkeypatch):
    from types import SimpleNamespace
    ocr = SimpleNamespace(text_det=SimpleNamespace(limit_side_len=736))
    monkeypatch.setattr(rapidocr_onnxruntime, 'RapidOCR', lambda **kwargs: ocr)
    control = Control()
    vision = Vision(control)
    calls = []
    def classify(image):
        calls.append(ocr.text_det.limit_side_len)
        return 'sandbox' if image[0, 0, 0] == 0 else 'blocked'
    vision._page = classify
    image = np.zeros((30, 100, 3), dtype=np.uint8)
    assert vision.page(image) == 'sandbox'
    assert vision.page(image.copy()) == 'sandbox'
    assert calls == [128] and ocr.text_det.limit_side_len == 736
    image[0, 0] = 255
    assert vision.page(image) == 'blocked' and calls == [128, 128]
    control.stop.set()
    with pytest.raises(Stopped): vision.page(image)


def test_unknown_fast_page_falls_back_and_cancellation_restores_detector(monkeypatch):
    from types import SimpleNamespace
    ocr = SimpleNamespace(text_det=SimpleNamespace(limit_side_len=736))
    monkeypatch.setattr(rapidocr_onnxruntime, 'RapidOCR', lambda **kwargs: ocr)
    vision = Vision()
    calls = []
    def classify(image):
        calls.append(ocr.text_det.limit_side_len)
        if calls[-1] == 128:
            vision.saw_text = True   # 读到了文字但页型不明：才需要原精度复核
            return 'unknown'
        return 'monthly'
    vision._page = classify
    assert vision.page(np.zeros((30, 100, 3), np.uint8)) == 'monthly'
    assert calls == [128, Vision.FALLBACK_DETECT_LIMIT] and ocr.text_det.limit_side_len == 736
    def cancel(image): raise Stopped()
    vision._page = cancel
    with pytest.raises(Stopped): vision.page(np.ones((30, 100, 3), np.uint8))
    assert ocr.text_det.limit_side_len == 736


def test_unknown_page_without_any_text_skips_the_expensive_second_pass(monkeypatch):
    # 切页动画、黑屏、加载画面：快速检测一个词都没读到，原精度复核同样读不出页型，
    # 却要多花 8~18 秒；直接返回 unknown 交给调用方等下一帧。
    from types import SimpleNamespace
    ocr = SimpleNamespace(text_det=SimpleNamespace(limit_side_len=736))
    monkeypatch.setattr(rapidocr_onnxruntime, 'RapidOCR', lambda **kwargs: ocr)
    vision = Vision()
    calls = []
    def classify(image):
        calls.append(ocr.text_det.limit_side_len)
        return 'unknown'
    vision._page = classify
    assert vision.page(np.zeros((1080, 1920, 3), np.uint8)) == 'unknown'
    assert calls == [128] and ocr.text_det.limit_side_len == 736


@pytest.mark.parametrize('labels', [(('180万/600万', .99), ('181万/600万', .99)),
                                   (('180万', .99), ('180万', .99)),
                                   (('180万/600万', .5), ('180万/600万', .5))])
def test_quick_grain_rejects_conflict_partial_or_low_confidence(monkeypatch, labels):
    results = iter(labels)
    monkeypatch.setattr(rapidocr_onnxruntime, 'RapidOCR', lambda **kwargs: lambda *a, **k: ([list(next(results))], None))
    vision = Vision()
    # 刻意使两种滤色像素不同，避免合法的相同裁剪缓存吞掉冲突样本。
    image = np.zeros((1080, 1920, 3), np.uint8)
    image[40, 1110] = [160, 160, 160]
    vision.read = lambda *a, **k: []
    assert vision.grain(image) is None


def test_single_line_cache_checks_pixels_and_stop(monkeypatch):
    calls = []
    def recognize(image, **kwargs):
        assert kwargs == {'use_det': False, 'use_cls': False}
        calls.append(1)
        return [['180万/600万', .99]], None
    monkeypatch.setattr(rapidocr_onnxruntime, 'RapidOCR', lambda **kwargs: recognize)
    control = Control(); vision = Vision(control)
    image = np.zeros((51, 188, 3), np.uint8)
    assert vision.read_line(image) == '180万/600万'
    assert vision.read_line(image.copy()) == '180万/600万' and len(calls) == 1
    image[0, 0] = 255
    assert vision.read_line(image) == '180万/600万' and len(calls) == 2
    control.stop.set()
    with pytest.raises(Stopped): vision.read_line(image)
