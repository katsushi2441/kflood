# -*- coding: utf-8 -*-
"""Kurage 洪水・内水ハザードマップ（内部の略称 kflood）

住所を入れると、その地点の洪水（河川の氾濫）と内水（下水道・水路からの溢水）の浸水想定を返す。

設計の芯（ここを崩さない）:
  1. ○×ではなく「何メートル・何日」を返す。0.5mと5mでは取るべき行動が違う。
  2. 「区域外」「未収録」を必ず区別する。取り込んでいないメッシュ・自治体で黙って「区域外」と答えない。
  3. 判定結果には必ずデータ時点と出典を添える。datasets 表に時点が無いデータでは判定しない。
  4. 住所から求めた座標は町丁目の代表点。近くに区域があるときは「区域外」と言い切らない（khazard で実測 14〜50m のずれ）。
  5. 一次情報（自治体のハザードマップ・重ねるハザードマップ）への導線を必ず返す。判定は参考であって公的な証明ではない。

構成:
  ジオコーディング: 国土地理院 AddressSearch API（無料・キー不要）
  海抜            : 国土地理院 標高API
  判定            : PostGIS（kflood-db）ST_Contains
  データ          : 国土数値情報 A31 第4.0版（全国・洪水）＋ 名古屋市 内水氾濫ハザードマップ（CC BY）
"""
import csv
import io
import json
import os
import re
import time
from datetime import date, datetime

import psycopg2
import requests
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app import alerts as live_alerts
from app.codes import (CATEGORY, COLLAPSE, DEPTH_ACTION, DEPTH_RANK, DURATION_RANK, LONG_DURATION_RANK, RIVER,
                       minutes_label, naisui_band)

PORT = int(os.environ.get('KFLOOD_PORT', '18386'))
DB = dict(host='127.0.0.1', port=int(os.environ.get('KFLOOD_DB_PORT', '55434')), dbname='kflood', user='postgres',
          password=os.environ.get('KFLOOD_DB_PASS', 'kflood_local'))
GSI = 'https://msearch.gsi.go.jp/address-search/AddressSearch'
GSI_ELEV = 'https://cyberjapandata2.gsi.go.jp/general/dem/scripts/getelevation.php'
UA = {'User-Agent': 'kflood/1.0 (kurage.exbridge.jp)'}
STALE_YEARS = 6       # 国のデータ作成年度からこれ以上経っていたら注意書き
NEAR_M = 250          # これより近くに区域があれば「区域外」と言い切らない
BATCH_MAX = 300       # CSV一括判定の上限行数（地理院APIへの負荷を抑える）
SITE = os.environ.get('KFLOOD_SITE_NAME', 'Kurage 洪水・内水ハザードマップ')
LINKS = {
    'portal': 'https://disaportal.gsi.go.jp/maps/',
    'nagoya_naisui': 'https://www.city.nagoya.jp/bosaikikikanri/page/0000154015.html',
    'khazard': 'https://kurage.exbridge.jp/khazard.php/',
    'ktsunami': 'https://kurage.exbridge.jp/ktsunami.php/',
    'krefuge': 'https://kurage.exbridge.jp/krefuge.php/',
}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(ROOT, 'app', 'templates'))
app = FastAPI(title=SITE)
_rate = {}


def limited(ip, per_min=20, bucket='check'):
    now = time.time()
    k = (ip, bucket)
    q = [t for t in _rate.get(k, []) if now - t < 60]
    if len(q) >= per_min:
        return True
    q.append(now)
    _rate[k] = q
    return False


def client_ip(request: Request):
    return (request.headers.get('x-forwarded-for') or request.client.host or '').split(',')[0].strip()


def db():
    return psycopg2.connect(**DB)


def geocode(q: str):
    """国土地理院の住所検索。施設名だと別地方の類似住所が先頭に来るので、クエリを含む候補を優先する（khazard と同じ）。"""
    r = requests.get(GSI, params={'q': q}, timeout=10, headers=UA)
    r.raise_for_status()
    items = r.json()
    if not items:
        return None

    def score(it):
        t = it.get('properties', {}).get('title', '')
        return (q in t, t.startswith(q), -len(t))
    it = max(items, key=score)
    lon, lat = it['geometry']['coordinates']
    return dict(lon=float(lon), lat=float(lat), title=it['properties'].get('title', ''))


