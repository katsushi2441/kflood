# -*- coding: utf-8 -*-
"""区・河川ページのデータ層。fixture（2026-09-08 の実発令）を種にした履歴と対応表を固定する。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import alerts, nagoya  # noqa: E402

nagoya.DB_PATH = os.path.join(tempfile.mkdtemp(), 'alerts_history.sqlite')   # 本番の履歴DBに触らない


def test_seed_and_rivers():
    assert nagoya.seed_if_empty() is True
    rs = nagoya.rivers()
    names = {r['target'] for r in rs}
    assert {'天白川', '矢田川', '新川', '土砂災害'} <= names and len(rs) == 17
    ten = next(r for r in rs if r['target'] == '天白川')
    assert ten['slug'] == 'tempaku' and '天白区' in ten['wards'] and ten['last_alert']['level'] == 4


def test_river_detail_and_ward_alerts():
    live = alerts.parse(open(nagoya.FIXTURE, encoding='utf-8').read(), year=2026)
    r = nagoya.river_detail('yada', live)
    assert r['target'] == '矢田川' and r['current'][0]['level'] == 5 and any(w['name'] == '中村区' and '*' in w['gakku'] for w in r['ward_list'])
    wa = nagoya.ward_alerts('中川区', live)
    assert wa and wa[0]['level'] == 5 and wa[0]['target'] == '矢田川'
    assert nagoya.river_detail('nonexistent', live) is None
    assert nagoya.river_slug('未知川').startswith('r-') and nagoya.river_name(nagoya.river_slug('未知川')) == '未知川'


def test_wards_file():
    ws = nagoya.wards()
    assert len(ws) == 16 and nagoya.ward_by_slug('nakagawa')['name'] == '中川区' and ws[0]['office_address']
