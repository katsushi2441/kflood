#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""市区町村ごとの洪水浸水想定の集計を作る（/area/<団体コード> の中身）。

A31b（洪水浸水想定区域・1次メッシュ単位）は**市区町村コードを持たない**。
そこで ktsunami と同じ方式で、krefuge（国土地理院 指定緊急避難場所・CC BY 4.0）の
実在する施設の座標を使い、**その施設が洪水浸水想定区域の中にあるか**を数える。

ここで測っているのは「市域の何%が浸水するか」ではない（面積の標本になっていない）。
「その市区町村の指定緊急避難場所のうち何件が洪水浸水想定区域の中にあるか、どの深さか」であり、
避難先そのものが浸かる想定かどうかという、確かめられる事実。ページにもそう書く。

判定は app/main.py の check_point / merge_muni_flood と同じ考え方にそろえる:
  ・国の想定最大規模（category=20）の浸水深ランクの最大
  ・家屋倒壊等氾濫想定区域（category=40）
  ・自治体版・県版（muni_flood_*: 名古屋市 R8・愛知県の県管理河川）が当たる場所は、国と**深い方**
  ・1次メッシュが無い場所（meshes に当たらない）は「未収録」として別に数える（区域外と混ぜない）

  cd /home/kojima/work/kflood
  systemd-run --user --scope -p MemoryMax=8G .venv/bin/python scripts/build_muni_stats.py
