#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国土数値情報 洪水浸水想定区域（1次メッシュ単位）A31 第4.0版 を PostGIS に取り込む。

配布の形（2026-09-08 実測）:
  https://nlftp.mlit.go.jp/ksj/gml/data/A31/A31-22/A31-22_{河川区分}_{1次メッシュ}_GEOJSON.zip
    河川区分 10 = 洪水予報河川・水位周知河川（河川管理者が図面を提供した河川）
    河川区分 20 = その他の河川（2021年の水防法改正で加わった中小河川）
  zip の中に4つの GeoJSON（先頭2桁がカテゴリ）:
    10_計画規模            属性 A31_101 = 浸水深ランク
    20_想定最大規模        属性 A31_201 = 浸水深ランク
    30_浸水継続時間        属性 A31_301 = 浸水継続時間ランク
    40_家屋倒壊等氾濫想定区域  属性 A31_401 = 危険区域区分
  座標系 JGD2011 (EPSG:6668)。zip 内のパス区切りは「\\」。
  GeoJSON は GDAL がストリーミングで読めるので ogr2ogr で取り込む
  （431MB のファイルを 16秒・常駐メモリ 120MB で取り込めた。土砂(A33)の GML と違い自前解析は不要）。

取り込むと同時に datasets 表へ「どこから・いつ時点のデータか」、meshes 表へ「どの1次メッシュを
取り込んだか」を記録する。判定 API はこの2つで「未収録」と「区域外」を区別する。

使い方:
  python3 scripts/load_a31.py 5236 5237        # 1次メッシュを指定
  python3 scripts/load_a31.py 23               # 都道府県コード（広めの矩形でメッシュを選ぶ）
  python3 scripts/load_a31.py all              # 全国（GeoJSON zip 約9.7GB・数十分）
  python3 scripts/load_a31.py all --force      # 取り込み済みも入れ直す
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
RAW = os.path.join(os.environ.get('KFLOOD_RAW_DIR', os.path.join(ROOT, 'data', 'raw')), 'a31')
BASE = 'https://nlftp.mlit.go.jp/ksj/gml/data/A31/A31-22'
LIST_PAGE = 'https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-A31-v4_0.html'
UA = {'User-Agent': 'kflood/1.0 (kurage.exbridge.jp; +https://exbridge.jp/)'}
SRID = 6668
VINTAGE = '2022年度（令和4年度）第4.0版'   # 配布ページ「データ作成年度」。zip内ファイルの日付は取り込み時に併記する

DB = dict(host='127.0.0.1', port=int(os.environ.get('KFLOOD_DB_PORT', '55434')), dbname='kflood',
          user='postgres', password=os.environ.get('KFLOOD_DB_PASS', 'kflood_local'))

# 仕様書とコードリスト（water_depth_code / flood_duration_code / hazardous_area_classification_code）で確認した意味。
# 誤ると「3m以上なのに0.5m未満と表示する」事故になるので定数で持ち、API 側も同じ表を使う。
CATEGORY = {10: '計画規模', 20: '想定最大規模', 30: '浸水継続時間', 40: '家屋倒壊等氾濫想定区域'}
RIVER = {10: '洪水予報河川・水位周知河川', 20: 'その他の河川'}
ATTR = {10: 'A31_101', 20: 'A31_201', 30: 'A31_301', 40: 'A31_401'}
DEPTH_RANK = {1: '0.5m未満', 2: '0.5m以上3.0m未満', 3: '3.0m以上5.0m未満', 4: '5.0m以上10.0m未満',
              5: '10.0m以上20.0m未満', 6: '20.0m以上'}
DURATION_RANK = {1: '12時間未満', 2: '12時間以上24時間未満（1日）', 3: '24時間以上72時間未満（3日）',
                 4: '72時間以上168時間未満（1週間）', 5: '168時間以上336時間未満（2週間）',
                 6: '336時間以上672時間未満（4週間）', 7: '672時間以上（4週間以上）'}
COLLAPSE = {1: '氾濫流', 2: '河岸侵食', 3: '氾濫流・河岸侵食の両方'}

