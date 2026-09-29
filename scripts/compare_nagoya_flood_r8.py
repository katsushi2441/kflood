"""名古屋市 洪水ハザードマップ(R8, BODIK CC BY) の浸水セルが、kflood の国データ(A31/A31b)で区域内かを抜き取りで比べる。
使い方: python scripts/compare_nagoya_flood_r8.py <duration.shp> [間引き=200]
市外は学区ポリゴン(gakku)の外として除く。区域外になった点は経緯度0.02度の升で数える。"""
import subprocess, sys, os, csv, io
import psycopg2
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
shp = sys.argv[1]; step = int(sys.argv[2]) if len(sys.argv) > 2 else 200
out = subprocess.run(['ogr2ogr', '-f', 'CSV', '/vsistdout/', shp,
                      '-dialect', 'sqlite', '-sql',
                      f'SELECT X(Centroid(geometry)) AS lon, Y(Centroid(geometry)) AS lat, max_dur FROM "{os.path.splitext(os.path.basename(shp))[0]}" WHERE ROWID % {step} = 0'],
                     capture_output=True, text=True, check=True).stdout
pts = [(float(r['lon']), float(r['lat'])) for r in csv.DictReader(io.StringIO(out))]
conn = psycopg2.connect(host='127.0.0.1', port=int(os.environ.get('KFLOOD_DB_PORT', '55434')), dbname='kflood',
                        user='postgres', password=os.environ.get('KFLOOD_DB_PASS', 'kflood_local'))
cur = conn.cursor()
hit = miss = outside_city = 0; miss_pts = []
for lon, lat in pts:
    # 名古屋市外（学区ポリゴンの外）は数えない
    cur.execute('SELECT 1 FROM gakku WHERE ST_Contains(geom, ST_Transform(ST_SetSRID(ST_Point(%s,%s),2449), ST_SRID(geom))) LIMIT 1', (lon, lat))
    if not cur.fetchone(): outside_city += 1; continue
    cur.execute('SELECT 1 FROM flood WHERE ST_Contains(geom, ST_Transform(ST_SetSRID(ST_Point(%s,%s),2449),6668)) LIMIT 1', (lon, lat))
    if cur.fetchone(): hit += 1
    else: miss += 1; miss_pts.append((lon, lat))
print(f'抜き取り {len(pts)} 点（{step}セルに1つ）、うち市外 {outside_city} を除く {hit+miss} 点: 国データでも区域内 {hit} / 国データでは区域外 {miss} ({miss/max(1,hit+miss):.0%})')
# 区域外になった点を（平面直角7系→経緯度に直して）、ざっくり経度・緯度 0.02度の升で数える
from collections import Counter
cur.execute('SELECT ST_X(g), ST_Y(g) FROM (SELECT ST_Transform(ST_SetSRID(ST_Point(x,y),2449),6668) g FROM unnest(%s::float8[], %s::float8[]) AS t(x,y)) q', ([p[0] for p in miss_pts] or [0], [p[1] for p in miss_pts] or [0]))
miss_ll = cur.fetchall() if miss_pts else []
c = Counter((round(lon/0.02)*0.02, round(lat/0.02)*0.02) for lon, lat in miss_ll)
for (lo, la), n in c.most_common(12): print(f'  lon {lo:.2f} lat {la:.2f}: {n}')
