#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""名古屋市の洪水ハザードマップ（令和8年度版）を PostGIS に取り込む。

国の洪水データ（国土数値情報 A31/A31b）は、県が新しく指定した中小河川の区域が遅れて入る。
名古屋市は「令和7年3月までに国土交通省または愛知県が公表した各河川の洪水浸水想定区域図を重ね合わせ、
重なる場合は浸水深は深い方・継続時間は長い方」を採った図を、Shapefile・CC BY 4.0 で公開している
（名古屋市オープンデータカタログ＝BODIK、2026-06-01 掲載。2026-09-29 に防災企画課の回答で確認）。

  浸水深        flood_hazard_map-inundation_assumption2.zip  2ファイル 計15,831,785面・5mセル
                属性名は max_dur だが中身は浸水深(m)（0.0001〜16.3）。**属性名を信じない**
  浸水継続時間  flood_hazard_map-inundation_duration2.zip    974,880面・25mセル・max_dur(分)
                単位は資料に無い。国の継続時間ランクと突き合わせて分と確定
                （国「12時間未満」の地点の中央値 480、「1週間」の地点の中央値 8,040）
  家屋倒壊等    flood_hazard_map...house_collapse2.zip        1面。氾濫流・河岸侵食の区別は無い
  座標系 EPSG:2449（JGD2000 平面直角座標系 第VII系）。倒壊だけ経緯度

データの範囲は市域より広い（春日井・東海の一部まで入る）が、市は自分の図のために作ったので、
**市域（学区ポリゴンの和）の外は取り込まない。** 市外で「区域外」と誤って言わないため。

使い方:
  python3 scripts/load_nagoya_flood.py            # 3つとも
  python3 scripts/load_nagoya_flood.py --force    # 入れ直し
"""
import argparse
import glob
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import load_nagoya_naisui as _nai  # noqa: E402
from load_nagoya_naisui import SRID, extract_shp, pg_dsn, psql  # noqa: E402

RAW = os.path.join(os.environ.get('KFLOOD_RAW_DIR', os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'data', 'raw')), 'nagoya_flood')
AREA = '名古屋市'
PAGE = 'https://data.bodik.jp/dataset/231002_0200030000_35'
BASE = 'https://data.bodik.jp/dataset/ccd176b4-c72e-488f-8c2b-c9b8458cb3be/resource/'
VINTAGE = '令和8年度版（2026-06-01 掲載。令和7年3月までに国・愛知県が公表した区域図を重ね合わせ）'
DATASETS = {
    'nagoya_flood_depth': dict(
        name='名古屋市 洪水ハザードマップ（浸水想定）',
        url=BASE + '8d06b825-3396-468e-9c17-c851ec3d9cc2/download/flood_hazard_map-inundation_assumption2.zip',
        field='max_dur', col='depth_m', table='muni_flood_depth'),
    'nagoya_flood_duration': dict(
        name='名古屋市 洪水ハザードマップ（浸水継続時間）',
        url=BASE + 'fbf509c2-277a-4316-8309-070e1e585089/download/flood_hazard_map-inundation_duration2.zip',
        field='max_dur', col='minutes', table='muni_flood_duration'),
    'nagoya_flood_collapse': dict(
        name='名古屋市 洪水ハザードマップ（家屋倒壊等氾濫想定区域）',
        url=BASE + 'ae4ffeda-6a82-4210-9f9f-6f0c2e5d7e5d/download/flood_hazard_mapinundation_assumed_area_such_as_house_collapse2.zip',
        field=None, col=None, table='muni_flood_collapse'),
}
ATTRIBUTION = '出典: 名古屋市「{name}」（名古屋市オープンデータカタログ、CC BY 4.0）を加工して作成'

DDL = """
CREATE TABLE IF NOT EXISTS muni_flood_depth (
  id bigserial PRIMARY KEY, dataset_key text NOT NULL REFERENCES datasets(key) ON DELETE CASCADE,
  area text NOT NULL, depth_m real, geom geometry(MultiPolygon, 6668) NOT NULL);
CREATE INDEX IF NOT EXISTS muni_flood_depth_geom_idx ON muni_flood_depth USING GIST (geom);
CREATE TABLE IF NOT EXISTS muni_flood_duration (
  id bigserial PRIMARY KEY, dataset_key text NOT NULL REFERENCES datasets(key) ON DELETE CASCADE,
  area text NOT NULL, minutes real, geom geometry(MultiPolygon, 6668) NOT NULL);
CREATE INDEX IF NOT EXISTS muni_flood_duration_geom_idx ON muni_flood_duration USING GIST (geom);
CREATE TABLE IF NOT EXISTS muni_flood_collapse (
  id bigserial PRIMARY KEY, dataset_key text NOT NULL REFERENCES datasets(key) ON DELETE CASCADE,
  area text NOT NULL, geom geometry(MultiPolygon, 6668) NOT NULL);
CREATE INDEX IF NOT EXISTS muni_flood_collapse_geom_idx ON muni_flood_collapse USING GIST (geom);
-- 自治体版の洪水データの収録範囲（市域そのもの。外接矩形ではない）
CREATE TABLE IF NOT EXISTS muni_flood_coverage (
  area text PRIMARY KEY, dataset_keys text[] NOT NULL, loaded_at timestamptz NOT NULL,
  geom geometry(MultiPolygon, 6668) NOT NULL);
