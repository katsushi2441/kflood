#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""愛知県の「津波災害警戒区域」を PostGIS に取り込む。

**なぜ要るか**: 宅地建物取引業法施行規則 第16条の4の3 第3号は、津波災害警戒区域内に
あるときはその旨を説明することを求めている。ところが **国土数値情報にこのデータは無い**
（137データセットを 2026-09-18 に機械的に確認。A40 の「津波浸水想定」は別物）。
都道府県が個別に指定・公表しているので、県ごとに取り込むしかない。

**何を取り込むか**: マップあいちの「津波災害情報マップ」（市区町村別 Shapefile）。
中身は **基準水位** のポリゴンで、これが警戒区域そのものを表す。根拠は
津波防災地域づくりに関する法律 第53条第2項（2026-09-18 に e-Gov の法令APIで原文確認）:

  「前項の規定による指定は、当該指定の**区域及び基準水位**……を明らかにしてするものとする。」

つまり基準水位が定められている範囲＝指定された警戒区域である。

**ライセンス**: 愛知県オープンデータカタログの利用規約に基づき
**クリエイティブ・コモンズ表示 2.1 日本（CC BY 2.1 JP）**。
「改変や営利目的での二次利用も許可される」と県のページに明記（2026-09-18 確認）。
出典表示が条件なので、判定画面に必ず出典を出すこと。

**注意**: これは県の公示による指定を機械判読用にしたものなので、最終的な確認は
県の公示図書で行う。画面にもそう書く。

使い方:
  python3 scripts/load_aichi_tsunami_keikai.py            # 未取得のものだけ
  python3 scripts/load_aichi_tsunami_keikai.py --force    # 入れ直し
  python3 scripts/load_aichi_tsunami_keikai.py --only 名古屋市港区