def elevation(lon, lat):
    try:
        r = requests.get(GSI_ELEV, params={'lon': lon, 'lat': lat, 'outtype': 'JSON'}, timeout=6, headers=UA)
        j = r.json()
        e = j.get('elevation')
        if e in (None, '-----'):
            return None
        return dict(m=float(e), source=j.get('hsrc', ''))
    except Exception:  # noqa: BLE001
        return None


def vintage_year(v: str):
    m = re.search(r'(\d{4})', v or '')
    return int(m.group(1)) if m else None


def check_point(lon, lat, title=''):
    """1地点の判定。返り値は JSON にそのまま出せる dict。"""
    out = dict(lon=lon, lat=lat, address=title, national=None, naisui=None, alert=None, guidance=[], notes=[], datasets=[])
    with db() as conn, conn.cursor() as cur:
        pt = 'ST_SetSRID(ST_Point(%s,%s),6668)'
        # 1) 未収録か
        cur.execute(f'SELECT mesh FROM meshes WHERE ST_Contains(geom, {pt})', (lon, lat))
        row = cur.fetchone()
        nat = dict(status='uncovered', mesh=None, max=None, planned=None, duration=None, collapse=[], nearest_m=None)
        if row:
            nat['mesh'] = row[0]
            nat['status'] = 'outside'
            cur.execute(f"""SELECT category, river_kind, max(rank), array_agg(DISTINCT rank), array_agg(DISTINCT dataset_key)
                            FROM flood WHERE ST_Contains(geom, {pt}) GROUP BY category, river_kind""", (lon, lat))
            keys = set()
            by_cat = {}
            for cat, rk, mx, ranks, dkeys in cur.fetchall():
                keys |= set(dkeys)
                by_cat.setdefault(cat, []).append(dict(river_kind=rk, river=RIVER.get(rk), max=mx, ranks=sorted(r for r in ranks if r is not None)))
            if 20 in by_cat:
                nat['status'] = 'inside'
                mx = max(x['max'] for x in by_cat[20] if x['max'] is not None)
                nat['max'] = dict(rank=mx, label=DEPTH_RANK.get(mx, f'ランク{mx}'), rivers=[x['river'] for x in by_cat[20]])
            if 10 in by_cat:
                mx = max(x['max'] for x in by_cat[10] if x['max'] is not None)
                nat['planned'] = dict(rank=mx, label=DEPTH_RANK.get(mx, f'ランク{mx}'), rivers=[x['river'] for x in by_cat[10]])
            if 30 in by_cat:
                mx = max(x['max'] for x in by_cat[30] if x['max'] is not None)
                nat['duration'] = dict(rank=mx, label=DURATION_RANK.get(mx, f'ランク{mx}'))
            if 40 in by_cat:
                kinds = set()
                for x in by_cat[40]:
                    kinds |= set(x['ranks'])
                nat['collapse'] = [dict(code=k, label=COLLAPSE.get(k, f'区分{k}')) for k in sorted(kinds)]
                nat['status'] = 'inside' if nat['status'] == 'outside' else nat['status']
            if nat['status'] == 'outside':
                # 想定最大規模の区域までの距離（1km以内だけ探す）
                cur.execute(f"""SELECT ST_Distance(geom::geography, {pt}::geography) FROM flood
                                WHERE category=20 AND ST_DWithin(geom, {pt}, 0.01)
                                ORDER BY geom <-> {pt} LIMIT 1""", (lon, lat, lon, lat, lon, lat))
                r2 = cur.fetchone()
                nat['nearest_m'] = round(r2[0]) if r2 else None
            # 使ったデータの時点（該当が無ければそのメッシュの datasets）
            if not keys:
                cur.execute('SELECT key FROM datasets WHERE key LIKE %s', (f'A31-22_%_{row[0]}',))
                keys = {k[0] for k in cur.fetchall()}
            if keys:
                cur.execute('SELECT key,name,data_vintage,attribution,note FROM datasets WHERE key = ANY(%s) ORDER BY key', (sorted(keys),))
                for k, n, v, a, note in cur.fetchall():
                    out['datasets'].append(dict(key=k, name=n, vintage=v, attribution=a, note=note))
        out['national'] = nat

        # 2) 内水（自治体版）。収録自治体名が住所に含まれるときだけ判定する。
        cur.execute('SELECT area, dataset_keys FROM naisui_coverage')
        cov = cur.fetchall()
        area = next((a for a, _ in cov if a and a in (title or '')), None)
        nai = dict(status='uncovered', area=None, depth_m=None, depth_label=None, minutes=None, minutes_label=None)
        if area:
            nai['area'] = area
            cur.execute(f'SELECT max(depth_m) FROM naisui_depth WHERE area=%s AND ST_Contains(geom, {pt})', (area, lon, lat))
            d = cur.fetchone()[0]
            cur.execute(f'SELECT max(minutes) FROM naisui_duration WHERE area=%s AND ST_Contains(geom, {pt})', (area, lon, lat))
            m = cur.fetchone()[0]
            nai.update(status='inside' if d is not None else 'outside', depth_m=(round(float(d), 2) if d is not None else None),
                       depth_label=naisui_band(float(d)) if d is not None else None,
                       minutes=(round(float(m)) if m is not None else None), minutes_label=minutes_label(m))
            dk = [k for _, ks in cov if _ == area for k in ks]
            cur.execute('SELECT key,name,data_vintage,attribution,note FROM datasets WHERE key = ANY(%s) ORDER BY key', (dk,))
            for k, n, v, a, note in cur.fetchall():
                out['datasets'].append(dict(key=k, name=n, vintage=v, attribution=a, note=note))
        out['naisui'] = nai

        # 2.5) いま出ている避難情報（自治体版）。学区が引ける自治体だけ。取得失敗は「発令なし」と区別する。
        al = dict(status='uncovered', gakku=None, items=[], max_level=None, fetched_at=None, source=None, source_url=None)
        cur.execute(f'SELECT ward, name, area FROM gakku WHERE ST_Contains(geom, {pt}) LIMIT 1', (lon, lat))
        gk = cur.fetchone()
        if gk:
            al['gakku'] = dict(ward=gk[0], name=gk[1], area=gk[2])
            data = live_alerts.fetch()
            al.update(status=data.get('status', 'unavailable'), fetched_at=data.get('fetched_at'), source=data.get('source'), source_url=data.get('source_url'))
            if data.get('items'):
                hits = live_alerts.for_gakku(data, gk[0], gk[1])
                al['items'] = [dict(level=h['level'], label=h['label'], target=h['target'], issued_at=h['issued_at']) for h in hits]
                al['max_level'] = live_alerts.max_level(hits)
            cur.execute("SELECT key,name,data_vintage,attribution,note FROM datasets WHERE key='nagoya_gakku'")
            for k, n, v, a, note in cur.fetchall():
                out['datasets'].append(dict(key=k, name=n, vintage=v, attribution=a, note=note))
        out['alert'] = al

    # 3) 行動の目安と注意書き
    g, notes = out['guidance'], out['notes']
    al = out['alert']
    if al['status'] in ('ok', 'stale') and al['items']:
        top = al['items'][0]
        g.append(f"【いま】{al['gakku']['ward']}{al['gakku']['name']}学区に 警戒レベル{top['level']}・{top['label']}（{top['target']}）が出ています"
                 + (f"（{top['issued_at'][5:16].replace('T', ' ')} 発令）" if top['issued_at'] else '') + '。'
                 + {5: '災害がすでに起きているか切迫しています。外に出ず、その場で命を守る行動（上階・近くの頑丈な建物の高い場所へ）。',
                    4: '危険な場所から全員避難。下の浸水想定が深い・長い・倒壊区域なら区域外へ、移動が危険なほど雨が強ければ上階へ。',
                    3: '高齢者・乳幼児・障害のある方は避難を開始。その他の人も準備を終えて、避難の判断を。'}.get(top['level'], ''))
        if al['status'] == 'stale':
            notes.append('避難情報は市のページを取得できず、1時間以内の前回取得値を表示しています。最新は市の災害情報配信で確認してください。')
    elif al['status'] in ('ok', 'stale') and al['gakku']:
        g.append(f"【いま】{al['gakku']['ward']}{al['gakku']['name']}学区に、市の避難情報（警戒レベル3〜5）は出ていません（{al['fetched_at']} 取得）。")
    elif al['status'] == 'unavailable':
        notes.append('いまの避難情報（市の災害情報配信）を取得できませんでした。発令が無いという意味ではありません。市のページで確認してください。')
    if nat['status'] == 'uncovered':
        notes.append('この地点を含む1次メッシュの洪水データは取り込まれていません。「区域外」という意味ではありません。取り込み状況は healthz で確認できます。')
    else:
        if nat['collapse']:
            g.append('家屋倒壊等氾濫想定区域（' + '・'.join(c['label'] for c in nat['collapse']) + '）に入っています。'
                     '氾濫時に建物が壊れたり流されたりするおそれがあるため、上の階に逃げる垂直避難ではなく、区域外への立退き避難が必要です。')
        if nat['max']:
            g.append(f'想定最大規模の降雨で浸水深 {nat["max"]["label"]}。' + DEPTH_ACTION.get(nat['max']['rank'], ''))
        if nat['duration'] and nat['duration']['rank'] >= LONG_DURATION_RANK:
            g.append(f'浸水継続時間 {nat["duration"]["label"]}。3日以上浸水が続く想定では、自宅の上階に留まる避難は水・食料・トイレ・電気が持ちません。区域外への立退き避難を前提にしてください。')
        elif nat['duration']:
            g.append(f'浸水継続時間 {nat["duration"]["label"]}。')
        if nat['status'] == 'outside':
            if nat['nearest_m'] is not None and nat['nearest_m'] <= NEAR_M:
                notes.append(f'最も近い洪水浸水想定区域まで約{nat["nearest_m"]}mです。住所から求めた座標は町丁目のおおよその位置のため、実際の敷地が区域内である可能性があります。地番で確認してください。')
            g.append('国の洪水浸水想定区域（想定最大規模）には含まれていません。ただし対象は水防法で指定された河川の氾濫で、指定外の小さな河川や内水の浸水はこのデータでは分かりません。')
        for d in out['datasets']:
            y = vintage_year(d['vintage'])
            if d['key'].startswith('A31') and y and date.today().year - y > STALE_YEARS:
                notes.append(f'国の洪水データの作成年度（{d["vintage"]}）から{date.today().year - y}年経っています。自治体の最新のハザードマップで確認してください。')
                break
    if nai['status'] == 'uncovered':
        notes.append('内水（下水道・水路からの浸水）は自治体ごとのデータで、この住所の自治体分は収録していません。自治体の内水ハザードマップで確認してください。')
    elif nai['status'] == 'inside':
        g.append(f'内水（下水道・水路からの浸水）の想定最大規模で浸水深 {nai["depth_label"]}（{nai["depth_m"]}m）'
                 + (f'、浸水継続時間 {nai["minutes_label"]}' if nai['minutes_label'] else '') + '。河川が氾濫しなくても、短時間の強い雨で道路や地下・半地下が浸水します。')
    else:
        g.append(f'{nai["area"]}の内水氾濫ハザードマップでは、この地点に浸水想定はありません（想定最大規模）。')
    out['disclaimer'] = ('この判定は公開データを住所の代表点で照らした参考情報で、公的な証明ではありません。'
                         '不動産取引の重要事項説明には、自治体が作成した水害ハザードマップそのものを使ってください。')
    return out


