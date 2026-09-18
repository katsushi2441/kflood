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
  データ          : 国土数値情報 洪水浸水想定区域（1次メッシュ単位）全国。旧識別子 A31 第4.0版(2022年度)は後継 A31b(毎年5月更新)へ
                    移行する。どの版を使っているかは .env の KFLOOD_A31_PREFIX（datasets.key の接頭辞）で決める
                    ＋ 名古屋市 内水氾濫ハザードマップ（CC BY）
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

from app import alerts as live_alerts
from app import siblings
from app import nagoya
from app.codes import (CATEGORY, COLLAPSE, DEPTH_ACTION, DEPTH_RANK, DURATION_RANK, LONG_DURATION_RANK, RIVER,
                       minutes_label, naisui_band)

PORT = int(os.environ.get('KFLOOD_PORT', '18386'))
DB = dict(host='127.0.0.1', port=int(os.environ.get('KFLOOD_DB_PORT', '55434')), dbname='kflood', user='postgres',
          password=os.environ.get('KFLOOD_DB_PASS', 'kflood_local'))
GSI = 'https://msearch.gsi.go.jp/address-search/AddressSearch'
GSI_ELEV = 'https://cyberjapandata2.gsi.go.jp/general/dem/scripts/getelevation.php'
UA = {'User-Agent': 'kflood/1.0 (kurage.exbridge.jp)'}
STALE_YEARS = 6       # 国のデータ作成年度からこれ以上経っていたら注意書き
# 国の洪水データの版。datasets.key の接頭辞（A31-22 = 旧A31 第4.0版 2022年度 / A31b-25 = A31b 2025年度版）。
# 新しい版を flood 表に入れ替えたら .env で切り替える（scripts/load_a31b.py と docs/SETUP.md）
A31_PREFIX = os.environ.get('KFLOOD_A31_PREFIX', 'A31-22')
A31_PAGES = {'A31-22': 'https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-A31-v4_0.html',
             'A31b-25': 'https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-A31b-2025.html'}
