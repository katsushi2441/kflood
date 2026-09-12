#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""名古屋市の高潮ハザードマップを PostGIS に取り込む。

なぜ kflood に足すか（別製品にしない理由）:
  洪水・内水・高潮は「その住所が何メートル浸かるか」という**同じ問い**。
  製品を分けると、利用者が3つのサイトを回ることになり導線が割れる。

なぜ名古屋で高潮が重いか:
  伊勢湾台風（1959年）は高潮で5,000人以上が亡くなった。名古屋で最も人が死んだ災害は高潮で、
  市の南西部は今も海抜ゼロメートル地帯が広がる。

データ（BODIK 名古屋市オープンデータ・CC BY 4.0。2026-09-12 実測）:
  浸水想定     storm_surge_hazard_map-inundation_assumption.zip
  浸水継続時間 storm_surge_hazard_map-inundation_duration.zip
  愛知県が令和3年6月に公表した高潮浸水想定区域図がもと。
  **室戸台風規模の台風が満潮時に伊勢湾へ最悪の経路で来た場合**の想定であり、
  毎年来る台風の想定ではない。画面にも必ずそう書く。

使い方:
  python3 scripts/load_nagoya_takashio.py            # 2つとも
  python3 scripts/load_nagoya_takashio.py --force    # 入れ直し
"""
import argparse
import os
import subprocess
import sys
import time
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(os.environ.get('KFLOOD_RAW_DIR', os.path.join(ROOT, 'data', 'raw')), 'nagoya')
UA = {'User-Agent': 'kflood/1.0 (kurage.exbridge.jp; +https://exbridge.jp/)'}
SRID = 6668
DB = dict(host='127.0.0.1', port=int(os.environ.get('KFLOOD_DB_PORT', '55434')), dbname='kflood',
          user='postgres', password=os.environ.get('KFLOOD_DB_PASS', 'kflood_local'))

CATALOG = 'https://data.bodik.jp/dataset/231002_0200030000_33'
DATASETS = {
    'nagoya_takashio_depth': dict(
        name='名古屋市 高潮ハザードマップ（浸水想定）',
        url='https://data.bodik.jp/dataset/95308e7c-abb0-44b5-b052-cc5fc097566f/resource/'
            '3e9c2595-54fb-46b3-98fd-9652058e4206/download/storm_surge_hazard_map-inundation_assumption.zip',
        page=CATALOG,
        vintage='愛知県が令和3年6月に公表した高潮浸水想定区域図にもとづく（室戸台風規模・満潮時）',
        field='suishin', col='depth_m', table='takashio_depth', unit='m', area='名古屋市'),
    'nagoya_takashio_duration': dict(
        name='名古屋市 高潮ハザードマップ（浸水継続時間）',
        url='https://data.bodik.jp/dataset/95308e7c-abb0-44b5-b052-cc5fc097566f/resource/'
            '6b65352a-062c-4165-8a5e-660801129d76/download/storm_surge_hazard_map-inundation_duration.zip',
        page=CATALOG,
        vintage='愛知県が令和3年6月に公表した高潮浸水想定区域図にもとづく（室戸台風規模・満潮時）',
        # jikan_h は**時間**（分ではない）。内水は Time_min＝分なので混同しないこと
        field='jikan_h', col='hours', table='takashio_duration', unit='時間', area='名古屋市'),
}
ATTRIBUTION = '出典: 名古屋市「{name}」（オープンデータ、CC BY 4.0）を加工して作成'

DDL = """
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE IF NOT EXISTS takashio_depth (
  id bigserial PRIMARY KEY, dataset_key text NOT NULL REFERENCES datasets(key) ON DELETE CASCADE,
  area text NOT NULL, depth_m real, geom geometry(MultiPolygon, 6668) NOT NULL);
CREATE INDEX IF NOT EXISTS takashio_depth_geom_idx ON takashio_depth USING GIST (geom);
CREATE TABLE IF NOT EXISTS takashio_duration (
  id bigserial PRIMARY KEY, dataset_key text NOT NULL REFERENCES datasets(key) ON DELETE CASCADE,
  area text NOT NULL, hours real, geom geometry(MultiPolygon, 6668) NOT NULL);
CREATE INDEX IF NOT EXISTS takashio_duration_geom_idx ON takashio_duration USING GIST (geom);
-- 高潮データの収録範囲。範囲外は「未収録」であって「浸水なし」ではない
CREATE TABLE IF NOT EXISTS takashio_coverage (
  area text PRIMARY KEY, dataset_keys text[] NOT NULL, loaded_at timestamptz NOT NULL, geom geometry(Polygon, 6668));
