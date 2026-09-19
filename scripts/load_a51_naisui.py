#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国土数値情報 A51（雨水出水＝内水）浸水想定区域を取り込む。

**名古屋市の内水は市の CC BY データで入れている**（load_nagoya_naisui.py）。
こちらは国が出している全国版だが、**中身は全国ではない**。

実測（2026-09-20・2024年度版）:
- 配信されている都道府県は **9**（埼玉・東京・神奈川・富山・福井・長野・愛知・広島・福岡）
- その中に入っている市区町村は **合計10**（川越市・福生市・綾瀬市・富山市・小浜市・
  長野市・豊山町・広島市・大牟田市・福岡市）。愛知県は豊山町だけで、名古屋市は無い
- 一方、国のハザードマップポータルで内水マップを公開している自治体は **459**

つまり **公開されているうち、住所から機械判定できる形で出ているのは 2.2%** しかない。
この道具の対応範囲が狭いのはそのためで、取り込む側の都合ではない。

属性: A51_001=都道府県 / A51_003=市区町村 / A51_004=団体コード / A51_005=浸水深の区分（文字列）
**浸水深は数値ではなく「0.3m未満」のような区分の文字列**なので、区分の下限を数値に直して入れる
（上限で入れると危険側に見積もりすぎる。判定は「◯m以上」と読ませる）。

  python3 scripts/load_a51_naisui.py            # 9都府県ぶん
  python3 scripts/load_a51_naisui.py --force    # 入れ直し
"""
import argparse
import io
import json
import os
import re
import sys
import time
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE = "https://nlftp.mlit.go.jp/ksj/gml/data/A51/A51-24"
REFERER = "https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-A51-2024.html"
PREFS = {"11": "埼玉県", "13": "東京都", "14": "神奈川県", "16": "富山県", "18": "福井県",
         "20": "長野県", "23": "愛知県", "34": "広島県", "40": "福岡県"}
VINTAGE = "2024年度（令和6年度）版"
ATTRIB = ("出典: 国土数値情報（雨水出水（内水）浸水想定区域データ A51）国土交通省 を加工して作成")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")


def depth_min_m(label: str):
    """区分の文字列から下限(m)を取る。「0.3m未満」→0.0、「0.3m以上0.5m未満」→0.3。

    **上限で入れない。** 区分の上側で入れると、実際より深い想定として出てしまう。
    """
    s = str(label or "")
    ms = re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*m\s*以上", s)
    if ms:
        return float(ms[0])
    if "未満" in s:
        return 0.0
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", s)
    return float(m.group(1)) if m else None


def fetch(pref: str, cache: str) -> bytes:
    path = os.path.join(cache, f"A51-24_{pref}_GML.zip")
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return open(path, "rb").read()
    req = urllib.request.Request(f"{BASE}/A51-24_{pref}_GML.zip",
                                 headers={"User-Agent": UA, "Referer": REFERER})
    with urllib.request.urlopen(req, timeout=300) as r:
        b = r.read()
    os.makedirs(cache, exist_ok=True)
    open(path, "wb").write(b)
    return b


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--cache", default=os.path.join(ROOT, "outputs", "a51"))
    a = ap.parse_args()

    import psycopg2
    from psycopg2.extras import execute_values
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    from load_nagoya_naisui import DDL, pg_dsn      # 同じ接続とスキーマを使う

    con = psycopg2.connect(pg_dsn())
    con.autocommit = True
    with con.cursor() as cur:
        cur.execute(DDL)

    total_area, total_face = 0, 0
    for pref, pname in PREFS.items():
        key = f"a51_naisui_{pref}"
        with con.cursor() as cur:
            cur.execute("SELECT 1 FROM datasets WHERE key=%s", (key,))
            if cur.fetchone() and not a.force:
                print(f"  {pname}: 取り込み済み（--force で入れ直し）")
                continue
        t0 = time.time()
        z = zipfile.ZipFile(io.BytesIO(fetch(pref, a.cache)))
        rows, areas = [], {}
        for n in [x for x in z.namelist() if x.endswith(".geojson")]:
            d = json.load(io.TextIOWrapper(z.open(n), encoding="utf-8"))
            for f in d["features"]:
                p = f["properties"]
                area = f"{p.get('A51_001','')}{p.get('A51_003','')}"
                dm = depth_min_m(p.get("A51_005"))
                if dm is None or not f.get("geometry"):
                    continue
                areas[area] = p.get("A51_005")
                rows.append((key, area, dm, json.dumps(f["geometry"])))
        if not rows:
            print(f"  {pname}: 面がありません")
            continue
        with con.cursor() as cur:
            cur.execute("""INSERT INTO datasets(key,name,source_url,data_vintage,loaded_at,attribution,note)
                           VALUES (%s,%s,%s,%s,now(),%s,%s)
                           ON CONFLICT (key) DO UPDATE SET loaded_at=now(), data_vintage=EXCLUDED.data_vintage""",
                        (key, f"国土数値情報 雨水出水（内水）浸水想定区域 A51 {pname}",
                         REFERER, VINTAGE, ATTRIB,
                         "浸水深は区分の文字列なので、区分の下限をメートルに直して保存している"))
            cur.execute("DELETE FROM naisui_depth WHERE dataset_key=%s", (key,))
            execute_values(cur,
                           "INSERT INTO naisui_depth(dataset_key, area, depth_m, geom) VALUES %s",
                           rows,
                           template="(%s,%s,%s,ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s),6668)))",
                           page_size=500)
            for area in areas:
                cur.execute("""INSERT INTO naisui_coverage(area, dataset_keys, loaded_at, geom)
                               SELECT %s, ARRAY[%s], now(),
                                      ST_Envelope(ST_Extent(geom))::geometry(Polygon,6668)
                               FROM naisui_depth WHERE area=%s
                               ON CONFLICT (area) DO UPDATE SET
                                 dataset_keys = array_append(array_remove(naisui_coverage.dataset_keys, %s), %s),
                                 loaded_at=now(), geom=EXCLUDED.geom""",
                            (area, key, area, key, key))
        total_area += len(areas)
        total_face += len(rows)
        print(f"  {pname}: {len(areas)}市区町村 / {len(rows):,}面（{time.time() - t0:.0f}s）"
              f"  {'・'.join(list(areas)[:4])}")
    con.close()
    print(f"\n合計 {total_area}市区町村 / {total_face:,}面")
    print("※ 国のポータルで内水マップを公開している自治体は459。機械判定できる形で"
          "出ているのはこの10市区町村だけ（2.2%）")


if __name__ == "__main__":
    main()