A31_PAGE = os.environ.get('KFLOOD_A31_PAGE', A31_PAGES.get(A31_PREFIX, 'https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-A31b-2025.html'))
NEAR_M = 250          # これより近くに区域があれば「区域外」と言い切らない
BATCH_MAX = 300       # CSV一括判定の上限行数（地理院APIへの負荷を抑える）
SITE = os.environ.get('KFLOOD_SITE_NAME', 'Kurage 洪水・内水ハザードマップ')
PUBLIC_BASE = os.environ.get('KFLOOD_PUBLIC_BASE', 'https://kurage.exbridge.jp/kflood.php').rstrip('/')
LINKS = {
    'portal': 'https://disaportal.gsi.go.jp/maps/',
    # 市はページを移し、呼び名も法令用語へ変えた（内水ハザードマップ→雨水出水浸水想定区域）。
    # 旧URL bosaikikikanri/page/0000154015.html は404（2026-09-14 実測）。リンク切れは定期的に検査する。
    'nagoya_naisui': 'https://www.city.nagoya.jp/bousaiportal/hazardmap/1013531/1013532.html',
    'khazard': 'https://kurage.exbridge.jp/khazard.php/',
    'ktsunami': 'https://kurage.exbridge.jp/ktsunami.php/',
    'krefuge': 'https://kurage.exbridge.jp/krefuge.php/',
    'buy': 'https://kappstore.exbridge.jp/app.php?id=41a09acc163dcb7d&ref=kflood',
    'komon': 'https://exbridge.jp/ai-it-komon.html?ref=kflood',
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


def get_live() -> dict:
    """市の避難情報を取得し、発令があれば履歴（河川→学区の対応）に記録する。"""
    d = live_alerts.fetch()
    if d.get('items'):
        try:
            nagoya.record(d)
        except Exception:  # noqa: BLE001
            pass
    return d


def vintage_year(v: str):
    m = re.search(r'(\d{4})', v or '')
    return int(m.group(1)) if m else None


def check_point(lon, lat, title=''):
    """1地点の判定。返り値は JSON にそのまま出せる dict。"""
    out = dict(lon=lon, lat=lat, address=title, national=None, naisui=None, takashio=None, alert=None, guidance=[], notes=[], datasets=[])
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
                cur.execute('SELECT key FROM datasets WHERE key LIKE %s', (f'{A31_PREFIX}_%_{row[0]}',))
                keys = {k[0] for k in cur.fetchall()}
            if keys:
                cur.execute('SELECT key,name,data_vintage,attribution,note FROM datasets WHERE key = ANY(%s) ORDER BY key', (sorted(keys),))
                for k, n, v, a, note in cur.fetchall():
                    out['datasets'].append(dict(key=k, name=n, vintage=v, attribution=a, note=note))
        out['national'] = nat

        # 2) 内水（自治体版）。収録自治体名が住所に含まれるときだけ判定する。
        cur.execute('SELECT area, dataset_keys FROM naisui_coverage')
        cov = cur.fetchall()
        # 収録範囲は座標で決める（地図クリックのように住所文字列が無い場合も判定できる）。念のため住所文字列でも補う
        cur.execute(f'SELECT area FROM naisui_coverage WHERE ST_Contains(geom, {pt}) LIMIT 1', (lon, lat))
        row = cur.fetchone()
        area = row[0] if row else next((a for a, _ in cov if a and a in (title or '')), None)
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

        # 2.2) 高潮（自治体版）。洪水・内水と同じ「何メートル浸かるか」の問いなので、
        #      別サイトに分けず同じ画面に並べる。名古屋にとって高潮は伊勢湾台風の災害そのもの。
        tks = dict(status='uncovered', area=None, depth_m=None, depth_label=None, hours=None)
        try:
            cur.execute(f'SELECT area FROM takashio_coverage WHERE ST_Contains(geom, {pt}) LIMIT 1', (lon, lat))
            trow = cur.fetchone()
            tarea = trow[0] if trow else None
            if tarea:
                tks['area'] = tarea
                cur.execute(f'SELECT max(depth_m) FROM takashio_depth WHERE area=%s AND ST_Contains(geom, {pt})',
                            (tarea, lon, lat))
                td = cur.fetchone()[0]
                cur.execute(f'SELECT max(hours) FROM takashio_duration WHERE area=%s AND ST_Contains(geom, {pt})',
                            (tarea, lon, lat))
                th = cur.fetchone()[0]
                tks.update(status='inside' if td is not None else 'outside',
                           depth_m=(round(float(td), 2) if td is not None else None),
                           depth_label=naisui_band(float(td)) if td is not None else None,
                           hours=(round(float(th), 1) if th is not None else None))
                cur.execute("SELECT key,name,data_vintage,attribution,note FROM datasets "
                            "WHERE key LIKE %s ORDER BY key", ('%takashio%',))
                for k, n, v, a, note in cur.fetchall():
                    out['datasets'].append(dict(key=k, name=n, vintage=v, attribution=a, note=note))
        except Exception:
            # 高潮データをまだ取り込んでいない環境でも、洪水・内水の判定は続けられるようにする
            conn.rollback()
        out['takashio'] = tks

        # 2.5) いま出ている避難情報（自治体版）。学区が引ける自治体だけ。取得失敗は「発令なし」と区別する。
        al = dict(status='uncovered', gakku=None, items=[], max_level=None, fetched_at=None, source=None, source_url=None)
        cur.execute(f'SELECT ward, name, area FROM gakku WHERE ST_Contains(geom, {pt}) LIMIT 1', (lon, lat))
        gk = cur.fetchone()
        if gk:
            al['gakku'] = dict(ward=gk[0], name=gk[1], area=gk[2])
            data = get_live()
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
def api_check(request: Request, q: str = '', lat: float = None, lon: float = None):
    if limited(client_ip(request)):
        raise HTTPException(429, '短時間に多くの判定が行われました。1分ほど待ってから再度お試しください')
    ensure_ready()
    if lat is not None and lon is not None:
        # 地図をクリックした地点（住所検索なし）
        if not (20 < lat < 46 and 122 < lon < 154):
            raise HTTPException(400, '緯度経度が日本の範囲外です')
        res = check_point(lon, lat, title=f'地図で指定した地点（{lat:.5f}, {lon:.5f}）')
        res['query'] = ''
        res['elevation'] = elevation(lon, lat)
        return JSONResponse(res, headers={'Cache-Control': 'no-store'})
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



# ---- 地図（MapLibre）用: ベクタータイルと学区GeoJSON -------------------------------
TILE_DIR = os.path.join(ROOT, 'data', 'tiles')   # 生成したタイルはディスクに残す（初回だけ PostGIS を叩く）
TILE_LAYERS = {
    # 名古屋市 内水氾濫ハザードマップ（CC BY）: 5mセルを深さ区分にして配信
    'naisui': dict(minz=12, maxz=17, sql="""SELECT ST_AsMVT(q, 'naisui', 4096, 'geom') FROM (
        SELECT CASE WHEN depth_m < 0.3 THEN 1 WHEN depth_m < 0.5 THEN 2 WHEN depth_m < 1 THEN 3
                    WHEN depth_m < 2 THEN 4 WHEN depth_m < 3 THEN 5 ELSE 6 END AS cls,
               ST_AsMVTGeom(ST_Transform(geom, 3857), ST_TileEnvelope(%(z)s, %(x)s, %(y)s), 4096, 64, true) AS geom
        FROM naisui_depth WHERE depth_m > 0 AND geom && ST_Transform(ST_TileEnvelope(%(z)s, %(x)s, %(y)s), 6668)) q"""),
    # 国土数値情報（A31/A31b）想定最大規模: 当社の判定に使っている元データ（国ポータルとの差を見るため）
    'a31': dict(minz=10, maxz=17, sql="""SELECT ST_AsMVT(q, 'a31', 4096, 'geom') FROM (
        SELECT rank, ST_AsMVTGeom(ST_Transform(geom, 3857), ST_TileEnvelope(%(z)s, %(x)s, %(y)s), 4096, 64, true) AS geom
        FROM flood WHERE category = 20 AND geom && ST_Transform(ST_TileEnvelope(%(z)s, %(x)s, %(y)s), 6668)) q"""),
}


@app.get('/tiles/{layer}/{z}/{x}/{y}.pbf')
def vector_tile(layer: str, z: int, x: int, y: int):
    L = TILE_LAYERS.get(layer)
    if not L:
        raise HTTPException(404, 'そのレイヤはありません')
    hdr = {'Cache-Control': 'public, max-age=86400'}
    if z < L['minz'] or z > L['maxz'] or x < 0 or y < 0 or x >= 2 ** z or y >= 2 ** z:
        return Response(status_code=204, headers=hdr)
    path = os.path.join(TILE_DIR, layer, str(z), str(x), f'{y}.pbf')
    if os.path.exists(path):
        data = open(path, 'rb').read()
    else:
        with db() as conn, conn.cursor() as cur:
            cur.execute(L['sql'], dict(z=z, x=x, y=y))
            row = cur.fetchone()
        data = bytes(row[0]) if row and row[0] else b''
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)
    if not data:
        return Response(status_code=204, headers=hdr)
    return Response(content=data, media_type='application/vnd.mapbox-vector-tile', headers=hdr)


