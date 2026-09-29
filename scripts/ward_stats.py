#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""区ごとの浸水想定の目安を、学区線（区の形）にランダム点を打って推定し、ward_stats 表に入れる。

厳密な面積計算（2,100万面との ST_Intersection）は重いので、区ごとに N 点（既定 800）を打ち、
各点で「想定最大規模の浸水深ランク」と「内水の浸水深」を引いて割合を出す。誤差はおおむね ±3ポイント。
画面には「サンプリングによる推定」と明記する。

  .venv/bin/python scripts/ward_stats.py            # 16区
  .venv/bin/python scripts/ward_stats.py --n 1500   # 点を増やす
"""
import argparse
import json
import os
import sys
import time

import psycopg2

DB = dict(host='127.0.0.1', port=int(os.environ.get('KFLOOD_DB_PORT', '55434')), dbname='kflood',
          user='postgres', password=os.environ.get('KFLOOD_DB_PASS', 'kflood_local'))
DDL = """
CREATE TABLE IF NOT EXISTS ward_stats (
  area text NOT NULL, ward text NOT NULL, sample_n integer NOT NULL,
  pct_rank jsonb NOT NULL,        -- {"0": 区域外%, "1": ランク1%, ...}
  pct_depth05 real NOT NULL,      -- 想定最大規模で 0.5m以上（ランク2以上）
  pct_depth3 real NOT NULL,       -- 3m以上（ランク3以上）
  pct_collapse real NOT NULL,     -- 家屋倒壊等氾濫想定区域
  pct_naisui real NOT NULL,       -- 内水 浸水想定セルあり
  computed_at timestamptz NOT NULL,
  PRIMARY KEY (area, ward)
);
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--n', type=int, default=800)
    a = ap.parse_args()
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute(DDL)
    cur.execute("SELECT DISTINCT area, ward FROM gakku ORDER BY area, ward")
    wards = cur.fetchall()
    t0 = time.time()
    for area, ward in wards:
        t = time.time()
        cur.execute("""WITH w AS (SELECT ST_Union(geom) AS g FROM gakku WHERE area=%s AND ward=%s),
                            pts AS (SELECT (ST_Dump(ST_GeneratePoints(g, %s, 42))).geom AS p FROM w)
                       SELECT
                         (SELECT count(*) FROM pts) AS n,
                         (SELECT json_object_agg(r, c) FROM (
                            -- 国のランクと、自治体版（名古屋市の洪水ハザードマップ）の浸水深をランクにしたものの深い方。
                            -- 判定画面（app/main.py merge_muni_flood）と同じ考え方にそろえる
                            SELECT greatest(
                                     coalesce((SELECT max(rank) FROM flood f WHERE f.category=20 AND ST_Contains(f.geom, pts.p)), 0),
                                     coalesce((SELECT CASE WHEN max(depth_m) < 0.5 THEN 1 WHEN max(depth_m) < 3 THEN 2 WHEN max(depth_m) < 5 THEN 3
                                                           WHEN max(depth_m) < 10 THEN 4 WHEN max(depth_m) < 20 THEN 5 WHEN max(depth_m) >= 20 THEN 6 END
                                               FROM muni_flood_depth m WHERE m.area=%s AND ST_Contains(m.geom, pts.p)), 0)) AS r, count(*) AS c
                            FROM pts GROUP BY 1) x) AS by_rank,
                         (SELECT count(*) FROM pts WHERE EXISTS (SELECT 1 FROM flood f WHERE f.category=40 AND ST_Contains(f.geom, pts.p))
                                                      OR EXISTS (SELECT 1 FROM muni_flood_collapse m WHERE m.area=%s AND ST_Contains(m.geom, pts.p))) AS collapse,
                         (SELECT count(*) FROM pts WHERE EXISTS (SELECT 1 FROM naisui_depth d WHERE d.area=%s AND ST_Contains(d.geom, pts.p))) AS naisui
                    """, (area, ward, a.n, area, area, area))
        n, by_rank, collapse, naisui = cur.fetchone()
        by_rank = {str(k): round(100.0 * v / n, 1) for k, v in (by_rank or {}).items()}
        d05 = round(sum(v for k, v in by_rank.items() if int(k) >= 2), 1)
        d3 = round(sum(v for k, v in by_rank.items() if int(k) >= 3), 1)
        cur.execute("""INSERT INTO ward_stats(area,ward,sample_n,pct_rank,pct_depth05,pct_depth3,pct_collapse,pct_naisui,computed_at)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,now())
                       ON CONFLICT (area, ward) DO UPDATE SET sample_n=EXCLUDED.sample_n, pct_rank=EXCLUDED.pct_rank, pct_depth05=EXCLUDED.pct_depth05,
                         pct_depth3=EXCLUDED.pct_depth3, pct_collapse=EXCLUDED.pct_collapse, pct_naisui=EXCLUDED.pct_naisui, computed_at=now()""",
                    (area, ward, n, json.dumps(by_rank), d05, d3, round(100.0 * collapse / n, 1), round(100.0 * naisui / n, 1)))
        conn.commit()
        print(f'  {ward}: n={n} 0.5m以上 {d05}% / 3m以上 {d3}% / 倒壊区域 {100.0*collapse/n:.1f}% / 内水 {100.0*naisui/n:.1f}% ({time.time()-t:.0f}s)', flush=True)
    print(f'done {len(wards)} wards ({(time.time()-t0)/60:.1f}min)')


if __name__ == '__main__':
    main()
