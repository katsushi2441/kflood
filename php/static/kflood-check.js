/* Kurage 洪水ハザードマップ — ブラウザだけで住所を判定する部品（当社のサーバーを使わない）。
 *
 *   KFloodCheck.check('名古屋市北区辻町').then(r => ...)
 *   KFloodCheck.checkPoint(35.21, 136.92).then(r => ...)
 *
 * - 位置: 国土地理院の住所検索（msearch.gsi.go.jp・CORS 可）
 * - 洪水: 重ねるハザードマップの画像タイル（disaportaldata.gsi.go.jp・CORS 可）を canvas で読み、その地点の色で判定する。
 *   色の対応は国交省「水害ハザードマップ作成の手引き」表3-2（詳細版を含む）と、重ねるハザードマップの公式凡例（docs/heteml-only-plan.md）。
 *   区域の無いタイルは 404（＝区域外）。読めなかったときは「分からない」と返し、「区域外」とは言わない。
 */
(function (root) {
  'use strict';
  var Z = 17;                       // 地理院のハザードタイルの最大ズーム（1画素 ≒ 1m前後）
  var BASE = 'https://disaportaldata.gsi.go.jp/raster/';
  var DEPTH = {                     // 想定最大規模の浸水深
    '255,255,179': ['0.3m未満', 0.15], '247,245,169': ['0.5m未満', 0.3], '248,225,166': ['0.5〜1m', 0.75],
    '255,216,192': ['0.5〜3m', 2], '255,183,183': ['3〜5m', 4], '255,145,145': ['5〜10m', 7.5],
    '242,133,201': ['10〜20m', 15], '220,122,220': ['20m以上', 20]
  };
  var DURATION = {                  // 浸水継続時間（想定最大規模）
    '160,210,255': '12時間未満', '0,65,255': '12時間〜1日', '250,245,0': '1日〜3日', '255,153,0': '3日〜1週間',
    '255,40,0': '1週間〜2週間', '180,0,104': '2週間以上', '96,0,96': '4週間以上'
  };
  var cache = {};

  function tileXY(lat, lon, z) {
    var n = Math.pow(2, z), x = (lon + 180) / 360 * n;
    var r = lat * Math.PI / 180;
    var y = (1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * n;
    return { x: Math.floor(x), y: Math.floor(y), px: Math.floor((x - Math.floor(x)) * 256), py: Math.floor((y - Math.floor(y)) * 256) };
  }

  /** タイルの画素を読む。戻り値: [r,g,b,a]（区域なしは null）。読めなかったら例外 */
  function pixel(layer, lat, lon) {
    var t = tileXY(lat, lon, Z), url = BASE + layer + '/' + Z + '/' + t.x + '/' + t.y + '.png';
    var p = cache[url] || (cache[url] = fetch(url, { mode: 'cors' }).then(function (res) {
      if (res.status === 404) { return null; }
      if (!res.ok) { throw new Error('tile ' + res.status); }
      return res.blob().then(function (b) { return createImageBitmap(b); }).then(function (bmp) {
        var c = document.createElement('canvas'); c.width = 256; c.height = 256;
        var g = c.getContext('2d', { willReadFrequently: true }); g.drawImage(bmp, 0, 0);
        return g.getImageData(0, 0, 256, 256).data;
      });
    }));
    return p.then(function (d) {
      if (!d) { return null; }
      var i = (t.py * 256 + t.px) * 4;
      return d[i + 3] ? [d[i], d[i + 1], d[i + 2], d[i + 3]] : null;
    });
  }

  function key(px) { return px ? px[0] + ',' + px[1] + ',' + px[2] : null; }

  function checkPoint(lat, lon, title) {
    var safe = function (p) { return p.then(function (v) { return { ok: true, v: v }; }, function () { return { ok: false }; }); };
    return Promise.all([
      safe(pixel('01_flood_l2_shinsuishin_data', lat, lon)),
      safe(pixel('01_flood_l2_keizoku_data', lat, lon)),
      safe(pixel('01_flood_l2_kaokutoukai_hanran_data', lat, lon)),
      safe(pixel('01_flood_l2_kaokutoukai_kagan_data', lat, lon))
    ]).then(function (a) {
      var d = a[0], k = a[1], h = a[2], g = a[3];
      var dep = d.ok ? (d.v ? (DEPTH[key(d.v)] || ['区域内（深さの色を読めない）', null]) : null) : undefined;
      return {
        lat: lat, lon: lon, address: title || '',
        flood: dep === undefined ? { known: false } : dep === null ? { known: true, inside: false } : { known: true, inside: true, depth: dep[0], depth_m: dep[1] },
        duration: k.ok ? (k.v ? (DURATION[key(k.v)] || '区域内') : null) : undefined,
        collapse_flow: h.ok ? !!h.v : undefined,
        collapse_erosion: g.ok ? !!g.v : undefined,
        source: '国土地理院「重ねるハザードマップ」洪水浸水想定区域（想定最大規模）・浸水継続時間・家屋倒壊等氾濫想定区域'
      };
    });
  }

  function geocode(q) {
    return fetch('https://msearch.gsi.go.jp/address-search/AddressSearch?q=' + encodeURIComponent(q)).then(function (r) {
      if (!r.ok) { throw new Error('住所の検索に失敗しました'); }
      return r.json();
    }).then(function (j) {
      if (!j || !j.length) { throw new Error('その住所が見つかりませんでした'); }
      var c = j[0].geometry.coordinates;
      return { lon: c[0], lat: c[1], title: j[0].properties.title };
    });
  }

  function check(q) { return geocode(q).then(function (g) { return checkPoint(g.lat, g.lon, g.title); }); }

  root.KFloodCheck = { check: check, checkPoint: checkPoint, geocode: geocode, tileXY: tileXY };
})(typeof window !== 'undefined' ? window : this);