def gakku_levels(live: dict) -> dict:
    """学区ごとの現在の最大警戒レベル。(区, 学区名) → level。区の全学区なら (区, '*')。"""
    lv = {}
    for it in (live.get('items') or []):
        for w, gs in (it.get('wards') or {}).items():
            for g in gs:
                lv[(w, g)] = max(lv.get((w, g), 0), it['level'])
    return lv


@app.get('/api/gakku.geojson')
def gakku_geojson():
    live = get_live()
    lv = gakku_levels(live)
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT ward, name, ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, 0.00015)) FROM gakku ORDER BY ward, name")
        feats = []
        for w, n, g in cur.fetchall():
            level = max(lv.get((w, n), 0), lv.get((w, '*'), 0))
            feats.append(dict(type='Feature', properties=dict(ward=w, name=n, level=level), geometry=json.loads(g)))
    return JSONResponse(dict(type='FeatureCollection', features=feats, fetched_at=live.get('fetched_at'), status=live.get('status')),
                        headers={'Cache-Control': 'public, max-age=180'})


@app.get('/map/', response_class=HTMLResponse)
def map_page(request: Request, lat: float = None, lon: float = None, q: str = ''):
    return page(request, 'map.html', lat=lat, lon=lon, q=q[:100])


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
        cur.execute("SELECT min(data_vintage), max(loaded_at)::date FROM datasets WHERE key LIKE %s", (A31_PREFIX + '_%',))
        v, d = cur.fetchone()
        cur.execute('SELECT count(*) FROM gakku')
        gk = cur.fetchone()[0]
    a = get_live()
    return dict(ok=True, flood_meshes=meshes, flood_polygons=polys, flood_vintage=v, flood_loaded=str(d) if d else None,
                naisui_coverage=cov, municipal_datasets=muni, gakku=gk,
                alerts=dict(status=a.get('status'), items=len(a.get('items', [])), fetched_at=a.get('fetched_at'), source=a.get('source_url')))


_a31v = {'t': 0, 'v': None}


def a31_vintage():
    """flood 表に入っている国の洪水データの時点（datasets.data_vintage）。表示用。10分キャッシュ"""
    if time.time() - _a31v['t'] > 600:
        try:
            with db() as conn, conn.cursor() as cur:
                cur.execute("SELECT min(data_vintage) FROM datasets WHERE key LIKE %s", (A31_PREFIX + '_%',))
                r = cur.fetchone()
                _a31v.update(t=time.time(), v=(r[0] if r else None))
        except Exception:
            _a31v['t'] = time.time()
    return _a31v['v']


def a31_vintage_short():
    m = re.search(r'\d{4}年度', a31_vintage() or '')
    return m.group(0) if m else (a31_vintage() or '')


FAQ = [
    ("洪水ハザードマップと内水ハザードマップは何が違うのですか",
     "洪水は河川が氾濫して水が来る想定、内水は下水道や水路が雨をさばききれずに街の中で溢れる想定です。"
     "別々の図で公開されているため、片方だけ見て「うちは大丈夫」と判断してしまうことがあります。"
     "このサイトは住所ひとつで両方を同時に照らします。"),
    ("浸水深が何メートルなら、上の階へ逃げれば足りますか",
     "浸水深3m以上は2階まで浸かる想定なので垂直避難では足りません。家屋倒壊等氾濫想定区域（氾濫流・河岸侵食）"
     "や、浸水が3日以上続く場所も同様で、立退き避難が必要です。このサイトはその判断の目安まで返します。"),
    ("データはいつ時点のものですか",
     "洪水は国土数値情報の洪水浸水想定区域（1次メッシュ単位）で、毎年5月に更新されます。判定結果には"
     "必ずデータ時点を添えます。時点を確認できないデータでは判定そのものを行いません。"),
    ("データを取り込んでいない場所はどう表示されますか",
     "「区域外」とは言わず「この地点のデータは取り込まれていません」と返します。未収録と区域外を混同すると"
     "危険な場所を安全と誤解させるためです。"),
]


def jsonld_for(path: str) -> str:
    """構造化データ。AI検索・検索エンジンに「何を答えるサイトか」を機械可読で渡す。"""
    graph = [{
        "@type": "WebSite",
        "@id": PUBLIC_BASE + "/#website",
        "name": SITE,
        "url": PUBLIC_BASE + "/",
        "inLanguage": "ja",
        "publisher": {"@type": "Organization", "name": "株式会社エクスブリッジ", "url": "https://exbridge.jp/"},
        "potentialAction": {
            "@type": "SearchAction",
            "target": {"@type": "EntryPoint", "urlTemplate": PUBLIC_BASE + "/?q={search_term_string}"},
            "query-input": "required name=search_term_string",
        },
    }]
    if path in ('/', '/about'):
        graph.append({"@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in FAQ]})
    if path != '/':
        graph.append({"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": SITE, "item": PUBLIC_BASE + "/"},
            {"@type": "ListItem", "position": 2, "name": path.strip('/'), "item": PUBLIC_BASE + path}]})
    return json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False)


