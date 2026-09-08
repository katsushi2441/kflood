#!/usr/bin/env python3
"""OGP画像 1200×630（ライトテーマ・中央寄せ・文字大きく・マスコット入り・成長する数字は焼き込まない）。
  /usr/bin/python3 scripts/make_ogp.py  → app/static/ogp.png（アプリが /ogp.png で配信）
"""
import os

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "app", "static", "ogp.png")
MASCOT = "/home/kojima/work/kurage_web/images/kurage-mascot-cutout.png"
FB = "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc"
FM = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
FR = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
W, H = 1200, 630
img = Image.new("RGB", (W, H), "#ffffff")
dr = ImageDraw.Draw(img, "RGBA")
dr.ellipse([-220, -260, 440, 340], fill=(230, 244, 242, 255))
dr.ellipse([W - 420, H - 300, W + 260, H + 280], fill=(240, 246, 246, 255))
mascot = Image.open(MASCOT).convert("RGBA") if os.path.exists(MASCOT) else None
cx = 500 if mascot else W // 2
f_badge, f_h, f_h2, f_s, f_brand = (ImageFont.truetype(FM, 26), ImageFont.truetype(FB, 58), ImageFont.truetype(FB, 44),
                                     ImageFont.truetype(FR, 26), ImageFont.truetype(FM, 30))
badge = "住所 → 洪水・内水の浸水想定  データ時点つき"
bw = dr.textlength(badge, font=f_badge) + 44
dr.rounded_rectangle([cx - bw / 2, 84, cx + bw / 2, 132], radius=24, fill="#e6f4f2", outline="#bfe3de")
dr.text((cx, 108), badge, font=f_badge, fill="#0a726b", anchor="mm")
dr.text((cx, 210), "何メートル・何日、浸かるのか。", font=f_h, fill="#12202f", anchor="mm")
dr.text((cx, 288), "洪水と内水を、住所ひとつで。", font=f_h2, fill="#0a9a8f", anchor="mm")
dr.text((cx, 360), "想定最大規模・計画規模の浸水深、浸水継続時間、家屋倒壊等氾濫想定区域、", font=f_s, fill="#5d6b7a", anchor="mm")
dr.text((cx, 398), "内水の浸水深。行動の目安とマイ・タイムラインまで。", font=f_s, fill="#5d6b7a", anchor="mm")
chips = ["全国の洪水（国土数値情報）", "内水は名古屋市版を同梱", "CSV一括判定"]
f_chip = ImageFont.truetype(FM, 22)
widths = [dr.textlength(c, font=f_chip) + 36 for c in chips]
x = cx - (sum(widths) + 14 * (len(chips) - 1)) / 2
for c, w in zip(chips, widths):
    dr.rounded_rectangle([x, 440, x + w, 484], radius=22, fill="#ffffff", outline="#0a9a8f", width=2)
    dr.text((x + w / 2, 462), c, font=f_chip, fill="#0a726b", anchor="mm")
    x += w + 14
dr.rounded_rectangle([cx - 250, 520, cx + 250, 572], radius=26, fill="#0a9a8f")
dr.text((cx, 546), "Kurage 洪水・内水ハザードマップ", font=f_brand, fill="#ffffff", anchor="mm")
if mascot:
    mh = 300
    m = mascot.resize((int(mascot.width * mh / mascot.height), mh))
    img.paste(m, (W - m.width - 40, H - m.height - 30), m)
dr.text((40, H - 34), "kurage.exbridge.jp/kflood.php/", font=ImageFont.truetype(FR, 22), fill="#5d6b7a", anchor="lm")
img.save(OUT, optimize=True)
print(OUT, img.size)
