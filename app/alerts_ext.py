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

  静岡県 https://www.bousai-portal.pref.shizuoka.jp/api/...
    **JSON API が公開されている**（スクレイプ不要）。
      /master/getCities            … 市町のID→名前（35件）
      /evacuation/getSummaryList   … いま出ている発令（市町ID・発表時刻・種別・対象世帯/人数）
      /evacuation/getReports       … 発令の理由と対象（guideLine「避難指示（土砂災害（特別）警戒区域）」）
      /evacuation/getAreas         … 地区の一覧。**空のことが多い**（市町全体または区域指定の発令）

  神奈川県 https://www.bousai.pref.kanagawa.jp/K_PUB_VF_HinanKankokuList
    千葉県と**同じ基盤**だが、一覧は表ではなく <dl><a><dt>種別</dt><dd>日時 + 市区町村</dd></a></dl>。
    詳細の書式もわずかに違い、**「避難指示 （警戒レベル４）」と括弧が付く**。対象世帯数は「－」のことが多い。
    市区町村は政令市だと**区まで**入る（横浜市神奈川区）。町丁目単位で「宝町の一部（内水）（浸水害）」まで分かる。

使うのは事実（区市町村・地区・発令区分・レベル・日時・対象世帯/人数）だけ。
都県サイトの文章は転載しない。**取得できないときは「発令なし」と言わず「取得できない」と返す。**
黙って安全側に倒さない。