"""
import argparse
import os
import subprocess
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
RAW = os.path.join(os.environ.get('KFLOOD_RAW_DIR', os.path.join(ROOT, 'data', 'raw')), 'aichi_tsunami')
UA = {'User-Agent': 'kflood/1.0 (kurage.exbridge.jp; +https://exbridge.jp/)'}
SRID = 6668
BASE = 'https://maps.pref.aichi.jp/map/files/opendata/{code}/{code}_shp.zip'
PAGE = 'https://maps.pref.aichi.jp/opendata.html'
VINTAGE = '2019-07-29'          # 配布ファイルの日付。指定の公示は令和元年7月30日
# **マップあいちの配布は10万面で打ち切られる。** 港区はちょうど100,000面・ちょうど10.00km2
# （10mメッシュ）で、港区の面積45.64km2 に足りない。瑞穂区9,883面・大府市547面は打ち切り無し。
# 打ち切られた市区町村では「該当」は確定だが「非該当」とは言えない（2026-09-18 実測）。
TRUNCATE_AT = 100000
ATTRIB = ('出典: 愛知県「マップあいち」津波災害情報マップ（CC BY 2.1 JP）を加工。'
          '指定は愛知県知事による（令和元年7月30日公示）')

# マップあいちの配布コード。**「図郭」(c205044) は区域ではないので入れない。**
CITIES = {
    '名古屋市中村区': 'c205026', '名古屋市熱田区': 'c205028', '名古屋市中川区': 'c205025',
    '名古屋市港区': 'c205023', '名古屋市南区': 'c205027', '名古屋市緑区': 'c205029',
    '名古屋市瑞穂区': 'c205024',
    '豊橋市': 'c205021', '豊川市': 'c205022', '蒲郡市': 'c205004', '田原市': 'c205012',
    '西尾市': 'c205008', '碧南市': 'c205020', '刈谷市': 'c205005', '安城市': 'c205002',
    '高浜市': 'c205006',
    '半田市': 'c205016', '常滑市': 'c205007', '東海市': 'c205014', '大府市': 'c205009',
    '知多市': 'c205010', '阿久比町': 'c205000', '東浦町': 'c205013', '南知多町': 'c205015',
    '美浜町': 'c205018', '武豊町': 'c205019',
    '津島市': 'c205011', '愛西市': 'c205001', '弥富市': 'c205030', 'あま市': 'c204999',
    '蟹江町': 'c205003', '飛島村': 'c205017',
}

DDL = """
CREATE TABLE IF NOT EXISTS tsunami_keikai (
  id bigserial PRIMARY KEY,
  pref text NOT NULL,
  city text NOT NULL,
  base_level double precision,          -- 基準水位(m)
  geom geometry(MultiPolygon, 6668) NOT NULL
);
CREATE INDEX IF NOT EXISTS tsunami_keikai_gix ON tsunami_keikai USING GIST (geom);
CREATE INDEX IF NOT EXISTS tsunami_keikai_city ON tsunami_keikai (city);
-- 収録した市区町村。**「収録していない」と「区域外」を必ず区別するため**に持つ。
CREATE TABLE IF NOT EXISTS tsunami_keikai_coverage (
  city text PRIMARY KEY, pref text NOT NULL, n bigint NOT NULL,
  truncated boolean NOT NULL DEFAULT false,   -- 配布が10万面で打ち切られているか
  data_vintage text, source_url text, attribution text, loaded_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE tsunami_keikai_coverage ADD COLUMN IF NOT EXISTS truncated boolean NOT NULL DEFAULT false;
"""


def pg_dsn():
    return (f"host=127.0.0.1 port={os.environ.get('KFLOOD_DB_PORT', '55434')} dbname=kflood "
            f"user=postgres password={os.environ.get('KFLOOD_DB_PASS', 'kflood_local')}")


def psql(sql, args=None, fetch=True):
    import psycopg2
    with psycopg2.connect(pg_dsn().replace(' ', ' ')) as c, c.cursor() as cur:
        cur.execute(sql, args or ())
        return cur.fetchall() if fetch and cur.description else None


def fetch(code):
    os.makedirs(RAW, exist_ok=True)
    dst = os.path.join(RAW, f'{code}.zip')
    if os.path.exists(dst) and os.path.getsize(dst) > 10000:
        return dst
    url = BASE.format(code=code)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=180) as r, open(dst, 'wb') as f:
        f.write(r.read())
    return dst


def shp_of(zpath, code):
    d = os.path.join(RAW, code)
    if not os.path.isdir(d):
        with zipfile.ZipFile(zpath) as z:
            z.extractall(d)
    for base, _dirs, files in os.walk(d):
        for fn in files:
            if fn.lower().endswith('.shp'):
                return os.path.join(base, fn)
    raise RuntimeError(f'shp が見つからない: {code}')


def load(city, code, force=False):
    got = psql('SELECT n FROM tsunami_keikai_coverage WHERE city=%s', (city,))
    if got and not force:
        print(f'  済 {city}（{got[0][0]:,}面）')
        return 0
    shp = shp_of(fetch(code), code)
    env = dict(os.environ, SHAPE_ENCODING='CP932')   # DBF は Shift-JIS
    cmd = ['ogr2ogr', '-f', 'PostgreSQL', 'PG:' + pg_dsn(), shp, '-nln', 'tk_stage', '-overwrite',
           '-t_srs', f'EPSG:{SRID}', '-nlt', 'PROMOTE_TO_MULTI',
           '-lco', 'GEOMETRY_NAME=geom', '-lco', 'FID=id', '-gt', '20000',
           '--config', 'PG_USE_COPY', 'YES']
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f'ogr2ogr 失敗 {city}: {r.stderr[-700:]}')
    cols = [c[0] for c in psql("""SELECT column_name FROM information_schema.columns
                                  WHERE table_name='tk_stage' AND column_name NOT IN ('id','geom')""")]
    # 基準水位の列を選ぶ。列名は環境によって化けることがあるので、数値列を優先して拾う。
    lvl = next((c for c in cols if '基準' in c or 'suii' in c.lower()), None)
    if lvl is None:
        nums = [c[0] for c in psql("""SELECT column_name FROM information_schema.columns
                                      WHERE table_name='tk_stage'
                                        AND data_type IN ('double precision','numeric','real','integer')
                                        AND column_name <> 'id'""")]
        lvl = nums[0] if nums else None
    sel = f'NULLIF(("{lvl}")::text, \'\')::double precision' if lvl else 'NULL::double precision'
    psql('DELETE FROM tsunami_keikai WHERE city=%s', (city,), fetch=False)
    n = psql(f"""INSERT INTO tsunami_keikai(pref, city, base_level, geom)
                 SELECT '愛知県', %s, {sel}, ST_Multi(ST_MakeValid(geom))
                 FROM tk_stage WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
                 RETURNING 1""", (city,))
    cnt = len(n or [])
    trunc = cnt >= TRUNCATE_AT
    psql("""INSERT INTO tsunami_keikai_coverage(city, pref, n, truncated, data_vintage, source_url, attribution, loaded_at)
            VALUES(%s,'愛知県',%s,%s,%s,%s,%s,now())
            ON CONFLICT (city) DO UPDATE SET n=EXCLUDED.n, truncated=EXCLUDED.truncated,
              data_vintage=EXCLUDED.data_vintage, source_url=EXCLUDED.source_url,
              attribution=EXCLUDED.attribution, loaded_at=now()""",
         (city, cnt, trunc, VINTAGE, PAGE, ATTRIB), fetch=False)
    psql('DROP TABLE IF EXISTS tk_stage', fetch=False)
    print(f'  入れた {city}: {cnt:,}面（基準水位={lvl}）' + ('  ※10万面で打ち切り＝非該当とは言えない' if trunc else ''))
    return cnt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--only', default='')
    a = ap.parse_args()
    for stmt in DDL.strip().split(';'):
        if stmt.strip():
            psql(stmt, fetch=False)
    total = 0
    targets = {a.only: CITIES[a.only]} if a.only else CITIES
    for city, code in targets.items():
        try:
            total += load(city, code, a.force)
        except Exception as e:  # noqa: BLE001
            print(f'  !! {city}: {e}')
    print(f'合計 {total:,} 面を追加')
    rows = psql('SELECT count(*), sum(n), count(*) FILTER (WHERE truncated) FROM tsunami_keikai_coverage')
    print(f'収録: {rows[0][0]} 市区町村 / {rows[0][1]:,} 面 / うち打ち切り {rows[0][2]} 市区町村')
    for c, n in psql('SELECT city, n FROM tsunami_keikai_coverage WHERE truncated ORDER BY city'):
        print(f'  打ち切り: {c}（{n:,}面）→ 非該当とは表示しない')


if __name__ == '__main__':
    main()
