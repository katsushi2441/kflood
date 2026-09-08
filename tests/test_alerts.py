# -*- coding: utf-8 -*-
"""名古屋市 災害情報配信ページの解析を、2026-09-08 の実ページ（24件発令）で固定する。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import alerts  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'nagoya_saigai_20260908.html')


def _data():
    return alerts.parse(open(FIX, encoding='utf-8').read(), year=2026)


def test_parse_counts_and_levels():
    d = _data()
    assert len(d['items']) == 24
    assert sum(1 for i in d['items'] if i['level'] == 5) == 3
    assert {i['target'] for i in d['items'] if i['level'] == 5} == {'矢田川', '植田川', '香流川'}


def test_parse_wards_and_all_districts():
    d = _data()
    ten = next(i for i in d['items'] if i['target'] == '天白川' and i['level'] == 4)
    assert ten['issued_at'] == '2026-09-08T18:10:00'
    assert ten['wards']['港区'] == ['東築地'] and '野並' in ten['wards']['天白区']
    yada = next(i for i in d['items'] if i['target'] == '矢田川' and i['level'] == 5)
    assert yada['wards']['中村区'] == ['*']            # 「全学区」


def test_for_gakku_and_typo():
    d = _data()
    hits = alerts.for_gakku(d, '中川区', '千音寺')
    assert [(h['level'], h['target']) for h in hits] == [(4, '新川')]
    assert alerts.for_gakku(d, '中村区', '稲葉地')[0]['level'] == 5      # 全学区
    assert [(h['level'], h['target']) for h in alerts.for_gakku(d, '千種区', '星ケ丘')] == [(4, '土砂災害')]   # 河川ではなく土砂災害の発令
    assert alerts.for_gakku(d, '千種区', '存在しない学区') == []
    typo = [i for i in d['items'] if i['target'] == '植田川' and i['level'] == 3]
    assert typo and typo[0]['label'] == '高齢者等避難'                   # 市ページの「高齢者等避」表記でもラベルは表から


def test_unavailable_is_not_none():
    assert alerts.max_level([]) is None
