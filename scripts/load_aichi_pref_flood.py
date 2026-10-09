#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""愛知県の洪水浸水想定区域図（県管理河川・Shapefile）を PostGIS に取り込む。

国土数値情報（A31b）は、県が新しく指定した河川が遅れて入る。愛知県河川課から提供を受けた
県の区域図（2026-10-09 受領・利用条件は特に無し・出典は「愛知県」と明記）で、名古屋市の外を補う。
名古屋市内は、市の R8 洪水ハザードマップが国・県の区域図（令和7年3月まで）を重ね合わせ済みなので、
**名古屋市域では取り込まない**（判定の収録範囲が重なると、どちらを使うかが曖昧になる）。

受け取ったもの（/mnt/data/kflood/aichi_pref_20261009/extracted/洪水/x_*）:
  【優先】20250328_R7.3指定図面SHP   12流域。**同じ流域が 2024 版にもあれば、こちらを使う**（県の指示）
  20241112指定_浸水想定区域図SHPデータ  約30流域。想定最大規模・計画規模・浸水継続時間・氾濫流・河岸侵食

**展開したシェープファイル（約17GB）は取り込み後に消した。** 入れ直すときは extracted/洪水/ の2つの zip を
x_<zip名>/ に展開し直す（ファイル名は CP932）。2026-10-09 の取り込み結果: 浸水深 41,447,797 面・家屋倒壊 1,122 面。
八田川・内津川の想定最大規模は、県から届いた DBF の最後の32,768件分が切れていたので、読める件数だけ入れた
（--only で後から足した。県に送り直しを依頼する）。

流域ごとに作った業者が違い、浸水深の列名（浸水深/DEEP/deep/depth/水深…）も単位も揃っていない。
**列名を信じず、値の範囲を見てから使う**（名古屋市の図は浸水深の列名が max_dur だった）。

  /usr/bin/python3 scripts/load_aichi_pref_flood.py inventory   # 流域ごとの棚卸し → outputs/aichi_pref_inventory.tsv
  /usr/bin/python3 scripts/load_aichi_pref_flood.py load [--force] [--only 流域名の一部]
