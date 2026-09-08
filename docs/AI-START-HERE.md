# AI設置指示書（Claude Code / Codex / Cursor などに、このファイルを最初に読ませてください）

あなたは「Kurage 洪水・内水ハザードマップ (kflood)」を、利用者のサーバーに設置するAIエージェントです。
利用者に聞く前に、この手順で自分で進めてください。

## 1. 何を作るか

住所 → 洪水（河川氾濫）の浸水深・継続時間・家屋倒壊区域と、内水（下水道・水路）の浸水深を返す Web アプリ。
FastAPI ＋ PostGIS。データは国土交通省（国土数値情報 A31）と自治体（名古屋市 CC BY）の公開データ。

## 2. 手順（そのまま実行）

```bash
# 前提: Docker / Docker Compose v2 / Python 3.10+ / GDAL(ogr2ogr)
sudo apt install -y gdal-bin
cp .env.example .env               # 別ディスクがあれば KFLOOD_PG_DIR をそこに
docker compose up -d
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/load_a31.py 5236 5237      # まず名古屋周辺。全国は all（1〜2時間・約60GB）
.venv/bin/python scripts/load_nagoya_naisui.py      # 名古屋市の内水
.venv/bin/python -m pytest -q tests
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 18386 &
curl http://127.0.0.1:18386/healthz
curl 'http://127.0.0.1:18386/api/check?q=愛知県名古屋市中川区富田町大字千音寺'
```

`/api/check` が `national.max.label` に「0.5m以上3.0m未満」を返し、`datasets` に時点が入っていれば成功です。

## 3. 常駐と公開

- 常駐: `systemd/kflood.service` の `WorkingDirectory` と `ExecStart` のパスを設置先に直し、`systemctl --user enable --now kflood.service`
- 公開: PHP が動くサーバーに `php/kflood.php` と `kflood_config.php`（`define("KFLOOD_BACKEND","http://設置先:18386")`）を置く。公開URLは `/kflood.php/`（末尾スラッシュ）
- ポートを変えるときは `.env` の `KFLOOD_PORT` と unit の両方

## 4. 他の自治体の内水を足す

`scripts/load_nagoya_naisui.py` の `DATASETS` に1件追加（URL・浸水深の属性名・座標系・出典表記・時点・`area`=自治体名）。
判定は住所にその自治体名が含まれるときだけ動く。画面・APIの変更は不要。

## 5. 変えてはいけないこと

- `app/codes.py` の数字（浸水深ランク・継続時間ランク・危険区域区分）。国土数値情報のコードリストそのもの
- 「未収録」と「区域外」の区別、データ時点の表示、250m 以内の注意書き。安全側の設計
- 出典表記（国土数値情報利用約款・CC BY の要件）

## 6. うまくいかないとき

| 症状 | 見るところ |
| --- | --- |
| `datasets` が空で 503 | 取り込みが完了していない。`load_a31.py` のログ |
| ogr2ogr が無い | `apt install gdal-bin`（GDAL 3.4 で確認） |
| 住所が見つからない 404 | 国土地理院の住所検索は都道府県から書く |
| 判定が遅い | `flood_geom_idx`（GIST）が張られているか `\d flood` で確認 |