CREATE INDEX IF NOT EXISTS muni_flood_coverage_geom_idx ON muni_flood_coverage USING GIST (geom);
"""


def fetch(url, name):
    _nai.RAW = RAW   # 置き場所だけ洪水用に変える（取得の処理は内水と同じ）
    return _nai.fetch(url, name)


def city_boundary():
    """市域 = 学区ポリゴンの和。判定と取り込みの両方で使う。細かく割って索引を効かせる。"""
    psql("""DROP TABLE IF EXISTS muni_flood_clip;
            CREATE TABLE muni_flood_clip AS
              SELECT ST_Subdivide(ST_Union(geom), 256) AS geom FROM gakku WHERE area=%s;
            CREATE INDEX ON muni_flood_clip USING GIST (geom);""", (AREA,), fetch=False)
    psql("""INSERT INTO muni_flood_coverage(area, dataset_keys, loaded_at, geom)
            SELECT %s, ARRAY[]::text[], now(), ST_Multi(ST_Union(geom)) FROM gakku WHERE area=%s
            ON CONFLICT (area) DO UPDATE SET geom=EXCLUDED.geom""", (AREA, AREA), fetch=False)


def load(key, ds, force=False):
    if not force and psql('SELECT 1 FROM datasets WHERE key=%s', (key,)):
        print(f'  {key}: 取り込み済み（--force で入れ直し）')
        return 0
    zpath = fetch(ds['url'], os.path.basename(ds['url']))
    first = extract_shp(zpath)
    shps = sorted(glob.glob(os.path.join(os.path.dirname(first), '*.shp')))   # 浸水深は2ファイルに分かれている
    t = time.time()
    psql('DROP TABLE IF EXISTS muni_flood_stage', fetch=False)
    for i, shp in enumerate(shps):
        print(f'  取り込み中: {os.path.basename(shp)}')
        cmd = ['ogr2ogr', '-f', 'PostgreSQL', 'PG:' + pg_dsn(), shp, '-nln', 'muni_flood_stage',
               '-t_srs', f'EPSG:{SRID}', '-nlt', 'PROMOTE_TO_MULTI',
               '-lco', 'GEOMETRY_NAME=geom', '-lco', 'FID=id', '-lco', 'SPATIAL_INDEX=NONE',
               '-gt', '50000', '--config', 'PG_USE_COPY', 'YES']
        if ds['field'] and not i:   # -append と -select は併用できない。2つ目以降は同名の列に入る
            cmd += ['-select', ds['field']]
        cmd += ['-append'] if i else ['-overwrite']
        r = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ, SHAPE_ENCODING='CP932'))
        if r.returncode != 0:
            raise RuntimeError(f'ogr2ogr 失敗: {r.stderr[-800:]}')
    psql('CREATE INDEX ON muni_flood_stage USING GIST (geom)', fetch=False)
    psql("""INSERT INTO datasets(key,name,source_url,data_vintage,loaded_at,attribution,note)
            VALUES(%s,%s,%s,%s,now(),%s,%s)
            ON CONFLICT (key) DO UPDATE SET source_url=EXCLUDED.source_url, data_vintage=EXCLUDED.data_vintage,
              loaded_at=EXCLUDED.loaded_at, attribution=EXCLUDED.attribution, note=EXCLUDED.note""",
         (key, ds['name'], ds['url'], VINTAGE, ATTRIBUTION.format(name=ds['name'].replace('名古屋市 ', '')),
          f'カタログ: {PAGE}'), fetch=False)
    psql(f'DELETE FROM {ds["table"]} WHERE dataset_key=%s', (key,), fetch=False)
    val = f', "{ds["field"]}"' if ds['field'] else ''
    col = f', {ds["col"]}' if ds['col'] else ''
    # 市域に掛かるセルだけ入れる（市外は市が自分の図のために作った範囲外の断片）
    n = psql(f"""INSERT INTO {ds["table"]}(dataset_key, area{col}, geom)
                 SELECT %s, %s{val}, ST_Multi(s.geom) FROM muni_flood_stage s
                 WHERE s.geom IS NOT NULL AND NOT ST_IsEmpty(s.geom)
                   AND EXISTS (SELECT 1 FROM muni_flood_clip c WHERE ST_Intersects(c.geom, s.geom))
                 RETURNING 1""", (key, AREA))
    n = len(n or [])
    psql('DROP TABLE IF EXISTS muni_flood_stage', fetch=False)
    psql("""UPDATE muni_flood_coverage SET dataset_keys = array_append(array_remove(dataset_keys, %s), %s),
              loaded_at=now() WHERE area=%s""", (key, key, AREA), fetch=False)
    print(f'  {key}: {n:,} 面（{time.time() - t:.0f}s）')
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--only', choices=list(DATASETS))
    a = ap.parse_args()
    psql(DDL, fetch=False)
    city_boundary()
    total = 0
    for key, ds in DATASETS.items():
        if a.only and key != a.only:
            continue
        print(f'== {ds["name"]}')
        try:
            total += load(key, ds, force=a.force)
        except Exception as e:  # noqa: BLE001
            print(f'  {key}: 失敗 {e}', file=sys.stderr)
    psql('DROP TABLE IF EXISTS muni_flood_clip', fetch=False)
    psql('ANALYZE muni_flood_depth; ANALYZE muni_flood_duration; ANALYZE muni_flood_collapse', fetch=False)
    print(f'\n合計 {total:,} 面')


if __name__ == '__main__':
    main()