"""
import argparse
import csv
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from load_nagoya_naisui import SRID, pg_dsn, psql  # noqa: E402

SRC = os.environ.get('KFLOOD_AICHI_PREF_DIR', '/mnt/data/kflood/aichi_pref_20261009/extracted/洪水')
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'outputs')
ENV = dict(os.environ, SHAPE_ENCODING='CP932')

DEPTH_FIELDS = ['浸水深', '最大浸水深', 'DEEP', 'deep', 'depth', 'Depth', '水深', 'suisin', 'sinsui', 'sinsuiL2', 'Hs', 'MESH_MAX']
DUR_FIELDS = ['継続時間', '浸水継続時', 'keizoku', 'keizoku_m', '浸水時間ﾗﾝ']


def role(name):
    """ファイル名から役割を決める。CAD の注記などは None"""
    if re.search(r'氾濫流', name):
        return 'collapse_flow'
    if re.search(r'河岸', name):
        return 'collapse_bank'
    if re.search(r'継続', name):
        return 'duration'
    if re.search(r'計画規模|L1\.shp$', name):
        return 'l1'
    if re.search(r'想定最大|L2\.shp$|MAXALL', name):
        return 'l2'
    return None


def basin(path):
    """流域名（「天白川流域」「矢作川中流支川」など）。版をまたいで同じ流域を見分けるのに使う"""
    for part in reversed(path.split('/')):
        m = re.search(r'([^/_（(]*?(?:流域|支川))', part)
        if m and not part.endswith('.shp'):
            name = re.sub(r'^\d+_', '', m.group(1))
            return re.sub(r'^.*?水系', '', name) or name
    m = re.search(r'([^/_（(]*?(?:流域|支川))', path.split('/')[-1])
    return re.sub(r'^.*?水系', '', m.group(1)) if m else '?'


def ogr_fields(shp):
    r = subprocess.run(['ogrinfo', '-so', '-al', '-ro', shp], capture_output=True, text=True, env=ENV).stdout
    n = re.search(r'Feature Count: (\d+)', r)
    fields = [l.split(':')[0].strip() for l in r.split('\n') if re.match(r'^\S.*: (Real|Integer|Integer64|String)', l)]
    geom = re.search(r'Geometry: (.+)', r)
    return int(n.group(1)) if n else 0, fields, (geom.group(1) if geom else '?')


def stats(shp, field):
    """列の値の範囲（ogr の SQL で min/max/count）。文字型の列も数値に直して見る。数値にならない列は None"""
    layer = os.path.splitext(os.path.basename(shp))[0]
    v = f'CAST("{field}" AS REAL)'   # 標準の OGRSQL は集計の中で CAST できないので SQLite の書き方
    sql = f'SELECT MIN({v}) AS lo, MAX({v}) AS hi, COUNT("{field}") AS n FROM "{layer}"'
    r = subprocess.run(['ogrinfo', '-ro', '-q', shp, '-dialect', 'SQLite', '-sql', sql], capture_output=True, text=True, env=ENV).stdout
    vals = dict(re.findall(r'(lo|hi|n) \(\w+\) = (\S+)', r))
    try:
        return float(vals['lo']), float(vals['hi']), int(float(vals['n']))
    except (KeyError, ValueError):
        return None


def files():
    """(版, 流域, 役割, shp)。【優先】版にある流域は 2024 版を捨てる"""
    rows = []
    for root, _, fs in os.walk(SRC):
        for f in fs:
            if f.endswith('.shp'):
                p = os.path.join(root, f)
                ver = 'r7' if '【優先】' in p else 'r6'
                rows.append((ver, basin(p), role(f), p))
    prio = {b for v, b, _, _ in rows if v == 'r7'}
    return [r for r in rows if r[0] == 'r7' or r[1] not in prio], sorted(prio)


def inventory():
    rows, prio = files()
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, 'aichi_pref_inventory.tsv')
    with open(path, 'w', newline='') as fh:
        w = csv.writer(fh, delimiter='\t')
        w.writerow(['版', '流域', '役割', '面', '形', '使う列', '最小', '最大', '列', 'ファイル'])
        for ver, b, rl, p in sorted(rows):
            n, fields, geom = ogr_fields(p)
            pick, st = '', None
            cands = DEPTH_FIELDS if rl in ('l1', 'l2') else DUR_FIELDS if rl == 'duration' else []
            for c in cands:
                if c in fields:
                    st = stats(p, c)
                    if st:
                        pick = c
                        break
            w.writerow([ver, b, rl or '-', n, geom, pick, st[0] if st else '', st[1] if st else '',
                        ','.join(fields)[:120], p.replace(SRC + '/', '')])
    print('【優先】版の流域:', '、'.join(prio))
    print('書き出し:', path)


# ---------------------------------------------------------------- 取り込み
AREA = '愛知県'
VINTAGE = '2024年11月12日指定（20241120 版）・2025年3月28日指定（R7.3・県が優先と指定）。2026-10-09 に愛知県河川課から提供'
ATTR = '出典: 愛知県「洪水浸水想定区域図」（県管理河川・{v}。愛知県建設局河川課から提供）を加工して作成'
DATASETS = {
    'aichi_pref_flood_depth': dict(name='愛知県 洪水浸水想定区域図（想定最大規模の浸水深）', table='muni_flood_depth'),
    'aichi_pref_flood_collapse': dict(name='愛知県 洪水浸水想定区域図（家屋倒壊等氾濫想定区域）', table='muni_flood_collapse'),
}
# 変換後の外接矩形がこの中に収まらないファイルは、座標系の取り違えとみなして取り込まない
AICHI_BBOX = (136.55, 34.50, 137.90, 35.45)


def dbf_rows(shp):
    """DBF のヘッダーの件数と、ファイルの大きさから実際に読める件数。県から届いた八田川・内津川の
    想定最大規模は、最後の32,768件分の属性が切れていた（zip は正常。2026-10-09）"""
    import struct
    f = os.path.splitext(shp)[0] + '.dbf'
    with open(f, 'rb') as fh:
        n, hl, rl = struct.unpack('<IHH', fh.read(32)[4:12])
    return n, max(0, (os.path.getsize(f) - hl) // rl)


def ogr_load(shp, sql=None, limit=None):
    """1ファイルを aichi_stage に入れる（経緯度 JGD2011 に変換）。.prj が無ければ平面直角座標系 第VII系とみなす"""
    psql('DROP TABLE IF EXISTS aichi_stage', fetch=False)
    cmd = ['ogr2ogr', '-f', 'PostgreSQL', 'PG:' + pg_dsn(), shp, '-nln', 'aichi_stage',
           '-t_srs', f'EPSG:{SRID}', '-nlt', 'PROMOTE_TO_MULTI', '-lco', 'GEOMETRY_NAME=geom', '-lco', 'FID=id',
           '-lco', 'SPATIAL_INDEX=NONE', '-gt', '100000', '--config', 'PG_USE_COPY', 'YES', '-overwrite']
    if not os.path.exists(os.path.splitext(shp)[0] + '.prj'):
        cmd += ['-s_srs', 'EPSG:2449']
    if sql:
        cmd += ['-sql', sql, '-dialect', 'OGRSQL']
    if limit:
        cmd += ['-limit', str(limit)]
    r = subprocess.run(cmd, capture_output=True, text=True, env=ENV)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-600:])
    if not psql("SELECT to_regclass('aichi_stage')")[0][0]:
        return None   # 地物が0件のファイルは ogr2ogr がテーブルを作らない
    bb = psql('SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) FROM (SELECT ST_Extent(geom) e FROM aichi_stage) x')[0]
    if bb[0] is None:
        return None
    ok = bb[0] >= AICHI_BBOX[0] and bb[1] >= AICHI_BBOX[1] and bb[2] <= AICHI_BBOX[2] and bb[3] <= AICHI_BBOX[3]
    return bb if ok else ('外れ', bb)


def load(force=False, only=None):
    from load_nagoya_flood import DDL
    psql(DDL, fetch=False)
    psql('ALTER TABLE muni_flood_collapse ADD COLUMN IF NOT EXISTS kind text', fetch=False)
    if not force and not only and psql("SELECT 1 FROM muni_flood_coverage WHERE area=%s", (AREA,)):
        print('取り込み済み（--force で入れ直し）')
        return
    rows, prio = files()
    for k, ds in DATASETS.items():
        if not only:   # --only は足すだけ（全流域を入れ終えたあとに一部を入れ直すとき、ほかを消さない）
            psql(f'DELETE FROM {ds["table"]} WHERE dataset_key=%s', (k,), fetch=False)
        psql("""INSERT INTO datasets(key,name,source_url,data_vintage,loaded_at,attribution,note)
                VALUES(%s,%s,%s,%s,now(),%s,%s)
                ON CONFLICT (key) DO UPDATE SET name=EXCLUDED.name, data_vintage=EXCLUDED.data_vintage,
                  loaded_at=EXCLUDED.loaded_at, attribution=EXCLUDED.attribution, note=EXCLUDED.note""",
             (k, ds['name'], 'https://www.pref.aichi.jp/soshiki/kasen/', VINTAGE,
              ATTR.format(v='2024年11月・2025年3月指定'), '県管理河川。名古屋市域では使わない（市の図が県の区域図を含むため）'),
             fetch=False)
    psql('DROP TABLE IF EXISTS aichi_hull; CREATE TABLE aichi_hull (basin text, geom geometry(Geometry, 6668))', fetch=False)
    report = []
    for ver, b, rl, p in sorted(rows):
        if only and only not in b:
            continue
        if rl not in ('l2', 'collapse_flow', 'collapse_bank'):
            continue
        if only and rl != 'l2':
            continue   # --only は浸水深の入れ直し用。家屋倒壊は全体の取り込みで入っているので二重にしない
        layer = os.path.splitext(os.path.basename(p))[0]
        n, fields, geom = ogr_fields(p)
        if 'Polygon' not in geom:
            report.append((b, rl, '面ではない', geom)); continue
        if rl == 'l2':
            total, ok_rows = dbf_rows(p)
            cut = ok_rows < total
            if cut:
                # 属性が途中で切れている。読める件数までだけ入れ、値の範囲は入れたあとで確かめる
                f = next((c for c in DEPTH_FIELDS if c in fields), None)
                if not f:
                    report.append((b, rl, '浸水深の列が無い', ','.join(fields))); continue
            else:
                f = next((c for c in DEPTH_FIELDS if c in fields and stats(p, c)), None)
                if not f:
                    report.append((b, rl, '浸水深の列が無い', ','.join(fields))); continue
                st = stats(p, f)
                if st[1] > 30:
                    report.append((b, rl, f'浸水深の最大が{st[1]}（m ではない？）', f)); continue
            bb = ogr_load(p, f'SELECT CAST("{f}" AS float) AS depth_m FROM "{layer}"', limit=ok_rows if cut else None)
            if cut and bb and bb[0] != '外れ':
                hi = psql('SELECT max(depth_m) FROM aichi_stage')[0][0]
                if hi is None or hi > 30:
                    report.append((b, rl, f'浸水深の最大が{hi}（m ではない？）', f)); continue
                print(f'  {b}: 属性が {total - ok_rows:,} 件欠けている（{ok_rows:,}/{total:,} 件だけ入れる）', flush=True)
        else:
            bb = ogr_load(p)
        if bb is None or bb[0] == '外れ':
            report.append((b, rl, '空のファイル' if bb is None else '範囲が愛知県の外（座標系？）', str(bb))); continue
        if rl == 'l2':
            m = psql("""WITH ins AS (INSERT INTO muni_flood_depth(dataset_key, area, depth_m, geom)
                        SELECT 'aichi_pref_flood_depth', %s, depth_m, geom FROM aichi_stage
                        WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom) AND depth_m > 0 RETURNING 1)
                        SELECT count(*) FROM ins""", (AREA,))[0][0]
            psql("INSERT INTO aichi_hull SELECT %s, ST_ConvexHull(ST_Collect(geom)) FROM aichi_stage", (b,), fetch=False)
        else:
            kind = '氾濫流' if rl == 'collapse_flow' else '河岸侵食'
            m = psql("""WITH ins AS (INSERT INTO muni_flood_collapse(dataset_key, area, kind, geom)
                        SELECT 'aichi_pref_flood_collapse', %s, %s, geom FROM aichi_stage
                        WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom) RETURNING 1)
                        SELECT count(*) FROM ins""", (AREA, kind))[0][0]
        report.append((b, rl, f'{m:,} 面', ver))
        print(f'  {ver} {b} {rl}: {m:,} 面', flush=True)
    psql('DROP TABLE IF EXISTS aichi_stage', fetch=False)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, 'aichi_pref_load_report.tsv'), 'w') as fh:
        for r in report:
            fh.write('\t'.join(map(str, r)) + '\n')
    for r in report:
        if not str(r[2]).endswith('面'):
            print('  取り込まなかった:', *r, flush=True)
    if only:
        if psql("SELECT 1 FROM muni_flood_coverage WHERE area=%s", (AREA,)):
            # 全流域を入れ終えたあとの追加: その流域の範囲を収録範囲に足す
            psql("""UPDATE muni_flood_coverage SET loaded_at=now(), geom=ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Union(geom,
                      ST_Difference((SELECT ST_Union(geom) FROM aichi_hull),
                        COALESCE((SELECT geom FROM muni_flood_coverage WHERE area='名古屋市'), ST_GeomFromText('POLYGON EMPTY', 6668))))), 3))
                    WHERE area=%s AND EXISTS (SELECT 1 FROM aichi_hull)""", (AREA,), fetch=False)
            print('収録範囲に足した')
        else:
            print('--only のときは収録範囲を登録しない（判定に使われない）')
        return
    # 収録範囲 = 流域ごとの浸水域の凸包の和 − 名古屋市域（名古屋市は市の図を使う。範囲が重なると判定が曖昧になる）
    psql("""INSERT INTO muni_flood_coverage(area, dataset_keys, loaded_at, geom)
            SELECT %s, ARRAY['aichi_pref_flood_depth','aichi_pref_flood_collapse'], now(),
                   ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Difference(
                     (SELECT ST_Union(geom) FROM aichi_hull),
                     COALESCE((SELECT geom FROM muni_flood_coverage WHERE area='名古屋市'), ST_GeomFromText('POLYGON EMPTY', 6668)))), 3))
            ON CONFLICT (area) DO UPDATE SET dataset_keys=EXCLUDED.dataset_keys, loaded_at=now(), geom=EXCLUDED.geom""",
         (AREA,), fetch=False)
    psql('ANALYZE muni_flood_depth; ANALYZE muni_flood_collapse; ANALYZE muni_flood_coverage', fetch=False)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, 'aichi_pref_load_report.tsv'), 'w') as fh:
        for r in report:
            fh.write('\t'.join(map(str, r)) + '\n')
    print('記録: outputs/aichi_pref_load_report.tsv')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['inventory', 'load'])
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--only')
    a = ap.parse_args()
    if a.cmd == 'inventory':
        inventory()
    else:
        load(force=a.force, only=a.only)
