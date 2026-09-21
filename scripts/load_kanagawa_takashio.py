#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""神奈川県の高潮浸水想定区域（東京湾沿岸・相模灘沿岸）を PostGIS に取り込む。

**神奈川県は国土数値情報 A49 に入っていない**（A49の配信は東京・千葉・兵庫・福岡の4都府県だけ）。
県がオープンデータカタログで Shapefile を CC BY で出しているので、そちらから取る。

  カタログ: https://catalog.opendata.pref.kanagawa.jp/dataset/9b3fec79-3679-46f8-a9ed-7895d44cfb9b
  「高潮浸水想定等のGISデータ」（クリエイティブ・コモンズ 表示）

**A49 より細かい。** 属性は区分の文字列ではなく**実測のメートル値**（field_2）で、
格子は約5m。東京湾だけで 413万ポリゴンある。**浸水継続時間も入っている**（A49 には無い）。

県の注意書き「当該データの形状は概略であるため、正式な区域の形状については、県のHP等で
公開している高潮浸水想定区域図を参照するようにしてください」を画面の出典に添える。

  python3 scripts/load_kanagawa_takashio.py              # 浸水深＋継続時間（東京湾・相模灘）
  python3 scripts/load_kanagawa_takashio.py --only depth # 浸水深だけ
  python3 scripts/load_kanagawa_takashio.py --force      # 入れ直し
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
RAW = os.path.join(os.environ.get('KFLOOD_RAW_DIR', os.path.join(ROOT, 'data', 'raw')), 'kanagawa')
UA = {'User-Agent': 'kflood/1.0 (kurage.exbridge.jp; +https://exbridge.jp/)'}
SRID = 6668
AREA = '神奈川県'
DB = dict(host='127.0.0.1', port=int(os.environ.get('KFLOOD_DB_PORT', '55434')), dbname='kflood',
          user='postgres', password=os.environ.get('KFLOOD_DB_PASS', 'kflood_local'))
CATALOG = 'https://catalog.opendata.pref.kanagawa.jp/dataset/9b3fec79-3679-46f8-a9ed-7895d44cfb9b'
BASE = CATALOG + '/resource/{res}/download/.zip'
ATTRIBUTION = ('出典: 神奈川県「高潮浸水想定等のGISデータ」（CC BY）を加工して作成。'
               '県の注意書き: データの形状は概略。正式な区域の形状は県が公開する高潮浸水想定区域図を参照')
VINTAGE_TOKYO = '令和6年2月公表・東京湾沿岸の高潮浸水想定区域（想定最大規模）'
VINTAGE_SAGAMI = '令和3年5月公表・相模灘沿岸の高潮浸水想定区域（想定最大規模）'

DATASETS = {
    'kanagawa_takashio_depth_tokyowan': dict(
        res='232d6c25-60c9-4343-ba0b-d024b53a595f', kind='depth', table='takashio_depth', col='depth_m',
        name='神奈川県 高潮浸水想定区域図 浸水深（東京湾沿岸）', vintage=VINTAGE_TOKYO),
    'kanagawa_takashio_depth_sagami': dict(
        res='01ba8718-51b9-4175-aab5-fc1cdfa7a37c', kind='depth', table='takashio_depth', col='depth_m',
        name='神奈川県 高潮浸水想定区域図 浸水深（相模灘沿岸）', vintage=VINTAGE_SAGAMI),
    'kanagawa_takashio_dur_tokyowan': dict(
        res='005cc54c-76ad-4c58-8849-1c8eab7a0616', kind='duration', table='takashio_duration', col='hours',
        name='神奈川県 高潮浸水想定区域図 浸水継続時間（東京湾沿岸）', vintage=VINTAGE_TOKYO),
    'kanagawa_takashio_dur_sagami': dict(
        res='0fddc356-5055-4b99-899d-76b230ae8a85', kind='duration', table='takashio_duration', col='hours',
        name='神奈川県 高潮浸水想定区域図 浸水継続時間（相模灘沿岸）', vintage=VINTAGE_SAGAMI),
}

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


def fetch(key, ds):
    os.makedirs(RAW, exist_ok=True)
    path = os.path.join(RAW, key + '.zip')
    if os.path.exists(path) and os.path.getsize(path) > 100000:
        return path
    url = BASE.format(res=ds['res'])
    print(f'  取得中: {ds["name"]}')
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=3600) as r, open(path + '.part', 'wb') as f:
        while True:
            b = r.read(1 << 22)
            if not b:
                break
            f.write(b)
    os.replace(path + '.part', path)
    return path


def extract_shp(zpath):
    """ZIP 内のファイル名は日本語（cp437 で入っている）。Shapefile 一式だけ出す。"""
    d = zpath[:-4]
    if not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
        with zipfile.ZipFile(zpath) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                try:
                    fn = info.filename.encode('cp437').decode('cp932')
                except (UnicodeEncodeError, UnicodeDecodeError):
                    fn = info.filename
                ext = os.path.splitext(fn)[1].lower()
                if ext not in ('.shp', '.shx', '.dbf', '.prj', '.cpg'):
                    continue          # .grd（320MB）は使わない
                with z.open(info) as src, open(os.path.join(d, os.path.basename(fn)), 'wb') as dst:
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



