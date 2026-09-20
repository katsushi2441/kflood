# -*- coding: utf-8 -*-
"""気象庁の警報・注意報（市区町村単位）。

出典: 気象庁 防災情報（bosai）の JSON。鍵は要らない。
  区域表   https://www.jma.go.jp/bosai/common/const/area.json
  警報等   https://www.jma.go.jp/bosai/warning/data/warning/<府県予報区コード>.json

**なぜ足したか（2026-09-21）**: 港区の防災ポータルは「警戒レベル相当情報」「避難情報」の枠が
常にあって、出ていないときは「ありません」と書く。名古屋市は発令が無いと枠ごと消えるので、
住民からは「出ていない」のか「取れていない」のか分からない。kflood は後者を区別する作りなので、
同じ考え方で気象警報・注意報の枠を足す。

**取得できないときは「発表なし」と言わない。** 市の避難情報（alerts.py）と同じ原則。

住所→市区町村コードは area.json の class20s（1,805件）を住所文字列に対する最長一致で引く。
政令市は区ではなく市の単位で発表されるので（名古屋市=2310000）、これで正しく当たる。
"""
import json
import os
import threading
import time
from datetime import datetime

import requests

AREA_URL = 'https://www.jma.go.jp/bosai/common/const/area.json'
WARN_URL = 'https://www.jma.go.jp/bosai/warning/data/warning/{office}.json'
SOURCE_NAME = '気象庁 防災情報'
SOURCE_URL = 'https://www.jma.go.jp/bosai/warning/'
UA = {'User-Agent': 'kflood/1.0 (kurage.exbridge.jp; jma)'}
CACHE_SEC = 180
AREA_MAX_AGE = 7 * 24 * 3600
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AREA_PATH = os.path.join(ROOT, 'data', 'jma_area.json')

# 気象庁の警報・注意報の種別コード。数字だけが JSON に入るので、ここで名前に直す。
CODES = {
    '00': '警報級の可能性', '02': '暴風雪警報', '03': '大雨警報', '04': '洪水警報',
    '05': '暴風警報', '06': '大雪警報', '07': '波浪警報', '08': '高潮警報',
    '10': '大雨注意報', '12': '大雪注意報', '13': '風雪注意報', '14': '雷注意報',
    '15': '強風注意報', '16': '波浪注意報', '17': '融雪注意報', '18': '洪水注意報',
    '19': '高潮注意報', '20': '濃霧注意報', '21': '乾燥注意報', '22': 'なだれ注意報',
    '23': '低温注意報', '24': '霜注意報', '25': '着氷注意報', '26': '着雪注意報',
    '27': 'その他の注意報', '32': '暴風雪特別警報', '33': '大雨特別警報',
    '35': '暴風特別警報', '36': '大雪特別警報', '37': '波浪特別警報', '38': '高潮特別警報',
}
# 画面の色と並びに使う。特別警報 > 警報 > 注意報。
SPECIAL = {'32', '33', '35', '36', '37', '38'}
WARNINGS = {'02', '03', '04', '05', '06', '07', '08'}

_lock = threading.Lock()
_cache = {}          # office -> {'at': float, 'data': dict}
_area = {'at': 0.0, 'd': None}


