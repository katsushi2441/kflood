"""判定結果の画面をスマホ幅で撮る（地点指定）。使い方: python scripts/shot_check.py <lat> <lon> <出力png>"""
import sys
from playwright.sync_api import sync_playwright
lat, lon, out = sys.argv[1], sys.argv[2], sys.argv[3]
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={'width': 390, 'height': 1400})
    pg.goto('http://127.0.0.1:18386/')
    pg.evaluate(f"fetch('api/check?lat={lat}&lon={lon}').then(r=>r.json()).then(render)")
    pg.wait_for_timeout(2500)
    print('scrollWidth', pg.evaluate('document.documentElement.scrollWidth'))
    el = pg.query_selector('h2:has-text("洪水")')
    if el: el.scroll_into_view_if_needed()
    pg.screenshot(path=out, full_page=False); b.close()
