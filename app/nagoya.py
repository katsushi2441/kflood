# -*- coding: utf-8 -*-
"""名古屋市版の受け皿ページ（区・河川・市）のためのデータ層。

検索は「天白川 氾濫」「矢田川 氾濫」「名古屋 浸水／冠水」「中川区 避難所」のように、河川名・区名で来る
（Keyword Planner 2026-09-09 実測: 天白川 氾濫 210／矢田川 氾濫 110／名古屋 冠水 70／名古屋 浸水 70／名古屋 ハザードマップ 洪水 40）。
住所入力の画面だけでは検索に出ないので、河川ごと・区ごとに固定URLのページを持つ。

- 発令の履歴と「河川→対象学区」の対応は、市の災害情報配信から取れた事実を SQLite に貯める（data/alerts_history.sqlite）。
  発令が解除された後も「この川の流域はどの学区か」「いつ何が出たか」を出せる。
- 初回は 2026-09-08 の実際の発令（tests/fixtures）を種として入れる。
"""
import json
import os
import re
import sqlite3
import threading
import urllib.parse
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(os.environ.get('KFLOOD_DATA_DIR', os.path.join(ROOT, 'data')), 'alerts_history.sqlite')
WARDS_PATH = os.path.join(ROOT, 'data', 'nagoya_wards.json')
FIXTURE = os.path.join(ROOT, 'tests', 'fixtures', 'nagoya_saigai_20260908.html')
CITY = '名古屋市'
_lock = threading.Lock()

RIVER_SLUG = {
    '天白川': 'tempaku', '矢田川': 'yada', '植田川': 'ueda', '大山川': 'oyama', '扇川': 'ogi', '蟹江川': 'kanie', '新川': 'shinkawa',
    '香流川': 'kanare', '隅除川': 'sumiyoke', '堀川・新堀川': 'horikawa', '新地蔵川': 'shinjizo', '山崎川': 'yamazaki', '八田川': 'hatta',
    '天神川': 'tenjin', '守山川': 'moriyama', '水場川': 'mizuba', '庄内川': 'shonai', '荒子川': 'arako', '中川運河': 'nakagawa-unga',
    '土砂災害': 'dosha', '高潮': 'takashio', '内水': 'naisui',
}
KIND_LABEL = {'土砂災害': '土砂災害（河川ではなく崖・斜面の警戒）'}


def river_slug(name: str) -> str:
    if name in RIVER_SLUG:
        return RIVER_SLUG[name]
    return 'r-' + urllib.parse.quote(name, safe='')


def river_name(slug: str):
    for k, v in RIVER_SLUG.items():
        if v == slug:
            return k
    if slug.startswith('r-'):
        return urllib.parse.unquote(slug[2:])
    return None


def wards() -> list:
    d = json.load(open(WARDS_PATH, encoding='utf-8'))
    return d['wards']


def ward_by_slug(slug: str):
    return next((w for w in wards() if w['slug'] == slug), None)


def ward_by_name(name: str):
    return next((w for w in wards() if w['name'] == name), None)


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute("""CREATE TABLE IF NOT EXISTS alerts (
        target TEXT NOT NULL, level INTEGER NOT NULL, kind TEXT, issued_at TEXT, wards_json TEXT NOT NULL,
        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, source TEXT,
        PRIMARY KEY (target, level, issued_at))""")
    c.execute("""CREATE TABLE IF NOT EXISTS river_wards (
        target TEXT PRIMARY KEY, wards_json TEXT NOT NULL, updated_at TEXT NOT NULL, issued_at TEXT)""")
    return c


def record(data: dict, source: str = 'live') -> int:
    """取得した発令を履歴に入れる（同じ発令は last_seen だけ更新）。河川→学区の対応も最新で上書き。"""
    items = data.get('items') or []
    if not items:
        return 0
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    n = 0
    with _lock, _conn() as c:
        for it in items:
            wj = json.dumps(it['wards'], ensure_ascii=False)
            cur = c.execute("UPDATE alerts SET last_seen=? WHERE target=? AND level=? AND issued_at IS ?",
                            (now, it['target'], it['level'], it.get('issued_at')))
            if cur.rowcount == 0:
                c.execute("INSERT INTO alerts(target,level,kind,issued_at,wards_json,first_seen,last_seen,source) VALUES(?,?,?,?,?,?,?,?)",
                          (it['target'], it['level'], it.get('kind'), it.get('issued_at'), wj, now, now, source))
                n += 1
            # 河川→学区: 学区が具体的に入っている発令を優先（「全学区」だけより情報が多い）
            row = c.execute("SELECT issued_at, wards_json FROM river_wards WHERE target=?", (it['target'],)).fetchone()
            if not row or (it.get('issued_at') or '') >= (row[0] or '') or _count(wj) > _count(row[1]):
                c.execute("INSERT OR REPLACE INTO river_wards(target,wards_json,updated_at,issued_at) VALUES(?,?,?,?)",
                          (it['target'], wj, now, it.get('issued_at')))
    return n


