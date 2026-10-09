#!/usr/bin/env python3
"""DirectCloud のリンクの中身を全部選んで、1つの zip で受け取る（fetch_directcloud.py の続き）。

  /usr/bin/python3 scripts/fetch_directcloud_all.py <リンクURL> <保存先フォルダ>
"""
import os, sys
from playwright.sync_api import sync_playwright

url, out = sys.argv[1], sys.argv[2]
pw = open(os.path.join(out, ".pw")).read().strip()
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(accept_downloads=True, locale="ja-JP")
    pg = ctx.new_page()
    pg.goto(url, wait_until="networkidle", timeout=60000)
    pg.wait_for_selector("input[type=password]", timeout=30000)
    pg.fill("input[type=password]", pw)
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(6000)
    boxes = pg.locator("input[type=checkbox]")
    print("チェックボックス", boxes.count())
    boxes.first.check(force=True)          # 見出しの「全部選ぶ」
    pg.wait_for_timeout(1000)
    pg.screenshot(path=os.path.join(out, "selected.png"), full_page=True)
    with pg.expect_download(timeout=40 * 60 * 1000) as dl:
        pg.get_by_text("ダウンロード", exact=True).first.click()
    d = dl.value
    dest = os.path.join(out, d.suggested_filename or "directcloud.zip")
    d.save_as(dest)
    print("保存", dest, os.path.getsize(dest))
    b.close()
