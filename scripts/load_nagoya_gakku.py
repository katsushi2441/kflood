#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""名古屋市の学区線（なごや防災オープンデータカタログ・CC BY）を PostGIS に取り込む。

名古屋市の避難情報（警戒レベル）は「河川 × 対象学区」で発令されるので、住所→学区が引ければ
「いま、この住所の学区に何が出ているか」を判定に添えられる（app/alerts.py）。
  268 学区・属性 学区コード / 学区名称 / ★区名。.prj が無いので EPSG:2449（平面直角VII系）を明示（2026-09-08 実測）

使い方:
  python3 scripts/load_nagoya_gakku.py            # 取り込み
  python3 scripts/load_nagoya_gakku.py --force    # 入れ直し
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
KEY = 'nagoya_gakku'
DS = dict(name='名古屋市 学区線（令和6年11月時点）',
          url='https://nui-ngy-bosai.jp/dataset/b6bfb4cc-06f5-4f1e-aa6d-2ca4e90b05dc/resource/b2c5c514-b720-4cca-ac2c-0bffc670f0c4/download/school_district_line_november_2024.zip',
          page='https://nui-ngy-bosai.jp/dataset/schooldistrictline-20220713',
          vintage='令和6年11月時点（2024-11）', area='名古屋市')
ATTRIBUTION = '出典: 名古屋市「学区線（令和6年11月時点）」（なごや防災オープンデータカタログ、CC BY 4.0）を加工して作成'

DDL = """
CREATE TABLE IF NOT EXISTS datasets (
  key text PRIMARY KEY, name text NOT NULL, source_url text NOT NULL, data_vintage text NOT NULL,
  loaded_at timestamptz NOT NULL, attribution text NOT NULL, note text);
CREATE TABLE IF NOT EXISTS gakku (
  id bigserial PRIMARY KEY, dataset_key text NOT NULL REFERENCES datasets(key) ON DELETE CASCADE,
  area text NOT NULL, ward text NOT NULL, code integer, name text NOT NULL, geom geometry(MultiPolygon, 6668) NOT NULL);
CREATE INDEX IF NOT EXISTS gakku_geom_idx ON gakku USING GIST (geom);
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
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=300) as r, open(path + '.part', 'wb') as f:
        f.write(r.read())
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
                    dst.write(src.read())
    for root, _, files in os.walk(d):
        for fn in files:
            if fn.lower().endswith('.shp'):
                return os.path.join(root, fn)
    sys.exit(f'Shapefile が見つかりません: {d}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true')
    a = ap.parse_args()
    psql(DDL, fetch=False)
    if not a.force and psql('SELECT 1 FROM datasets WHERE key=%s', (KEY,)):
        print(f'  {KEY}: 取り込み済み（--force で入れ直し）')
        return
    shp = extract_shp(fetch(DS['url'], os.path.basename(DS['url'])))
    t = time.time()
    env = dict(os.environ, SHAPE_ENCODING='CP932')
    cmd = ['ogr2ogr', '-f', 'PostgreSQL', 'PG:' + pg_dsn(), shp, '-nln', 'gakku_stage', '-overwrite', '-s_srs', 'EPSG:2449', '-t_srs', f'EPSG:{SRID}',
           '-nlt', 'PROMOTE_TO_MULTI', '-lco', 'GEOMETRY_NAME=geom', '-lco', 'FID=id']
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f'ogr2ogr 失敗: {r.stderr[-800:]}')
    cols = [c[0] for c in psql("SELECT column_name FROM information_schema.columns WHERE table_name='gakku_stage'")]
    name_col = next((c for c in cols if '学区名' in c), None)
    ward_col = next((c for c in cols if '区名' in c and '学区' not in c), None)
    code_col = next((c for c in cols if '学区コード' in c or c.lower() in ('code', 'gakkucode')), None)
    if not (name_col and ward_col):
        raise RuntimeError(f'列が見つかりません: {cols}')
    psql("""INSERT INTO datasets(key,name,source_url,data_vintage,loaded_at,attribution,note) VALUES(%s,%s,%s,%s,now(),%s,%s)
            ON CONFLICT (key) DO UPDATE SET source_url=EXCLUDED.source_url, data_vintage=EXCLUDED.data_vintage, loaded_at=EXCLUDED.loaded_at,
              attribution=EXCLUDED.attribution, note=EXCLUDED.note""",
         (KEY, DS['name'], DS['url'], DS['vintage'], ATTRIBUTION, f'カタログ: {DS["page"]}'), fetch=False)
    psql('DELETE FROM gakku WHERE dataset_key=%s', (KEY,), fetch=False)
    code_expr = f'"{code_col}"::integer' if code_col else 'NULL'
    n = psql(f"""INSERT INTO gakku(dataset_key, area, ward, code, name, geom)
                 SELECT %s, %s, trim("{ward_col}"), {code_expr}, trim("{name_col}"), ST_Multi(geom) FROM gakku_stage
                 WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom) RETURNING 1""", (KEY, DS['area']))
    psql('DROP TABLE IF EXISTS gakku_stage', fetch=False)
    print(f'  {KEY}: {len(n or [])} 学区（{time.time() - t:.0f}s）列 {ward_col}/{name_col}/{code_col}')
    print('  例:', psql('SELECT ward, name FROM gakku ORDER BY code LIMIT 5'))


if __name__ == '__main__':
    main()