def check_query(q: str):
    q = (q or '').strip()
    if not q or len(q) > 100:
        raise HTTPException(400, '住所を入力してください（100文字まで）')
    try:
        g = geocode(q)
    except Exception:  # noqa: BLE001
        raise HTTPException(502, '住所検索（国土地理院API）に接続できませんでした。時間をおいて再度お試しください')
    if not g:
        raise HTTPException(404, 'その住所が見つかりませんでした。都道府県から入力してください')
    res = check_point(g['lon'], g['lat'], g['title'])
    res['query'] = q
    res['elevation'] = elevation(g['lon'], g['lat'])
    res['checked_at'] = datetime.now().strftime('%Y-%m-%d %H:%M')
    return res


def ensure_ready():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM datasets WHERE data_vintage <> ''")
        if cur.fetchone()[0] == 0:
            raise HTTPException(503, 'データ時点を確認できるデータがありません。scripts/load_a31.py でデータを取り込んでください')


@app.get('/api/check')
def api_check(request: Request, q: str = ''):
    if limited(client_ip(request)):
        raise HTTPException(429, '短時間に多くの判定が行われました。1分ほど待ってから再度お試しください')
    ensure_ready()
    return JSONResponse(check_query(q), headers={'Cache-Control': 'no-store'})


@app.post('/api/batch')
async def api_batch(request: Request, file: UploadFile = File(...)):
    if limited(client_ip(request), per_min=2, bucket='batch'):
        raise HTTPException(429, 'CSV一括判定は1分に2回までです')
    ensure_ready()
    raw = await file.read()
    if len(raw) > 1_000_000:
        raise HTTPException(413, 'CSVは1MBまでです')
    text = None
    for enc in ('utf-8-sig', 'cp932', 'utf-8'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise HTTPException(400, 'CSVの文字コードを読めません（UTF-8 か Shift_JIS）')
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise HTTPException(400, 'CSVが空です')
    head = [c.strip() for c in rows[0]]
    col = next((i for i, c in enumerate(head) if c in ('住所', 'address', 'Address', '所在地')), 0)
    has_header = any(c in ('住所', 'address', 'Address', '所在地', '名称', 'name') for c in head)
    body = rows[1:] if has_header else rows
    if len(body) > BATCH_MAX:
        raise HTTPException(413, f'1回の一括判定は{BATCH_MAX}行までです（{len(body)}行）')
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['入力', '判定に使った住所', '洪水 想定最大規模 浸水深', '洪水 計画規模 浸水深', '浸水継続時間', '家屋倒壊等氾濫想定区域',
                '内水 浸水深(m)', '内水 継続時間', '海抜(m)', '判定', '注意', 'データ時点', '学区', 'いまの避難情報'])
    for r in body:
        q = (r[col] if col < len(r) else '').strip()
        if not q:
            continue
        try:
            res = check_query(q)
        except HTTPException as e:
            w.writerow([q, '', '', '', '', '', '', '', '', f'エラー: {e.detail}', '', ''])
            continue
        nat, nai = res['national'], res['naisui']
        status = {'inside': '洪水浸水想定区域内', 'outside': '洪水区域外', 'uncovered': '洪水データ未収録'}[nat['status']]
        w.writerow([q, res['address'], nat['max']['label'] if nat['max'] else '', nat['planned']['label'] if nat['planned'] else '',
                    nat['duration']['label'] if nat['duration'] else '', '・'.join(c['label'] for c in nat['collapse']),
                    nai['depth_m'] if nai['depth_m'] is not None else ('' if nai['status'] == 'uncovered' else '想定なし'),
                    nai['minutes_label'] or '', res['elevation']['m'] if res.get('elevation') else '',
                    status, ' / '.join(res['notes']), ' / '.join(sorted({d['vintage'] for d in res['datasets']})),
                    (res['alert']['gakku']['ward'] + res['alert']['gakku']['name']) if res['alert'].get('gakku') else '',
                    ('; '.join(f"レベル{i['level']} {i['label']}（{i['target']}）" for i in res['alert']['items']) if res['alert']['items'] else
                     ('発令なし' if res['alert']['status'] in ('ok', 'stale') and res['alert'].get('gakku') else ('取得不可' if res['alert']['status'] == 'unavailable' else '')))])
        time.sleep(0.2)   # 地理院APIへの負荷を抑える
    data = ('﻿' + out.getvalue()).encode('utf-8')
    fn = f'kflood_batch_{datetime.now():%Y%m%d_%H%M}.csv'
    return StreamingResponse(io.BytesIO(data), media_type='text/csv; charset=utf-8',
                             headers={'Content-Disposition': f'attachment; filename="{fn}"', 'Cache-Control': 'no-store'})


