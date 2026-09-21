#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""東京都「浸水予想区域図」（建設局・CC BY）を PostGIS に取り込む。

**なぜこの経路か（2026-09-21 実測）**
- 重ねるハザードマップの内水タイルは、東京都では **福生市しか無い**（23区はタイルが404）。
- 国土数値情報 A51 も東京都は福生市だけ。
- 都建設局が浸水予想区域図を **CC BY の CSV** で出している。都が管理する全河川（島しょ除く）を
  14区域に分けたもので、17リソース・約687MB。これが東京都で唯一、全域を機械判定できる形。

**中身は面ではなく点群。** 列は `図郭No, 浸水深, 地盤高, 緯度, 経度` で、刻みは約11m。
神田川流域だけで110万点ある。面に変換すると2,000万ポリゴンになって配布物にできないので、
**点のまま入れて最近傍で引く**（naisui_points）。

**この区域図は内水だけではない。** 都の説明に「川から水があふれる外水氾濫と、下水道管の能力を
超えた雨水がたまる内水氾濫の両方を示しています」とある。国が指定する洪水浸水想定区域
（A31b・大河川）とは別物なので、画面では内水の枠に置きつつ出典名をそのまま出す。

**配布ファイルに3つの罠がある（2026-09-21 実測）。推測で読まず、毎回検査する。**
 1. **文字コードが混在**。UTF-8(BOM付き) と CP932 の両方がある（秋川・浅川・多摩川は CP932）。
 2. **ヘッダは「緯度,経度」なのに、実データが「経度,緯度」のファイルがある**
    （秋川の分割版3本）。ヘッダを信じると東京が中国大陸へ飛ぶ。**値域で必ず検算する。**
 3. **同じ中身のリソースが重複して登録されている**。
    - 秋川: 全体1本(81MB) と 分割3本(合計81MB) の両方があり、図郭No も 1〜242 で一致する
    - 境川と鶴見川: **バイト単位で同一のファイル**が別名で登録されている
    そのまま全部入れると二重に数える。名前と内容ハッシュで1本に落とす。

  python3 scripts/load_tokyo_naisui.py              # 全流域
  python3 scripts/load_tokyo_naisui.py --only kanda # ファイル名に kanda を含むものだけ
  python3 scripts/load_tokyo_naisui.py --force      # 入れ直し
  python3 scripts/load_tokyo_naisui.py --plan       # 何を入れて何を捨てるかだけ出す
"""
import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(os.environ.get('KFLOOD_RAW_DIR', os.path.join(ROOT, 'data', 'raw')), 'tokyo_naisui')
UA = {'User-Agent': 'kflood/1.0 (kurage.exbridge.jp; +https://exbridge.jp/)'}
SRID = 6668
AREA = '東京都'
DB = dict(host='127.0.0.1', port=int(os.environ.get('KFLOOD_DB_PORT', '55434')), dbname='kflood',
          user='postgres', password=os.environ.get('KFLOOD_DB_PASS', 'kflood_local'))
CKAN = 'https://catalog.data.metro.tokyo.lg.jp/api/3/action/package_search'
PAGE = 'https://catalog.data.metro.tokyo.lg.jp/'
VINTAGE = '令和3年度改定（東京都建設局・浸水予想区域図／想定最大規模）'
ATTRIBUTION = '出典: 東京都建設局「浸水予想区域図」（CC BY 4.0）を加工して作成'
LAT_RANGE = (20.0, 46.0)
LON_RANGE = (122.0, 154.0)

DDL = """
CREATE EXTENSION IF NOT EXISTS postgis;
-- 面ではなく点で持つ内水データ。**11mメッシュの点群**なので、判定は最近傍で行う。
CREATE TABLE IF NOT EXISTS naisui_points (
  id bigserial PRIMARY KEY,
  dataset_key text NOT NULL REFERENCES datasets(key) ON DELETE CASCADE,
  area text NOT NULL,
  depth_m real, ground_m real,
  geom geometry(Point, 6668) NOT NULL);
