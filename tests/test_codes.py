# -*- coding: utf-8 -*-
"""コード表と判定ロジックの固定テスト。数字の取り違えを防ぐ。  実行: .venv/bin/python -m pytest -q tests"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import codes  # noqa: E402
from app.main import build_timeline  # noqa: E402


def test_depth_rank_matches_ksj_codelist():
    assert codes.DEPTH_RANK[1] == '0.5m未満'
    assert codes.DEPTH_RANK[2] == '0.5m以上3.0m未満'
    assert codes.DEPTH_RANK[3] == '3.0m以上5.0m未満'
    assert codes.DEPTH_RANK[6] == '20.0m以上'
    assert codes.DURATION_RANK[3].startswith('24時間以上72時間未満')
    assert codes.DURATION_RANK[7].startswith('672時間以上')
    assert codes.COLLAPSE == {1: '氾濫流', 2: '河岸侵食', 3: '氾濫流・河岸侵食の両方'}


def test_loader_uses_same_codes():
    import importlib.util
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts', 'load_a31.py')
    spec = importlib.util.spec_from_file_location('load_a31', p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.DEPTH_RANK == codes.DEPTH_RANK and m.DURATION_RANK == codes.DURATION_RANK and m.COLLAPSE == codes.COLLAPSE
    assert m.ATTR == codes.ATTR and m.CATEGORY == codes.CATEGORY


def test_naisui_band():
    assert codes.naisui_band(0.1) == '0.3m未満'
    assert codes.naisui_band(0.5) == '0.5m以上1.0m未満'
    assert codes.naisui_band(3.2) == '3.0m以上'
    assert codes.minutes_label(30) == '約30分' and codes.minutes_label(600) == '約10時間' and codes.minutes_label(2880) == '約2.0日'


def _res(max_rank=None, dur=None, collapse=False, naisui=None):
    return dict(national=dict(max=(dict(rank=max_rank, label=codes.DEPTH_RANK[max_rank]) if max_rank else None),
                              duration=(dict(rank=dur, label=codes.DURATION_RANK[dur]) if dur else None),
                              collapse=([dict(code=1, label='氾濫流')] if collapse else []), status='inside' if max_rank else 'outside'),
                naisui=dict(status='inside' if naisui is not None else 'outside', depth_m=naisui, depth_label=codes.naisui_band(naisui)))


def test_timeline_policy():
    assert build_timeline(_res(collapse=True), {})['policy'].startswith('立退き')
    assert build_timeline(_res(max_rank=3), {})['policy'].startswith('立退き')
    assert build_timeline(_res(max_rank=2, dur=4), {})['policy'].startswith('立退き')          # 3日以上
    assert build_timeline(_res(max_rank=2, dur=1), {'upper_floor': 1})['policy'].startswith('早めの立退き')
    assert build_timeline(_res(max_rank=1), {})['policy'].startswith('立退き')                  # 上階なし
    assert build_timeline(_res(naisui=0.8), {})['policy'].startswith('外出を避け')
    assert build_timeline(_res(), {})['policy'].startswith('在宅')


def test_timeline_household_rows():
    tl = build_timeline(_res(max_rank=2, dur=1), {'elderly': 1, 'car': 1, 'pet': 1, 'upper_floor': 1})
    assert any('要配慮者' in a for a in tl['rows'][0]) and any('ガソリン' in a for a in tl['rows'][0]) and any('ペット' in a for a in tl['rows'][0])
    assert any('警戒レベル3' in a and '避難を開始' in a for a in tl['rows'][1])
    assert len(tl['rows']) == 4 and len(tl['levels']) == 4