def _load_area():
    """区域表を読む。手元に無ければ取りに行き、data/jma_area.json に置く（週1で取り直す)。"""
    with _lock:
        if _area['d'] is not None and time.time() - _area['at'] < AREA_MAX_AGE:
            return _area['d']
        d = None
        try:
            st = os.path.getmtime(AREA_PATH)
            if time.time() - st < AREA_MAX_AGE:
                d = json.load(open(AREA_PATH, encoding='utf-8'))
        except (OSError, ValueError):
            d = None
        if d is None:
            try:
                r = requests.get(AREA_URL, headers=UA, timeout=20)
                r.raise_for_status()
                d = r.json()
                os.makedirs(os.path.dirname(AREA_PATH), exist_ok=True)
                with open(AREA_PATH, 'w', encoding='utf-8') as f:
                    json.dump(d, f, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                # 取りに行けなかったときは、古くても手元のものを使う（無ければ諦める）
                try:
                    d = json.load(open(AREA_PATH, encoding='utf-8'))
                except (OSError, ValueError):
                    return None
        _area.update(at=time.time(), d=d)
        return d


def office_for(muni_code: str):
    """市区町村コード（class20）から府県予報区コード（office）へ、親をたどる。"""
    d = _load_area()
    if not d:
        return None
    code = muni_code
    for key in ('class20s', 'class15s', 'class10s'):
        it = (d.get(key) or {}).get(code)
        if not it:
            return None
        code = it.get('parent')
    return code if code in (d.get('offices') or {}) else None


def muni_for_address(address: str):
    """住所の文字列から市区町村を引く（最長一致）。当たらなければ None。

    「愛知県名古屋市瑞穂区内浜町」→ 2310000（名古屋市）。政令市は市の単位で発表されるので、
    区名まで一致させない。同名の町村があるため、長い名前から先に見る。
    """
    d = _load_area()
    if not d or not address:
        return None
    best = None
    for code, it in (d.get('class20s') or {}).items():
        name = it.get('name') or ''
        if name and name in address and (best is None or len(name) > len(best[1])):
            best = (code, name)
    return dict(code=best[0], name=best[1]) if best else None


def fetch(office: str):
    """府県予報区の警報・注意報。取得できなければ前回値を stale で返す。"""
    now = time.time()
    with _lock:
        c = _cache.get(office)
        if c and now - c['at'] < CACHE_SEC:
            return c['data']
    try:
        r = requests.get(WARN_URL.format(office=office), headers=UA, timeout=12)
        r.raise_for_status()
        raw = r.json()
        data = dict(status='ok', raw=raw, fetched_at=datetime.now().strftime('%Y-%m-%d %H:%M'),
                    report_at=raw.get('reportDatetime'), headline=(raw.get('headlineText') or '').strip(),
                    office_name=raw.get('publishingOffice'))
    except Exception as e:  # noqa: BLE001
        with _lock:
            c = _cache.get(office)
        if c:
            data = dict(c['data'])
            data['status'] = 'stale'
            data['error'] = type(e).__name__
            return data
        return dict(status='unavailable', error=type(e).__name__, raw=None,
                    fetched_at=datetime.now().strftime('%Y-%m-%d %H:%M'), headline='', report_at=None)
    with _lock:
        _cache[office] = dict(at=now, data=data)
    return data


def _items_for(raw, muni_code):
    """その市区町村に出ている警報・注意報だけを、名前にして返す。解除・なしは落とす。"""
    out = []
    for at in (raw or {}).get('areaTypes', []):
        for a in at.get('areas', []):
            if a.get('code') != muni_code:
                continue
            for w in a.get('warnings', []):
                st = w.get('status') or ''
                if st in ('解除', 'なし', '',):
                    continue
                code = w.get('code')
                name = CODES.get(code)
                if not name:
                    continue
                kind = 'special' if code in SPECIAL else ('warning' if code in WARNINGS else 'advisory')
                out.append(dict(code=code, name=name, kind=kind, status=st))
    order = {'special': 0, 'warning': 1, 'advisory': 2}
    seen, uniq = set(), []
    for it in sorted(out, key=lambda x: (order[x['kind']], x['code'])):
        if it['code'] in seen:
            continue
        seen.add(it['code'])
        uniq.append(it)
    return uniq


def status_for(address: str):
    """住所ひとつぶんの「いまの気象警報・注意報」。**枠は常に返す**（空でも返す）。

    status: ok（発表あり/なしを言い切れる）／stale（前回値）／unavailable（取得できない）／
            uncovered（市区町村を特定できない）
    """
    base = dict(status='uncovered', muni=None, muni_name=None, items=[], headline='',
                fetched_at=None, report_at=None, source=SOURCE_NAME, source_url=SOURCE_URL, office=None)
    m = muni_for_address(address or '')
    if not m:
        return base
    office = office_for(m['code'])
    if not office:
        return base
    d = fetch(office)
    base.update(muni=m['code'], muni_name=m['name'], office=office, status=d.get('status'),
                fetched_at=d.get('fetched_at'), report_at=d.get('report_at'),
                headline=d.get('headline') or '', office_name=d.get('office_name'))
    if d.get('status') in ('ok', 'stale'):
        base['items'] = _items_for(d.get('raw'), m['code'])
    return base


def max_kind(items):
    """いちばん重いものを返す（色を決めるのに使う）。"""
    for k in ('special', 'warning', 'advisory'):
        if any(i['kind'] == k for i in items or []):
            return k
    return None