@app.get('/healthz')
def healthz():
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT count(*) FROM meshes')
        meshes = cur.fetchone()[0]
        cur.execute('SELECT count(*) FROM flood')
        polys = cur.fetchone()[0]
        cur.execute('SELECT area, dataset_keys FROM naisui_coverage')
        cov = [dict(area=a, datasets=k) for a, k in cur.fetchall()]
        cur.execute("SELECT key, data_vintage, loaded_at::date FROM datasets WHERE key NOT LIKE 'A31%' ORDER BY key")
        muni = [dict(key=k, vintage=v, loaded=str(d)) for k, v, d in cur.fetchall()]
        cur.execute("SELECT min(data_vintage), max(loaded_at)::date FROM datasets WHERE key LIKE 'A31%'")
        v, d = cur.fetchone()
        cur.execute('SELECT count(*) FROM gakku')
        gk = cur.fetchone()[0]
    a = live_alerts.fetch()
    return dict(ok=True, flood_meshes=meshes, flood_polygons=polys, flood_vintage=v, flood_loaded=str(d) if d else None,
                naisui_coverage=cov, municipal_datasets=muni, gakku=gk,
                alerts=dict(status=a.get('status'), items=len(a.get('items', [])), fetched_at=a.get('fetched_at'), source=a.get('source_url')))


