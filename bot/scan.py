"""Background scanner — everything in one file.

Runs on GitHub's servers every fifteen minutes so scanning does not depend on
a phone being awake with Safari open. Writes signals.json next to the page,
which the app reads and merges in.

This is the engine and the runner combined into a single file on purpose:
uploading four files and keeping their folders intact is fiddly on a phone,
and a setup step people cannot complete is a feature that does not exist.

No exchange keys are used or needed — only public market data.
"""

from __future__ import annotations

import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import Any

UA = {"User-Agent": "signals-bot/1.0 (+github actions)"}

# Binance answers a GitHub Actions runner with 451 Unavailable For Legal
# Reasons — the runners live in Microsoft datacentres, and Binance blocks
# those ranges regardless of who is asking. The same code that works fine
# from a phone dies in five seconds on the server. So the data layer tries
# several venues and uses whichever one answers, rather than assuming one.
#
# The order matters: Binance first because it is what the page in your hand
# uses, so server and phone agree whenever Binance is reachable. Bybit and
# OKX are the fallbacks, and both carry the same USDT perpetuals.
SOURCES = [
    ("binance", "https://fapi.binance.com"),
    ("binance", "https://fapi1.binance.com"),
    ("bybit",   "https://api.bybit.com"),
    ("okx",     "https://www.okx.com"),
]

# Resolved once per run, then reused. Re-probing every venue on every call
# would multiply a 12-minute budget by four.
SOURCE: tuple[str, str] | None = None
# Probing a venue means asking it for the ticker list, which is the same
# call the run needs next. Keeping it saves a duplicate round trip.
_FIRST_TICKERS: list[dict] | None = None


class Blocked(Exception):
    """The venue answered, but not with data — geo-block, ban, or outage."""


# --------------------------------------------------------------------- data

def get_json(url: str, tries: int = 3) -> Any:
    """One request, with a couple of retries. Exchanges rate-limit by weight,
    and a scheduled job that hammers one on failure gets banned rather than
    fixed, so the backoff is deliberate.

    A 4xx is not retried. A geo-block does not become un-blocked by asking
    again nine seconds later; retrying it only burns the run's time budget
    before the fallback venue gets a turn."""
    import json
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                raise Blocked(f"HTTP {e.code} from {url.split('/')[2]}") from e
            last = e
            time.sleep(1.5 * (attempt + 1))
        except Exception as e:            # noqa: BLE001 - any failure retries
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise Blocked(f"failed after {tries}: {last}")


# Every venue names its timeframes differently. Only these two are used.
_BYBIT_TF = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60",
             "2h": "120", "4h": "240", "6h": "360", "12h": "720",
             "1d": "D", "1w": "W"}
_OKX_TF = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H",
           "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H", "1d": "1D",
           "1w": "1W"}


def _okx_inst(symbol: str) -> str:
    """BTCUSDT -> BTC-USDT-SWAP."""
    return symbol[:-4] + "-USDT-SWAP"


