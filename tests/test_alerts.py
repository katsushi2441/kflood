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


def test_kaijo_notice_is_not_an_active_alert():
    """解除通知（警戒レベル4・避難指示を解除（矢田川））を発令として出さない（2026-09-09 実測の退行）。"""
    html_text = (__import__('pathlib').Path(__file__).parent / 'fixtures' / 'nagoya_saigai_20260909_kaijo.html').read_text(encoding='utf-8')
    d = alerts.parse(html_text, year=2026)
    assert d['items'] == []
    kaijo = [n for n in d['notes'] if n.get('kind') == '解除']
    assert kaijo and kaijo[0]['target'] == '矢田川'
    assert kaijo[0]['issued_at'] == '2026-09-09T02:45:00'


def test_gakku_levels_from_live_items():
    """学区→現在の最大レベル（区の全学区は '*'）。地図の学区レイヤの色に使う。"""
    from app import main
    live = {'items': [
        {'level': 4, 'wards': {'天白区': ['植田', '大坪'], '熱田区': ['*']}},
        {'level': 5, 'wards': {'天白区': ['植田']}},
    ]}
    lv = main.gakku_levels(live)
    assert lv[('天白区', '植田')] == 5 and lv[('天白区', '大坪')] == 4 and lv[('熱田区', '*')] == 4
