"""地域ページ（/area/...）をスマホ幅で開いて、横にはみ出していないか確かめる。

  .venv/bin/python scripts/shot_area.py [URL ...]   # 既定は公開URLの代表数本。outputs/shot_area_*.png に保存
"""
import sys
from playwright.sync_api import sync_playwright

BASE = 'https://kurage.exbridge.jp/kflood.php/'
URLS = sys.argv[1:] or [BASE + p for p in ('area/', 'area/pref/23', 'area/aichi-nagoya', 'area/14210', 'area/39428', 'area/13101')]
with sync_playwright() as p:
    b = p.chromium.launch()
    for w in (320, 390):
        pg = b.new_page(viewport={'width': w, 'height': 900})
        for u in URLS:
            pg.goto(u, wait_until='networkidle')
            sw = pg.evaluate('document.documentElement.scrollWidth')
            wide = pg.evaluate("""() => [...document.querySelectorAll('body *')].filter(e => {const r = e.getBoundingClientRect();
                return r.right > window.innerWidth + 1 && !e.closest('.tscroll')}).slice(0, 5).map(e => e.tagName + '.' + e.className)""")
            name = u.rstrip('/').split('kflood.php/')[-1].replace('/', '_') or 'top'
            if w == 390:
                pg.screenshot(path=f'outputs/shot_area_{name}.png', full_page=True)
            print(w, u, 'scrollWidth', sw, 'OK' if sw <= w else 'OVERFLOW', wide)
        pg.close()
    b.close()