町丁目の表記ゆれ（池之端１丁目 ⇔ 池之端一丁目 ⇔ 池之端1丁目）は match_area() で吸収する。
"""
import html
import re
import threading
import time
from datetime import datetime, timedelta

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


# 発令地区には但し書きが付く。「宝町の一部（内水）（浸水害）」「中央区（土砂災害警戒区域）【R8.5.29～】」。
# 突き合わせるのは町名の部分だけだが、**「の一部」は町の一部しか対象でない**ので区別する。
QUALIFIER = re.compile(r'[（(「【\[][^）)」】\]]*[)）」】\]]')


def area_core(area):
    """地区名から但し書きを落として、町丁目の部分だけ残す。"""
    a = norm_area(area)
    a = QUALIFIER.sub('', a)
    a = re.sub(r'の一部.*$|地区$|付近$', '', a)
    return a.strip('　 ・')


def match_area(addr, area):
    """住所 addr が発令地区 area に当てはまるか。'full' / 'partial' / False を返す。

    「全域」「市内全域」は市区町村が一致していればすべて当てはまる（full）。
    町名は一致するが対象が「その町の一部」のときは partial。
    **partial を full と混ぜない。** 「あなたは避難指示の対象です」と言い切ってしまうため。
    """
    if not area:
        return False
    a = norm_area(area)
    if not a or '全域' in a or a in ('全市', '全区', '全町', '全村'):
        return 'full'
    if '地区は' in a or '対象は' in a or '警戒区域' in a or '浸水想定' in a:
        # 市区町村の一部だけが対象だが、どこかは自治体の地図でしか分からない
        return 'partial'
    core = area_core(area)
    if not core or len(core) < 2:
        return False
    if norm_area(addr).find(core) < 0:
        return False
    return 'partial' if ('一部' in a or QUALIFIER.search(a)) else 'full' 


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
    r'[（(]?\s*警戒レベル\s*(?P<lv>[0-9０-９])\s*[)）]?\s*'
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
        if city_filter and row['city'] and row['city'] != city_filter and row['city'] not in city_filter:
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


# ── 神奈川県 ──────────────────────────────────────────
# **一覧が2つあり、役割が違う。**
#   トップ（/）の「避難発令」タブ … *いま出ている* 市区町村と種別・発表時刻。ただし地区は載っていない
#   /K_PUB_VF_HinanKankokuList    … その年度の *履歴*。詳細（地区）へのリンクがあるが、解除済みも混ざる
# 履歴だけを読むと6月に解除された発令まで「いま出ている」ことにしてしまう（2026-09-21 実際にやった）。
# だから **現況をトップで確定し、その発表時刻に一致する履歴の詳細だけ** を開いて地区を取る。
KANAGAWA_TOP = 'https://www.bousai.pref.kanagawa.jp/'
KANAGAWA_LIST = 'https://www.bousai.pref.kanagawa.jp/K_PUB_VF_HinanKankokuList'
KANAGAWA_DETAIL = 'https://www.bousai.pref.kanagawa.jp/PUB_VF_Detail_Hinan?pid={pid}&type='
KANAGAWA_NAME = '神奈川県災害情報ポータル'
KANAGAWA_KIND = {'saigai': 5, 'shiji': 4, 'junbi': 3}


def parse_kanagawa_top(text):
    """トップの「避難発令」タブから、いま出ている (市区町村, 発表時刻 MM/DD HH:MM, レベル) を取る。"""
    j = text.find('id="Hinan"')
    if j < 0:
        return []
    seg = re.sub(r'<style.*?</style>|<script.*?</script>', '', text[j:j + 200000], flags=re.S)
    out = []
    for cell in re.split(r'<td class="tdL">', seg)[1:]:
        mc = re.search(r'<a class="cityName"[^>]*>(.*?)</a>', cell, re.S)
        if not mc:
            continue
        city = _clean_city(html.unescape(re.sub(r'<[^>]+>', '', mc.group(1))).strip())
        head = cell[:cell.find('<td class="tdL">')] if '<td class="tdL">' in cell else cell
        mt = re.search(r'(\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2})\s*発表', html.unescape(re.sub(r'<[^>]+>', ' ', head)))
        levels = sorted({KANAGAWA_KIND[k] for k in KANAGAWA_KIND
                         if re.search(r'class="kankokuWarning ' + k + r'"', head)}, reverse=True)
        if city and levels:
            out.append(dict(city=city, at=mt.group(1) if mt else None, levels=levels))
    return out


def parse_kanagawa_list(text):
    """履歴一覧から (pid, 市区町村, 発表時刻 YYYY/MM/DD HH:MM) を取る。"""
    out, seen = [], set()
    for m in re.finditer(r'<a href="/PUB_VF_Detail_Hinan\?pid=([A-Za-z0-9]+)[^"]*">(.*?)</a>', text, re.S):
        pid = m.group(1)
        if pid in seen:
            continue
        seen.add(pid)
        inner = re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', m.group(2)))).strip()
        mc = re.search(r'([^\s]{2,12}?[市区町村])\s*避難情報', inner)
        mt = re.search(r'(\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2})', inner)
        out.append(dict(pid=pid, city=_clean_city(mc.group(1)) if mc else None,
                        at=mt.group(1) if mt else None))
    return out


def kanagawa(city_filter=None):
    return _cached('kanagawa', lambda: _fetch_kanagawa(city_filter), city_filter)


def _kanagawa_live():
    # いま発令が出ている市区町村。**住所に関係なく1回だけ取る**（住所ごとに取り直さない）
    return _cached('kanagawa_live', lambda: dict(status='ok', items=[], notes=[], cities=[],
                                                 live=parse_kanagawa_top(_get(KANAGAWA_TOP)),
                                                 source=KANAGAWA_NAME, source_url=KANAGAWA_TOP))


def _kanagawa_hist():
    return _cached('kanagawa_hist', lambda: dict(status='ok', items=[], notes=[], cities=[],
                                                 hist=parse_kanagawa_list(_get(KANAGAWA_LIST)),
                                                 source=KANAGAWA_NAME, source_url=KANAGAWA_LIST))


def _fetch_kanagawa(addr):
    live = _kanagawa_live().get('live') or []
    if addr:
        live = [r for r in live if r['city'] and r['city'] in addr]
    if not live:
        return dict(status='ok', items=[], notes=[], source=KANAGAWA_NAME, source_url=KANAGAWA_TOP, cities=[])
    hist = _kanagawa_hist().get('hist') or []
    items, notes = [], []
    opened = 0
    for row in live:
        # 履歴の中から、同じ市区町村で発表時刻が一致するものを探す（MM/DD HH:MM で突き合わせる）
        pid = None
        for h in hist:
            if h['city'] == row['city'] and h['at'] and row['at'] and h['at'][5:] == row['at']:
                pid = h['pid']
                break
        if pid and opened < MAX_DETAIL:
            opened += 1
            ok = True
            try:
                it, nt = parse_chiba_detail(_get(KANAGAWA_DETAIL.format(pid=pid)), row['city'])
            except Exception:
                it, nt, ok = [], [], False
            for x in it:
                x['city'] = row['city']
            if it:
                items += it
                notes += nt
                continue
            if ok and nt:
                # 詳細が読めて、書かれている地区が全部「解除」のとき。県のトップの「避難発令」タブには
                # 解除だけの報も残るので、そこを「市区町村ぜんぶに発令中」と読まない
                # （2026-09-28 横浜市中区・港北区・戸塚区：9/22 の解除報を6日間「避難指示」と出していた）
                notes += nt
                continue
        # 地区まで分からないときは、市区町村ぜんぶを対象として返す。**黙って落とさない。**
        for lv in row['levels']:
            items.append(dict(city=row['city'], area='地区は県のページで確認', level=lv,
                              label=LEVEL_LABEL.get(lv, ''), issued_at=None,
                              households=None, people=None, whole_city=True))
    items.sort(key=lambda x: (-x['level'], x['issued_at'] or ''))
    return dict(status='ok', items=items, notes=notes, source=KANAGAWA_NAME, source_url=KANAGAWA_TOP,
                cities=sorted({r['city'] for r in live if r['city']}))


# ── 静岡県 ────────────────────────────────────────────
SHIZUOKA_API = 'https://www.bousai-portal.pref.shizuoka.jp/api'
SHIZUOKA_NAME = '静岡県防災ポータル'
SHIZUOKA_URL = 'https://www.bousai-portal.pref.shizuoka.jp/evacuation'


def _json(url):
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _shizuoka_cities():
    # 市町の ID→名前。1日に何度も変わるものではないが、他と同じ3分キャッシュで足りる
    def fetch():
        d = _json(f'{SHIZUOKA_API}/master/getCities')
        m = {}
        for grp in d.get('records', []):
            for c in grp.get('cities', []):
                m[c['id']] = c['name']
        return dict(status='ok', items=[], notes=[], cities=[], cmap=m,
                    source=SHIZUOKA_NAME, source_url=SHIZUOKA_URL)
    return _cached('shizuoka_cities', fetch)


def shizuoka(city_filter=None):
    return _cached('shizuoka', lambda: _fetch_shizuoka(city_filter), city_filter)


def _fetch_shizuoka(addr):
    cmap = _shizuoka_cities().get('cmap') or {}
    summary = _json(f'{SHIZUOKA_API}/evacuation/getSummaryList').get('records', [])
    items, notes = [], []
    opened = 0
    for r in summary:
        city = cmap.get(r.get('organizationId')) or ''
        if addr and city and city not in addr:
            continue
        # 対象の区域は getAreas に入っていることもあるが、**空のことが多い**。
        # そのときは getReports の guideLine（「避難指示（土砂災害（特別）警戒区域）」）を使う。
        area = ''
        if opened < MAX_DETAIL:
            opened += 1
            try:
                areas = _json(f'{SHIZUOKA_API}/evacuation/getAreas?evacuationId={r["id"]}').get('records', [])
                area = '・'.join(a.get('name') or '' for a in areas if a.get('name'))
            except Exception:
                areas = []
            if not area:
                try:
                    reps = _json(f'{SHIZUOKA_API}/evacuation/getReports'
                                 f'?organizationId={r["organizationId"]}').get('records', [])
                    rep = next((x for x in reps if x.get('id') == r['id']), (reps[0] if reps else None))
                    area = (rep or {}).get('guideLine') or ''
                except Exception:
                    pass
        for kind in (r.get('announceTypes') or []):
            lv = _level_of(kind)
            if lv is None:
                notes.append(f'{city} {kind}')
                continue
            limited = bool(re.search(r'警戒区域|浸水想定|区域|地区|沿い|流域', area))
            items.append(dict(city=city, area=area or '対象は県のページで確認', level=lv,
                              label=LEVEL_LABEL.get(lv, kind), issued_at=_jst_iso(r.get('reportDateTime')),
                              households=r.get('cityHousehold'), people=r.get('cityPeople'),
                              # **市町単位の発令。** どの地区かは県のAPIに入っていないので、
                              # 住所での突き合わせはせず、市町が一致すれば該当とする
                              city_wide=True, limited=limited))
    items.sort(key=lambda x: (-x['level'], x['issued_at'] or ''))
    return dict(status='ok', items=items, notes=notes, source=SHIZUOKA_NAME, source_url=SHIZUOKA_URL,
                cities=sorted({cmap.get(r.get('organizationId')) for r in summary if cmap.get(r.get('organizationId'))}))


def _jst_iso(t):
    """API は UTC（…Z）で返す。表示は日本時間なので +9 する。"""
    if not t:
        return None
    try:
        dt = datetime.strptime(t[:19], '%Y-%m-%dT%H:%M:%S') + timedelta(hours=9)
        return dt.isoformat(timespec='minutes')
    except ValueError:
        return None


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
    elif pref == '神奈川県':
        data = kanagawa(city_filter=address)
    elif pref == '静岡県':
        data = shizuoka(city_filter=address)
    else:
        return None
    # **市区町村名は「住所に含まれるか」で見る。** 政令市では都県側が「横浜市神奈川区」と
    # 区まで書くのに、住所の分割は「横浜市」までしか返さない。等号で比べると全部外れる。
    def same_city(i):
        c = i.get('city')
        return (not c) or (c in (address or '')) or (city and (c == city or c.startswith(city)))
    out = dict(data)
    hit = []
    for i in data.get('items', []):
        if not same_city(i):
            continue
        if i.get('city_wide') or i.get('whole_city'):
            # 市区町村ぜんぶが対象（またはどの地区かが公表データに無い）。地区では絞らない
            m = 'partial' if i.get('limited') or i.get('whole_city') else 'full'
        else:
            m = match_area(address, i.get('area'))
        if m:
            x = dict(i)
            x['partial'] = (m == 'partial')
            hit.append(x)
    out['items'] = hit
    out['city_items'] = [i for i in data.get('items', []) if same_city(i)]
    return out