CREATE INDEX IF NOT EXISTS naisui_points_geom_idx ON naisui_points USING GIST (geom);
CREATE INDEX IF NOT EXISTS naisui_points_area_idx ON naisui_points (area);
CREATE TABLE IF NOT EXISTS naisui_coverage (
  area text PRIMARY KEY, dataset_keys text[] NOT NULL, loaded_at timestamptz NOT NULL,
  geom geometry(MultiPolygon, 6668));
"""


def psql(sql, args=None, fetch=True):
    import psycopg2
    with psycopg2.connect(**DB) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            if fetch and cur.description:
                return cur.fetchall()
    return None


def catalog():
    url = CKAN + '?' + urllib.parse.urlencode({'q': '浸水予想区域図', 'rows': 30})
    d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120))
    out, seen = [], set()
    for p in d['result']['results']:
        if p.get('organization', {}).get('title') != '東京都建設局':
            continue
        for r in p.get('resources', []):
            u = r.get('url', '')
            if not u.endswith('.csv') or u in seen:
                continue
            seen.add(u)
            out.append(dict(name=(r.get('name') or '').strip(), url=u, file=os.path.basename(u)))
    return out


def fetch(item):
    os.makedirs(RAW, exist_ok=True)
    path = os.path.join(RAW, item['file'])
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return path
    print(f"  取得中: {item['file']}")
    req = urllib.request.Request(item['url'], headers=UA)
    with urllib.request.urlopen(req, timeout=3600) as r, open(path + '.part', 'wb') as f:
        while True:
            b = r.read(1 << 22)
            if not b:
                break
            f.write(b)
    os.replace(path + '.part', path)
    return path


def open_text(path):
    """BOM / UTF-8 / CP932 を見分けて開く。文字コードは決め打ちしない。"""
    with open(path, 'rb') as f:
        head = f.read(4096)
    if head[:3] == b'\xef\xbb\xbf':
        return open(path, encoding='utf-8-sig', newline=''), 'utf-8-sig'
    try:
        head.decode('utf-8')
        return open(path, encoding='utf-8', newline=''), 'utf-8'
    except UnicodeDecodeError:
        return open(path, encoding='cp932', errors='replace', newline=''), 'cp932'


def probe(path, sample=2000):
    """ヘッダの列位置と、実データの値域から lat/lon の列を決める。

    ヘッダが「緯度,経度」でも中身が逆のファイルが実在する。**値域が正**とする。
    """
    fh, enc = open_text(path)
    with fh:
        rd = csv.reader(fh)
        header = next(rd, [])
        cols = [(h or '').strip().lower() for h in header]
        def find(*names):
            for i, c in enumerate(cols):
                if any(n in c for n in names):
                    return i
            return None
        i_depth, i_ground = find('浸水深'), find('地盤')
        i_lat, i_lon = find('緯度'), find('経度')
        rows = []
        for i, row in enumerate(rd):
            if i >= sample:
                break
            if len(row) >= max(x for x in (i_depth, i_ground, i_lat, i_lon) if x is not None) + 1:
                rows.append(row)
    if None in (i_depth, i_ground, i_lat, i_lon):
        raise RuntimeError(f'ヘッダから列を決められない: {header}')

    def frac_in(idx, lo, hi):
        ok = 0
        for r in rows:
            try:
                v = float(r[idx])
            except (ValueError, IndexError):
                continue
            if lo <= v <= hi:
                ok += 1
        return ok / max(len(rows), 1)

    swapped = False
    if frac_in(i_lat, *LAT_RANGE) < 0.9 and frac_in(i_lat, *LON_RANGE) > 0.9:
        i_lat, i_lon, swapped = i_lon, i_lat, True
    if frac_in(i_lat, *LAT_RANGE) < 0.9 or frac_in(i_lon, *LON_RANGE) < 0.9:
        raise RuntimeError('緯度経度の値域が日本の範囲に入らない（列の対応が判定できない）')
    return dict(enc=enc, depth=i_depth, ground=i_ground, lat=i_lat, lon=i_lon, swapped=swapped, header=header)


def rows_for_copy(path, spec):
    """COPY に流す正規化済みのテキスト（depth,ground,lat,lon）。列順と文字コードをここで吸収する。"""
    fh, _ = open_text(path)
    with fh:
        rd = csv.reader(fh)
        next(rd, None)
        buf = io.StringIO()
        w = csv.writer(buf)
        n = 0
        for row in rd:
            try:
                d, g = row[spec['depth']], row[spec['ground']]
                la, lo = float(row[spec['lat']]), float(row[spec['lon']])
            except (ValueError, IndexError):
                continue
            if not (LAT_RANGE[0] <= la <= LAT_RANGE[1] and LON_RANGE[0] <= lo <= LON_RANGE[1]):
                continue
            w.writerow([d or '', g or '', la, lo])
            n += 1
            if buf.tell() > (1 << 22):
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)
        if buf.tell():
            yield buf.getvalue()


def dedupe(items):
    """重複リソースを落とす。(1) 名前が同じなら最大のものだけ (2) 中身が同じなら1本だけ。"""
    for it in items:
        it['path'] = fetch(it)
        it['size'] = os.path.getsize(it['path'])
        with open(it['path'], 'rb') as f:
            it['hash'] = hashlib.md5(f.read(1 << 20)).hexdigest()[:16] + f'-{it["size"]}'
        it['base'] = re.sub(r'\s*\(\d+\)\s*$', '', it['name']).strip()

    keep, drop = [], []
    by_name = {}
    for it in items:
        by_name.setdefault(it['base'], []).append(it)
    for base, group in by_name.items():
        if len(group) == 1:
            keep.append(group[0])
            continue
        best = max(group, key=lambda x: x['size'])
        keep.append(best)
        for it in group:
            if it is not best:
                it['why'] = f'同名リソースの分割版（{best["file"]} に含まれる）'
                drop.append(it)
    seen = {}
    final = []
    for it in sorted(keep, key=lambda x: x['file']):
        if it['hash'] in seen:
            it['why'] = f'{seen[it["hash"]]["file"]} と中身が同一'
            drop.append(it)
            continue
        seen[it['hash']] = it
        final.append(it)
    return final, drop


def load(item, force=False):
    import psycopg2
    key = 'tokyo_naisui_' + re.sub(r'[^0-9a-z]+', '_', os.path.splitext(item['file'])[0].replace('shinsui_', '').lower())
    if not force and psql('SELECT 1 FROM datasets WHERE key=%s', (key,)):
        print(f'  {key}: 取り込み済み（--force で入れ直し）')
        return 0
    spec = probe(item['path'])
    note = f"文字コード {spec['enc']}" + ('・列順が経度/緯度で入っていたので入れ替えた' if spec['swapped'] else '')
    print(f"    {note}")
    t = time.time()
    psql("""INSERT INTO datasets(key,name,source_url,data_vintage,loaded_at,attribution,note)
            VALUES(%s,%s,%s,%s,now(),%s,%s)
            ON CONFLICT (key) DO UPDATE SET source_url=EXCLUDED.source_url, data_vintage=EXCLUDED.data_vintage,
              loaded_at=EXCLUDED.loaded_at, attribution=EXCLUDED.attribution, note=EXCLUDED.note""",
         (key, '東京都 ' + item['name'].replace('　', ' ').strip(), item['url'], VINTAGE, ATTRIBUTION,
          '都が管理する中小河川の外水氾濫と下水道からの内水氾濫の両方を示した区域図'), fetch=False)

    with psycopg2.connect(**DB) as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM naisui_points WHERE dataset_key=%s', (key,))
            cur.execute('CREATE TEMP TABLE stage (depth real, ground real, '
                        'lat double precision, lon double precision) ON COMMIT DROP')
            src = io.StringIO()

            class Chunks(io.TextIOBase):
                """COPY へ渡すための、行を作りながら流すストリーム。"""
                def __init__(self, gen):
                    self.gen, self.buf = gen, ''

                def read(self, size=-1):
                    while size < 0 or len(self.buf) < size:
                        try:
                            self.buf += next(self.gen)
                        except StopIteration:
                            break
                    if size < 0:
                        out, self.buf = self.buf, ''
                        return out
                    out, self.buf = self.buf[:size], self.buf[size:]
                    return out

                def readline(self, size=-1):
                    while '\n' not in self.buf:
                        try:
                            self.buf += next(self.gen)
                        except StopIteration:
                            break
                    i = self.buf.find('\n')
                    if i < 0:
                        out, self.buf = self.buf, ''
                        return out
                    out, self.buf = self.buf[:i + 1], self.buf[i + 1:]
                    return out

            del src
            cur.copy_expert("COPY stage FROM STDIN WITH (FORMAT csv)", Chunks(rows_for_copy(item['path'], spec)))
            cur.execute("""INSERT INTO naisui_points(dataset_key, area, depth_m, ground_m, geom)
                           SELECT %s, %s, depth, ground, ST_SetSRID(ST_MakePoint(lon, lat), %s) FROM stage""",
                        (key, AREA, SRID))
            n = cur.rowcount
        conn.commit()
    print(f'  {key}: {n:,} 点（{time.time() - t:.0f}s）')
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--plan', action='store_true', help='取り込まずに、使うファイルと捨てるファイルだけ出す')
    a = ap.parse_args()
    psql(DDL, fetch=False)
    items = catalog()
    if a.only:
        items = [i for i in items if a.only in i['file']]
    print(f'カタログ {len(items)} リソース')
    use, drop = dedupe(items)
    print(f'  取り込む {len(use)} 本 / 重複として捨てる {len(drop)} 本')
    for it in drop:
        print(f"    - {it['file']}（{it['why']}）")
    if a.plan:
        for it in use:
            print(f"    + {it['file']} {it['size'] / 1048576:.0f}MB  {it['name'][:44]}")
        return

    total = 0
    for it in use:
        print(f"== {it['name'][:50]}")
        try:
            total += load(it, force=a.force)
        except Exception as e:  # noqa: BLE001
            print(f'  失敗: {e}', file=sys.stderr)

    keys = [k for (k,) in psql('SELECT DISTINCT dataset_key FROM naisui_points WHERE area=%s', (AREA,)) or []]
    if keys:
        # 収録範囲は点群の外接矩形にしない。矩形にすると、浸水想定が載っていない場所まで
        # 「範囲内・浸水なし」と答えてしまう（豊山町で踏んだのと同じ誤り）。
        # 点を30mバッファして融合した形にする。
        print('  収録範囲を作成中（点の集合から）…')
        psql("""INSERT INTO naisui_coverage(area, dataset_keys, loaded_at, geom)
                SELECT %s, %s, now(),
                       ST_Multi(ST_Union(ST_Buffer(geom, 0.0004)))::geometry(MultiPolygon,6668)
                  FROM (SELECT ST_SnapToGrid(geom, 0.002) AS geom FROM naisui_points
                         WHERE area=%s GROUP BY 1) g
                ON CONFLICT (area) DO UPDATE SET dataset_keys=EXCLUDED.dataset_keys,
                  loaded_at=now(), geom=EXCLUDED.geom""", (AREA, keys, AREA), fetch=False)
        n = psql('SELECT count(*) FROM naisui_points WHERE area=%s', (AREA,))[0][0]
        print(f'  {AREA} の点: {n:,}')
    print(f'\n合計 {total:,} 点')


if __name__ == '__main__':
    main()
