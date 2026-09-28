# -*- coding: utf-8 -*-
"""神奈川県：トップの「避難発令」タブに残る解除だけの報を、市区町村ぜんぶの発令と読まない（2026-09-28）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import alerts_ext as a  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')


def _run(monkeypatch, city, at, pid, fixture):
    live = [dict(city=city, at=at, levels=[4])]
    hist = [dict(pid=pid, city=city, at='2026/' + at)]
    monkeypatch.setattr(a, '_kanagawa_live', lambda: dict(live=live))
    monkeypatch.setattr(a, '_kanagawa_hist', lambda: dict(hist=hist))
    body = open(os.path.join(FIX, fixture), encoding='utf-8').read()
    monkeypatch.setattr(a, '_get', lambda url: body)
    return a._fetch_kanagawa('神奈川県' + city)


def test_all_lifted_report_is_not_an_active_order(monkeypatch):
    r = _run(monkeypatch, '横浜市中区', '09/22 16:25', 'a3ihA00000017sHQAQ', 'kanagawa_detail_naka_kaijo_20260928.html')
    assert r['items'] == []
    assert r['notes'] and all('解除' in n for n in r['notes'])


def test_remaining_area_stays_active(monkeypatch):
    r = _run(monkeypatch, '横浜市港北区', '09/21 22:36', 'a3ihA00000014paQAA', 'kanagawa_detail_kohoku_20260928.html')
    assert [(x['area'], x['level']) for x in r['items']] == [('日吉本町3丁目の一部', 4)]


def test_unreadable_detail_falls_back_to_whole_city(monkeypatch):
    live = [dict(city='横浜市中区', at='09/22 16:25', levels=[4])]
    monkeypatch.setattr(a, '_kanagawa_live', lambda: dict(live=live))
    monkeypatch.setattr(a, '_kanagawa_hist', lambda: dict(hist=[dict(pid='x', city='横浜市中区', at='2026/09/22 16:25')]))

    def boom(url):
        raise OSError('down')
    monkeypatch.setattr(a, '_get', boom)
    r = a._fetch_kanagawa('神奈川県横浜市中区')
    assert r['items'] and r['items'][0].get('whole_city')   # 読めないときは黙って落とさない
