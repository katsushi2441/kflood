# -*- coding: utf-8 -*-
"""名古屋市の「いま出ている避難情報」（警戒レベル × 河川 × 対象学区）。

出典: 名古屋市 災害情報配信 https://www.city.nagoya.jp/1000103.html
  <div class="saigaitopic"><h3 id="groupN">警戒レベル4・避難指示（天白川）<span class="textright">9月8日 18時10分配信</span></h3>
  <p>【発令内容】…【理由】…【行動要請】…【対象学区】<br>瑞穂区：高田,堀田,…<br>中村区：全学区<br>…</p></div>
  という定型（2026-09-08 実測。同じ内容がすぐメールPlus+のバックナンバーにも流れる）。

使うのは事実（レベル・種別・河川/災害・発令時刻・対象学区）だけ。市の文言は表示しない（市サイト本文は転載しない原則）。
取得できないときは「発令なし」と言わず「取得できない」と返す（黙って安全側に倒さない）。
"""
import html
import re
import threading
import time
from datetime import date, datetime

import requests

SOURCE_URL = 'https://www.city.nagoya.jp/1000103.html'
SOURCE_NAME = '名古屋市 災害情報配信'
UA = {'User-Agent': 'kflood/1.0 (kurage.exbridge.jp; alerts)'}
CACHE_SEC = 180
LEVEL_LABEL = {3: '高齢者等避難', 4: '避難指示', 5: '緊急安全確保'}
_lock = threading.Lock()
_cache = {'at': 0.0, 'data': None}


def _jst_datetime(text: str, year: int):
    m = re.search(r'(\d{1,2})月(\d{1,2})日\s*(\d{1,2})時(\d{1,2})分', text)
    if not m:
        return None
    mo, d, h, mi = map(int, m.groups())
    try:
        return datetime(year, mo, d, h, mi)
    except ValueError:
        return None


def parse(html_text: str, year: int = None) -> dict:
    """発令一覧を dict にする。解除・本部設置などレベルの無い項目は items に入れない（note に残す）。"""
    year = year or date.today().year
    body = html_text[html_text.find('ここから本文'):] if 'ここから本文' in html_text else html_text
    items, notes = [], []
    for block in re.findall(r'<div class="saigaitopic">(.*?)</div>', body, re.S):
        h = re.search(r'<h3[^>]*>(.*?)</h3>', block, re.S)
        if not h:
            continue
        title_html = h.group(1)
        issued = _jst_datetime(re.sub(r'<[^>]+>', ' ', title_html), year)
        title = html.unescape(re.sub(r'<span.*?</span>', '', title_html, flags=re.S))
        title = re.sub(r'<[^>]+>', '', title).strip()
        if '解除' in title:
            # 「警戒レベル4・避難指示を解除（矢田川）」は正規表現に一致してしまうので、先に除外する
            # （2026-09-09 実測: 解除通知がレベル4の発令として表示されていた）
            mt = re.search(r'[（(]([^）)]+)[）)]', title)
            notes.append(dict(title=title, target=mt.group(1).strip() if mt else None,
                              issued_at=issued.isoformat() if issued else None, kind='解除'))
            continue
        m = re.match(r'警戒レベル\s*(\d)\s*[・･]\s*([^（(]+)[（(]([^）)]+)[）)]', title)
        if not m:
            continue
        level = int(m.group(1))
        kind = m.group(2).strip()
        target = m.group(3).strip()      # 河川名 または 「土砂災害」など
        text = html.unescape(re.sub(r'<br\s*/?>', '\n', block))
        text = re.sub(r'<[^>]+>', '', text)
        wards = {}
        seg = text.split('【対象学区】', 1)
        if len(seg) == 2:
            for ln in seg[1].split('\n'):
                ln = ln.strip()
                mm = re.match(r'([^\s：:]+区)\s*[：:]\s*(.+)$', ln)
                if not mm:
                    if ln.startswith('【'):
                        break
                    continue
                ward = mm.group(1)
                names = [x.strip() for x in re.split(r'[,、，]', mm.group(2)) if x.strip()]
                wards[ward] = ['*'] if any(n == '全学区' for n in names) else names
        items.append(dict(level=level, label=LEVEL_LABEL.get(level, kind), kind=kind, target=target,
                          issued_at=issued.isoformat() if issued else None, wards=wards))
    return dict(items=items, notes=notes, source=SOURCE_NAME, source_url=SOURCE_URL)


def fetch(force: bool = False) -> dict:
    """キャッシュ付き取得。失敗時は status='unavailable'（発令なし、ではない）。"""
    with _lock:
        now = time.time()
        if not force and _cache['data'] and now - _cache['at'] < CACHE_SEC:
            return _cache['data']
        try:
            r = requests.get(SOURCE_URL, headers=UA, timeout=8)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or 'utf-8'
            d = parse(r.text)
            d.update(status='ok', fetched_at=datetime.now().strftime('%Y-%m-%d %H:%M'))
        except Exception as e:  # noqa: BLE001
            d = dict(status='unavailable', error=type(e).__name__, items=[], notes=[], source=SOURCE_NAME, source_url=SOURCE_URL,
                     fetched_at=datetime.now().strftime('%Y-%m-%d %H:%M'))
            if _cache['data'] and _cache['data'].get('status') == 'ok' and now - _cache['at'] < 3600:
                d = dict(_cache['data'], status='stale')   # 1時間以内の前回値は「古い」と明示して使う
        _cache['at'], _cache['data'] = now, d
        return d


def for_gakku(data: dict, ward: str, name: str) -> list:
    """その学区に出ている発令だけ。レベルの高い順・新しい順。"""
    out = []
    for it in data.get('items', []):
        names = it['wards'].get(ward)
        if names is None:
            continue
        if '*' in names or name in names:
            out.append(it)
    out.sort(key=lambda x: (-x['level'], x['issued_at'] or ''))
    return out


def max_level(alerts: list):
    return max((a['level'] for a in alerts), default=None)