def page(request: Request, name: str, **kw):
    depth = max(0, request.url.path.strip('/').count('/') + (1 if request.url.path.strip('/') and request.url.path.endswith('/') else 0))
    kw.update(site=SITE, links=LINKS, year=date.today().year, root='../' * depth if depth else './',
              a31_vintage=a31_vintage(), a31_vintage_short=a31_vintage_short(), a31_page=A31_PAGE,
              public_base=PUBLIC_BASE, canonical=PUBLIC_BASE + request.url.path,
              jsonld=jsonld_for(request.url.path))
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
    a31 = [r for r in rows if r[0].startswith(A31_PREFIX + '_')]
    muni = [r for r in rows if not r[0].startswith('A31')]
    return page(request, 'about.html', a31=a31, muni=muni, meshes=meshes)


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


def build_timeline(res, hh, sib=None):
    """とるべき行動を決める。

    **判断の型は内閣府「避難情報に関するガイドライン」（令和8年3月改定）に合わせている。**
    自宅に留まる「屋内安全確保」を選べるのは洪水等・高潮に限られ、しかも
      ❶家屋倒壊等氾濫想定区域に存していないこと
      ❷浸水しない居室があること
      ❸一定期間の浸水による支障を許容できること
    の3つを満たすときだけ。**土砂災害と津波は立退き避難が基本**なので、
    洪水の想定が小さくても「在宅で安全確保」と言ってはいけない。
    土砂・津波の判定は siblings が取れたときだけ足す（取れないときは黙って出さない）。
    """
    nat, nai = res['national'], res['naisui']
    sib = sib or {}
    dosha, tsunami = sib.get('dosha'), sib.get('tsunami')
    dosha_in = bool(dosha and dosha.get('inside'))
    tsunami_in = bool(tsunami and tsunami.get('inside'))
    depth = nat['max']['rank'] if nat.get('max') else 0
    long_dur = bool(nat.get('duration') and nat['duration']['rank'] >= LONG_DURATION_RANK)
    collapse = bool(nat.get('collapse'))
    care = hh.get('elderly') or hh.get('infant') or hh.get('disabled')
    # 避難の方針
    if dosha_in or tsunami_in:
        # ガイドラインで屋内安全確保の対象外。洪水の深さに関わらず立退き避難。
        parts = []
        if dosha_in:
            parts.append('土砂災害特別警戒区域（レッドゾーン）' if dosha.get('special') else '土砂災害警戒区域')
        if tsunami_in:
            parts.append('津波浸水想定区域')
        policy = '立退き避難（区域の外へ。この場所では屋内での安全確保に頼れません）'
        why = '・'.join(parts) + 'のため（土砂災害・津波は上の階に逃げる方法が使えません）'
    elif collapse or depth >= 3 or long_dur:
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
    # **避難先は名前と徒歩何分まで出す。**「避難所へ」だけでは、その場で調べ直すことになる。
    ref = sib.get('refuge') or {}
    for it in (ref.get('items') or [])[:2]:
        mins = it.get('walk_minutes')
        rows[1].append(f"避難先の候補: {it['name']}（徒歩約{mins}分・{it.get('address', '')}）"
                       if mins else f"避難先の候補: {it['name']}（{it.get('address', '')}）")
    return dict(policy=policy, why=why, levels=LEVELS, rows=rows,
                dosha=dosha, tsunami=tsunami, refuge=ref or None)