def page(request: Request, name: str, **kw):
    kw.update(site=SITE, links=LINKS, year=date.today().year)
    return templates.TemplateResponse(request, name, kw)


@app.get('/', response_class=HTMLResponse)
def index(request: Request):
    return page(request, 'index.html')


@app.get('/about', response_class=HTMLResponse)
def about(request: Request):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT key,name,data_vintage,loaded_at::date,attribution,note FROM datasets ORDER BY key NOT LIKE 'A31%', key")
        rows = cur.fetchall()
        cur.execute('SELECT count(*) FROM meshes')
        meshes = cur.fetchone()[0]
    a31 = [r for r in rows if r[0].startswith('A31')]
    muni = [r for r in rows if not r[0].startswith('A31')]
    return page(request, 'about.html', a31=a31, muni=muni, meshes=meshes,
                                                        a31_vintage=(a31[0][2] if a31 else None))


@app.get('/batch', response_class=HTMLResponse)
def batch_page(request: Request):
    return page(request, 'batch.html', batch_max=BATCH_MAX)


# ---- マイ・タイムライン（判定結果＋世帯条件 → 避難行動の時系列表。印刷して冷蔵庫に貼る前提） ----
LEVELS = [
    ('数日前〜', '警戒レベル1〜2', '早期注意情報・大雨注意報／洪水注意報'),
    ('前日〜半日前', '警戒レベル3', '高齢者等避難（市区町村が発令）'),
    ('直前', '警戒レベル4', '避難指示（市区町村が発令）'),
    ('発災', '警戒レベル5', '緊急安全確保'),
]


