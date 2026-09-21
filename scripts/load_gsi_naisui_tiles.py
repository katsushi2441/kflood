#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国土地理院「重ねるハザードマップ」の内水（雨水出水）浸水想定区域タイルを取り込む。

**なぜこの経路か（2026-09-20 実測）**
- 国土数値情報 A51 は 9都府県・10市区町村しかない（千葉県は提供なし）。
- 千葉市の WEB 版ハザードマップは地図データの著作権がゼンリンに帰属し、
  「私的使用の範囲」「閲覧と紙への印刷のみ」「複製・送信・第三者利用を禁止」と
  利用条件に明記されている。**使えない。**
- 重ねるハザードマップの内水は 66 市町村を掲載し（一覧CSVの実数。ページ表記は65）、出典一覧の「オープンデータ」列が
  〇（複製・公衆送信・翻案が自由、商用利用も可。PDL1.0・出典記載）。千葉県は
  船橋市・柏市・我孫子市・八街市・一宮町の5市町。**千葉市は載っていない。**

**中身はベクターではなく画像タイル**（256px PNG・RGBA・alpha は 0/255 の2値・
色は凡例と完全一致）。画素を凡例の色で分類し、行方向の連続画素を矩形にして
PostGIS に入れる。座標はウェブメルカトルのタイル座標から経緯度へ数式で戻す。

凡例（「水害ハザードマップ作成の手引き」令和5年5月に基づく・重ねるHMの
image/legend/naisui_legend.png を実測）。**深さは区分の下限で入れる**
（上限で入れると危険側に見積もりすぎる。判定は「◯m以上」と読ませる）:
  (255,255,179) 〜0.3m        → 0.0
  (247,245,169) 〜0.5m        → 0.3  ※0.3 の細分が無い自治体では 0〜0.5 を指す
  (248,225,166) 0.5〜1.0m     → 0.5
  (255,216,192) 0.5〜3.0m     → 0.5  ※1.0 の細分が無い自治体では 0.5〜3.0 を指す
  (255,183,183) 3.0〜5.0m     → 3.0
  (255,145,145) 5.0〜10.0m    → 5.0
  (242,133,201) 10.0〜20.0m   → 10.0
  (220,122,220) 20.0m〜       → 20.0
凡例に無い色が出たら数えて報告し、**推測で割り当てない**。

収録範囲（naisui_coverage）は都道府県の外接矩形ではなく、**実際に取得できたタイルの
矩形の和**にする。矩形にすると、載っていない市（千葉市など）が「範囲内・浸水想定なし」
と出てしまい、豊山町で踏んだのと同じ誤判定になる。

  python3 scripts/load_gsi_naisui_tiles.py --pref 12            # 千葉県
  python3 scripts/load_gsi_naisui_tiles.py --pref 12 --zoom 15  # 粗く（既定 16）
  python3 scripts/load_gsi_naisui_tiles.py --pref 12 --force    # 入れ直し
