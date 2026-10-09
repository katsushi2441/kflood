#!/usr/bin/env python3
"""愛知県河川課から DirectCloud のリンクで届いた洪水浸水想定区域図データを受け取る（2026-10-09）。

  /usr/bin/python3 scripts/fetch_directcloud.py <リンクURL> <保存先フォルダ> [--list]

パスワードは保存先フォルダの .pw（メールから取り出して置く。表示しない・リポジトリに入れない）。
使い捨てのヘッドレス Chromium で開く（共有の chrome-profile は使わない）。リンクはアクセス回数に上限がある。
"""
import json, os, sys
from playwright.sync_api import sync_playwright

url, out = sys.argv[1], sys.argv[2]
pw = open(os.path.join(out, ".pw")).read().strip()
log = []
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(accept_downloads=True, locale="ja-JP")
    pg = ctx.new_page()
    def on_resp(r):
        if "/openapi/" in r.url or "/v2/link/" in r.url or "/v1/link/" in r.url:
            try:
                body = r.text()[:4000]
            except Exception:
                body = ""
            log.append({"url": r.url, "status": r.status, "body": body})
    pg.on("response", on_resp)
    pg.goto(url, wait_until="networkidle", timeout=60000)
    pg.wait_for_selector("input[type=password]", timeout=30000)
    pg.fill("input[type=password]", pw)
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(6000)
    pg.screenshot(path=os.path.join(out, "after_login.png"), full_page=True)
    open(os.path.join(out, "page_text.txt"), "w").write(pg.inner_text("body"))
    b.close()
json.dump(log, open(os.path.join(out, "network.json"), "w"), ensure_ascii=False, indent=1)
for x in log:
    print(x["status"], x["url"])
