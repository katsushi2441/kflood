# -*- coding: utf-8 -*-
"""東京都・千葉県の「いま出ている避難情報」。

名古屋市は市の災害情報配信を学区単位で読む（alerts.py）。東京都と千葉県は、
**都県が区市町村の発令を集約して公開している**ので、1つの入口で全市区町村ぶんが取れる。

  東京都 https://www.bousai.metro.tokyo.lg.jp/ev/pc/tlist.html
    一覧に「日時 / [新規|更新] / 区市町村:避難情報」が並び、詳細 dtail_<n>.html に
    <dl> で「発令区分・発令／解除・発令・解除日時・発令地区・対象世帯(戸)数・対象人数」。
    **発令地区は町丁目**（池之端１丁目）まである。名古屋の学区より細かい。

  千葉県 https://www.bousai.pref.chiba.lg.jp/
    トップの表に「地域 / 市町村 / 発表時刻 / 種別」が並び、
    詳細 PUB_VF_Detail_Hinan?pid=... に「<市町村><地区>：<種別> 警戒レベルN 発令(時刻) 対象世帯数 対象人数」。

使うのは事実（区市町村・地区・発令区分・レベル・日時・対象世帯/人数）だけ。
都県サイトの文章は転載しない。**取得できないときは「発令なし」と言わず「取得できない」と返す。**
黙って安全側に倒さない。

町丁目の表記ゆれ（池之端１丁目 ⇔ 池之端一丁目 ⇔ 池之端1丁目）は match_area() で吸収する。
"""
import html
import re
import threading
import time
from datetime import datetime

import requests

UA = {'User-Agent': 'kflood/1.0 (kurage.exbridge.jp; alerts)'}
CACHE_SEC = 180
TIMEOUT = 12
MAX_DETAIL = 60          # 一度に開く詳細ページの上限（台風時は件数が増える）

TOKYO_LIST = 'https://www.bousai.metro.tokyo.lg.jp/ev/pc/tlist.html'
TOKYO_BASE = 'https://www.bousai.metro.tokyo.lg.jp/ev/pc/'
TOKYO_NAME = '東京都防災ホームページ 避難情報一覧'
CHIBA_TOP = 'https://www.bousai.pref.chiba.lg.jp/'
CHIBA_DETAIL = 'https://www.bousai.pref.chiba.lg.jp/PUB_VF_Detail_Hinan?pid={pid}&type='
CHIBA_NAME = '千葉県防災ポータルサイト'

# 発令区分 → 警戒レベル。**「解除」はレベルを持たない**ので items に入れない（notes に回す）。
LEVELS = [
    (5, ('緊急安全確保',)),
    (4, ('避難指示', '避難勧告', '避難指示（緊急）')),
    (3, ('高齢者等避難', '高齢避難', '避難準備', '高齢者等避難開始')),
]
LEVEL_LABEL = {3: '高齢者等避難', 4: '避難指示', 5: '緊急安全確保'}

_lock = threading.Lock()
_cache = {}

_ZEN = str.maketrans('０１２３４５６７８９', '0123456789')
_KANJI = {'一': '1', '二': '2', '三': '3', '四': '4', '五': '5',
          '六': '6', '七': '7', '八': '8', '九': '9', '十': '10'}


def _level_of(text):
    for lv, words in LEVELS:
        if any(w in text for w in words):
            return lv
    return None


def norm_area(s):
    """地区名を突き合わせ用にそろえる。全角数字・漢数字・「丁目」の有無を吸収する。"""
    if not s:
        return ''
    s = html.unescape(s).strip().translate(_ZEN)
    s = re.sub(r'[\s　]', '', s)
    # 「一丁目」→「1丁目」。十一丁目のような2桁は素直に置く
    s = re.sub(r'([一二三四五六七八九十]+)丁目',
               lambda m: (_KANJI.get(m.group(1)) or
                          (str(10 + int(_KANJI.get(m.group(1)[1], '0'))) if m.group(1).startswith('十') and len(m.group(1)) > 1
                           else _KANJI.get(m.group(1), m.group(1)))) + '丁目', s)
    s = s.replace('丁目', '')
    return s


def match_area(addr, area):
    """住所 addr が、発令地区 area に当てはまるか。

    「全域」「市内全域」は市区町村が一致していればすべて当てはまる。
    それ以外は地区名（町丁目）が住所に含まれるかで見る。
    """
    if not area:
        return False
    a = norm_area(area)
    if not a or '全域' in a or a in ('全市', '全区', '全町', '全村'):
        return True
    return norm_area(addr).find(a) >= 0


def _get(url):
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r.text


def _jst(text):
    m = re.search(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})\s+(\d{1,2}):(\d{2})', text)
    if m:
        y, mo, d, h, mi = map(int, m.groups())
        try:
            return datetime(y, mo, d, h, mi).isoformat(timespec='minutes')
        except ValueError:
            return None
    return None