# 都道府県コード → 広めの緯度経度矩形（1次メッシュを選ぶためだけに使う。配布の無いメッシュは飛ばすので広めで安全）
PREF_BBOX = {
    '01': (41.3, 45.6, 139.3, 146.0), '02': (40.2, 41.6, 139.4, 141.7), '03': (38.7, 40.5, 140.6, 142.1),
    '04': (37.7, 39.0, 140.2, 141.7), '05': (38.8, 40.5, 139.6, 141.0), '06': (37.7, 39.2, 139.5, 140.7),
    '07': (36.7, 38.0, 139.1, 141.1), '08': (35.7, 36.9, 139.7, 140.9), '09': (36.2, 37.2, 139.3, 140.3),
    '10': (35.9, 37.1, 138.4, 139.7), '11': (35.7, 36.3, 138.7, 139.9), '12': (34.9, 36.1, 139.7, 140.9),
    '13': (35.4, 35.95, 138.9, 139.95), '14': (35.1, 35.7, 138.9, 139.8), '15': (36.7, 38.6, 137.6, 139.9),
    '16': (36.2, 36.98, 136.7, 137.8), '17': (36.0, 37.9, 136.2, 137.4), '18': (35.3, 36.3, 135.4, 136.9),
    '19': (35.1, 35.98, 138.1, 139.2), '20': (35.1, 37.1, 137.3, 138.8), '21': (35.1, 36.5, 136.2, 137.7),
    '22': (34.5, 35.7, 137.4, 139.2), '23': (34.5, 35.5, 136.6, 137.9), '24': (33.7, 35.3, 135.8, 136.99),
    '25': (34.7, 35.8, 135.7, 136.5), '26': (34.7, 35.8, 134.8, 136.1), '27': (34.2, 35.1, 135.0, 135.8),
    '28': (34.1, 35.7, 134.2, 135.5), '29': (33.8, 34.8, 135.5, 136.2), '30': (33.4, 34.4, 134.9, 136.1),
    '31': (35.0, 35.7, 133.1, 134.6), '32': (34.2, 36.4, 131.6, 133.5), '33': (34.2, 35.4, 133.2, 134.5),
    '34': (34.0, 35.2, 132.0, 133.5), '35': (33.7, 34.9, 130.7, 132.3), '36': (33.5, 34.3, 133.6, 134.9),
    '37': (34.0, 34.6, 133.4, 134.5), '38': (32.8, 34.4, 132.0, 133.8), '39': (32.6, 33.9, 132.4, 134.4),
    '40': (33.0, 34.3, 129.9, 131.3), '41': (32.9, 33.7, 129.7, 130.6), '42': (32.4, 34.8, 128.5, 130.5),
    '43': (32.1, 33.3, 129.9, 131.4), '44': (32.6, 33.8, 130.7, 132.1), '45': (31.3, 32.9, 130.6, 131.9),
    '46': (27.0, 32.4, 128.3, 131.3), '47': (24.0, 27.0, 122.9, 131.4),
}

DDL = """
CREATE EXTENSION IF NOT EXISTS postgis;

-- どのデータを、どこから、いつ時点のものとして取り込んだか。
-- 判定結果に必ず添えるため、この表が空だと API は判定を返さない。
CREATE TABLE IF NOT EXISTS datasets (
  key           text PRIMARY KEY,
  name          text NOT NULL,
  source_url    text NOT NULL,
  data_vintage  text NOT NULL,
  loaded_at     timestamptz NOT NULL,
  attribution   text NOT NULL,   -- 出典表記（国土数値情報利用約款 / CC BY の要件）
  note          text
);

-- 洪水浸水想定区域（4カテゴリ×2河川区分を1つの表に）
CREATE TABLE IF NOT EXISTS flood (
  id            bigserial PRIMARY KEY,
  dataset_key   text NOT NULL REFERENCES datasets(key) ON DELETE CASCADE,
  mesh          text NOT NULL,        -- 1次メッシュ番号
  river_kind    smallint NOT NULL,    -- 10=洪水予報河川・水位周知河川 20=その他の河川
  category      smallint NOT NULL,    -- 10=計画規模 20=想定最大規模 30=浸水継続時間 40=家屋倒壊等氾濫想定区域
  rank          smallint,             -- 浸水深ランク / 浸水継続時間ランク / 危険区域区分
  geom          geometry(MultiPolygon, 6668) NOT NULL
);
CREATE INDEX IF NOT EXISTS flood_geom_idx ON flood USING GIST (geom);
CREATE INDEX IF NOT EXISTS flood_cat_idx ON flood (category, mesh);

-- 取り込み済みの1次メッシュ。ここに無い場所は「未収録」であって「区域外」ではない。
CREATE TABLE IF NOT EXISTS meshes (
  mesh          text PRIMARY KEY,
  loaded_at     timestamptz NOT NULL,
  geom          geometry(Polygon, 6668) NOT NULL
);
CREATE INDEX IF NOT EXISTS meshes_geom_idx ON meshes USING GIST (geom);
"""

