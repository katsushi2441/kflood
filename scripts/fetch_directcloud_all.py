#!/usr/bin/env python3
"""DirectCloud のリンクの中身を全部選んで、1つの zip で受け取る（fetch_directcloud.py の続き）。

  /usr/bin/python3 scripts/fetch_directcloud_all.py <リンクURL> <保存先フォルダ> [ファイル名の一部]
ファイルが2本以上あると、全選択しても1本ずつ別のダウンロードになり、最初の1本しか受け取れない（2026-10-19）。
そのときはファイル名の一部を渡して1本ずつ受け取る。
"""
import os, sys
from playwright.sync_api import sync_playwright

url, out = sys.argv[1], sys.argv[2]
want = sys.argv[3] if len(sys.argv) > 3 else None
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
    if want:
        # 行の作りが分からないので、チェックボックスから親をたどり、その名前を含む一番近い要素の箱を選ぶ
        i = pg.evaluate("""w => { const bs=[...document.querySelectorAll('input[type=checkbox]')];
            for (let k=1;k<bs.length;k++){ let e=bs[k]; for(let d=0;d<6&&e;d++){ e=e.parentElement; if(e&&e.textContent.includes(w)&&!bs.some((o,j)=>j!==k&&e.contains(o))) return k; } } return -1; }""", want)
        if i < 0: raise SystemExit("その名前の行が見つからない: " + want)
        boxes.nth(i).check(force=True)
    else:
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
