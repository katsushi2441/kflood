#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国土数値情報 A49（高潮浸水想定区域）を PostGIS に取り込む。

**配信は4都府県だけ**（2026-09-21 実測）。東京・千葉・兵庫・福岡。
  東京都・兵庫県・福岡県 … オープンデータとして利用可（商用可・再配信可）
  千葉県 … 条件付き公開（原則として商用利用可・再配信可。県の説明資料を確認することが条件）
名古屋市の高潮は市の CC BY データで別に入れている（load_nagoya_takashio.py）。

属性は3つだけ。**浸水深は数値ではなく「0.3m以上0.5m未満」のような区分の文字列**なので、
区分の下限を数値に直して入れる（上限で入れると危険側に見積もりすぎる。画面は「◯m以上」と読ませる）。
  A49_001 = 都道府県名 / A49_002 = 都道府県コード / A49_003 = 浸水深の区分

**継続時間は A49 に無い。** 名古屋市版にはあるので、画面で「この地域は継続時間が未収録」と
書き分けられるように、takashio_duration には何も入れない。

判定範囲（takashio_coverage）は**都道府県の単位**にする。区域の外接矩形にすると
隣の県に食い込むし、区域そのものにすると「指定区域の外」を「未収録」と答えてしまう。
都道府県が全域を対象に指定を公表しているので、その県の住所なら「判定済み」でよい。
画面側は住所の文字列に area が含まれるかで先に選ぶ（naisui と同じ考え方）。

  python3 scripts/load_a49_takashio.py             # 東京都・千葉県
  python3 scripts/load_a49_takashio.py --pref 13   # 東京都だけ
  python3 scripts/load_a49_takashio.py --force     # 入れ直し
"""
import argparse
import os
import re
import subprocess
import sys
import time
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(os.environ.get('KFLOOD_RAW_DIR', os.path.join(ROOT, 'data', 'raw')), 'a49')
UA = {'User-Agent': 'kflood/1.0 (kurage.exbridge.jp; +https://exbridge.jp/)'}
SRID = 6668
DB = dict(host='127.0.0.1', port=int(os.environ.get('KFLOOD_DB_PORT', '55434')), dbname='kflood',
          user='postgres', password=os.environ.get('KFLOOD_DB_PASS', 'kflood_local'))
BASE = 'https://nlftp.mlit.go.jp/ksj/gml/data/A49/A49-20/A49-20_{pref}.GML.zip'
PAGE = 'https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-A49-v1_0.html'

PREFS = {
    '13': dict(area='東京都', vintage='令和2年（2020年）公表・東京都の高潮浸水想定区域（想定最大規模）',
               terms='オープンデータとして利用可（商用利用可・再配信可）'),
    '12': dict(area='千葉県', vintage='令和2年（2020年）公表・東京湾沿岸（千葉県区間）の高潮浸水想定区域（想定最大規模）',
               terms='条件付き公開（原則として商用利用可・再配信可。県の説明資料を確認すること）',
               note='利用条件: 千葉県の「高潮浸水想定区域図について（東京湾沿岸[千葉県区間]）説明資料」を確認のうえ利用'),
}
ATTRIBUTION = '出典: 国土数値情報（高潮浸水想定区域）国土交通省 を加工して作成'

# 区分の下限（m）。ここに無い表記が出たら止める（推測で割り当てない）。
BANDS = {
    '0.3m未満': 0.0,
    '0.3m以上0.5m未満': 0.3,
    '0.5m以上1m未満': 0.5,
    '0.5m以上1.0m未満': 0.5,
    '1m以上3m未満': 1.0,
    '1.0m以上3.0m未満': 1.0,
    '3m以上5m未満': 3.0,
    '3.0m以上5.0m未満': 3.0,
    '5m以上10m未満': 5.0,
    '5.0m以上10.0m未満': 5.0,
    '10m以上20m未満': 10.0,
    '10.0m以上20.0m未満': 10.0,
    '20m以上': 20.0,
    '20.0m以上': 20.0,
}

DDL = """
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE IF NOT EXISTS takashio_depth (
  id bigserial PRIMARY KEY, dataset_key text NOT NULL REFERENCES datasets(key) ON DELETE CASCADE,
  area text NOT NULL, depth_m real, geom geometry(MultiPolygon, 6668) NOT NULL);
CREATE INDEX IF NOT EXISTS takashio_depth_geom_idx ON takashio_depth USING GIST (geom);
CREATE TABLE IF NOT EXISTS takashio_coverage (
  area text PRIMARY KEY, dataset_keys text[] NOT NULL, loaded_at timestamptz NOT NULL, geom geometry(Polygon, 6668));