def _klines_from(kind: str, host: str, symbol: str, interval: str,
                 limit: int) -> list[dict]:
    if kind == "binance":
        q = urllib.parse.urlencode(
            {"symbol": symbol, "interval": interval, "limit": limit})
        rows = get_json(f"{host}/fapi/v1/klines?{q}")
        return [{"t": int(r[0]), "o": float(r[1]), "h": float(r[2]),
                 "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}
                for r in rows]

    if kind == "bybit":
        q = urllib.parse.urlencode({
            "category": "linear", "symbol": symbol,
            "interval": _BYBIT_TF.get(interval, "60"),
            "limit": min(limit, 1000)})
        d = get_json(f"{host}/v5/market/kline?{q}")
        rows = (d.get("result") or {}).get("list") or []
        # Bybit hands back newest-first. Every indicator here walks forward
        # in time, so a reversed list would compute the whole thing
        # backwards and still look plausible.
        rows = list(reversed(rows))
        return [{"t": int(r[0]), "o": float(r[1]), "h": float(r[2]),
                 "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}
                for r in rows]

    q = urllib.parse.urlencode({
        "instId": _okx_inst(symbol), "bar": _OKX_TF.get(interval, "1H"),
        "limit": min(limit, 300)})
    d = get_json(f"{host}/api/v5/market/candles?{q}")
    rows = list(reversed(d.get("data") or []))          # newest-first too
    return [{"t": int(r[0]), "o": float(r[1]), "h": float(r[2]),
             "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}
            for r in rows]


def _tickers_from(kind: str, host: str) -> list[dict]:
    out = []
    if kind == "binance":
        rows = get_json(f"{host}/fapi/v1/ticker/24hr")
        for r in rows:
            sym = r.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            try:
                out.append({"symbol": sym,
                            "vol": float(r["quoteVolume"]),
                            "price": float(r["lastPrice"]),
                            "change": float(r["priceChangePercent"])})
            except (KeyError, ValueError):
                continue

    elif kind == "bybit":
        d = get_json(f"{host}/v5/market/tickers?category=linear")
        for r in (d.get("result") or {}).get("list") or []:
            sym = r.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            try:
                out.append({"symbol": sym,
                            "vol": float(r["turnover24h"]),
                            "price": float(r["lastPrice"]),
                            # Bybit gives a fraction, Binance gives percent.
                            # Mixing the two would make every coin look flat.
                            "change": float(r["price24hPcnt"]) * 100})
            except (KeyError, ValueError):
                continue

    else:
        d = get_json(f"{host}/api/v5/market/tickers?instType=SWAP")
        for r in d.get("data") or []:
            inst = r.get("instId", "")
            if not inst.endswith("-USDT-SWAP"):
                continue
            try:
                last = float(r["last"])
                open24 = float(r.get("open24h") or 0) or last
                out.append({"symbol": inst.split("-")[0] + "USDT",
                            "vol": float(r.get("volCcy24h") or 0) * last,
                            "price": last,
                            "change": (last - open24) / open24 * 100})
            except (KeyError, ValueError, ZeroDivisionError):
                continue

    out.sort(key=lambda x: -x["vol"])
    return out


def pick_source() -> tuple[str, str]:
    """Find a venue that will actually talk to us, once, at the top of a run.

    A venue that returns an empty list counts as a failure. A geo-block that
    answers 200 with `[]` would otherwise be indistinguishable from a quiet
    market, and the run would write an empty file over a good one."""
    global SOURCE, _FIRST_TICKERS
    if SOURCE:
        return SOURCE
    problems = []
    for kind, host in SOURCES:
        try:
            rows = _tickers_from(kind, host)
            if len(rows) >= 20:
                SOURCE = (kind, host)
                _FIRST_TICKERS = rows
                print(f"data source: {kind} ({host})")
                return SOURCE
            problems.append(f"{host}: only {len(rows)} pairs")
        except Exception as e:            # noqa: BLE001
            problems.append(f"{host}: {e}")
    raise RuntimeError("no exchange would answer — " + "; ".join(problems))


def klines(symbol: str, interval: str, limit: int = 500) -> list[dict]:
    kind, host = pick_source()
    return _klines_from(kind, host, symbol, interval, limit)


def tickers() -> list[dict]:
    global _FIRST_TICKERS
    kind, host = pick_source()
    if _FIRST_TICKERS is not None:
        out, _FIRST_TICKERS = _FIRST_TICKERS, None
        return out
    return _tickers_from(kind, host)


# --------------------------------------------------------------- indicators

def ema(vals: list[float], n: int) -> list[float]:
    if not vals:
        return []
    k = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def sma(vals: list[float], n: int) -> list[float]:
    out, run = [], 0.0
    for i, v in enumerate(vals):
        run += v
        if i >= n:
            run -= vals[i - n]
        out.append(run / min(i + 1, n))
    return out


def rsi(vals: list[float], n: int = 14) -> list[float]:
    if len(vals) < 2:
        return [50.0] * len(vals)
    gains, losses = [0.0], [0.0]
    for i in range(1, len(vals)):
        d = vals[i] - vals[i - 1]
        gains.append(max(0.0, d))
        losses.append(max(0.0, -d))
    ag, al = sma(gains, n), sma(losses, n)
    return [100.0 if al[i] == 0 else 100 - 100 / (1 + ag[i] / al[i])
            for i in range(len(vals))]


def macd(vals: list[float]) -> tuple[list[float], list[float], list[float]]:
    f, s = ema(vals, 12), ema(vals, 26)
    line = [f[i] - s[i] for i in range(len(vals))]
    sig = ema(line, 9)
    return line, sig, [line[i] - sig[i] for i in range(len(vals))]


def true_range(c: list[dict]) -> list[float]:
    out = [c[0]["h"] - c[0]["l"]] if c else []
    for i in range(1, len(c)):
        p = c[i - 1]["c"]
        out.append(max(c[i]["h"] - c[i]["l"],
                       abs(c[i]["h"] - p), abs(c[i]["l"] - p)))
    return out


def atr(c: list[dict], n: int = 14) -> list[float]:
    return sma(true_range(c), n)


def adx(c: list[dict], n: int = 14) -> list[float]:
    """Wilder's ADX. Ported rather than approximated — a wrong ADX silently
    flips the breakout and mean-reversion gates, which are the two strategies
    that depend on it most."""
    if len(c) < 2:
        return [0.0] * len(c)
    plus, minus = [0.0], [0.0]
    for i in range(1, len(c)):
        up = c[i]["h"] - c[i - 1]["h"]
        dn = c[i - 1]["l"] - c[i]["l"]
        plus.append(up if (up > dn and up > 0) else 0.0)
        minus.append(dn if (dn > up and dn > 0) else 0.0)
    tr, sp, sm = sma(true_range(c), n), sma(plus, n), sma(minus, n)
    dx = []
    for i in range(len(c)):
        if tr[i] <= 0:
            dx.append(0.0)
            continue
        pdi, mdi = 100 * sp[i] / tr[i], 100 * sm[i] / tr[i]
        tot = pdi + mdi
        dx.append(0.0 if tot == 0 else 100 * abs(pdi - mdi) / tot)
    return sma(dx, n)


@dataclass
class Series:
    c: list[dict]
    close: list[float] = field(default_factory=list)
    ef: list[float] = field(default_factory=list)
    em: list[float] = field(default_factory=list)
    es: list[float] = field(default_factory=list)
    rsi: list[float] = field(default_factory=list)
    hist: list[float] = field(default_factory=list)
    macd: list[float] = field(default_factory=list)
    msig: list[float] = field(default_factory=list)
    atr: list[float] = field(default_factory=list)
    atr_pct: list[float] = field(default_factory=list)
    adx: list[float] = field(default_factory=list)
    vol: list[float] = field(default_factory=list)
    vol_ma: list[float] = field(default_factory=list)


def enrich(c: list[dict]) -> Series:
    close = [x["c"] for x in c]
    vol = [x["v"] for x in c]
    line, sig, hist = macd(close)
    a = atr(c)
    return Series(
        c=c, close=close,
        ef=ema(close, 21), em=ema(close, 50), es=ema(close, 200),
        rsi=rsi(close), macd=line, msig=sig, hist=hist,
        atr=a, atr_pct=[a[i] / close[i] * 100 if close[i] else 0
                        for i in range(len(close))],
        adx=adx(c), vol=vol, vol_ma=sma(vol, 20),
    )


# ------------------------------------------------------------------ config

CFG: dict[str, Any] = {
    "pairs": 60,
    "bars": 500,
    "signal_tf": "1h",
    "trend_tf": "4h",
    "min_score": 6,
    "adx_min": 20,
    "vol_mult": 1.15,
    "require_htf": True,
    "min_agree": 2,
    "cooldown_bars": 8,
    "sl_atr": 1.5,
    "tp_atr": [0.75, 1.5, 2.5, 3.5, 5.0],
    "max_loss_pct": 30,
    "min_lev": 3,
    "max_lev": 20,
    "fee_pct": 0.05,
    "slip_pct": 0.02,
    "atr_min_pct": 0.3,
    "atr_max_pct": 12,
    "max_age_bars": 24,
    "chase_warn_r": 0.5,
    "chase_max_r": 1.2,
    # breakout / rsi / mean reversion / momentum
    "brk_len": 20, "brk_min_adx": 18,
    "rsi_low": 30, "rsi_high": 70, "rsi_with_trend": True,
    "mr_len": 20, "mr_z": 2.2, "mr_max_adx": 22,
    "mom_len": 12, "mom_pct": 2.5, "mom_min_adx": 20,
}

STRATS = ["confluence", "ut", "brk", "rsi", "mr", "mom"]
STRAT_NAMES = {"confluence": "Confluence", "ut": "UT Bot", "brk": "Breakout",
               "rsi": "RSI", "mr": "Mean reversion", "mom": "Momentum"}
NOT_PORTED = ["Open/Close Cross", "Target Trend", "Support & Resistance",
              "Chart patterns"]


def trade_cost_pct(lev: int) -> float:
    return (CFG["fee_pct"] * 2 + CFG["slip_pct"] * 2) * max(1, lev)


def net_roi(gross: float, lev: int) -> float:
    return round(gross - trade_cost_pct(lev), 2)


def rnd(v: float, ref: float) -> float:
    a = abs(ref)
    if a >= 100:
        return round(v, 2)
    if a >= 1:
        return round(v, 3)
    if a >= 0.01:
        return round(v, 5)
    return float(f"{v:.6g}")


# ------------------------------------------------------------------ signals

def build_signal(sym: str, d: Series, i: int, side: str, tag: str,
                 why: list[str], stop_px: float | None = None,
                 strat: str = "") -> dict | None:
    """Entry, stop, leverage, targets. Leverage is chosen so that hitting the
    stop costs a fixed share of margin — never picked first and hoped for."""
    entry, a = d.close[i], d.atr[i]
    ap = d.atr_pct[i]
    if not (a > 0) or ap < CFG["atr_min_pct"] or ap > CFG["atr_max_pct"]:
        return None
    sign = 1 if side == "LONG" else -1
    ok_stop = stop_px is not None and (
        stop_px < entry if side == "LONG" else stop_px > entry)
    stop = stop_px if ok_stop else entry - sign * CFG["sl_atr"] * a
    sl_pct = abs(entry - stop) / entry * 100
    if sl_pct <= 0:
        return None
    lev = max(CFG["min_lev"],
              min(CFG["max_lev"], int(CFG["max_loss_pct"] / sl_pct))) \
        or CFG["min_lev"]
    risk = "high" if (lev >= 20 or ap >= 4) else \
           "low" if (lev <= 10 and ap < 2) else "medium"
    targets = []
    for k, m in enumerate(CFG["tp_atr"]):
        tp = entry + sign * m * a
        targets.append({"index": k + 1, "price": rnd(tp, entry),
                        "roi": round(abs(tp - entry) / entry * 100 * lev, 2)})
    return {
        "symbol": sym, "side": side, "leverage": lev, "risk": risk,
        "tag": tag, "reasons": why, "strat": strat,
        "strat_name": STRAT_NAMES.get(strat, strat),
        "entry": rnd(entry, entry), "stop": rnd(stop, entry),
        "initial_stop": rnd(stop, entry),
        "atr_pct": round(ap, 2), "adx": round(d.adx[i], 1),
        "at": d.c[i]["t"], "idx": i, "targets": targets,
        "stf": CFG["signal_tf"], "ttf": CFG["trend_tf"],
    }


def score_confluence(d: Series, i: int, side: str) -> tuple[int, list[str]]:
    long = side == "LONG"
    pts, why = 0, []

    def add(cond: bool, txt: str) -> None:
        nonlocal pts
        if cond:
            pts += 1
            why.append(txt)

    add(d.ef[i] > d.em[i] if long else d.ef[i] < d.em[i],
        "EMA21 > EMA50" if long else "EMA21 < EMA50")
    add(d.em[i] > d.es[i] if long else d.em[i] < d.es[i],
        "EMA50 > EMA200" if long else "EMA50 < EMA200")
    add(d.close[i] > d.ef[i] if long else d.close[i] < d.ef[i],
        "price above EMA21" if long else "price below EMA21")
    r = d.rsi[i]
    add((50 <= r <= 72) if long else (28 <= r <= 50),
        f"RSI {r:.0f} in band")
    add(d.macd[i] > d.msig[i] if long else d.macd[i] < d.msig[i],
        "MACD above signal" if long else "MACD below signal")
    add(abs(d.hist[i]) > abs(d.hist[i - 1]) and
        (d.hist[i] > 0 if long else d.hist[i] < 0),
        "MACD histogram expanding")
    add(d.adx[i] >= CFG["adx_min"], f"ADX {d.adx[i]:.0f} (trending)")
    add(d.vol_ma[i] > 0 and d.vol[i] > d.vol_ma[i] * CFG["vol_mult"],
        "volume above average")
    return pts, why


def scan_confluence(sym: str, d: Series) -> list[dict]:
    out, last = [], -999
    for i in range(210, len(d.c) - 1):
        if i - last < CFG["cooldown_bars"]:
            continue
        pl, wl = score_confluence(d, i, "LONG")
        ps, ws = score_confluence(d, i, "SHORT")
        side = "LONG" if pl >= ps else "SHORT"
        pts, why = (pl, wl) if side == "LONG" else (ps, ws)
        if pts < CFG["min_score"]:
            continue
        s = build_signal(sym, d, i, side, f"{pts}/8", why, None, "confluence")
        if s:
            s["score"] = pts
            out.append(s)
            last = i
    return out


def ut_stops(d: Series, key: float = 3.0, n: int = 10) -> list[float]:
    a = sma(true_range(d.c), n)
    stop = [0.0] * len(d.c)
    for i in range(len(d.c)):
        src = d.close[i]
        prev = stop[i - 1] if i else 0.0
        prev_src = d.close[i - 1] if i else src
        loss = key * a[i]
        if src > prev and prev_src > prev:
            stop[i] = max(prev, src - loss)
        elif src < prev and prev_src < prev:
            stop[i] = min(prev, src + loss)
        elif src > prev:
            stop[i] = src - loss
        else:
            stop[i] = src + loss
    return stop


def scan_ut(sym: str, d: Series) -> list[dict]:
    stop = ut_stops(d)
    out = []
    for i in range(210, len(d.c) - 1):
        above, was = d.close[i] > stop[i], d.close[i - 1] > stop[i - 1]
        if above == was:
            continue
        side = "LONG" if above else "SHORT"
        s = build_signal(sym, d, i, side, "UT 3/10",
                         ["price crossed the ATR trailing stop",
                          "the stop rides that same line"], stop[i], "ut")
        if s:
            out.append(s)
    return out


def scan_brk(sym: str, d: Series) -> list[dict]:
    out, n = [], CFG["brk_len"]
    for i in range(210, len(d.c) - 1):
        if d.adx[i] < CFG["brk_min_adx"]:
            continue
        hh = max(x["h"] for x in d.c[i - n:i])
        ll = min(x["l"] for x in d.c[i - n:i])
        px, prev, a = d.close[i], d.close[i - 1], d.atr[i]
        if a <= 0:
            continue
        if px > hh >= prev:
            stop = ll if (px - ll) < 3.5 * a else px - 1.5 * a
            s = build_signal(sym, d, i, "LONG", f"Breakout {n}",
                             [f"closed above the {n}-bar high",
                              f"ADX {d.adx[i]:.0f} — trending"], stop, "brk")
            if s:
                out.append(s)
        elif px < ll <= prev:
            stop = hh if (hh - px) < 3.5 * a else px + 1.5 * a
            s = build_signal(sym, d, i, "SHORT", f"Breakdown {n}",
                             [f"closed below the {n}-bar low",
                              f"ADX {d.adx[i]:.0f} — trending"], stop, "brk")
            if s:
                out.append(s)
    return out


def scan_rsi(sym: str, d: Series) -> list[dict]:
    out = []
    for i in range(210, len(d.c) - 1):
        up = d.rsi[i - 1] <= CFG["rsi_low"] < d.rsi[i]
        dn = d.rsi[i - 1] >= CFG["rsi_high"] > d.rsi[i]
        if not up and not dn:
            continue
        side = "LONG" if up else "SHORT"
        if CFG["rsi_with_trend"] and (side == "LONG") != (d.ef[i] > d.em[i]):
            continue
        edge = CFG["rsi_low"] if up else CFG["rsi_high"]
        s = build_signal(sym, d, i, side, f"RSI out of {edge}",
                         [f"RSI crossed back through {edge}",
                          f"RSI now {d.rsi[i]:.0f}"], None, "rsi")
        if s:
            out.append(s)
    return out


def scan_mr(sym: str, d: Series) -> list[dict]:
    out, n = [], CFG["mr_len"]
    for i in range(210, len(d.c) - 1):
        if d.adx[i] > CFG["mr_max_adx"]:
            continue
        window = d.close[i - n:i]
        mean = sum(window) / n
        var = sum((x - mean) ** 2 for x in window) / n
        sd = math.sqrt(var)
        if sd <= 0:
            continue
        z, zp = (d.close[i] - mean) / sd, (d.close[i - 1] - mean) / sd
        if zp < -CFG["mr_z"] and z > zp:
            s = build_signal(sym, d, i, "LONG", f"Mean reversion -{CFG['mr_z']}s",
                             [f"{abs(z):.1f} sd below the {n}-bar mean",
                              f"ADX {d.adx[i]:.0f} — no strong trend to fight"],
                             d.close[i] - abs(z) * sd * 0.6, "mr")
            if s:
                out.append(s)
        elif zp > CFG["mr_z"] and z < zp:
            s = build_signal(sym, d, i, "SHORT", f"Mean reversion +{CFG['mr_z']}s",
                             [f"{z:.1f} sd above the {n}-bar mean",
                              f"ADX {d.adx[i]:.0f} — no strong trend to fight"],
                             d.close[i] + abs(z) * sd * 0.6, "mr")
            if s:
                out.append(s)
    return out


def scan_mom(sym: str, d: Series) -> list[dict]:
    out, n, last = [], CFG["mom_len"], -999
    for i in range(210, len(d.c) - 1):
        if i - last < CFG["cooldown_bars"] or d.adx[i] < CFG["mom_min_adx"]:
            continue
        base = d.close[i - n]
        if base <= 0:
            continue
        roc = (d.close[i] - base) / base * 100
        expanding = abs(d.hist[i]) > abs(d.hist[i - 1])
        if roc >= CFG["mom_pct"] and d.hist[i] > 0 and expanding:
            s = build_signal(sym, d, i, "LONG", f"Momentum +{roc:.1f}%",
                             [f"+{roc:.1f}% over {n} bars",
                              "MACD histogram positive and growing"], None, "mom")
            if s:
                out.append(s)
                last = i
        elif roc <= -CFG["mom_pct"] and d.hist[i] < 0 and expanding:
            s = build_signal(sym, d, i, "SHORT", f"Momentum {roc:.1f}%",
                             [f"{roc:.1f}% over {n} bars",
                              "MACD histogram negative and growing"], None, "mom")
            if s:
                out.append(s)
                last = i
    return out


SCANNERS = {"confluence": scan_confluence, "ut": scan_ut, "brk": scan_brk,
            "rsi": scan_rsi, "mr": scan_mr, "mom": scan_mom}


# ------------------------------------------------------------------ filters

def htf_bias(h: Series, t: int) -> str | None:
    """Trend on the slower chart at the moment the signal fired. Uses the last
    higher-timeframe candle that had already closed — using the one still
    forming would be reading the future."""
    idx = None
    for i, bar in enumerate(h.c):
        if bar["t"] <= t:
            idx = i
        else:
            break
    if idx is None or idx < 50:
        return None
    return "LONG" if h.ef[idx] > h.em[idx] else "SHORT"


def agreement(d: Series, i: int, side: str) -> tuple[int, int]:
    """Four independent reads of direction. Deliberately simple and
    deliberately not the strategy that fired — a strategy agreeing with
    itself is not confirmation."""
    want = 1 if side == "LONG" else -1
    votes = [
        1 if d.ef[i] > d.em[i] else -1,
        1 if d.close[i] > d.es[i] else -1,
        1 if d.macd[i] > d.msig[i] else -1,
        1 if d.rsi[i] > 50 else -1,
    ]
    return sum(1 for v in votes if v == want), 4


def track(sig: dict, d: Series) -> dict:
    """Walk forward from the signal and record what happened. The stop is
    checked before the targets on every bar: when a candle covers both, the
    pessimistic reading is the honest one."""
    long = sig["side"] == "LONG"
    stop = sig["initial_stop"]
    hits, closed_at, status, price = 0, None, "in_progress", sig["entry"]
    for i in range(sig["idx"] + 1, len(d.c)):
        b = d.c[i]
        if (b["l"] <= stop) if long else (b["h"] >= stop):
            price, status, closed_at = stop, "stopped", b["t"]
            break
        for t in sig["targets"]:
            if (b["h"] >= t["price"]) if long else (b["l"] <= t["price"]):
                hits = max(hits, t["index"])
        if hits >= len(sig["targets"]):
            price, status, closed_at = sig["targets"][-1]["price"], "completed", b["t"]
            break
    else:
        price = d.close[-1]
    sign = 1 if long else -1
    sig["hits"] = hits
    sig["status"] = status
    sig["closed_at"] = closed_at
    sig["price"] = rnd(price, sig["entry"])
    sig["roi"] = net_roi((price - sig["entry"]) / sig["entry"] * 100 * sign
                         * sig["leverage"], sig["leverage"])
    return sig


def chase_check(d: Series, sig: dict) -> dict:
    """Is the entry still there? Everything relative to the coin's own
    volatility, so it means the same on BTC as on a mid-cap."""
    long = sig["side"] == "LONG"
    risk = abs(sig["entry"] - sig["initial_stop"])
    now = d.close[-1]
    if risk <= 0:
        return {"verdict": "take", "why": [], "ran": 0.0}
    ran = ((now - sig["entry"]) if long else (sig["entry"] - now)) / risk
    if ran >= CFG["chase_max_r"]:
        return {"verdict": "late", "ran": round(ran, 2),
                "why": [f"already {ran:.1f}R past the signal price"]}
    if ran >= CFG["chase_warn_r"]:
        retest = sig["entry"] + (1 if long else -1) * risk * 0.2
        return {"verdict": "retest", "ran": round(ran, 2),
                "retest": rnd(retest, sig["entry"]),
                "why": [f"ran {ran:.1f}R — wait for a pullback"]}
    return {"verdict": "take", "ran": round(ran, 2),
            "why": [f"still within {CFG['chase_warn_r']}R of the signal price"]}


def record_for(closed: list[dict]) -> dict[str, dict]:
    """How often each coin reached its first target, per strategy family."""
    by: dict[str, dict] = {}
    for s in closed:
        if not s.get("closed_at"):
            continue
        r = by.setdefault(s["symbol"], {"n": 0, "wins": 0})
        r["n"] += 1
        if s.get("hits", 0) >= 1:
            r["wins"] += 1
    for r in by.values():
        r["rate"] = round(r["wins"] / r["n"] * 100) if r["n"] else 0
    return by


def scan_symbol(sym: str, ltf: list[dict], htf: list[dict]) -> dict:
    """Every strategy over one coin. Returns the open signals and the resolved
    ones, so the record and the live list come from the same replay."""
    d, h = enrich(ltf), enrich(htf)
    if len(d.c) < 220:
        return {"open": [], "closed": []}
    found: list[dict] = []
    for key in STRATS:
        try:
            found.extend(SCANNERS[key](sym, d))
        except Exception:                 # noqa: BLE001 - one bad strategy
            continue                      # must not take the whole coin down
    open_, closed = [], []
    bar_ms = (d.c[1]["t"] - d.c[0]["t"]) if len(d.c) > 1 else 3_600_000
    fresh_cut = d.c[-1]["t"] - CFG["max_age_bars"] * bar_ms
    for s in found:
        n, of = agreement(d, s["idx"], s["side"])
        s["agree"], s["agree_of"] = n, of
        if CFG["require_htf"]:
            bias = htf_bias(h, s["at"])
            if bias and bias != s["side"]:
                continue
        track(s, d)
        if s.get("closed_at"):
            closed.append(s)
            continue
        if CFG["min_agree"] and n < CFG["min_agree"]:
            continue
        if s["at"] < fresh_cut:
            continue
        s["chase"] = chase_check(d, s)
        open_.append(s)
    # newest first, one per coin and direction
    open_.sort(key=lambda x: -x["at"])
    seen, uniq = set(), []
    for s in open_:
        k = (s["symbol"], s["side"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(s)
    return {"open": uniq, "closed": closed}


# ======================================================================
# THE SCHEDULED RUN
# ======================================================================

import json
import os
import pathlib
import smtplib
import ssl
import sys
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "signals.json"
STATE = ROOT / "bot" / "state.json"

# How much of the coin list one run covers. A scheduled job that tries to do
# everything gets killed halfway and writes nothing; one that does a slice
# every fifteen minutes covers the whole list within the hour and always
# leaves a complete file behind.
def env_int(name: str, default: int) -> int:
    """A number from the environment, or the default.

    `os.environ.get(name, "18")` is not enough here. A workflow that maps
    `SCAN_BATCH: ${{ vars.SCAN_BATCH }}` with the variable unset does not
    leave the variable out — it sets it to the empty string. So the default
    never fires, `int("")` raises, and the job dies on line one before it
    has looked at a single candle. Anything unreadable falls back rather
    than crashing: a typo in an optional tuning knob should not stop the
    scan."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        print(f"{name}={raw!r} is not a number — using {default}")
        return default


BATCH = env_int("SCAN_BATCH", 18)
MIN_CONF_ALERT = env_int("MIN_CONFIDENCE", 0)


def load_state() -> dict:
    try:
        return json.loads(STATread_text())
    except Exception:                      # noqa: BLE001
        return {"cursor": 0, "alerted": [], "record": {}, "runs": 0}


def save_state(s: dict) -> None:
    s["alerted"] = s.get("alerted", [])[-400:]
    STATparent.mkdir(parents=True, exist_ok=True)
    STATwrite_text(json.dumps(s, indent=1))


def confidence(sig: dict) -> int:
    """A deliberately plain score on the server: agreement, trend strength,
    freshness of the entry, and the coin's own record. The page's full
    sixteen-module version needs history the server does not carry yet, so
    rather than fake it this is labelled as the simple one."""
    parts = []
    parts.append(sig.get("agree", 0) / max(1, sig.get("agree_of", 4)))
    parts.append(min(1.0, max(0.0, (sig.get("adx", 0) - 12) / 20)))
    ran = abs(sig.get("chase", {}).get("ran", 0))
    parts.append(max(0.0, 1 - ran / max(0.01, CFG["chase_max_r"])))
    rec = sig.get("record")
    if rec and rec.get("n", 0) >= 4:
        parts.append(min(1.0, rec["rate"] / 70))
    return round(sum(parts) / len(parts) * 100)


def send_email(subject: str, body: str) -> str:
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    to = os.environ.get("ALERT_EMAIL")
    if not (host and user and pw and to):
        return "email: not configured"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)
    port = env_int("SMTP_PORT", 465)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)
    return f"email: sent to {to}"


def send_telegram(text: str) -> str:
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return "telegram: not configured"
    data = urllib.parse.urlencode(
        {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=25):
        pass
    return "telegram: sent"


def card(s: dict) -> str:
    dot = "LONG" if s["side"] == "LONG" else "SHORT"
    lines = [
        f"{s['symbol'].replace('USDT','')}/USDT - {dot}",
        "",
        f"Confidence      {s.get('confidence','-')} / 100",
        f"Strategy        {s.get('strat_name','')} on {s['stf']} / {s['ttf']}",
        f"Confirmation    {s.get('agree','-')} / {s.get('agree_of',4)}",
        "",
        f"Entry           {s['entry']}",
        f"Stop loss       {s['stop']}",
        "",
        "Targets",
    ]
    for t in s["targets"]:
        lines.append(f"  TP{t['index']}   {t['price']}   {t['roi']}%")
    lines += [
        "",
        f"Leverage        {s['leverage']}x "
        f"(stop costs {CFG['max_loss_pct']}% of margin)",
        f"Entry now       {s.get('chase',{}).get('verdict','-')}",
        "",
        "Why?",
    ]
    for w in s["reasons"][:6]:
        lines.append(f"  - {w}")
    rec = s.get("record")
    if rec and rec.get("n", 0) >= 4:
        lines += ["", f"This coin: {rec['rate']}% reached TP1 over "
                      f"{rec['n']} finished trades."]
    return "\n".join(lines)


def main() -> int:
    started = time.time()
    state = load_state()
    log: list[str] = []

    try:
        tk = tickers()
    except Exception as e:                 # noqa: BLE001
        # Say which venues refused and why, rather than a traceback. Almost
        # every failure here is a geo-block, and the message should read as
        # one instead of as a bug in the maths.
        print(f"could not reach any exchange: {e}")
        print("leaving the last signals.json alone")
        return 1
    if not tk:
        print("no ticker data; leaving the last file alone")
        return 1
    universe = [t["symbol"] for t in tk[:CFG["pairs"]]]
    by_sym = {t["symbol"]: t for t in tk}

    cursor = state.get("cursor", 0) % max(1, len(universe))
    batch = [universe[(cursor + k) % len(universe)] for k in range(min(BATCH, len(universe)))]
    state["cursor"] = (cursor + len(batch)) % len(universe)

    # Carry forward what earlier runs found, so the file always describes the
    # whole universe rather than only the slice this run happened to cover.
    try:
        prev = json.loads(OUT.read_text())
    except Exception:                      # noqa: BLE001
        prev = {"signals": []}
    kept = [s for s in prev.get("signals", []) if s["symbol"] not in batch]

    fresh, closed_all, failed = [], [], []
    for sym in batch:
        try:
            ltf = klines(sym, CFG["signal_tf"], CFG["bars"])
            htf = klines(sym, CFG["trend_tf"], min(500, CFG["bars"]))
            res = scan_symbol(sym, ltf, htf)
            fresh.extend(res["open"])
            closed_all.extend(res["closed"])
        except Exception as e:             # noqa: BLE001
            failed.append(f"{sym}: {e}")
        time.sleep(0.15)                   # be polite to the API

    # the coin's own record, merged across runs
    rec = state.get("record", {})
    for sym, r in record_for(closed_all).items():
        old = rec.get(sym, {"n": 0, "wins": 0})
        rec[sym] = {"n": r["n"], "wins": r["wins"], "rate": r["rate"]}
        _ = old
    state["record"] = rec

    for s in fresh:
        s["record"] = rec.get(s["symbol"])
        s["confidence"] = confidence(s)
        px = by_sym.get(s["symbol"], {}).get("price")
        if px:
            s["last_price"] = px

    allsig = kept + fresh
    # drop anything that has aged out of usefulness since an earlier run
    now_ms = int(time.time() * 1000)
    allsig = [s for s in allsig if now_ms - s["at"] < 36 * 3600 * 1000]
    allsig.sort(key=lambda x: (-x.get("confidence", 0), -x["at"]))

    payload = {
        "generated": now_ms,
        "generated_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_seconds": round(time.time() - started, 1),
        "scanned": batch,
        "universe": len(universe),
        "cursor": state["cursor"],
        "runs": state.get("runs", 0) + 1,
        "strategies": [STRAT_NAMES[k] for k in STRATS],
        "not_ported": NOT_PORTED,
        "config": {k: CFG[k] for k in
                   ("signal_tf", "trend_tf", "min_score", "min_agree",
                    "max_age_bars", "sl_atr", "max_loss_pct", "max_lev")},
        "failed": failed[:10],
        "signals": allsig[:120],
    }
    state["runs"] = payload["runs"]
    OUT.write_text(json.dumps(payload, indent=1))
    log.append(f"{len(fresh)} found in this slice, {len(allsig)} live in total")

    # ---- alerts, once per signal, only above the threshold
    alerted = set(state.get("alerted", []))
    new = [s for s in fresh
           if s.get("confidence", 0) >= MIN_CONF_ALERT
           and s.get("chase", {}).get("verdict") != "late"
           and f"{s['symbol']}|{s['at']}|{s['side']}" not in alerted]
    for s in new:
        alerted.add(f"{s['symbol']}|{s['at']}|{s['side']}")
    state["alerted"] = list(alerted)

    if new:
        head = (f"{len(new)} new signal" + ("" if len(new) == 1 else "s"))
        body = "\n\n---\n\n".join(card(s) for s in new[:8])
        body += ("\n\nNot advice. Size every position so the stop is one you "
                 "can take.")
        try:
            log.append(send_email(head, body))
        except Exception as e:             # noqa: BLE001
            log.append(f"email failed: {e}")
        try:
            log.append(send_telegram(head + "\n\n" + body[:3500]))
        except Exception as e:             # noqa: BLE001
            log.append(f"telegram failed: {e}")
    else:
        log.append("nothing new to alert")

    save_state(state)
    for line in log:
        print(line)
    if failed:
        print(f"{len(failed)} pairs failed: {failed[:3]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