ATTRIBUTION = ('出典: 国土数値情報（洪水浸水想定区域（1次メッシュ単位）データ 第4.0版）国土交通省 を加工して作成')


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


def mesh_bounds(mesh):
    """1次メッシュ番号 → (lat_min, lat_max, lon_min, lon_max)。緯度は 1/1.5 度、経度は 1 度刻み。"""
    p, u = int(mesh[:2]), int(mesh[2:])
    return p / 1.5, (p + 1) / 1.5, 100 + u, 101 + u


def meshes_in_bbox(lat0, lat1, lon0, lon1):
    out = set()
    for p in range(int(lat0 * 1.5), int(lat1 * 1.5) + 1):
        for u in range(int(lon0) - 100, int(lon1) - 100 + 1):
            out.add(f'{p:02d}{u:02d}')
    return out


def available_files():
    """配布ページから、実際に置かれている GEOJSON zip の一覧を取る（存在しないメッシュを決め打ちしない）。"""
    cache = os.path.join(RAW, 'list.txt')
    os.makedirs(RAW, exist_ok=True)
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 7 * 86400:
        names = [l.strip() for l in open(cache, encoding='utf-8') if l.strip()]
    else:
        html = urllib.request.urlopen(urllib.request.Request(LIST_PAGE, headers=UA), timeout=60).read().decode('utf-8', 'replace')
        names = sorted(set(re.findall(r'A31-22_[12]0_\d{4}_GEOJSON\.zip', html)))
        if not names:
            sys.exit('配布ページから zip の一覧を読めません（ページ構成が変わった可能性）')
        open(cache, 'w', encoding='utf-8').write('\n'.join(names) + '\n')
    out = []
    for n in names:
        m = re.match(r'A31-22_(\d\d)_(\d{4})_GEOJSON\.zip', n)
        out.append((int(m.group(1)), m.group(2), n))
    return out


def fetch(name):
    path = os.path.join(RAW, name)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    url = f'{BASE}/{name}'
    print(f'  取得中: {url}')
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=600) as r, open(path + '.part', 'wb') as f:
        while True:
            b = r.read(1 << 22)
            if not b:
                break
            f.write(b)
    os.replace(path + '.part', path)
    return path


def ogr2ogr(src, attr, table):
    """GeoJSON → 一時表（属性は1列だけ選ぶ）。定数列（メッシュ番号など）は後段の INSERT ... SELECT で付ける。
    レイヤ名は FeatureCollection の name（元のファイル名）になるので -sql でテーブル名を指定しない。"""
    cmd = ['ogr2ogr', '-f', 'PostgreSQL', 'PG:' + pg_dsn(), src, '-select', attr, '-nln', table, '-overwrite',
           '-nlt', 'PROMOTE_TO_MULTI', '-lco', 'GEOMETRY_NAME=geom', '-lco', 'FID=id', '-a_srs', f'EPSG:{SRID}',
           '-gt', '20000', '--config', 'PG_USE_COPY', 'YES']
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f'ogr2ogr 失敗: {r.stderr[-800:]}')


