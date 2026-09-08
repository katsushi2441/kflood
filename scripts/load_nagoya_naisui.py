#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""名古屋市の内水氾濫ハザードマップ（想定最大規模）を PostGIS に取り込む。

内水（下水道や水路から溢れる浸水）は国の一括データが無く、自治体ごとに公開される。
名古屋市は「なごや防災オープンデータカタログ」で Shapefile を CC BY で配布している（2026-09-08 実測）:
  浸水想定   inland_flood_hazard_map-inundation_assumption.zip  4,877,779 面・属性「浸水深」(m)
  浸水継続時間 inland_flood_hazard_map-inundation_duration.zip   3,423,111 面・属性 Time_min(分)
  座標系 EPSG:2449（JGD2000 平面直角座標系 第VII系）・約5m四方のセル

他の自治体版を作るときは DATASETS にその自治体の配布物を足す（属性名・座標系・出典表記を合わせる）。

使い方:
  python3 scripts/load_nagoya_naisui.py            # 2つとも
  python3 scripts/load_nagoya_naisui.py --force    # 入れ直し
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

CATALOG = 'https://nui-ngy-bosai.jp/dataset/'
DATASETS = {
    'nagoya_naisui_depth': dict(
        name='名古屋市 内水氾濫ハザードマップ（浸水想定・想定最大規模）',
        url='https://nui-ngy-bosai.jp/dataset/2fb7a42f-c9e7-44d7-84e0-e96ccac6549f/resource/78f5389f-912f-47ec-badb-7b088ecb37dc/download/inland_flood_hazard_map-inundation_assumption.zip',
        page=CATALOG + 'inlandwaterassumption-20220527',
        vintage='2022-05-27（令和4年5月 想定最大規模の浸水想定区域図を指定）',
        field='浸水深', col='depth_m', table='naisui_depth', unit='m',
        area='名古屋市'),
    'nagoya_naisui_duration': dict(
        name='名古屋市 内水氾濫ハザードマップ（浸水継続時間）',
        url='https://nui-ngy-bosai.jp/dataset/63fc3466-6a08-4e2b-99e6-38af3670d5fc/resource/9486e7a4-5765-4762-9d6e-e5ac44ee920d/download/inland_flood_hazard_map-inundation_duration.zip',
        page=CATALOG + 'inlandwatertime-20230307',
        vintage='2023-03-07（カタログ掲載日）',
        field='Time_min', col='minutes', table='naisui_duration', unit='分',
        area='名古屋市'),
}
ATTRIBUTION = '出典: 名古屋市「{name}」（なごや防災オープンデータカタログ、CC BY 4.0）を加工して作成'

DDL = """
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE IF NOT EXISTS datasets (
  key text PRIMARY KEY, name text NOT NULL, source_url text NOT NULL, data_vintage text NOT NULL,
  loaded_at timestamptz NOT NULL, attribution text NOT NULL, note text);
CREATE TABLE IF NOT EXISTS naisui_depth (
  id bigserial PRIMARY KEY, dataset_key text NOT NULL REFERENCES datasets(key) ON DELETE CASCADE,
  area text NOT NULL, depth_m real, geom geometry(MultiPolygon, 6668) NOT NULL);
CREATE INDEX IF NOT EXISTS naisui_depth_geom_idx ON naisui_depth USING GIST (geom);
CREATE TABLE IF NOT EXISTS naisui_duration (
  id bigserial PRIMARY KEY, dataset_key text NOT NULL REFERENCES datasets(key) ON DELETE CASCADE,
  area text NOT NULL, minutes real, geom geometry(MultiPolygon, 6668) NOT NULL);
CREATE INDEX IF NOT EXISTS naisui_duration_geom_idx ON naisui_duration USING GIST (geom);
-- 内水データの収録範囲（自治体名で判定する。範囲外は「未収録」であって「浸水なし」ではない）
CREATE TABLE IF NOT EXISTS naisui_coverage (
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
    """zip 内のファイル名は CP932。展開先は zip 名のディレクトリ。"""
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
    print(f'  取り込み中: {layer}（{ds["vintage"]}）')
    t = time.time()
    env = dict(os.environ, SHAPE_ENCODING='CP932')
    cmd = ['ogr2ogr', '-f', 'PostgreSQL', 'PG:' + pg_dsn(), shp, '-nln', 'naisui_stage', '-overwrite',
           '-select', ds['field'], '-t_srs', f'EPSG:{SRID}', '-nlt', 'PROMOTE_TO_MULTI',
           '-lco', 'GEOMETRY_NAME=geom', '-lco', 'FID=id', '-gt', '20000', '--config', 'PG_USE_COPY', 'YES']
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f'ogr2ogr 失敗: {r.stderr[-800:]}')
    cols = [c[0] for c in psql("""SELECT column_name FROM information_schema.columns
                                  WHERE table_name='naisui_stage' AND column_name NOT IN ('id','geom')""")]
    if len(cols) != 1:
        raise RuntimeError(f'一時表の列が想定外: {cols}')
    val = cols[0]
    psql("""INSERT INTO datasets(key,name,source_url,data_vintage,loaded_at,attribution,note)
            VALUES(%s,%s,%s,%s,now(),%s,%s)
            ON CONFLICT (key) DO UPDATE SET source_url=EXCLUDED.source_url, data_vintage=EXCLUDED.data_vintage,
              loaded_at=EXCLUDED.loaded_at, attribution=EXCLUDED.attribution, note=EXCLUDED.note""",
         (key, ds['name'], ds['url'], ds['vintage'], ATTRIBUTION.format(name=ds['name'].replace('名古屋市 ', '')),
          f'カタログ: {ds["page"]}'), fetch=False)
    psql(f'DELETE FROM {ds["table"]} WHERE dataset_key=%s', (key,), fetch=False)
    n = psql(f"""INSERT INTO {ds["table"]}(dataset_key, area, {ds["col"]}, geom)
                 SELECT %s, %s, "{val}", ST_Multi(geom) FROM naisui_stage
                 WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom) RETURNING 1""", (key, ds['area']))
    n = len(n or [])
    psql('DROP TABLE IF EXISTS naisui_stage', fetch=False)
    psql("""INSERT INTO naisui_coverage(area, dataset_keys, loaded_at, geom)
            SELECT %s, ARRAY[%s], now(), ST_Envelope(ST_Extent(geom))::geometry(Polygon,6668) FROM {t} WHERE area=%s
            ON CONFLICT (area) DO UPDATE SET dataset_keys = array_append(array_remove(naisui_coverage.dataset_keys, %s), %s),
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