# ── 東京都 ────────────────────────────────────────────
def parse_tokyo_list(text):
    """一覧から (詳細ページ, 区市町村, 更新種別) を取る。新しい順。"""
    out = []
    body = text[text.find('ここから本文'):] if 'ここから本文' in text else text
    for m in re.finditer(r'<li>\s*\[(新規|更新|解除)\]\s*<a href="(dtail_\d+\.html)">([^<]*?)</a>', body):
        kind, href, label = m.group(1), m.group(2), html.unescape(m.group(3))
        city = label.split(':')[0].split('：')[0].strip()
        out.append(dict(href=href, city=city, kind=kind))
    return out


def parse_tokyo_detail(text, city):
    """詳細ページの <dl> を1件ずつ読む。解除は items に入れず notes に回す。"""
    items, notes = [], []
    for block in re.findall(r'<dl>(.*?)</dl>', text, re.S):
        d = {}
        for k, v in re.findall(r'<dt>(.*?)</dt>\s*<dd>(.*?)</dd>', block, re.S):
            d[re.sub(r'<[^>]+>', '', k).strip()] = html.unescape(re.sub(r'<[^>]+>', '', v)).strip()
        kubun = d.get('発令区分', '')
        state = d.get('発令／解除', d.get('発令/解除', ''))
        area = d.get('発令地区', '')
        at = _jst(d.get('発令・解除日時', ''))
        lv = _level_of(kubun)
        if '解除' in state or lv is None:
            if kubun or area:
                notes.append(f'{city} {area} {kubun} {state}'.strip())
            continue
        items.append(dict(city=city, area=area, level=lv, label=LEVEL_LABEL.get(lv, kubun),
                          issued_at=at,
                          households=_int(d.get('対象世帯(戸)数') or d.get('対象世帯数')),
                          people=_int(d.get('対象人数'))))
    return items, notes


def _int(s):
    if not s:
        return None
    m = re.search(r'([\d,]+)', s.translate(_ZEN))
    return int(m.group(1).replace(',', '')) if m else None


def tokyo(city_filter=None):
    return _cached('tokyo', lambda: _fetch_tokyo(city_filter), city_filter)


def _fetch_tokyo(city_filter):
    listing = parse_tokyo_list(_get(TOKYO_LIST))
    items, notes = [], []
    opened = 0
    for row in listing:
        if city_filter and row['city'] != city_filter:
            continue
        if opened >= MAX_DETAIL:
            break
        opened += 1
        try:
            it, nt = parse_tokyo_detail(_get(TOKYO_BASE + row['href']), row['city'])
        except Exception:      # 1ページ落ちても全体は返す
            continue
        items += it
        notes += nt
    items.sort(key=lambda x: (-x['level'], x['issued_at'] or ''))
    return dict(status='ok', items=items, notes=notes, source=TOKYO_NAME, source_url=TOKYO_LIST,
                cities=sorted({r['city'] for r in listing}))


# ── 千葉県 ────────────────────────────────────────────
def parse_chiba_top(text):
    """トップの表から (pid, 市町村) を取る。

    **市町村名はリンクのテキストそのもの**（<td class="tdL"><a href="...pid=...">千葉市</a></td>）。
    以前は pid の手前のセルを後ろから探していたが、表が2列組みのため隣の市の名前を拾い、
    大多喜町の発令を「いすみ市」として返していた（2026-09-21）。**リンクの中身を使う。**
    同じ入口に避難所の開設情報も混ざるので、それは詳細ページ側で弾く。
    """
    out, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="/PUB_VF_Detail_Hinan\?pid=([A-Za-z0-9]+)[^"]*"[^>]*>(.*?)</a>', text, re.S):
        pid = m.group(1)
        if pid in seen:
            continue
        seen.add(pid)
        city = _clean_city(html.unescape(re.sub(r'<[^>]+>', '', m.group(2))).strip())
        out.append(dict(pid=pid, city=city or None))
    return out


# 千葉県の詳細ページの本文。**改行位置が中途半端で、1件が複数行にまたがる**
# （「発令( 2026/09/21 00:20」で改行し、次の行が「) 対象世帯数:16828世帯 対象人数:33666人」）。
# 行で切ると件と数字がずれるので、**改行を潰してから1件ずつ拾う**。
CHIBA_ITEM = re.compile(
    # 1件の前は「…されました。」か、前の件の末尾「対象人数:N人 」で終わる。そこから地区名が始まる。
    # 地区名には括弧が入る（中央区（土砂災害警戒区域）【R8.5.29～】）ので、括弧は除外しない。
    r'(?:^|。\s*|人\s+|\)\s+)(?P<area>[^：:。]{1,60}?)[：:]\s*'
    r'(?P<kind>緊急安全確保|避難指示|避難勧告|高齢者等避難|高齢避難|避難準備)\s*'
    r'警戒レベル\s*(?P<lv>[0-9０-９])\s*'
    r'(?P<state>発令|解除)\s*\(\s*(?P<at>[\d/]{8,10}\s+[\d:]{4,5})\s*\)'
    r'(?:[^。]{0,40}?対象世帯数[:：]?\s*(?P<hh>[\d,]+)\s*世帯)?'
    r'(?:[^。]{0,40}?対象人数[:：]?\s*(?P<pp>[\d,]+)\s*人)?')


