"""ほかのハザード判定（土砂災害・津波・避難所）を、同じ住所について横から尋ねる。

**なぜ要るか**: 内閣府「避難情報に関するガイドライン」（令和8年3月改定）では、
自宅に留まる「屋内安全確保」が選べるのは**洪水等と高潮だけ**で、しかも
  ❶家屋倒壊等氾濫想定区域に存していないこと
  ❷浸水しない居室があること
  ❸一定期間の浸水による支障を許容できること
の3条件を満たす場合に限られる。土砂災害・津波は立退き避難が基本である。
つまり洪水の想定だけでは「とるべき行動」を出しきれない。

**製品として売るときの既定はオフ**。環境変数で相手先を指定したときだけ尋ねる。
買い切り版を入れたお客さまの環境から当社のサーバーへ勝手に問い合わせない
ため、既定値は空にしてある（自社の設置では .env で 127.0.0.1 を指す）。

    KFLOOD_KHAZARD_API=http://127.0.0.1:18376    # 土砂災害
    KFLOOD_KTSUNAMI_API=http://127.0.0.1:18380   # 津波
    KFLOOD_KREFUGE_API=http://127.0.0.1:18378    # 指定緊急避難場所

**落ちても止めない**。どれかが応答しなくても判定そのものは洪水・内水だけで
成立するので、失敗は None にして画面から黙って消す（「区域外」とは言わない）。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request

TIMEOUT = float(os.environ.get('KFLOOD_SIBLING_TIMEOUT', '3.0'))
CACHE_SEC = int(os.environ.get('KFLOOD_SIBLING_CACHE', '300'))

APIS = {
    'dosha': os.environ.get('KFLOOD_KHAZARD_API', '').rstrip('/'),
    'tsunami': os.environ.get('KFLOOD_KTSUNAMI_API', '').rstrip('/'),
    'refuge': os.environ.get('KFLOOD_KREFUGE_API', '').rstrip('/'),
    # 重要事項説明の災害項目で使う。どちらも「規則16条の4の3」の4項目そのものではないが、
    # 別の号で説明が要る指定なので、同じ紙に載せる（取り違えないよう名前をそのまま出す）。
    'morido': os.environ.get('KFLOOD_KMORIDO_API', '').rstrip('/'),      # 宅地造成等工事規制区域・特定盛土等規制区域（A56）
    'riskarea': os.environ.get('KFLOOD_KRISKAREA_API', '').rstrip('/'),  # 災害危険区域（建築基準法39条・A48）
}

_cache: dict = {}
_lock = threading.Lock()


def enabled() -> bool:
    return any(APIS.values())


def _get(url: str):
    req = urllib.request.Request(url, headers={'User-Agent': 'kflood/timeline'})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def _dosha(lat: float, lon: float):
    """土砂災害。**区域内かどうかだけでなく、近いときの距離も返す**（代表点のずれを言い切らないため）。"""
    base = APIS['dosha']
    if not base:
        return None
    d = _get(f'{base}/api/check?lat={lat}&lon={lon}')
    if not d.get('judged'):
        return None
    zones = d.get('zones') or []
    kinds = {z.get('zone_kind') for z in zones}
    return dict(
        inside=bool(d.get('in_hazard_zone')),
        special=bool(kinds & {2, 4}),          # 特別警戒区域（レッドゾーン）
        labels=sorted({z.get('zone_kind_label', '') for z in zones} - {''}),
        phenomena=sorted({z.get('phenomenon_label', '') for z in zones} - {''}),
        nearest_m=(d.get('nearest') or {}).get('distance_m'),
        vintage=d.get('data_as_of'),
    )


def _tsunami(lat: float, lon: float):
    base = APIS['tsunami']
    if not base:
        return None
    d = _get(f'{base}/api/check?lat={lat}&lon={lon}')
    if d.get('inundated') is None:
        return None
    return dict(
        inside=bool(d.get('inundated')),
        depth_label=d.get('depth_label'),
        elevation_m=(d.get('elevation') or {}).get('m'),
        vintage=d.get('data_vintage'),
    )


def _refuge(lat: float, lon: float, hazard: str = 'flood'):
    """指定緊急避難場所。**その災害に対応している場所だけ**を近い順に返す。"""
    base = APIS['refuge']
    if not base:
        return None
    d = _get(f'{base}/api/check?lat={lat}&lon={lon}&hazard={urllib.parse.quote(hazard)}')
    sh = d.get('shelters') or []
    if not sh:
        return None
    return dict(
        hazard_label=d.get('hazard_label'),
        walk_basis=d.get('walk_basis'),
        vintage=d.get('data_vintage'),
        items=[dict(name=s.get('name'), address=s.get('address'),
                    walk_minutes=s.get('walk_minutes'), distance_m=s.get('distance_m'),
                    hazards=s.get('hazards') or []) for s in sh[:3]],
    )


def probe(lat: float, lon: float, hazard: str = 'flood') -> dict:
    """3つまとめて尋ねる。**1つ落ちても他は返す。**

    避難先は「その災害に対応している場所」でなければ意味がないので、
    先に土砂・津波を判定してから、**その場所でいちばん重い災害**で絞って探す。
    洪水対応の避難所を土砂災害警戒区域の人に勧めてはいけない。
    """
    out = dict(dosha=None, tsunami=None, refuge=None, asked=enabled())
    if not enabled() or lat is None or lon is None:
        return out
    key = (round(lat, 5), round(lon, 5), hazard)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_SEC:
            return hit[1]
    for name, fn in (('dosha', _dosha), ('tsunami', _tsunami)):
        try:
            out[name] = fn(lat, lon)
        except Exception:  # noqa: BLE001  取れないだけ。判定は止めない
            out[name] = None
    # 立退き避難しかない災害（土砂・津波）が該当するなら、その災害で避難先を絞る
    if out['dosha'] and out['dosha'].get('inside'):
        hazard = 'landslid'
    elif out['tsunami'] and out['tsunami'].get('inside'):
        hazard = 'tsunami'
    try:
        out['refuge'] = _refuge(lat, lon, hazard)
    except Exception:  # noqa: BLE001
        out['refuge'] = None
    with _lock:
        _cache[key] = (now, out)
        if len(_cache) > 2000:
            _cache.clear()
    return out


def _simple(base_key: str, lat: float, lon: float):
    """kmorido / kriskarea は同じ形（status, areas, data_vintage）で返す。"""
    base = APIS[base_key]
    if not base:
        return None
    d = _get(f'{base}/api/check?lat={lat}&lon={lon}')
    if 'status' not in d:
        return None
    areas = d.get('areas') or []
    names = []
    for a in areas:
        for k in ('area_type', 'name', 'zone_name', 'title'):
            if a.get(k):
                names.append(str(a[k]))
                break
    return dict(inside=(d.get('status') == 'inside'), status=d.get('status'),
                labels=sorted(set(names)), nearest_m=d.get('nearest_m'),
                vintage=d.get('data_vintage') or d.get('data_as_of'),
                attribution=d.get('attribution'))


def probe_juyo(lat: float, lon: float) -> dict:
    """重要事項説明の災害項目のために、必要な判定先だけを尋ねる。

    **避難先（krefuge）は聞かない。** 重説の紙に避難所は要らない。
    落ちた相手は None のままにして、画面で「取得できず」と出す。
    **「取得できなかった」を「該当なし」と書かない**のがこの紙のいちばん大事な決まり。
    """
    out = dict(dosha=None, tsunami=None, morido=None, riskarea=None)
    for name, fn in (('dosha', _dosha), ('tsunami', _tsunami)):
        try:
            out[name] = fn(lat, lon)
        except Exception:  # noqa: BLE001
            out[name] = None
    for name in ('morido', 'riskarea'):
        try:
            out[name] = _simple(name, lat, lon)
        except Exception:  # noqa: BLE001
            out[name] = None
    return out