"""
import argparse
import datetime as dt
import io
import json
import math
import os
import sys
import time
import urllib.request

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

BASE = "https://disaportaldata.gsi.go.jp/raster/02_naisui_pref_data/{pref}/{z}/{x}/{y}.png"
PORTAL = "https://disaportal.gsi.go.jp/hazardmapportal/hazardmap/copyright/opendata.html"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
PREF_NAME = {"01": "北海道", "02": "青森県", "03": "岩手県", "04": "宮城県", "05": "秋田県", "06": "山形県",
             "07": "福島県", "08": "茨城県", "09": "栃木県", "10": "群馬県", "11": "埼玉県", "12": "千葉県",
             "13": "東京都", "14": "神奈川県", "15": "新潟県", "16": "富山県", "17": "石川県", "18": "福井県",
             "19": "山梨県", "20": "長野県", "21": "岐阜県", "22": "静岡県", "23": "愛知県", "24": "三重県",
             "25": "滋賀県", "26": "京都府", "27": "大阪府", "28": "兵庫県", "29": "奈良県", "30": "和歌山県",
             "31": "鳥取県", "32": "島根県", "33": "岡山県", "34": "広島県", "35": "山口県", "36": "徳島県",
             "37": "香川県", "38": "愛媛県", "39": "高知県", "40": "福岡県", "41": "佐賀県", "42": "長崎県",
             "43": "熊本県", "44": "大分県", "45": "宮崎県", "46": "鹿児島県", "47": "沖縄県"}
# 都道府県の外接矩形（四分木の出発点。粗くてよい）
# 都道府県の外接矩形（lon0, lat0, lon1, lat1）。ここから z10 のタイルを起点にして掘る。
PREF_BBOX = {"12": (139.70, 34.85, 140.90, 36.12),   # 千葉県
             "14": (138.90, 35.10, 139.80, 35.68)}   # 神奈川県
LEGEND = {(255, 255, 179): 0.0, (247, 245, 169): 0.3, (248, 225, 166): 0.5, (255, 216, 192): 0.5,
          (255, 183, 183): 3.0, (255, 145, 145): 5.0, (242, 133, 201): 10.0, (220, 122, 220): 20.0}
ATTRIB = "出典：「ハザードマップポータルサイト」（{url}）（{date}に利用）／国土地理院 重ねるハザードマップ 内水（雨水出水）浸水想定区域 を加工して作成"


def tile_of(lon, lat, z):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def lonlat_of(x, y, z):
    """タイル座標（小数可）→ 経緯度。"""
    n = 2 ** z
    lon = x / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lon, lat


def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read() if r.status == 200 else None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def enumerate_tiles(pref, zoom, sleep=0.1, log=print):
    """都道府県の外接矩形から四分木で降り、存在するタイルだけを集める。"""
    lon0, lat0, lon1, lat1 = PREF_BBOX[pref]
    z0 = 10
    x0, y1 = tile_of(lon0, lat1, z0)
    x1, y0 = tile_of(lon1, lat0, z0)
    level = {(x, y) for x in range(x0, x1 + 1) for y in range(y1, y0 + 1)}
    for z in range(z0, zoom + 1):
        hit = set()
        for (x, y) in sorted(level):
            if fetch(BASE.format(pref=pref, z=z, x=x, y=y)) is not None:
                hit.add((x, y))
            time.sleep(sleep)
        log(f"  z{z}: 調べた{len(level)} あり{len(hit)}")
        if not hit:
            return set()
        if z == zoom:
            return hit
        level = {(2 * x + dx, 2 * y + dy) for (x, y) in hit for dx in (0, 1) for dy in (0, 1)}
    return set()


def tile_rects(png, x, y, z, unknown):
    """1枚のタイルを、行ごとの連続画素 → 矩形（経緯度）に変える。(depth, wkt) を返す。"""
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    w, h = im.size
    px = im.load()
    out = []
    for row in range(h):
        col = 0
        while col < w:
            r, g, b, a = px[col, row]
            if a == 0:
                col += 1
                continue
            key = (r, g, b)
            depth = LEGEND.get(key)
            if depth is None:
                unknown[key] = unknown.get(key, 0) + 1
                col += 1
                continue
            start = col
            while col < w and px[col, row][3] != 0 and px[col, row][:3] == key:
                col += 1
            # 画素の外枠をタイル座標の小数で表し、経緯度へ
            lon_a, lat_a = lonlat_of(x + start / w, y + row / h, z)
            lon_b, lat_b = lonlat_of(x + col / w, y + (row + 1) / h, z)
            out.append((depth, f"POLYGON(({lon_a} {lat_a},{lon_b} {lat_a},{lon_b} {lat_b},{lon_a} {lat_b},{lon_a} {lat_a}))"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pref", required=True, help="都道府県コード2桁（例: 12）")
    ap.add_argument("--zoom", type=int, default=16, help="取り込むズーム（16≈2.4m/px）")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.1)
    a = ap.parse_args()
    pref = a.pref.zfill(2)
    if pref not in PREF_BBOX:
        raise SystemExit(f"PREF_BBOX に {pref} の外接矩形を足してください")

    import psycopg2
    from psycopg2.extras import execute_values
    from load_nagoya_naisui import DDL, pg_dsn

    key = f"gsi_naisui_{pref}"
    area = f"{PREF_NAME[pref]}（重ねるハザードマップ掲載分）"
    con = psycopg2.connect(pg_dsn())
    con.autocommit = True
    with con.cursor() as cur:
        cur.execute(DDL)
        # 収録範囲を「矩形の和」で持てるように MultiPolygon へ（既存行は ST_Multi で保つ）
        cur.execute("SELECT type FROM geometry_columns WHERE f_table_name='naisui_coverage' AND f_geometry_column='geom'")
        if (cur.fetchone() or ["MULTIPOLYGON"])[0] != "MULTIPOLYGON":
            cur.execute("ALTER TABLE naisui_coverage ALTER COLUMN geom TYPE geometry(MultiPolygon,6668) USING ST_Multi(geom)")
            print("  naisui_coverage.geom を MultiPolygon にしました")
        cur.execute("SELECT 1 FROM datasets WHERE key=%s", (key,))
        if cur.fetchone() and not a.force:
            print(f"  {area}: 取り込み済み（--force で入れ直し）")
            return

    t0 = time.time()
    tiles = enumerate_tiles(pref, a.zoom, a.sleep)
    print(f"  z{a.zoom} のタイル {len(tiles)}枚（列挙 {time.time() - t0:.0f}s）")
    if not tiles:
        print("  タイルがありません。取り込みません")
        return

    today = dt.date.today().isoformat()
    with con.cursor() as cur:
        cur.execute("""INSERT INTO datasets(key,name,source_url,data_vintage,loaded_at,attribution,note)
                       VALUES (%s,%s,%s,%s,now(),%s,%s)
                       ON CONFLICT (key) DO UPDATE SET loaded_at=now(), data_vintage=EXCLUDED.data_vintage,
                         attribution=EXCLUDED.attribution, note=EXCLUDED.note""",
                    (key, f"重ねるハザードマップ 内水（雨水出水）浸水想定区域 {PREF_NAME[pref]}",
                     PORTAL, f"{today} 取得（重ねるハザードマップ配信分）",
                     ATTRIB.format(url=PORTAL, date=today),
                     "画像タイルを凡例の色で分類し、区分の下限をメートルで保存。細分の無い自治体では "
                     "0.3=「0.5m未満」、0.5=「0.5m以上3.0m未満」を指す。凡例に無い色は捨てている"))
        cur.execute("DELETE FROM naisui_depth WHERE dataset_key=%s", (key,))

    unknown, n_rect, n_tile, t1 = {}, 0, 0, time.time()
    cover = []
    for i, (x, y) in enumerate(sorted(tiles), 1):
        png = fetch(BASE.format(pref=pref, z=a.zoom, x=x, y=y))
        time.sleep(a.sleep)
        if png is None:
            continue
        n_tile += 1
        lon_a, lat_a = lonlat_of(x, y, a.zoom)
        lon_b, lat_b = lonlat_of(x + 1, y + 1, a.zoom)
        cover.append(f"(({lon_a} {lat_a},{lon_b} {lat_a},{lon_b} {lat_b},{lon_a} {lat_b},{lon_a} {lat_a}))")
        rects = tile_rects(png, x, y, a.zoom, unknown)
        if rects:
            with con.cursor() as cur:
                execute_values(cur,
                               "INSERT INTO naisui_depth(dataset_key, area, depth_m, geom) VALUES %s",
                               [(key, area, d, wkt) for d, wkt in rects],
                               template="(%s,%s,%s,ST_Multi(ST_GeomFromText(%s,6668)))", page_size=1000)
            n_rect += len(rects)
        if i % 50 == 0 or i == len(tiles):
            el = time.time() - t1
            print(f"  {i}/{len(tiles)} タイル  矩形{n_rect:,}  経過{el / 60:.1f}分  残り約{el / i * (len(tiles) - i) / 60:.0f}分", flush=True)

    with con.cursor() as cur:
        cur.execute("""INSERT INTO naisui_coverage(area, dataset_keys, loaded_at, geom)
                       VALUES (%s, ARRAY[%s], now(), ST_GeomFromText(%s, 6668))
                       ON CONFLICT (area) DO UPDATE SET dataset_keys=ARRAY[%s], loaded_at=now(), geom=EXCLUDED.geom""",
                    (area, key, "MULTIPOLYGON(" + ",".join(cover) + ")", key))
    con.close()
    print(f"\n完了: {area}  タイル{n_tile}枚 → 矩形{n_rect:,}")
    if unknown:
        print("  凡例に無い色（捨てた画素）:", sorted(unknown.items(), key=lambda kv: -kv[1])[:8])


if __name__ == "__main__":
    main()