def pick_value_column(shp):
    """Shapefile の属性から、値（浸水深・継続時間）の数値列を1つ選ぶ。

    文字列の列（相模灘の layer）は持ち込まない。Shift_JIS のまま PostgreSQL に
    書こうとして落ちるうえ、判定には使わない。
    """
    r = subprocess.run(['ogrinfo', '-so', '-al', shp], capture_output=True)
    text = (r.stdout or b'').decode('utf-8', 'replace')
    body = text[text.find('Layer SRS WKT'):] if 'Layer SRS WKT' in text else text
    nums = [m.group(1) for m in re.finditer(r'^(\w+): (Real|Integer|Integer64)', body, re.M)]
    alls = [m.group(1) for m in re.finditer(r'^(\w+): (Real|Integer|Integer64|String|Date)', body, re.M)]
    if len(nums) != 1:
        raise RuntimeError(f'数値の属性を1つに決められない: {nums}')
    return nums[0], len(alls)


def load(key, ds, force=False):
    if not force and psql('SELECT 1 FROM datasets WHERE key=%s', (key,)):
        print(f'  {key}: 取り込み済み（--force で入れ直し）')
        return 0
    shp = extract_shp(fetch(key, ds))
    print(f'  取り込み中: {os.path.basename(shp)}')
    t = time.time()
    # **文字コードは .cpg が持っている**（このデータは Shift_JIS）。SHAPE_ENCODING を
    # 決め打ちすると属性が読めずに落ちる。ogr2ogr に .cpg を見させる。
    env = dict(os.environ)
    env.pop('SHAPE_ENCODING', None)
    # **属性の並びがファイルごとに違う。** 東京湾は field_2（Real）1列だが、相模灘は
    # layer（String・Shift_JIS）と value（Real）の2列で、文字列列をそのまま PostgreSQL へ
    # 書こうとすると Non UTF-8 content で落ちる。**数値の列だけを選ぶ。**
    val_col, n_attrs = pick_value_column(shp)
    print(f'    値の列: {val_col}（属性{n_attrs}列）')
    cmd = ['ogr2ogr', '-f', 'PostgreSQL', 'PG:' + pg_dsn(), shp, '-nln', 'kntaka_stage', '-overwrite',
           '-t_srs', f'EPSG:{SRID}', '-nlt', 'PROMOTE_TO_MULTI',
           '-lco', 'GEOMETRY_NAME=geom', '-lco', 'FID=id', '-gt', '50000',
           '--config', 'PG_USE_COPY', 'YES']
    if n_attrs > 1:
        cmd[6:6] = ['-select', val_col]
    # stderr は UTF-8 とは限らない（Windows 由来のデータでは cp932 が混ざる）。bytes で受けて自分で読む。
    r = subprocess.run(cmd, capture_output=True, env=env)
    if r.returncode != 0:
        err = r.stderr.decode('utf-8', 'replace') if r.stderr else ''
        raise RuntimeError(f'ogr2ogr 失敗: {err[-800:]}')

    cols = [c for (c, _t) in psql("""SELECT column_name, data_type FROM information_schema.columns
                                     WHERE table_name='kntaka_stage' AND column_name NOT IN ('id','geom')""")]
    if len(cols) != 1:
        psql('DROP TABLE IF EXISTS kntaka_stage', fetch=False)
        raise RuntimeError(f'一時表の列が想定外: {cols}（-select が効いていない）')
    val = cols[0]
    rng = psql(f'SELECT min("{val}"), max("{val}") FROM kntaka_stage')[0]
    print(f'    値の範囲: {rng[0]} 〜 {rng[1]}')

    psql("""INSERT INTO datasets(key,name,source_url,data_vintage,loaded_at,attribution,note)
            VALUES(%s,%s,%s,%s,now(),%s,%s)
            ON CONFLICT (key) DO UPDATE SET source_url=EXCLUDED.source_url, data_vintage=EXCLUDED.data_vintage,
              loaded_at=EXCLUDED.loaded_at, attribution=EXCLUDED.attribution, note=EXCLUDED.note""",
         (key, ds['name'], CATALOG, ds['vintage'], ATTRIBUTION,
          '格子は約5m。区分ではなく実測のメートル値（継続時間は時間）'), fetch=False)
    psql(f'DELETE FROM {ds["table"]} WHERE dataset_key=%s', (key,), fetch=False)
    n = psql(f'''INSERT INTO {ds["table"]}(dataset_key, area, {ds["col"]}, geom)
                 SELECT %s, %s, "{val}", ST_Multi(geom) FROM kntaka_stage
                  WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom) AND "{val}" IS NOT NULL
                 RETURNING 1''', (key, AREA))
    n = len(n or [])
    psql('DROP TABLE IF EXISTS kntaka_stage', fetch=False)
    print(f'  {key}: {n:,} 面（{time.time() - t:.0f}s）')
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', choices=['depth', 'duration'])
    ap.add_argument('--force', action='store_true')
    a = ap.parse_args()
    psql(DDL, fetch=False)
    total = 0
    for key, ds in DATASETS.items():
        if a.only and ds['kind'] != a.only:
            continue
        print(f'== {ds["name"]}')
        try:
            total += load(key, ds, force=a.force)
        except Exception as e:  # noqa: BLE001
            print(f'  失敗: {e}', file=sys.stderr)

    keys = [k for k, ds in DATASETS.items()]
    psql("""INSERT INTO takashio_coverage(area, dataset_keys, loaded_at, geom)
            SELECT %s, %s, now(), ST_Envelope(ST_Extent(geom))::geometry(Polygon,6668)
              FROM takashio_depth WHERE area=%s
            ON CONFLICT (area) DO UPDATE SET dataset_keys=EXCLUDED.dataset_keys,
              loaded_at=now(), geom=EXCLUDED.geom""", (AREA, keys, AREA), fetch=False)
    print(f'\n合計 {total:,} 面')


if __name__ == '__main__':
    main()
