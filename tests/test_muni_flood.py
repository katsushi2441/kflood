# -*- coding: utf-8 -*-
"""名古屋市の洪水ハザードマップ（自治体版）で国の判定を補う処理のテスト。  実行: .venv/bin/python -m pytest -q tests"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import codes  # noqa: E402


def test_depth_rank_boundaries_follow_ksj_bands():
    # 境界ちょうどは上のランク（「0.5m以上」「3.0m以上」）
    assert codes.depth_rank(0.0001) == 1
    assert codes.depth_rank(0.49) == 1
    assert codes.depth_rank(0.5) == 2
    assert codes.depth_rank(2.99) == 2
    assert codes.depth_rank(3.0) == 3
    assert codes.depth_rank(5.0) == 4
    assert codes.depth_rank(10.0) == 5
    assert codes.depth_rank(16.3) == 5
    assert codes.depth_rank(20.0) == 6
    assert codes.depth_rank(None) is None


def test_duration_rank_reads_minutes():
    # 名古屋市の継続時間は「分」（国のランクとの突き合わせで確定）
    assert codes.duration_rank(480) == 1          # 8時間
    assert codes.duration_rank(12 * 60) == 2      # 12時間ちょうど
    assert codes.duration_rank(24 * 60) == 3
    assert codes.duration_rank(8040) == 4         # 134時間
    assert codes.duration_rank(168 * 60) == 5
    assert codes.duration_rank(336 * 60) == 6
    assert codes.duration_rank(41596) == 7        # 693時間
    assert codes.duration_rank(None) is None


def _db_ready():
    try:
        import psycopg2
        from app.main import DB
        with psycopg2.connect(**DB) as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM muni_flood_coverage WHERE cardinality(dataset_keys) >= 2")
            return cur.fetchone()[0] > 0
    except Exception:
        return False


@pytest.mark.skipif(not _db_ready(), reason='名古屋市の洪水データが取り込まれていない')
def test_city_fills_gap_where_national_data_is_outside():
    """国のデータだけでは区域外、市のデータでは区域内の地点が、区域内として返ること。"""
    import psycopg2
    from app.main import DB, check_point
    with psycopg2.connect(**DB) as c, c.cursor() as cur:
        # 市の浸水セルのうち、国の想定最大規模（category=20）に入っていないものを1つ探す
        cur.execute("""SELECT ST_X(p), ST_Y(p) FROM (
                         SELECT ST_PointOnSurface(geom) p FROM muni_flood_depth WHERE depth_m >= 0.5
                         ORDER BY id LIMIT 3000) q
                       WHERE NOT EXISTS (SELECT 1 FROM flood f WHERE f.category=20 AND ST_Contains(f.geom, q.p))
                       LIMIT 1""")
        row = cur.fetchone()
    assert row, '国のデータで区域外になる市のセルが見つからない（比較の前提が崩れた）'
    res = check_point(row[0], row[1], '愛知県名古屋市')
    nat = res['national']
    assert nat['status'] == 'inside'
    assert nat['max'] and nat['max'].get('source') == 'muni'
    assert nat['city']['national_status'] == 'outside'
    assert any(d['key'] == 'nagoya_flood_depth' for d in res['datasets'])


@pytest.mark.skipif(not _db_ready(), reason='名古屋市の洪水データが取り込まれていない')
def test_outside_city_is_untouched():
    """市外（豊明市）では市のデータを使わない。"""
    from app.main import check_point
    res = check_point(137.0120, 35.0540, '愛知県豊明市')
    assert 'city' not in res['national']
