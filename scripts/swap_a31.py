#!/usr/bin/env python3
"""国の洪水データ（国土数値情報 A31/A31b）を新しい版へ入れ替える。

  1. KFLOOD_FLOOD_TABLE=flood_new で scripts/load_a31b.py all を終えておく
  2. python scripts/swap_a31.py --old-prefix A31-22 --new-prefix A31b-25
     → flood を flood_old に、flood_new を flood に付け替え、旧版の datasets 行を消し、a31 タイルのキャッシュを捨てる
  3. .env の KFLOOD_A31_PREFIX を新しい接頭辞にして kflood.service を再起動
flood_old は確認が済んだら DROP TABLE flood_old で消す（約20GB）。
"""
import argparse
import os
import shutil
import sys

import psycopg2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = dict(host='127.0.0.1', port=int(os.environ.get('KFLOOD_DB_PORT', '55434')), dbname='kflood', user='postgres',
          password=os.environ.get('KFLOOD_DB_PASS', 'kflood_local'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--old-prefix', required=True, help='今 flood に入っている版の datasets.key 接頭辞（例 A31-22）')
    ap.add_argument('--new-prefix', required=True, help='flood_new に入っている版の接頭辞（例 A31b-25）')
    ap.add_argument('--new-table', default='flood_new')
    ap.add_argument('--yes', action='store_true', help='確認なしで実行')
    a = ap.parse_args()
    conn = psycopg2.connect(**DB)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM datasets WHERE key LIKE %s", (a.new_prefix + '_%',))
    n_new = cur.fetchone()[0]
    cur.execute(f"SELECT count(DISTINCT mesh), count(*) FROM {a.new_table}")
    m_new, p_new = cur.fetchone()
    cur.execute("SELECT count(*) FROM datasets WHERE key LIKE %s", (a.old_prefix + '_%',))
    n_old = cur.fetchone()[0]
    print(f'{a.new_table}: メッシュ {m_new} / 面 {p_new:,} / datasets {n_new} 件（{a.new_prefix}）')
    print(f'flood（現行）: datasets {n_old} 件（{a.old_prefix}）')
    if p_new == 0 or n_new == 0:
        print('新しい表が空です。先に load_a31b.py を終えてください'); sys.exit(1)
    if not a.yes and input('入れ替えますか？ [y/N] ').strip().lower() != 'y':
        sys.exit(0)
    cur.execute('DROP TABLE IF EXISTS flood_old')
    cur.execute('ALTER TABLE flood RENAME TO flood_old')
    cur.execute(f'ALTER TABLE {a.new_table} RENAME TO flood')
    cur.execute('DELETE FROM datasets WHERE key LIKE %s', (a.old_prefix + '_%',))
    # meshes 表は新しい表に入っているメッシュだけにする（未収録の判定を正しく保つ）
    cur.execute('DELETE FROM meshes WHERE mesh NOT IN (SELECT DISTINCT mesh FROM flood)')
    conn.commit()
    tiles = os.path.join(ROOT, 'data', 'tiles', 'a31')
    if os.path.isdir(tiles):
        shutil.rmtree(os.path.realpath(tiles), ignore_errors=True)
        print(f'タイルキャッシュを捨てました: {tiles}')
    print(f'入れ替え完了。.env の KFLOOD_A31_PREFIX={a.new_prefix} にして systemctl --user restart kflood.service')
    print('その後 scripts/warm_tiles.py でタイルを温め直し、確認が済んだら DROP TABLE flood_old')


if __name__ == '__main__':
    main()