def _count(wj: str) -> int:
    try:
        return sum(len(v) for v in json.loads(wj).values())
    except Exception:  # noqa: BLE001
        return 0


def seed_if_empty():
    """履歴が空なら 2026-09-08 の実発令（fixture）を種にする。設置直後でも河川ページが空にならない。"""
    with _conn() as c:
        if c.execute("SELECT count(*) FROM alerts").fetchone()[0]:
            return False
    if not os.path.exists(FIXTURE):
        return False
    from app import alerts as live
    d = live.parse(open(FIXTURE, encoding='utf-8').read(), year=2026)
    record(d, source='fixture:2026-09-08')
    return True


def rivers() -> list:
    """河川一覧（対象学区の対応が分かっているもの）。区の数・学区の数・直近の発令を添える。"""
    out = []
    with _conn() as c:
        for target, wj, upd, issued in c.execute("SELECT target, wards_json, updated_at, issued_at FROM river_wards ORDER BY target"):
            w = json.loads(wj)
            last = c.execute("SELECT level, kind, issued_at FROM alerts WHERE target=? ORDER BY issued_at DESC LIMIT 1", (target,)).fetchone()
            out.append(dict(target=target, slug=river_slug(target), wards=sorted(w.keys()), gakku_count=sum(len(v) for v in w.values()),
                            all_wards=[k for k, v in w.items() if '*' in v],
                            last_alert=dict(level=last[0], kind=last[1], issued_at=last[2]) if last else None, kind_note=KIND_LABEL.get(target)))
    return out


def river_detail(slug: str, live: dict):
    target = river_name(slug)
    if not target:
        return None
    with _conn() as c:
        row = c.execute("SELECT wards_json, issued_at FROM river_wards WHERE target=?", (target,)).fetchone()
        hist = [dict(level=l, kind=k, issued_at=i, first_seen=f, last_seen=s) for l, k, i, f, s in
                c.execute("SELECT level, kind, issued_at, first_seen, last_seen FROM alerts WHERE target=? ORDER BY issued_at DESC LIMIT 30", (target,))]
    if not row and not hist:
        return None
    wmap = json.loads(row[0]) if row else {}
    current = [it for it in (live.get('items') or []) if it['target'] == target]
    current.sort(key=lambda x: -x['level'])
    return dict(target=target, slug=slug, wards=wmap, ward_list=[dict(name=k, slug=(ward_by_name(k) or {}).get('slug'), gakku=v) for k, v in wmap.items()],
                current=current, history=hist, kind_note=KIND_LABEL.get(target))


def ward_alerts(ward: str, live: dict) -> list:
    """区に出ている発令（学区の集合ごと）。"""
    out = []
    for it in (live.get('items') or []):
        g = it['wards'].get(ward)
        if g is None:
            continue
        out.append(dict(level=it['level'], label=it['label'], target=it['target'], slug=river_slug(it['target']), issued_at=it['issued_at'], gakku=g))
    out.sort(key=lambda x: (-x['level'], x['issued_at'] or ''))
    return out


def rivers_for_ward(ward: str) -> list:
    out = []
    with _conn() as c:
        for target, wj in c.execute("SELECT target, wards_json FROM river_wards"):
            w = json.loads(wj)
            if ward in w:
                out.append(dict(target=target, slug=river_slug(target), gakku=w[ward]))
    return sorted(out, key=lambda x: x['target'])


def city_summary(live: dict) -> dict:
    items = live.get('items') or []
    by_level = {5: [], 4: [], 3: []}
    for it in items:
        by_level.setdefault(it['level'], []).append(it)
    ward_max = {}
    for it in items:
        for w in it['wards']:
            ward_max[w] = max(ward_max.get(w, 0), it['level'])
    return dict(items=items, by_level=by_level, ward_max=ward_max, max_level=max((i['level'] for i in items), default=None))