"""


def psql(sql, args=None, fetch=True):
    import psycopg2
    with psycopg2.connect(**DB) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            if fetch and cur.description:
                return cur.fetchall()
    return None


def pg_dsn():
    return f"host={DB['host']} port={DB['port']} dbname={DB['dbname']} user={DB['user']} password={DB['password']}"


def fetch(pref):
    os.makedirs(RAW, exist_ok=True)
    path = os.path.join(RAW, f'A49-20_{pref}.zip')
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return path
    url = BASE.format(pref=pref)
    print(f'  取得中: {url}')
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=1800) as r, open(path + '.part', 'wb') as f:
        while True:
            b = r.read(1 << 22)
            if not b:
                break
            f.write(b)
    os.replace(path + '.part', path)
    return path


def extract_shp(zpath):
    d = zpath[:-4]
    if not os.path.isdir(d):
        with zipfile.ZipFile(zpath) as z:
            # geojson と xml は使わない（同じ中身で数十〜百MBある）
            for info in z.infolist():
                if info.is_dir() or re.search(r'\.(geojson|xml)$', info.filename, re.I):
                    continue
                target = os.path.join(d, os.path.basename(info.filename))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(info) as src, open(target, 'wb') as dst:
                    while True:
                        b = src.read(1 << 24)
                        if not b:
                            break
                        dst.write(b)
    for root, _, files in os.walk(d):
        for fn in files:
            if fn.lower().endswith('.shp'):
                return os.path.join(root, fn)
    sys.exit(f'Shapefile が見つかりません: {d}')


def load(pref, force=False):
    meta = PREFS[pref]
    key = f'a49_takashio_{pref}'
    if not force and psql('SELECT 1 FROM datasets WHERE key=%s', (key,)):
        print(f'  {key}: 取り込み済み（--force で入れ直し）')
        return 0
    shp = extract_shp(fetch(pref))
    print(f'  取り込み中: {os.path.basename(shp)}')
    t = time.time()
    env = dict(os.environ, SHAPE_ENCODING='CP932')
    cmd = ['ogr2ogr', '-f', 'PostgreSQL', 'PG:' + pg_dsn(), shp, '-nln', 'a49_stage', '-overwrite',
           '-select', 'A49_003', '-t_srs', f'EPSG:{SRID}', '-nlt', 'PROMOTE_TO_MULTI',
           '-lco', 'GEOMETRY_NAME=geom', '-lco', 'FID=id', '-gt', '20000', '--config', 'PG_USE_COPY', 'YES']
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f'ogr2ogr 失敗: {r.stderr[-800:]}')

    bands = [b for (b,) in psql('SELECT DISTINCT a49_003 FROM a49_stage')]
    unknown = [b for b in bands if b and b not in BANDS]
    if unknown:
        psql('DROP TABLE IF EXISTS a49_stage', fetch=False)
        raise RuntimeError(f'凡例に無い区分: {unknown}（推測で割り当てない）')
    print('    区分: ' + ' / '.join(f'{b}→{BANDS[b]}m' for b in sorted(bands, key=lambda x: BANDS[x]) if b))

    psql("""INSERT INTO datasets(key,name,source_url,data_vintage,loaded_at,attribution,note)
            VALUES(%s,%s,%s,%s,now(),%s,%s)
            ON CONFLICT (key) DO UPDATE SET source_url=EXCLUDED.source_url, data_vintage=EXCLUDED.data_vintage,
              loaded_at=EXCLUDED.loaded_at, attribution=EXCLUDED.attribution, note=EXCLUDED.note""",
         (key, f'国土数値情報 高潮浸水想定区域 A49 {meta["area"]}', BASE.format(pref=pref), meta['vintage'],
          ATTRIBUTION, meta.get('note') or f'利用条件: {meta["terms"]}。カタログ: {PAGE}'), fetch=False)
    psql('DELETE FROM takashio_depth WHERE dataset_key=%s', (key,), fetch=False)
    case = ' '.join(f"WHEN a49_003 = '{b}' THEN {v}" for b, v in BANDS.items())
    n = psql(f"""INSERT INTO takashio_depth(dataset_key, area, depth_m, geom)
                 SELECT %s, %s, (CASE {case} ELSE NULL END), ST_Multi(geom) FROM a49_stage
                 WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom) RETURNING 1""", (key, meta['area']))
    n = len(n or [])
    psql('DROP TABLE IF EXISTS a49_stage', fetch=False)
    # 判定範囲は都道府県単位。geom は参考（画面は住所の文字列で先に選ぶ）
    psql("""INSERT INTO takashio_coverage(area, dataset_keys, loaded_at, geom)
            SELECT %s, ARRAY[%s], now(), ST_Envelope(ST_Extent(geom))::geometry(Polygon,6668)
              FROM takashio_depth WHERE area=%s
            ON CONFLICT (area) DO UPDATE SET
              dataset_keys = array_append(array_remove(takashio_coverage.dataset_keys, %s), %s),
              loaded_at=now(), geom=EXCLUDED.geom""", (meta['area'], key, meta['area'], key, key), fetch=False)
    print(f'  {key}: {n:,} 面（{time.time() - t:.0f}s）')
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pref', action='append', choices=sorted(PREFS), help='既定は東京都・千葉県')
    ap.add_argument('--force', action='store_true')
    a = ap.parse_args()
    psql(DDL, fetch=False)
    total = 0
    for pref in (a.pref or ['13', '12']):
        print(f'== A49 {PREFS[pref]["area"]}')
        try:
            total += load(pref, force=a.force)
        except Exception as e:  # noqa: BLE001
            print(f'  失敗: {e}', file=sys.stderr)
    print(f'\n合計 {total:,} 面')


if __name__ == '__main__':
    main()