def build_now(res, tl, hh):
    """**いま、どうするか。** 平時の方針（tl）と、いま出ている避難情報を掛け合わせる。

    平時のハザードマップは「どこが危ないか」しか言わない。発令は「いま危ない」しか
    言わない。**避難するかどうかは、その2つを掛けないと決まらない**（浸水深50cmの家と
    3mの家では、同じ避難指示でも取るべき行動が違う）。ここはその掛け算だけを行う。

    **取得できなかったことを「発令なし」と書かない。** 市の配信が落ちているときに
    「発令はありません」と出すのが、この種の道具でいちばん危ない誤りになる。
    """
    al = res.get('alert') or {}
    items = al.get('items') or []
    live_ok = al.get('status') in ('ok', 'stale')
    care = bool(hh.get('elderly') or hh.get('infant') or hh.get('disabled'))
    # 立退き避難が要る場所か（屋内安全確保に頼れない場所を含む）
    leave = tl['policy'].startswith('立退き') or tl['policy'].startswith('早めの立退き')
    lv = al.get('max_level') if items else None

    if not al.get('gakku'):
        return dict(state='no_area', level=None, headline='この住所の自治体は、発令をまだ取り込んでいません',
                    detail='いま出ている避難情報は、お住まいの市区町村の発表でご確認ください。'
                           'このページの下にある「この場所の想定」と「とるべき行動」は、いつでも使えます。',
                    acts=[], leave=leave)
    area = f"{al['gakku']['ward']}{al['gakku']['name']}学区"
    if not live_ok:
        return dict(state='unknown', level=None, headline=f'{area}の避難情報を取得できませんでした',
                    detail='**発令が無いという意味ではありません。** 市の配信に接続できなかっただけです。'
                           '市区町村の発表・防災無線・テレビで確かめてください。',
                    acts=[], leave=leave)
    if not items:
        return dict(state='calm', level=None, headline=f'いま{area}に、警戒レベル3〜5の発令はありません',
                    detail='いまは平時です。下の「とるべき行動」を読んで、避難先と持ち出す物を決めておいてください。'
                           '発令されてから調べ始めると間に合いません。',
                    acts=[], leave=leave)

    acts = []
    if lv >= 5:
        headline = f'【警戒レベル5】{area}に緊急安全確保が出ています'
        detail = 'すでに災害が起きている、または起きる直前です。**避難所への移動は危険な場合があります。**'
        acts = ['外に出ず、その場でいちばん安全な場所へ。建物の上の階、山や崖から離れた側の部屋へ',
                '浸水した水には入らない。マンホール・側溝・流れのある水は見た目より危険']
    elif lv == 4:
        headline = f'【警戒レベル4】{area}に避難指示が出ています'
        if leave:
            detail = 'この住所は**立退き避難が必要な場所**です。いますぐ、区域の外の避難先へ移動してください。'
            acts = ['いますぐ全員で避難を完了する。夜間・豪雨のなかの移動になる前に出る',
                    '持ち出す物より命を優先する。近所にも声をかける']
            if hh.get('car'):
                acts.append('冠水した道路とアンダーパスには入らない。危なければ車を降りて高い場所へ')
            if hh.get('upper_floor'):
                acts.append('移動そのものが危険なほど状況が悪いときだけ、上の階へ切り替える')
        else:
            detail = 'この住所は、条件を満たせば**建物の上の階にとどまる**選択ができる場所です。外の状況で判断してください。'
            acts = ['外に出るのが危険なら、建物の浸水しない上の階へ移る',
                    '水・食料・薬・充電したスマホ・懐中電灯を上の階へ運ぶ',
                    '地下・半地下から離れ、ブレーカーを落とす']
    else:
        headline = f'【警戒レベル3】{area}に高齢者等避難が出ています'
        if care:
            detail = 'この世帯には**避難に時間がかかる人がいます。いまが避難を始めるタイミング**です。'
            acts = ['いま避難を始める。明るいうちに、雨が強くなる前に出発する',
                    '薬・介護用品・ミルク・おむつを持つ。移動の支援が要るなら早めに頼む']
        elif leave:
            detail = 'この住所は立退き避難が必要な場所です。**次のレベル4を待たずに**、準備を終えてください。'
            acts = ['避難先と経路を決め、持ち出す物をまとめ終える',
                    'レベル4を待たず、暗くなる前・雨が強くなる前に出る判断をする']
        else:
            detail = '避難に時間がかかる人は避難を始めるタイミングです。それ以外の人も準備をしてください。'
            acts = ['気象情報と市の避難情報をこまめに確認する', '外出の予定を見合わせ、避難の準備をする']
    for it in items:
        acts.append(f"発令: 警戒レベル{it['level']}・{it['label']}（{it['target']}）"
                    + (f" {str(it.get('issued_at') or '')[5:16].replace('T', ' ')}" if it.get('issued_at') else ''))
    return dict(state='alert', level=lv, headline=headline, detail=detail, acts=acts, leave=leave)


@app.get('/now', response_class=HTMLResponse)
def now_page(request: Request, q: str = '', elderly: int = 0, infant: int = 0, disabled: int = 0, car: int = 0,
             upper_floor: int = 0, pet: int = 0):
    """いま、この住所の人がどうすべきか。**発令 × 想定 × 避難先**を1画面で。"""
    if not q:
        return page(request, 'now_form.html')
    if limited(client_ip(request)):
        raise HTTPException(429, '短時間に多くの判定が行われました。1分ほど待ってから再度お試しください')
    ensure_ready()
    res = check_query(q)
    hh = dict(elderly=elderly, infant=infant, disabled=disabled, car=car, upper_floor=upper_floor, pet=pet, people=0)
    sib = siblings.probe(res.get('lat'), res.get('lon'))
    tl = build_timeline(res, hh, sib)
    nw = build_now(res, tl, hh)
    return page(request, 'now.html', res=res, hh=hh, tl=tl, nw=nw,
                depth_rank=DEPTH_RANK, today=date.today().strftime('%Y年%m月%d日'))


# 宅地建物取引業法施行規則 第16条の4の3 の災害に関する項目。
# **原文で確認した号だけを載せる**（2026-09-18 に e-Gov の法令APIで確認）。
# 「当社のデータで判定できるか」と「説明義務があるか」は別なので、列を分けて出す。
JUYO_ITEMS = [
    dict(key='zosei', no='一号',
         name='造成宅地防災区域',
         law='宅地造成及び特定盛土等規制法 第45条第1項',
         source=None,
         note='都道府県知事が個別に指定する区域で、国のオープンデータにありません。'
              '当社では判定できないため、自治体の公表資料でご確認ください。'),
    dict(key='dosha', no='二号',
         name='土砂災害警戒区域',
         law='土砂災害防止法 第7条第1項',
         source='khazard',
         note='国土数値情報「土砂災害警戒区域」A33 で判定します。'),
    dict(key='tsunami_keikai', no='三号',
         name='津波災害警戒区域',
         law='津波防災地域づくりに関する法律 第53条第1項',
         source=None,
         note='都道府県が個別に指定する区域で、国のオープンデータにありません。'
              '当社では判定できないため、都道府県の公表資料でご確認ください。'
              '（参考として、別データの「津波浸水想定」を下に載せています）'),
    dict(key='suigai', no='三号の二',
         name='水害ハザードマップにおける所在地',
         law='水防法施行規則 第11条第1号の図面',
         source='kflood',
         note='条文が指すのは**市町村長が提供する図面**です。当社が判定に使うのは国の'
              '洪水浸水想定区域と自治体の内水・高潮のデータなので、'
              '**説明には自治体が作成した水害ハザードマップそのものをお使いください。**'),
]