def load_zip(rk, mesh, name, force=False):
    key = f'A31-22_{rk}_{mesh}'
    if not force and psql('SELECT 1 FROM datasets WHERE key=%s', (key,)):
        print(f'  {key}: 取り込み済み（--force で入れ直し）')
        return 0
    path = fetch(name)
    work = os.path.join(RAW, 'work')
    os.makedirs(work, exist_ok=True)
    total = 0
    file_dates = set()
    with zipfile.ZipFile(path) as z:
        entries = [i for i in z.infolist() if i.filename.lower().endswith('.geojson')]
        if not entries:
            print(f'  {key}: GeoJSON が入っていません（飛ばします）')
            return 0
        psql("""INSERT INTO datasets(key,name,source_url,data_vintage,loaded_at,attribution,note)
                VALUES(%s,%s,%s,%s,now(),%s,%s)
                ON CONFLICT (key) DO UPDATE SET source_url=EXCLUDED.source_url, data_vintage=EXCLUDED.data_vintage,
                  loaded_at=EXCLUDED.loaded_at, attribution=EXCLUDED.attribution, note=EXCLUDED.note""",
             (key, f'国土数値情報 洪水浸水想定区域 A31 第4.0版 1次メッシュ{mesh}（{RIVER[rk]}）', f'{BASE}/{name}',
              VINTAGE, ATTRIBUTION, None), fetch=False)
        psql('DELETE FROM flood WHERE dataset_key=%s', (key,), fetch=False)
        for info in entries:
            cat = int(info.filename[:2])
            if cat not in CATEGORY:
                continue
            file_dates.add('%04d-%02d-%02d' % info.date_time[:3])
            out = os.path.join(work, f'A31-{cat}_{rk}_{mesh}.geojson')
            with z.open(info) as src, open(out, 'wb') as dst:
                while True:
                    b = src.read(1 << 24)
                    if not b:
                        break
                    dst.write(b)
            t = time.time()
            ogr2ogr(out, ATTR[cat], 'flood_stage')
            cols = [c[0] for c in psql("""SELECT column_name FROM information_schema.columns
                                          WHERE table_name='flood_stage' AND column_name NOT IN ('id','geom')""")]
            rank_col = next((c for c in cols if c.lower() == ATTR[cat].lower()), None)
            if not rank_col:
                raise RuntimeError(f'{ATTR[cat]} 列が見つかりません: {cols}')
            n = psql(f"""INSERT INTO flood(dataset_key, mesh, river_kind, category, rank, geom)
                        SELECT %s, %s, %s, %s, "{rank_col}"::smallint,
                               ST_Multi(CASE WHEN ST_IsValid(geom) THEN geom
                                             ELSE ST_CollectionExtract(ST_MakeValid(geom), 3) END)
                        FROM flood_stage WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
                        RETURNING 1""", (key, mesh, rk, cat), fetch=True)
            n = len(n or [])
            total += n
            os.remove(out)
            print(f'    {CATEGORY[cat]:<12} {n:>9,} 面 ({time.time() - t:.0f}s)')
    psql('DROP TABLE IF EXISTS flood_stage', fetch=False)
    lat0, lat1, lon0, lon1 = mesh_bounds(mesh)
    psql("""INSERT INTO meshes(mesh, loaded_at, geom) VALUES(%s, now(), ST_MakeEnvelope(%s,%s,%s,%s,6668))
            ON CONFLICT (mesh) DO UPDATE SET loaded_at=now()""", (mesh, lon0, lat0, lon1, lat1), fetch=False)
    psql('UPDATE datasets SET note=%s WHERE key=%s',
         (f'zip内ファイルの日付 {", ".join(sorted(file_dates))}', key), fetch=False)
    print(f'  {key}: 合計 {total:,} 面')
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('targets', nargs='+', help='1次メッシュ番号(4桁) / 都道府県コード(2桁) / all')
    ap.add_argument('--force', action='store_true', help='取り込み済みのメッシュも入れ直す')
    a = ap.parse_args()
    psql(DDL, fetch=False)
    files = available_files()
    want = None
    if 'all' not in a.targets:
        want = set()
        for t in a.targets:
            if re.fullmatch(r'\d{4}', t):
                want.add(t)
            elif t.zfill(2) in PREF_BBOX:
                want |= meshes_in_bbox(*PREF_BBOX[t.zfill(2)])
            else:
                sys.exit(f'指定を解釈できません: {t}')
    todo = [(rk, mesh, name) for rk, mesh, name in files if want is None or mesh in want]
    if not todo:
        sys.exit('対象の配布ファイルがありません（メッシュ番号・都道府県コードを確認してください）')
    print(f'対象 {len(todo)} ファイル / 配布 {len(files)} ファイル')
    total = 0
    t0 = time.time()
    for i, (rk, mesh, name) in enumerate(todo, 1):
        print(f'== [{i}/{len(todo)}] メッシュ {mesh} / {RIVER[rk]}')
        try:
            total += load_zip(rk, mesh, name, force=a.force)
        except Exception as e:  # noqa: BLE001
            print(f'  {name}: 失敗 {e}', file=sys.stderr)
    m = psql('SELECT count(*) FROM meshes')[0][0]
    n = psql('SELECT count(*) FROM flood')[0][0]
    print(f'\n今回 {total:,} 面 / 収録合計 {n:,} 面・{m} メッシュ（{(time.time() - t0) / 60:.0f}分）')


if __name__ == '__main__':
    main()