"""


def pg_dsn():
    return f"host={DB['host']} port={DB['port']} dbname={DB['dbname']} user={DB['user']} password={DB['password']}"


def psql(sql, args=None, fetch=True):
    import psycopg2
    with psycopg2.connect(**DB) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            if fetch and cur.description:
                return cur.fetchall()
    return None


def fetch(url, name):
    os.makedirs(RAW, exist_ok=True)
    path = os.path.join(RAW, name)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    print(f'  取得中: {url}')
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=900) as r, open(path + '.part', 'wb') as f:
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
            for info in z.infolist():
                try:
                    fn = info.filename.encode('cp437').decode('cp932')
                except (UnicodeEncodeError, UnicodeDecodeError):
                    fn = info.filename
                target = os.path.join(d, fn)
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
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


def load(key, ds, force=False):
    if not force and psql('SELECT 1 FROM datasets WHERE key=%s', (key,)):
        print(f'  {key}: 取り込み済み（--force で入れ直し）')
        return 0
    zpath = fetch(ds['url'], os.path.basename(ds['url']))
    shp = extract_shp(zpath)
    layer = os.path.splitext(os.path.basename(shp))[0]
    print(f'  取り込み中: {layer}')
    t = time.time()
    env = dict(os.environ, SHAPE_ENCODING='CP932')
    # 属性名は事前に分からないので、全列を持ってきてから数値列を1つ選ぶ
    # **列は必ず明示する。** 数値列を自動で選ぶと pointid（連番ID）を拾って
    # 継続時間が 1〜6,410,100 という無意味な値で入った（2026-09-12 実際にやった）。
    cmd = ['ogr2ogr', '-f', 'PostgreSQL', 'PG:' + pg_dsn(), shp, '-nln', 'takashio_stage', '-overwrite',
           '-select', ds['field'], '-t_srs', f'EPSG:{SRID}', '-nlt', 'PROMOTE_TO_MULTI',
           '-lco', 'GEOMETRY_NAME=geom', '-lco', 'FID=id', '-gt', '20000', '--config', 'PG_USE_COPY', 'YES']
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f'ogr2ogr 失敗: {r.stderr[-800:]}')
    cols = [c for c, _ in psql("""SELECT column_name, data_type FROM information_schema.columns
                   WHERE table_name='takashio_stage' AND column_name NOT IN ('id','geom')""")]
    if len(cols) != 1:
        raise RuntimeError(f'一時表の列が想定外: {cols}（-select {ds["field"]} が効いていない）')
    val = cols[0]
    print(f'    属性列: {val}（{ds["unit"]}）')
    psql("""INSERT INTO datasets(key,name,source_url,data_vintage,loaded_at,attribution,note)
            VALUES(%s,%s,%s,%s,now(),%s,%s)
            ON CONFLICT (key) DO UPDATE SET source_url=EXCLUDED.source_url, data_vintage=EXCLUDED.data_vintage,
              loaded_at=EXCLUDED.loaded_at, attribution=EXCLUDED.attribution, note=EXCLUDED.note""",
         (key, ds['name'], ds['url'], ds['vintage'],
          ATTRIBUTION.format(name=ds['name'].replace('名古屋市 ', '')), f'カタログ: {ds["page"]}'), fetch=False)
    psql(f'DELETE FROM {ds["table"]} WHERE dataset_key=%s', (key,), fetch=False)
    n = psql(f"""INSERT INTO {ds["table"]}(dataset_key, area, {ds["col"]}, geom)
                 SELECT %s, %s, "{val}", ST_Multi(geom) FROM takashio_stage
                 WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom) RETURNING 1""", (key, ds['area']))
    n = len(n or [])
    psql('DROP TABLE IF EXISTS takashio_stage', fetch=False)
    psql("""INSERT INTO takashio_coverage(area, dataset_keys, loaded_at, geom)
            SELECT %s, ARRAY[%s], now(), ST_Envelope(ST_Extent(geom))::geometry(Polygon,6668) FROM {t} WHERE area=%s
            ON CONFLICT (area) DO UPDATE SET dataset_keys = array_append(array_remove(takashio_coverage.dataset_keys, %s), %s),
              loaded_at=now(), geom=EXCLUDED.geom""".replace('{t}', ds['table']),
         (ds['area'], key, ds['area'], key, key), fetch=False)
    print(f'  {key}: {n:,} 面（{time.time() - t:.0f}s）')
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true')
    a = ap.parse_args()
    psql(DDL, fetch=False)
    total = 0
    for key, ds in DATASETS.items():
        print(f'== {ds["name"]}')
        try:
            total += load(key, ds, force=a.force)
        except Exception as e:  # noqa: BLE001
            print(f'  {key}: 失敗 {e}', file=sys.stderr)
    print(f'\n合計 {total:,} 面')


if __name__ == '__main__':
    main()