def build_timeline(res, hh):
    nat, nai = res['national'], res['naisui']
    depth = nat['max']['rank'] if nat.get('max') else 0
    long_dur = bool(nat.get('duration') and nat['duration']['rank'] >= LONG_DURATION_RANK)
    collapse = bool(nat.get('collapse'))
    care = hh.get('elderly') or hh.get('infant') or hh.get('disabled')
    # 避難の方針
    if collapse or depth >= 3 or long_dur:
        policy = '立退き避難（区域外の避難所・親戚宅・ホテルなどへ）'
        why = ('家屋倒壊等氾濫想定区域のため' if collapse else '浸水深が3m以上（2階も浸水）のため' if depth >= 3 else '浸水が3日以上続く想定のため')
    elif depth in (1, 2):
        if hh.get('upper_floor'):
            policy = '早めの立退き避難を基本に、間に合わないときは2階以上への垂直避難'
            why = f'浸水深 {DEPTH_RANK[depth]}・上の階がある建物のため'
        else:
            policy = '立退き避難（上の階が無いため垂直避難はできない）'
            why = f'浸水深 {DEPTH_RANK[depth]}・平屋または1階のみのため'
    elif nai.get('status') == 'inside' and (nai.get('depth_m') or 0) >= 0.5:
        policy = '外出を避け、地下・半地下・道路の冠水に注意。浸水深が深い場合は上の階へ'
        why = f'内水の浸水深 {nai["depth_label"]}のため'
    else:
        policy = '在宅で安全確保。周辺の河川・道路の状況で判断'
        why = '洪水浸水想定区域の外（内水の想定も小さい）のため'
    rows = []
    r0 = ['ハザードマップと避難先（避難所・親戚宅・ホテル）を家族で確認し、連絡方法と集合場所を決める',
          '飲料水（1人1日3L×3日）・食料・常備薬・モバイルバッテリー・懐中電灯を確認',
          '保険証・通帳・母子手帳などの貴重品を非常持出袋にまとめる']
    if hh.get('car'):
        r0.append('車のガソリンを満タンに。冠水した道路は走らないと決めておく')
    else:
        r0.append('徒歩で行ける避難先と、雨が強くなる前に出る目安時刻を決める')
    if hh.get('pet'):
        r0.append('ペット同行避難ができる避難所を確認し、キャリー・フード・リードを用意')
    if care:
        r0.append('要配慮者（高齢者・乳幼児・障害のある方）の薬・介護用品・ミルク・おむつを多めに用意')
    rows.append(r0)
    r1 = ['気象情報と市区町村の避難情報をこまめに確認（防災アプリ・防災無線・テレビ）',
          '雨戸を閉め、ベランダ・庭の飛びやすい物を片付ける。停電に備えて充電',
          '浴槽に水をためる（トイレ・生活用水）。冷蔵庫の温度を下げる']
    if care:
        r1.append('【要配慮者のいる世帯】警戒レベル3「高齢者等避難」で避難を開始する。明るいうちに出発')
    elif policy.startswith('立退き'):
        r1.append('警戒レベル3の時点で避難の準備を終え、夜間や豪雨の中の移動にならないよう早めに出発を判断')
    if hh.get('upper_floor') and not policy.startswith('立退き'):
        r1.append('2階以上に水・食料・懐中電灯・ラジオを運んでおく（垂直避難の準備）')
    rows.append(r1)
    r2 = []
    if policy.startswith('立退き') or policy.startswith('早めの立退き'):
        r2.append('警戒レベル4「避難指示」で全員が避難を完了。区域外の避難先へ移動')
        if hh.get('car'):
            r2.append('道路が冠水していたら車を降りて高い場所へ。アンダーパスに入らない')
        r2.append('避難所へは持出袋・上履き・マスクを持って。近所に声をかける')
        if hh.get('upper_floor'):
            r2.append('移動が危険なほど雨が強い場合だけ、2階以上への垂直避難に切り替える')
    else:
        r2.append('警戒レベル4「避難指示」が出たら、外出せず建物の高い場所で安全確保')
        r2.append('周辺の道路が冠水し始めたら地下・半地下から離れ、ブレーカーを落とす')
    rows.append(r2)
    r3 = ['警戒レベル5「緊急安全確保」はすでに災害が起きている状態。外に出ず、その場で命を守る行動（上階・近くの頑丈な建物の高い場所へ）',
          '浸水した水には近づかない。感電・マンホール・流されるおそれ']
    rows.append(r3)
    return dict(policy=policy, why=why, levels=LEVELS, rows=rows)


@app.get('/timeline', response_class=HTMLResponse)
def timeline(request: Request, q: str = '', elderly: int = 0, infant: int = 0, disabled: int = 0, car: int = 0,
             upper_floor: int = 0, pet: int = 0, people: int = 0):
    if not q:
        return page(request, 'timeline_form.html')
    if limited(client_ip(request)):
        raise HTTPException(429, '短時間に多くの判定が行われました。1分ほど待ってから再度お試しください')
    ensure_ready()
    res = check_query(q)
    hh = dict(elderly=elderly, infant=infant, disabled=disabled, car=car, upper_floor=upper_floor, pet=pet, people=people)
    tl = build_timeline(res, hh)
    return page(request, 'timeline.html', res=res, hh=hh, tl=tl,
                                                           depth_rank=DEPTH_RANK, today=date.today().strftime('%Y年%m月%d日'))


@app.get('/ogp.png')
def ogp():
    return FileResponse(os.path.join(ROOT, 'app', 'static', 'ogp.png'), media_type='image/png',
                        headers={'Cache-Control': 'public, max-age=86400'})


@app.get('/robots.txt', response_class=PlainTextResponse)
def robots():
    return 'User-agent: *\nAllow: /\nDisallow: /api/\n'
