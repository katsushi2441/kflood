#!/usr/bin/env python3
"""名古屋市の範囲で内水・A31のベクタータイルを先に生成してディスクに置く（初回アクセスの待ちを無くす）。
  python3 scripts/warm_tiles.py [--base http://127.0.0.1:18386] [--zmin 12] [--zmax 15]"""
import argparse, math, sys, time, urllib.request
ap = argparse.ArgumentParser(); ap.add_argument('--base', default='http://127.0.0.1:18386'); ap.add_argument('--zmin', type=int, default=12); ap.add_argument('--zmax', type=int, default=15)
ap.add_argument('--layers', default='naisui,a31'); a = ap.parse_args()
LA0, LA1, LO0, LO1 = 35.03, 35.27, 136.79, 137.07   # 名古屋市の外接矩形
def t(lat, lon, z):
    n = 2 ** z; x = (lon + 180) / 360 * n
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n
    return int(x), int(y)
n = ok = 0; t0 = time.time()
for layer in a.layers.split(','):
    for z in range(a.zmin, a.zmax + 1):
        x0, y1 = t(LA0, LO0, z); x1, y0 = t(LA1, LO1, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                n += 1
                try:
                    r = urllib.request.urlopen(f'{a.base}/tiles/{layer}/{z}/{x}/{y}.pbf', timeout=120); ok += 1
                except Exception as e:
                    print('ERR', layer, z, x, y, str(e)[:60], flush=True)
        print(f'{layer} z{z} done ({n} req, {time.time()-t0:.0f}s)', flush=True)
print('warm done', ok, '/', n)
