# -*- coding: utf-8 -*-
"""「いま、どうするか」の判断を固定する。

**発令中の分岐は、平時には一度も通らない。** 本番で初めて動くのが災害の最中に
なるので、ここで全部の状態を作って確かめる。とくに次の2つを外さない。

- 市の配信を取得できなかったときに「発令はありません」と書かないこと
- 同じ避難指示でも、立退き避難が要る場所と上の階にとどまれる場所で内容が変わること
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _build_now():
    """main.py は DB 接続を持つが、import の時点では繋がないので読み込める。"""
    from app.main import build_now
    return build_now


def _res(status='ok', items=(), gakku=True):
    al = dict(status=status, gakku=(dict(ward='西区', name='城西', area='名古屋市') if gakku else None),
              items=list(items), max_level=(max(i['level'] for i in items) if items else None),
              fetched_at='2026-09-18 07:00', source='名古屋市', source_url='https://example.invalid/')
    return dict(alert=al)


def _tl(policy='立退き避難（区域外の避難所・親戚宅・ホテルなどへ）'):
    return dict(policy=policy, why='テスト')


def _item(level, label, target='庄内川'):
    return dict(level=level, label=label, target=target, issued_at='2026-09-18T06:30')


LEAVE = '立退き避難（区域外の避難所・親戚宅・ホテルなどへ）'
STAY = '在宅で安全確保。周辺の河川・道路の状況で判断'


def test_no_area_is_not_no_alert():
    """学区を引けない自治体で「発令なし」と言わない。"""
    nw = _build_now()(_res(gakku=False), _tl(), {})
    assert nw['state'] == 'no_area'
    assert '発令はありません' not in nw['headline']


def test_unavailable_is_not_no_alert():
    """**取得できなかったことを「発令なし」と書かない。** ここが最も危ない誤り。"""
    nw = _build_now()(_res(status='unavailable'), _tl(), {})
    assert nw['state'] == 'unknown'
    assert '発令はありません' not in nw['headline']
    assert '発令が無いという意味ではありません' in nw['detail']


def test_calm_says_no_alert_explicitly():
    nw = _build_now()(_res(status='ok', items=[]), _tl(), {})
    assert nw['state'] == 'calm'
    assert '発令はありません' in nw['headline']


def test_level4_when_must_leave():
    nw = _build_now()(_res(items=[_item(4, '避難指示')]), _tl(LEAVE), {})
    assert nw['state'] == 'alert' and nw['level'] == 4
    assert '避難指示' in nw['headline']
    assert 'いますぐ' in nw['detail'] or 'いますぐ' in nw['acts'][0]


def test_level4_when_may_stay_upstairs():
    """同じレベル4でも、留まれる場所には「外へ」と書かない。"""
    nw = _build_now()(_res(items=[_item(4, '避難指示')]), _tl(STAY), {'upper_floor': 1})
    assert nw['level'] == 4 and nw['leave'] is False
    assert any('上の階' in a for a in nw['acts'])
    assert not any('いますぐ全員で避難を完了' in a for a in nw['acts'])


def test_level3_starts_evacuation_for_care_households():
    nw = _build_now()(_res(items=[_item(3, '高齢者等避難')]), _tl(LEAVE), {'elderly': 1})
    assert nw['level'] == 3
    assert any('いま避難を始める' in a for a in nw['acts'])


def test_level3_without_care_does_not_order_evacuation():
    nw = _build_now()(_res(items=[_item(3, '高齢者等避難')]), _tl(STAY), {})
    assert nw['level'] == 3
    assert not any('いま避難を始める' in a for a in nw['acts'])


def test_level5_tells_not_to_go_outside():
    nw = _build_now()(_res(items=[_item(5, '緊急安全確保')]), _tl(LEAVE), {})
    assert nw['level'] == 5
    assert any('外に出ず' in a for a in nw['acts'])


def test_highest_level_wins_and_all_items_listed():
    items = [_item(3, '高齢者等避難', '矢田川'), _item(5, '緊急安全確保', '庄内川'), _item(4, '避難指示', '香流川')]
    nw = _build_now()(_res(items=items), _tl(LEAVE), {})
    assert nw['level'] == 5
    for target in ('矢田川', '庄内川', '香流川'):
        assert any(target in a for a in nw['acts'])


def test_stale_is_treated_as_live_but_labelled():
    """前回値（stale）でも判断は出す。画面側で「前回取得できた値」と断る。"""
    nw = _build_now()(_res(status='stale', items=[_item(4, '避難指示')]), _tl(LEAVE), {})
    assert nw['state'] == 'alert' and nw['level'] == 4
