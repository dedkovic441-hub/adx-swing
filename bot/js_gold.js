/** Extract v7 swing engine from index.html and run it on the same klines as Python. */
const fs = require("fs");

function between(html, startName, endName) {
  const a = html.indexOf("    function " + startName);
  const b = html.indexOf("    function " + endName, a + 1);
  if (a < 0 || b < 0) throw new Error("extract fail " + startName + " -> " + endName);
  return html.slice(a, b);
}

function loadKlines(path) {
  const raw = JSON.parse(fs.readFileSync(path, "utf8"));
  return raw.map((k) => ({
    time: Math.floor(Number(k[0]) / 1000),
    open: +k[1], high: +k[2], low: +k[3], close: +k[4], volume: +k[5]
  }));
}

function detect(html, candles, tf, sens, nowMs) {
  const src = `
    var INTERVAL_MS = { "1m": 60000, "5m": 300000, "15m": 900000, "1h": 3600000, "4h": 14400000, "1d": 86400000 };
    var filters = { swingSens: ${JSON.stringify(sens)}, swingEarly: true };
    var state = { tf: ${JSON.stringify(tf)}, candles: candles };
    var __NOW = ${nowMs == null ? "Date.now()" : String(nowMs)};
    var Date = { now: function(){ return __NOW; } };
    // detectSwingSignals calls emaArr for the entry score.
    ${between(html, "emaArr", "wmaNum")}
    ${between(html, "trueRangeArr", "computeSsl")}
    ${between(html, "volSmaPrev", "linePts")}
    ${between(html, "atrArr", "getActiveSignals")}
    return detectSwingSignals(candles, true);
  `;
  return new Function("candles", src)(candles);
}

function slim(s) {
  return {
    time: s.time, dir: s.dir, name: s.name, price: s.price,
    lag: s.lag, confirmTime: s.confirmTime, index: s.index,
    stars: s.stars, reasons: s.reasons, swingKind: s.swingKind
  };
}

const htmlPath = process.argv[2];
const klinePath = process.argv[3];
const tf = process.argv[4];
const sens = process.argv[5] || "medium";
const nowMs = process.argv[6] ? Number(process.argv[6]) : null;
const html = fs.readFileSync(htmlPath, "utf8");
const candles = loadKlines(klinePath);
const pack = detect(html, candles, tf, sens, nowMs);
const out = {
  bars: candles.length,
  lastClosedHint: null,
  n: pack.signals.length,
  signals: pack.signals.map(slim),
  provisional: pack.provisional ? slim(pack.provisional) : null
};
process.stdout.write(JSON.stringify(out));