def build_juyo(res, sib):
    """重説の災害項目を1枚にまとめる。**判定できないものを「該当なし」と書かない。**"""
    nat, nai, tks = res['national'], res['naisui'], (res.get('takashio') or {})
    rows = []
    for it in JUYO_ITEMS:
        r = dict(it, verdict='未判定', detail='', vintage='', judged=False)
        if it['key'] == 'dosha':
            d = sib.get('dosha')
            if d is None:
                r.update(verdict='取得できず', detail='土砂災害の判定に接続できませんでした。該当なしという意味ではありません。')
            else:
                r['judged'] = True
                r['vintage'] = d.get('vintage') or ''
                if d.get('inside'):
                    r.update(verdict='該当', detail='・'.join(d.get('labels') or ['土砂災害警戒区域']))
                else:
                    near = d.get('nearest_m')
                    r.update(verdict='非該当',
                             detail=(f'区域外（最寄りの区域まで約{near}m）' if near and near <= 200 else '区域外'))
        elif it['key'] == 'suigai':
            r['judged'] = True
            r['vintage'] = a31_vintage()
            parts = []
            if nat.get('max'):
                parts.append('洪水 想定最大規模 ' + nat['max']['label'])
            elif nat.get('status') == 'uncovered':
                parts.append('洪水 未収録')
            else:
                parts.append('洪水 区域外')
            if nat.get('collapse'):
                parts.append('家屋倒壊等氾濫想定区域 ' + '・'.join(x['label'] for x in nat['collapse']))
            if nai.get('status') == 'inside':
                parts.append(f"内水 {nai.get('depth_label') or ''}")
            if tks.get('status') == 'inside':
                parts.append(f"高潮 {tks.get('depth_label') or ''}")
            r.update(verdict=('参考判定あり' if nat.get('max') or nai.get('status') == 'inside'
                              or tks.get('status') == 'inside' else '参考判定：想定なし'),
                     detail='／'.join(parts))
        else:
            r.update(verdict='当社データなし', detail='自治体・都道府県の公表資料でご確認ください。')
        rows.append(r)
    # 参考（別の号で説明が要るもの）
    ref = []
    m = sib.get('morido')
    if m is not None:
        ref.append(dict(name='宅地造成等工事規制区域・特定盛土等規制区域',
                        law='宅地造成及び特定盛土等規制法',
                        verdict=('該当' if m.get('inside') else '非該当'),
                        detail='・'.join(m.get('labels') or []) if m.get('inside') else '区域外',
                        vintage=m.get('vintage') or ''))
    k = sib.get('riskarea')
    if k is not None:
        ref.append(dict(name='災害危険区域', law='建築基準法 第39条',
                        verdict=('該当' if k.get('inside') else ('非該当' if k.get('status') == 'outside' else '未収録')),
                        detail='・'.join(k.get('labels') or []) if k.get('inside') else
                               ('区域外' if k.get('status') == 'outside' else 'この自治体のデータを収録していません'),
                        vintage=k.get('vintage') or ''))
    t = sib.get('tsunami')
    if t is not None:
        ref.append(dict(name='津波浸水想定（参考。津波災害警戒区域とは別のデータ）',
                        law='国土数値情報 A40',
                        verdict=('浸水想定あり' if t.get('inside') else '想定なし'),
                        detail=(t.get('depth_label') or '') if t.get('inside') else
                               (f"標高 {t.get('elevation_m')}m" if t.get('elevation_m') is not None else ''),
                        vintage=t.get('vintage') or ''))
    return dict(rows=rows, ref=ref)


@app.get('/juyo', response_class=HTMLResponse)
def juyo(request: Request, q: str = ''):
    """重要事項説明の災害項目を、根拠条文とデータ時点つきで1枚にする。

    **これは調査の下ごしらえであって、重要事項説明そのものではない。**
    4項目のうち2項目（造成宅地防災区域・津波災害警戒区域）は国のオープンデータが無く、
    当社では判定できない。そこを黙って「該当なし」にすると、重説の誤りに直結する。
    """
    if not q:
        return page(request, 'juyo_form.html')
    if limited(client_ip(request)):
        raise HTTPException(429, '短時間に多くの判定が行われました。1分ほど待ってから再度お試しください')
    ensure_ready()
    res = check_query(q)
    sib = siblings.probe_juyo(res.get('lat'), res.get('lon'))
    jy = build_juyo(res, sib)
    return page(request, 'juyo.html', res=res, jy=jy,
                checked_at=datetime.now().strftime('%Y年%m月%d日 %H:%M'))


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
    # 土砂・津波・避難先は別の道具に尋ねる。**すでに求めた代表点をそのまま渡す**（引き直さない）。
    sib = siblings.probe(res.get('lat'), res.get('lon'))
    tl = build_timeline(res, hh, sib)
    return page(request, 'timeline.html', res=res, hh=hh, tl=tl,
                                                           depth_rank=DEPTH_RANK, today=date.today().strftime('%Y年%m月%d日'))