"""
import collections
import json
import os
import sys
import time

import psycopg2
from psycopg2.extras import execute_values

KREFUGE_DB = '/home/kojima/work/krefuge/data/krefuge.db'
sys.path.insert(0, '/home/kojima/work/krefuge/scripts')
from muni import PREFS, extract, load_canon  # noqa: E402  krefuge の市区町村判定をそのまま使う（重複実装しない）

DB = dict(host='127.0.0.1', port=int(os.environ.get('KFLOOD_DB_PORT', '55434')), dbname='kflood',
          user='postgres', password=os.environ.get('KFLOOD_DB_PASS', 'kflood_local'))
CODE_BY_PREF = {p: f'{i + 1:02d}' for i, p in enumerate(PREFS)}

DDL = """
CREATE TABLE IF NOT EXISTS muni_stats (
  muni_code text PRIMARY KEY, pref_code text NOT NULL, pref text NOT NULL, muni text NOT NULL,
  shelters integer NOT NULL,          -- 指定緊急避難場所（座標あり・この市区町村に判定できたもの）
  uncovered integer NOT NULL,         -- 国のデータが無い1次メッシュにある（未収録。区域外とは数えない）
  inside integer NOT NULL,            -- 洪水浸水想定区域（想定最大規模）または家屋倒壊等氾濫想定区域の中
  by_rank jsonb NOT NULL,             -- {"1": 0.5m未満の件数, ... "6": 20m以上}
  max_rank integer NOT NULL,          -- 0=区域内なし
  collapse integer NOT NULL,          -- 家屋倒壊等氾濫想定区域の中
  flood_shelters integer NOT NULL,    -- 洪水の指定がある避難場所
  flood_inside integer NOT NULL,      -- そのうち浸水想定区域の中
  muni_src text[] NOT NULL,           -- 自治体版・県版で深さが変わった/足された地域（'名古屋市','愛知県'）
  samples jsonb NOT NULL,             -- 区域内の避難場所の例（深い順）
  computed_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS muni_stats_pref ON muni_stats(pref_code);
"""

RANK_SQL = """CASE WHEN d < 0.5 THEN 1 WHEN d < 3 THEN 2 WHEN d < 5 THEN 3 WHEN d < 10 THEN 4 WHEN d < 20 THEN 5 WHEN d >= 20 THEN 6 END"""

POINT_SQL = f"""
SELECT p.id,
       EXISTS (SELECT 1 FROM meshes m WHERE ST_Contains(m.geom, p.geom)) AS covered,
       (SELECT max(rank) FROM flood f WHERE f.category = 20 AND ST_Contains(f.geom, p.geom)) AS nrank,
       EXISTS (SELECT 1 FROM flood f WHERE f.category = 40 AND ST_Contains(f.geom, p.geom)) AS ncol,
       c.area,
       (SELECT (SELECT {RANK_SQL} FROM (SELECT max(depth_m) AS d FROM muni_flood_depth x
                                        WHERE x.area = c.area AND ST_Contains(x.geom, p.geom)) q)) AS mrank,
       (c.area IS NOT NULL AND EXISTS (SELECT 1 FROM muni_flood_collapse y
                                        WHERE y.area = c.area AND ST_Contains(y.geom, p.geom))) AS mcol
FROM kf_pts p
LEFT JOIN LATERAL (SELECT area FROM muni_flood_coverage cv WHERE ST_Contains(cv.geom, p.geom)
                   ORDER BY (area = '名古屋市') DESC LIMIT 1) c ON true
WHERE p.id BETWEEN %s AND %s
"""


def main():
    t0 = time.time()
    _, canon = load_canon(KREFUGE_DB)
    import sqlite3
    k = sqlite3.connect(KREFUGE_DB)
    pts, meta, miss = [], {}, collections.Counter()
    for sid, pref, muni, name, addr, lat, lon, flood in k.execute(
            'SELECT id, pref, muni, name, address, lat, lon, flood FROM shelters'):
        pc = CODE_BY_PREF.get(pref)
        if not pc or lat is None or lon is None:
            miss['県名・座標なし'] += 1
            continue
        # krefuge の build_muni_stats.py と同じ: 住所から引き、だめなら muni 列で引く
        hit = extract(pc, addr, canon) or (extract(pc, (muni or '') + '　', canon) if muni else None)
        if not hit:
            miss[pref] += 1
            continue
        mname, code = hit
        pts.append((sid, lon, lat))
        meta[sid] = (code, pc, pref, mname, name, addr, bool(flood))
    k.close()
    print(f'避難場所 {len(pts):,}（市区町村を特定できず {sum(miss.values()):,}）', flush=True)

    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute('DROP TABLE IF EXISTS kf_pts')
    cur.execute('CREATE UNLOGGED TABLE kf_pts (id integer PRIMARY KEY, geom geometry(Point, 6668))')
    execute_values(cur, 'INSERT INTO kf_pts (id, geom) VALUES %s', pts,
                   template='(%s, ST_SetSRID(ST_Point(%s, %s), 6668))', page_size=5000)
    conn.commit()
    ids = sorted(m for m in meta)
    res = {}
    step = 5000
    for i in range(0, len(ids), step):
        lo, hi = ids[i], ids[min(i + step, len(ids)) - 1]
        cur.execute(POINT_SQL, (lo, hi))
        for sid, covered, nrank, ncol, area, mrank, mcol in cur.fetchall():
            res[sid] = (covered, nrank or 0, ncol, area, mrank or 0, mcol)
        print(f'  {min(i + step, len(ids)):,}/{len(ids):,} ({time.time() - t0:.0f}s)', flush=True)
    cur.execute('DROP TABLE kf_pts')

    agg = {}
    for sid, (code, pc, pref, mname, name, addr, flood_ok) in meta.items():
        covered, nrank, ncol, area, mrank, mcol = res.get(sid, (False, 0, False, None, 0, False))
        a = agg.get(code)
        if a is None:
            a = agg[code] = dict(pref_code=pc, pref=pref, muni=mname, shelters=0, uncovered=0, inside=0,
                                 by_rank=collections.Counter(), max_rank=0, collapse=0, flood_shelters=0,
                                 flood_inside=0, muni_src=set(), samples=[])
        a['shelters'] += 1
        if flood_ok:
            a['flood_shelters'] += 1
        rank = max(nrank, mrank)
        col = bool(ncol or mcol)
        if area and (mrank > nrank or (mcol and not ncol)):
            a['muni_src'].add(area)
        if not covered and not rank and not col:
            a['uncovered'] += 1
            continue
        if rank:
            a['by_rank'][rank] += 1
            a['max_rank'] = max(a['max_rank'], rank)
        if col:
            a['collapse'] += 1
        if rank or col:
            a['inside'] += 1
            if flood_ok:
                a['flood_inside'] += 1
            if name:
                a['samples'].append(dict(name=name, address=addr, rank=rank, collapse=col, flood_ok=flood_ok))

    cur.execute(DDL)
    cur.execute('DELETE FROM muni_stats')
    rows = []
    for code, a in agg.items():
        s = sorted(a['samples'], key=lambda x: (-x['rank'], not x['collapse']))[:8]
        rows.append((code, a['pref_code'], a['pref'], a['muni'], a['shelters'], a['uncovered'], a['inside'],
                     json.dumps({str(r): n for r, n in sorted(a['by_rank'].items())}), a['max_rank'], a['collapse'],
                     a['flood_shelters'], a['flood_inside'], sorted(a['muni_src']),
                     json.dumps(s, ensure_ascii=False)))
    execute_values(cur, """INSERT INTO muni_stats (muni_code,pref_code,pref,muni,shelters,uncovered,inside,by_rank,max_rank,
                           collapse,flood_shelters,flood_inside,muni_src,samples,computed_at) VALUES %s""", rows,
                   template='(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())')
    conn.commit()
    n_in = sum(1 for a in agg.values() if a['inside'])
    print(f'市区町村 {len(agg):,}（避難場所が浸水想定区域内にある {n_in:,}）  {time.time() - t0:.0f}s')
    cur.execute('SELECT pref, muni, shelters, inside, max_rank, collapse, uncovered FROM muni_stats ORDER BY inside DESC LIMIT 8')
    for r in cur.fetchall():
        print('  ', r)
    cur.execute("SELECT pref, muni, shelters, inside, by_rank, collapse, muni_src FROM muni_stats WHERE muni_code IN ('23100','23211','14210','13101','01100')")
    for r in cur.fetchall():
        print('  ', r)
    cur.execute('SELECT sum(uncovered), sum(shelters) FROM muni_stats')
    print('  未収録/全体', cur.fetchone())
    conn.close()


if __name__ == '__main__':
    main()
