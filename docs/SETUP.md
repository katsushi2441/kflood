# Kurage 洪水・内水ハザードマップ — 設置手順

住所から洪水・内水の浸水想定を返すシステムを、自社のサーバーに置く手順です。

## 用意するもの

| 項目 | 目安 |
| --- | --- |
| OS | Linux（Ubuntu 22.04 で動作確認） |
| CPU / メモリ | 2コア・4GB以上（取り込み中は ogr2ogr が 1コアを使います） |
| ディスク | 全国分で約 60GB（配布 zip 約10GB ＋ PostGIS）。名古屋周辺の2メッシュだけなら約 6GB |
| ソフト | Docker、Docker Compose v2、Python 3.10以上、GDAL（`ogr2ogr`） |
| 通信 | 国土交通省・名古屋市のサイトからデータを取得します（初回のみ） |

外部の有料APIは使いません。ランニングコストはサーバー代だけです。

## 1. データベースを立てる

```bash
cd kflood
cp .env.example .env      # KFLOOD_PG_DIR を別ディスクにするならここで
docker compose up -d
```

PostGIS が `127.0.0.1:55434` で起動します。パスワードは `KFLOOD_DB_PASS` で変えられます（既定 `kflood_local`）。
**外部に公開する場合は必ず変えてください。**

## 2. Python の環境を作る

```bash
sudo apt install -y gdal-bin
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 3. データを取り込む

まず名古屋周辺の1次メッシュで試すことをおすすめします（5236 = 名古屋市の大部分、5237 = 三河）。

```bash
.venv/bin/python scripts/load_a31.py 5236 5237
.venv/bin/python scripts/load_nagoya_naisui.py
```

全国分は次のとおりです。回線にもよりますが 1〜2 時間かかります。途中で止めても再実行すれば続きから入ります。

```bash
.venv/bin/python scripts/load_a31.py all
```

取り込みが終わると、使ったデータの出典と時点が `datasets` 表に、取り込んだ1次メッシュが `meshes` 表に記録されます。
**この記録が無い場所は「未収録」と返し、「区域外」とは言いません。**

### 1次メッシュ番号の求め方

緯度を1.5倍した整数（2桁）＋ 経度から100を引いた整数（2桁）。名古屋（北緯35.17・東経136.9）なら 52 と 36 で `5236`。
都道府県コード（愛知=23）を渡すと、その県を含む矩形のメッシュをまとめて取り込みます。

## 4. 起動する

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 18386
```

常駐させる場合は `systemd/kflood.service` を使います（パスは環境に合わせて直してください）。

```bash
cp systemd/kflood.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kflood.service
```

## 5. 動作を確認する

```bash
curl http://127.0.0.1:18386/healthz
curl 'http://127.0.0.1:18386/api/check?q=愛知県名古屋市中川区富田町大字千音寺'
```

取り込んだメッシュ数・面数・データ時点が返れば成功です。

## 6. レンタルサーバーから公開する（任意）

`php/kflood.php` は、PHP が動くレンタルサーバーから自社サーバーの :18386 へ中継するためのものです。
同じディレクトリに `kflood_config.php` を置きます。

```php
<?php
define("KFLOOD_BACKEND", "http://あなたのサーバー:18386");
```

公開URLは `https://あなたのドメイン/kflood.php/`（末尾スラッシュ）です。

## 7. データの更新

国土数値情報は年度ごとに版が上がります。`scripts/load_a31.py all --force` で入れ直せます。
名古屋市の内水は市が改定したら `scripts/load_nagoya_naisui.py --force` を実行してください。

## 他の自治体の内水を足す

`scripts/load_nagoya_naisui.py` の `DATASETS` に1件足します。必要なのは配布 zip の URL・浸水深の属性名・
座標系（.prj があれば自動）・出典表記・データ時点です。`area` にその自治体名を入れると、住所にその名前が含まれる
ときだけ内水を判定します。