@app.get('/ogp.png')
def ogp():
    return FileResponse(os.path.join(ROOT, 'app', 'static', 'ogp.png'), media_type='image/png',
                        headers={'Cache-Control': 'public, max-age=86400'})


# ---- 名古屋市の受け皿ページ（河川別・区別）。検索は河川名・区名で来るので固定URLを持つ ----
try:
    nagoya.seed_if_empty()
except Exception:  # noqa: BLE001
    pass


def _ward_stats(area: str, ward: str):
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute("SELECT sample_n, pct_rank, pct_depth05, pct_depth3, pct_collapse, pct_naisui, computed_at::date FROM ward_stats WHERE area=%s AND ward=%s", (area, ward))
            r = cur.fetchone()
        if not r:
            return None
        return dict(sample_n=r[0], pct_rank=r[1], pct_depth05=r[2], pct_depth3=r[3], pct_collapse=r[4], pct_naisui=r[5], computed_at=str(r[6]))
    except Exception:  # noqa: BLE001
        return None



# 検索する人の言い方と、法令・行政の用語はずれている。両方の語で拾えるように対応表を置く。
# 実例: 名古屋市は「内水ハザードマップ」を「雨水出水浸水想定区域」へ改称し、URLも変えていた
# （旧 /bosaikikikanri/page/0000154015.html は404。2026-09-14 実測）。
TERMS = [("内水ハザードマップ", "雨水出水浸水想定区域（名古屋市はこの呼び名に変えました）"),
         ("浸水マップ・水害マップ", "洪水浸水想定区域"),
         ("何メートル浸かるか", "浸水深（想定最大規模／計画規模）"),
         ("何日浸かるか・いつ引くか", "浸水継続時間"),
         ("家が流される", "家屋倒壊等氾濫想定区域（氾濫流・河岸侵食）"),
         ("下水があふれる・道路冠水", "内水氾濫／雨水出水"),
         ("100年に1度の雨", "計画規模（想定最大規模はさらに大きい雨）")]


_WAGAMACHI_MTIME = 0.0


def _wagamachi_path():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "wagamachi.json")


def _wagamachi():
    """公式ハザードマップのリンク。ファイルが更新されていたら読み直す。

    生存確認のジョブ（kurage_web/backend/sourcelink_jobs.py）が ok を書き換えるので、
    mtime を見て読み直せば**サービスを再起動しなくても**死んだリンクが消える。
    """
    global WAGAMACHI, WAGAMACHI_FETCHED, _WAGAMACHI_MTIME
    try:
        m = os.path.getmtime(_wagamachi_path())
    except OSError:
        return WAGAMACHI
    if m != _WAGAMACHI_MTIME:
        WAGAMACHI, WAGAMACHI_FETCHED = _load_wagamachi()
        _WAGAMACHI_MTIME = m
    return WAGAMACHI