def _clean_city(name):
    """「[訂正]山武市」「【更新】市原市」のような接頭辞を落とす。"""
    if not name:
        return name
    return re.sub(r'^[\[\uff3b【(（][^\]\uff3d】)）]{0,8}[\]\uff3d】)）]\s*', '', name).strip()


def parse_chiba_detail(text, city_hint=None):
    """詳細ページから「<地区>：<種別> 警戒レベルN 発令(時刻) 対象世帯数 対象人数」を読む。"""
    body = re.sub(r'<script.*?</script>|<style.*?</style>', '', text, flags=re.S)
    body = html.unescape(re.sub(r'<[^>]+>', ' ', body))
    body = re.sub(r'\s+', ' ', body)
    # 同じ入口に避難所の開設情報も流れてくる。避難情報として読むと「〇〇センター：避難所 開設」を
    # 発令と取り違えるので、ここで弾く
    if '避難所情報' in body and '避難情報（詳細）' not in body:
        return [], []
    city = city_hint
    m = re.search(r'([^\s：:]{2,10}?[市町村])\s*避難情報', body)
    if m:
        city = _clean_city(m.group(1))
    items, notes = [], []
    for g in CHIBA_ITEM.finditer(body):
        area = g.group('area').strip()
        # 「いすみ市全域」のように市名が頭に付く。落とすと「全域」だけが残る
        if city and area.startswith(city):
            area = area[len(city):].strip() or '全域'
        area = re.sub(r'^内(全域)?$', '全域', area) or '全域'
        lv = int(g.group('lv').translate(_ZEN))
        if g.group('state') == '解除' or lv not in LEVEL_LABEL:
            notes.append(f"{city or ''} {area} {g.group('kind')} {g.group('state')}".strip())
            continue
        items.append(dict(city=city, area=area, level=lv, label=LEVEL_LABEL.get(lv, g.group('kind')),
                          issued_at=_jst(g.group('at')),
                          households=_int(g.group('hh') or ''), people=_int(g.group('pp') or '')))
    return items, notes


def chiba(city_filter=None):
    return _cached('chiba', lambda: _fetch_chiba(city_filter), city_filter)


def _fetch_chiba(city_filter):
    rows = parse_chiba_top(_get(CHIBA_TOP))
    items, notes = [], []
    opened = 0
    for row in rows:
        if city_filter and row['city'] != city_filter:
            continue
        if opened >= MAX_DETAIL:
            break
        opened += 1
        try:
            it, nt = parse_chiba_detail(_get(CHIBA_DETAIL.format(pid=row['pid'])), row['city'])
        except Exception:
            continue
        for x in it:
            if not x.get('city'):
                x['city'] = row['city']
        items += it
        notes += nt
    items.sort(key=lambda x: (-x['level'], x['issued_at'] or ''))
    return dict(status='ok', items=items, notes=notes, source=CHIBA_NAME, source_url=CHIBA_TOP,
                cities=sorted({r['city'] for r in rows if r['city']}))


# ── 共通 ──────────────────────────────────────────────
def _cached(name, fn, key=None):
    """取れなければ前回値を stale として返し、それも無ければ unavailable。発令なしとは書かない。"""
    ck = f'{name}:{key or "*"}'
    now = time.time()
    with _lock:
        c = _cache.get(ck)
        if c and now - c['at'] < CACHE_SEC:
            return c['data']
    try:
        data = fn()
        data['fetched_at'] = datetime.now().isoformat(timespec='seconds')
        with _lock:
            _cache[ck] = dict(at=now, data=data)
        return data
    except Exception as e:  # noqa: BLE001
        with _lock:
            c = _cache.get(ck)
        if c:
            d = dict(c['data'])
            d['status'] = 'stale'
            return d
        return dict(status='unavailable', items=[], notes=[], cities=[],
                    source=TOKYO_NAME if name == 'tokyo' else CHIBA_NAME,
                    source_url=TOKYO_LIST if name == 'tokyo' else CHIBA_TOP,
                    fetched_at=datetime.now().isoformat(timespec='seconds'), error=str(e)[:200])


def for_address(pref, city, address):
    """住所に当てはまる発令だけを返す。対応していない都道府県は None（＝未収録）。"""
    if pref == '東京都':
        data = tokyo(city_filter=city)
    elif pref == '千葉県':
        data = chiba(city_filter=city)
    else:
        return None
    out = dict(data)
    out['items'] = [i for i in data.get('items', [])
                    if (not i.get('city') or i['city'] == city) and match_area(address, i.get('area'))]
    out['city_items'] = [i for i in data.get('items', []) if not i.get('city') or i['city'] == city]
    return out