def _load_wagamachi():
    """市区町村の公式ハザードマップへのリンク（scripts/fetch_wagamachi.py が作る）。

    国のデータでの判定は参考情報で、正式なものは市区町村が作るハザードマップ。
    ポータル側のリンクも1割弱が切れているので、生存確認の結果（ok）を見て出し分ける。
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'data', 'wagamachi.json')
    try:
        d = json.load(open(path, encoding='utf-8'))
        return d.get('muni', {}), d.get('_fetched', '')
    except Exception as e:  # noqa: BLE001
        print('わがまちハザードマップを読めません（公式リンクは出しません）:', e)
        return {}, ''


WAGAMACHI, WAGAMACHI_FETCHED = _load_wagamachi()
try:
    _WAGAMACHI_MTIME = os.path.getmtime(_wagamachi_path())
except OSError:
    _WAGAMACHI_MTIME = 0.0


def wagamachi_for(code, kinds=('洪水', '内水')):
    """テンプレートに渡す形。生きているリンクだけ links に、窓口は contact に。"""
    w = _wagamachi().get(code) or {}
    links, contact = [], None
    for kind in kinds:
        it = w.get(kind)
        if not it:
            continue
        contact = contact or it
        if it.get('ok', True):
            links.append({'kind': kind, **it})
    return {'links': links, 'contact': contact, 'fetched': WAGAMACHI_FETCHED, 'terms': TERMS}


@app.get('/nagoya/', response_class=HTMLResponse)
def nagoya_hub(request: Request):
    live = get_live()
    sm = nagoya.city_summary(live)
    rivers = nagoya.rivers()
    return page(request, 'nagoya.html', live=live, sm=sm, wards=nagoya.wards(), rivers=rivers,
                slugs={r['target']: r['slug'] for r in rivers} | {it['target']: nagoya.river_slug(it['target']) for it in sm['items']},
                current_targets={it['target'] for it in sm['items']})


@app.get('/nagoya/{slug}/', response_class=HTMLResponse)
def nagoya_ward(request: Request, slug: str):
    w = nagoya.ward_by_slug(slug)
    if not w:
        raise HTTPException(404, 'その区のページはありません')
    live = get_live()
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT name FROM gakku WHERE area=%s AND ward=%s ORDER BY code NULLS LAST, name", (nagoya.CITY, w['name']))
        gakku = [r[0] for r in cur.fetchall()]
    walerts = nagoya.ward_alerts(w['name'], live)
    gmax = {}
    for a in walerts:
        for g in (gakku if '*' in a['gakku'] else a['gakku']):
            gmax[g] = max(gmax.get(g, 0), a['level'])
    return page(request, 'ward.html', w=w, live=live, gakku=gakku, walerts=walerts, gmax=gmax,
                stats=_ward_stats(nagoya.CITY, w['name']), rivers=nagoya.rivers_for_ward(w['name']),
                wm=wagamachi_for('23100'))


@app.get('/river/{slug}/', response_class=HTMLResponse)
def river_page(request: Request, slug: str):
    live = get_live()
    r = nagoya.river_detail(slug, live)
    if not r:
        raise HTTPException(404, 'その河川のページはありません')
    return page(request, 'river.html', r=r, live=live)


@app.get('/sitemap.xml')
def sitemap(request: Request):
    base = 'https://kurage.exbridge.jp/kflood.php/'
    urls = ['', 'now', 'juyo', 'nagoya/', 'map/', 'timeline', 'batch', 'about'] + [f"nagoya/{w['slug']}/" for w in nagoya.wards()] + [f"river/{r['slug']}/" for r in nagoya.rivers()]
    body = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(f'<url><loc>{base}{u}</loc></url>' for u in urls) + '</urlset>'
    return PlainTextResponse(body, media_type='application/xml')


@app.get('/llms.txt', response_class=PlainTextResponse)
def llms():
    """AI検索（ChatGPT/Claude/Perplexity 等）向けの要約。何を答えられる道具かを最初に書く。"""
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT name,data_vintage,attribution FROM datasets ORDER BY key NOT LIKE 'A31%%', key")
        rows = cur.fetchall()
        cur.execute('SELECT count(*) FROM meshes')
        meshes = cur.fetchone()[0]
    ds = "\n".join(f"- {n}（データ時点 {v}／{a}）" for n, v, a in rows)
    wards = "、".join(w['name'] for w in nagoya.wards())
    rivers = "、".join(r['target'] for r in nagoya.rivers())
    return f"""# {SITE}

> 住所を入れると、その地点が洪水（河川の氾濫）で何メートル・何日浸かる想定か、内水（下水道・水路からの
> 浸水）で何メートル浸かる想定かを、データ時点と出典つきで返すサイト。家屋倒壊等氾濫想定区域（氾濫流・
> 河岸侵食）まで判定し、立退き避難か垂直避難かの目安を返す。

## 洪水と内水の違い（よく混同される）
- 洪水: 河川が氾濫して水が来る想定。国が指定した洪水予報河川・水位周知河川が対象。
- 内水: 下水道や水路が雨をさばききれず、街の中で溢れる想定。自治体ごとに別の図で公開される。
- 別々の図なので片方だけ見て安全と判断されやすい。このサイトは住所ひとつで両方を同時に照らす。

## 収録データ
{ds}
- 収録メッシュ数: {meshes}
- 取り込んでいない場所は「区域外」ではなく「未収録」と返す（安全と誤解させないため）

## 判定して返るもの
- 想定最大規模／計画規模の浸水深ランク（0.5m未満〜20m以上）
- 浸水継続時間（12時間未満〜4週間以上の7段階）
- 家屋倒壊等氾濫想定区域（氾濫流／河岸侵食）
- 内水の浸水深・継続時間（名古屋市）
- 海抜（国土地理院の標高データ）
- 行動の目安（浸水深3m以上・家屋倒壊区域・3日以上の浸水は立退き避難）
- いまの避難情報（名古屋市。住所→学区を引き、市の災害情報配信の警戒レベル・河川・発令時刻を添える）

## 買い切り版
- 商品ページ: https://kappstore.exbridge.jp/app.php?id=41a09acc163dcb7d
- 税込55,000円。ソースコード（MIT）・データ取り込みスクリプト・設置手順書を同梱。自社サーバーで動かせる。

## 使い方
- 住所で調べる: {PUBLIC_BASE}/?q=<住所>
- 地図で見る（国の洪水想定・内水・学区の避難情報を重ねる）: {PUBLIC_BASE}/map/
- マイ・タイムライン（警戒レベル1〜5の行動表を印刷）: {PUBLIC_BASE}/timeline
- CSV一括判定（拠点・物件をまとめて）: {PUBLIC_BASE}/batch
- データと設計の説明: {PUBLIC_BASE}/about
- API: {PUBLIC_BASE}/api/check?q=<住所>

## 名古屋市の個別ページ
- 区: {wards}
- 河川: {rivers}

## 注意
判定は町丁目の代表点による参考情報で、公的な証明ではない。不動産取引の重要事項説明には
自治体のハザードマップそのものを使うこと。正確な区域は自治体の窓口で確認すること。

運営: 株式会社エクスブリッジ https://exbridge.jp/
"""


@app.get('/robots.txt', response_class=PlainTextResponse)
def robots():
    return 'User-agent: *\nAllow: /\nDisallow: /api/\nSitemap: https://kurage.exbridge.jp/kflood.php/sitemap.xml\n'
