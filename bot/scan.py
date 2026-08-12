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
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import Any

NAN = float("nan")

# ---------------------------------------------------------------- TLS trust
#
# Python does not use the macOS keychain. A fresh Mac install therefore has no
# root certificates as far as Python is concerned, and every exchange fails
# with "unable to get local issuer certificate" — which reads like the network
# is down when it is nothing of the sort.
#
# Worse, if a VPN or antivirus is inspecting traffic, the chain ends in that
# product's own root. It is in the keychain, so Safari is happy; Python has
# never heard of it, so you get "self-signed certificate in certificate
# chain". Same symptom, opposite cause.
#
# Both are solved by asking the operating system what it trusts, which is what
# truststore does. certifi is the fallback: a bundled list of public roots,
# which fixes the missing-roots case but not the intercepted one.
SSL_NOTE = "default"
try:
    import truststore                       # noqa: F401
    truststore.inject_into_ssl()
    SSL_NOTE = "system trust store"
except Exception:                           # noqa: BLE001
    try:
        import certifi
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
        SSL_NOTE = "certifi bundle"
    except Exception:                       # noqa: BLE001
        pass

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

# A last resort, and an honest one. Only public market data passes through
# here — no keys, no orders, nothing to steal — so the realistic worst case of
# skipping verification is being fed wrong prices by whatever is intercepting
# the connection. That is not nothing, which is why it is off unless you ask.
INSECURE = (os.environ.get("SSL_INSECURE") or "").strip().lower() in (
    "1", "true", "yes")
_NOVERIFY = None
if INSECURE:
    import ssl as _ssl
    _NOVERIFY = _ssl.create_default_context()
    _NOVERIFY.check_hostname = False
    _NOVERIFY.verify_mode = _ssl.CERT_NONE
    SSL_NOTE = "VERIFICATION OFF (SSL_INSECURE=1)"


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
            with urllib.request.urlopen(req, timeout=25,
                                        context=_NOVERIFY) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                raise Blocked(f"HTTP {e.code} from {url.split('/')[2]}") from e
        except urllib.error.URLError as e:
            # A certificate failure is not a rate limit; retrying three times
            # just makes you wait nine seconds for the same answer.
            if "CERTIFICATE_VERIFY_FAILED" in str(e.reason):
                raise Blocked(
                    "TLS trust failed — Python cannot verify this Mac's "
                    "certificates. Run: pip3 install --user truststore   "
                    "(or start with SSL_INSECURE=1 to skip verification)"
                ) from e
            last = e
            time.sleep(1.5 * (attempt + 1))
            last = e
            time.sleep(1.5 * (attempt + 1))
        except Exception as e:            # noqa: BLE001 - any failure retries
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise Blocked(f"failed after {tries}: {last}")


# Every venue names its timeframes differently, and — more importantly — they
# do not offer the same ones. Binance has 8h; OKX and Bybit do not. A lookup
# with a default silently substitutes another period, which is the worst
# possible failure: the scan succeeds, the card says 8h, and the numbers came
# from an hourly chart. These maps are exhaustive and a miss raises.
#
# Nothing shorter than a minute is here. Binance futures does not serve it,
# and five hundred one-second candles is eight minutes of history — less than
# the warm-up of a 200-period average, so every indicator would be undefined.
# A one-second chart is not a faster version of this; it is a different
# instrument needing a different engine.
_ALL_TF = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h",
           "12h", "1d", "3d", "1w", "1M"]

_BYBIT_TF = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
             "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
             "1d": "D", "1w": "W", "1M": "M"}
_OKX_TF = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
           "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H",
           "1d": "1D", "3d": "3D", "1w": "1W", "1M": "1M"}
_BINANCE_TF = {k: k for k in _ALL_TF}          # it serves all of them


class Unsupported(Exception):
    """This venue does not offer that chart period."""


def venue_tf(kind: str, interval: str) -> str:
    table = (_BINANCE_TF if kind == "binance"
             else _BYBIT_TF if kind == "bybit" else _OKX_TF)
    if interval not in table:
        raise Unsupported(f"{kind} has no {interval} chart")
    return table[interval]


def _okx_inst(symbol: str) -> str:
    """BTCUSDT -> BTC-USDT-SWAP."""
    return symbol[:-4] + "-USDT-SWAP"


def _klines_from(kind: str, host: str, symbol: str, interval: str,
                 limit: int) -> list[dict]:
    if kind == "binance":
        q = urllib.parse.urlencode(
            {"symbol": symbol, "interval": venue_tf("binance", interval),
             "limit": limit})
        rows = get_json(f"{host}/fapi/v1/klines?{q}")
        return [{"t": int(r[0]), "o": float(r[1]), "h": float(r[2]),
                 "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}
                for r in rows]

    if kind == "bybit":
        q = urllib.parse.urlencode({
            "category": "linear", "symbol": symbol,
            "interval": venue_tf("bybit", interval),
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
        "instId": _okx_inst(symbol), "bar": venue_tf("okx", interval),
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


def wilder(vals: list[float], n: int) -> list[float]:
    """Wilder's smoothing — what ATR, RSI and ADX are actually defined with.

    This was the single biggest reason server signals differed from the ones
    on the phone. The server averaged the true range instead of smoothing it,
    which makes ATR react faster, which moves every stop, every target, every
    zone width and the ATR% gate. Two engines, two answers, from the same
    candles. The browser is the reference, so the browser's formula wins."""
    out: list[float] = []
    e = vals[0] if vals else 0.0
    for i, x in enumerate(vals):
        e = x if i == 0 else e + (x - e) / n
        out.append(e)
    return out


def sma(vals: list[float], n: int) -> list[float]:
    """Simple average, undefined until the window is full.

    The warm-up matters: returning a partial average for the first n bars
    makes a moving-average cross fire during the warm-up, where the browser
    (which yields NaN) stays silent. That alone produced dozens of phantom
    Open/Close Cross and Target Trend signals the phone never saw."""
    out, run = [], 0.0
    for i, v in enumerate(vals):
        run += v
        if i >= n:
            run -= vals[i - n]
        out.append(run / n if i >= n - 1 else NAN)
    return out


def sma_win(vals: list[float], n: int) -> list[float]:
    """Windowed average that refuses to average over a NaN, rather than
    carrying an earlier warm-up forward for ever. Used where one average
    feeds another."""
    out = [NAN] * len(vals)
    for i in range(n - 1, len(vals)):
        s, ok = 0.0, True
        for k in range(n):
            x = vals[i - k]
            if x != x:                       # NaN
                ok = False
                break
            s += x
        if ok:
            out[i] = s / n
    return out


def wma(vals: list[float], n: int) -> list[float]:
    """Linearly weighted average — newest bar counts n times the oldest.
    NaN until the window fills, exactly as the browser does it."""
    out = [NAN] * len(vals)
    denom = n * (n + 1) / 2
    for i in range(n - 1, len(vals)):
        s = 0.0
        for k in range(n):
            s += vals[i - k] * (n - k)
        out[i] = s / denom
    return out


def rsi(vals: list[float], n: int = 14) -> list[float]:
    if len(vals) < 2:
        return [50.0] * len(vals)
    gains, losses = [0.0], [0.0]
    for i in range(1, len(vals)):
        d = vals[i] - vals[i - 1]
        gains.append(max(0.0, d))
        losses.append(max(0.0, -d))
    ag, al = wilder(gains, n), wilder(losses, n)
    return [100.0 if al[i] == 0 else 100 - 100 / (1 + ag[i] / al[i])
            for i in range(len(vals))]


def macd(vals: list[float]) -> tuple[list[float], list[float], list[float]]:
    f, s = ema(vals, 12), ema(vals, 26)
    line = [f[i] - s[i] for i in range(len(vals))]
    sig = ema(line, 9)
    return line, sig, [line[i] - sig[i] for i in range(len(vals))]


def now_ms() -> int:
    return int(time.time() * 1000)


def true_range(c: list[dict]) -> list[float]:
    out = [c[0]["h"] - c[0]["l"]] if c else []
    for i in range(1, len(c)):
        p = c[i - 1]["c"]
        out.append(max(c[i]["h"] - c[i]["l"],
                       abs(c[i]["h"] - p), abs(c[i]["l"] - p)))
    return out


def atr(c: list[dict], n: int = 14) -> list[float]:
    return wilder(true_range(c), n)


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
    tr, sp, sm = (wilder(true_range(c), n), wilder(plus, n),
                  wilder(minus, n))
    dx = []
    for i in range(len(c)):
        if not (tr[i] > 0):        # zero or NaN — the browser yields 0 here
            dx.append(0.0)
            continue
        pdi, mdi = 100 * sp[i] / tr[i], 100 * sm[i] / tr[i]
        tot = pdi + mdi
        dx.append(0.0 if tot == 0 else 100 * abs(pdi - mdi) / tot)
    return wilder(dx, n)


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
    # The default pair, and the ladder the sweep actually walks. Every coin
    # is judged on all three, and the best signal per direction is kept — so a
    # coin that only works on 15m is not missed because the app happened to be
    # set to 4h.
    "signal_tf": "1h",
    "trend_tf": "4h",
    "tf_ladder": [("15m", "1h"), ("1h", "4h"), ("4h", "1d")],
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
    # An absolute ceiling, whatever the candle size. 48 bars on a 1d
    # chart is 48 days; nobody means that by "fresh".
    "max_age_hours": 24,
    # How long an unresolved signal stays in the book before it is
    # dropped for age alone.
    "book_hours": 48,
    "chase_warn_r": 0.5,
    "chase_max_r": 1.2,
    # --- the four ported from the page. Same values as the browser defaults,
    #     because two engines that disagree about a tolerance produce two
    #     different signals from the same chart and neither can be trusted.
    "occ_ma_type": "SMMA", "occ_ma_len": 8, "occ_mult": 3,
    "occ_exit_on_cross": True,
    "tt_len": 10, "tt_target": 0,
    "sr_pivot": 5, "sr_zone_atr": 0.6, "sr_min_touch": 2, "sr_max_age": 400,
    "sr_mode": "both", "sr_zone_stop": True, "sr_break_atr": 0.25,
    "pat_pivot": 4, "pat_tol": 2.5, "pat_flat": 1.2, "pat_max_age": 140,
    "pat_cooldown": 8, "pat_min_height": 1.2, "pat_min_span": 14,
    "pat_flag_run": 12, "pat_flag_pause": 8, "pat_flag_atr": 3,
    "pat_cup_bars": 60, "pat_targets": [0.5, 1.0, 1.5, 2.0, 3.0],
    # breakout / rsi / mean reversion / momentum
    "brk_len": 20, "brk_min_adx": 18,
    "rsi_low": 30, "rsi_high": 70, "rsi_with_trend": True,
    "mr_len": 20, "mr_z": 2.2, "mr_max_adx": 22,
    "mom_len": 12, "mom_pct": 2.5, "mom_min_adx": 20,
}

STRATS = ["confluence", "occ", "ut", "tt", "sr", "pat",
          "brk", "rsi", "mr", "mom"]
STRAT_NAMES = {"confluence": "Confluence", "occ": "Open/Close Cross",
               "ut": "UT Bot", "tt": "Target Trend",
               "sr": "Support & Resistance", "pat": "Chart patterns",
               "brk": "Breakout", "rsi": "RSI", "mr": "Mean reversion",
               "mom": "Momentum"}
NOT_PORTED: list[str] = []      # all ten now run here


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
                 strat: str = "", unit: float | None = None,
                 mults: list[float] | None = None) -> dict | None:
    """Entry, stop, leverage, targets. Leverage is chosen so that hitting the
    stop costs a fixed share of margin — never picked first and hoped for.

    `unit` and `mults` let a strategy set its targets in its own measure
    rather than in ATR. A chart pattern that promises a 3% move should not be
    sold with a 12% target just because the coin happens to be volatile
    today, and Target Trend's rungs are multiples of its own band."""
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
    step = unit if (unit and unit > 0) else a
    ladder = mults if mults else CFG["tp_atr"]
    targets = []
    for k, m in enumerate(ladder):
        tp = entry + sign * m * step
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


# =========================================================================
# THE FOUR THAT USED TO RUN ONLY ON THE PHONE
#
# Open/Close Cross, Target Trend, Support & Resistance and the chart patterns
# carry more state than the others — pivots, zones, aggregated candles — which
# is why they were left on the device first. Ported here so the page can stop
# scanning altogether.
#
# These are line-for-line ports of the browser versions, not re-inventions.
# Where the two could drift, the browser is the reference: same pivot rule,
# same zone widths, same tolerances, same target ladders. A server that
# "improves" a strategy silently is a server whose numbers you cannot compare
# with the ones in your hand.
# =========================================================================

def pivots(c: list[dict], L: int) -> list[dict]:
    """Swing highs and lows, with the bar at which each became knowable.

    `conf` is the honest part: a swing high at bar i is not visible until
    bar i+L, because you need L bars on the right to know it was the top.
    Every replay below reads `conf`, never `i`, so nothing here can see a
    pivot before the chart could have drawn it."""
    out = []
    n = len(c)
    for i in range(L, n - L):
        is_h = is_l = True
        for k in range(i - L, i + L + 1):
            if k == i:
                continue
            if c[k]["h"] >= c[i]["h"]:
                is_h = False
            if c[k]["l"] <= c[i]["l"]:
                is_l = False
            if not is_h and not is_l:
                break
        if is_h:
            out.append({"i": i, "conf": i + L, "price": c[i]["h"],
                        "type": "H", "t": c[i]["t"]})
        if is_l:
            out.append({"i": i, "conf": i + L, "price": c[i]["l"],
                        "type": "L", "t": c[i]["t"]})
    return out


# ------------------------------------------------------ support & resistance

def sr_zones(pvs: list[dict], width: float, now_idx: int) -> list[dict]:
    if not pvs or not (width > 0):
        return []
    zones: list[dict] = []
    cur: dict | None = None
    for p in sorted(pvs, key=lambda x: x["price"]):
        if cur and p["price"] - cur["lo"] <= width:
            cur["hi"] = max(cur["hi"], p["price"])
            cur["pts"].append(p)
        else:
            cur = {"lo": p["price"], "hi": p["price"], "pts": [p]}
            zones.append(cur)
    for z in zones:
        z["mid"] = (z["lo"] + z["hi"]) / 2
        z["touches"] = len(z["pts"])
        z["highs"] = sum(1 for p in z["pts"] if p["type"] == "H")
        z["lows"] = z["touches"] - z["highs"]
        z["lastIdx"] = max(p["i"] for p in z["pts"])
        z["kind"] = ("flip" if z["highs"] and z["lows"]
                     else "resistance" if z["highs"] else "support")
        age = now_idx - z["lastIdx"]
        z["strength"] = round(z["touches"] + max(0.0, 1 - age / 400), 2)
    return zones


def sr_hits(d: Series) -> list[dict]:
    L = CFG["sr_pivot"]
    pvs = pivots(d.c, L)
    mode = CFG["sr_mode"]
    out: list[dict] = []
    avail: list[dict] = []
    ptr = 0
    zones: list[dict] = []
    next_calc = 0
    last_idx = -999

    for i in range(210, len(d.c) - 1):
        while ptr < len(pvs) and pvs[ptr]["conf"] <= i:
            avail.append(pvs[ptr])
            ptr += 1
        if i >= next_calc:
            live = [p for p in avail if i - p["i"] <= CFG["sr_max_age"]]
            zones = [z for z in sr_zones(live, CFG["sr_zone_atr"] * d.atr[i], i)
                     if z["touches"] >= CFG["sr_min_touch"]]
            # Rebuilt every third bar, not every bar. Zones move slowly and
            # this is the most expensive loop in the file.
            next_calc = i + 3
        if not zones or i - last_idx < CFG["cooldown_bars"]:
            continue

        b, p, a = d.c[i], d.c[i - 1], d.atr[i]
        brk = CFG["sr_break_atr"] * a
        best: dict | None = None

        def take(side: str, z: dict, kind: str) -> None:
            nonlocal best
            if best is None or z["strength"] > best["z"]["strength"]:
                best = {"side": side, "z": z, "kind": kind}

        for z in zones:
            if mode != "breakout":
                # Traded into the zone and closed back out of it.
                if (z["lows"] >= z["highs"] and b["l"] <= z["hi"]
                        and b["l"] >= z["lo"] - brk and b["c"] > z["hi"]
                        and p["c"] > z["lo"]):
                    take("LONG", z, "bounce")
                if (z["highs"] >= z["lows"] and b["h"] >= z["lo"]
                        and b["h"] <= z["hi"] + brk and b["c"] < z["lo"]
                        and p["c"] < z["hi"]):
                    take("SHORT", z, "bounce")
            if mode != "bounce":
                if p["c"] <= z["hi"] and b["c"] > z["hi"] + brk:
                    take("LONG", z, "breakout")
                if p["c"] >= z["lo"] and b["c"] < z["lo"] - brk:
                    take("SHORT", z, "breakout")

        if not best:
            continue
        last_idx = i
        out.append({"idx": i, **best})
    return out


def scan_sr(sym: str, d: Series) -> list[dict]:
    out = []
    for hit in sr_hits(d):
        i, z, side, kind = hit["idx"], hit["z"], hit["side"], hit["kind"]
        a = d.atr[i]
        stop_px = None
        if CFG["sr_zone_stop"] and a > 0:
            s = (z["lo"] - 0.3 * a) if side == "LONG" else (z["hi"] + 0.3 * a)
            dist = abs(d.close[i] - s)
            # Fall back to the ATR stop if the zone puts it absurdly near or far.
            if 0.3 * a < dist < 3.5 * a:
                stop_px = s
        lvl = rnd(z["mid"], d.close[i])
        why = [
            (f"Bounce off support {lvl}" if side == "LONG"
             else f"Rejected at resistance {lvl}") if kind == "bounce"
            else (f"Broke above resistance {lvl}" if side == "LONG"
                  else f"Broke below support {lvl}"),
            f"{z['touches']} touches",
            "stop placed beyond the zone" if stop_px is not None else "ATR stop",
        ]
        tag = ("S&R bounce" if kind == "bounce" else "S&R break") \
            + " x" + str(z["touches"])
        s = build_signal(sym, d, i, side, tag, why, stop_px, "sr")
        if s:
            s["zone"] = {"lo": z["lo"], "hi": z["hi"],
                         "touches": z["touches"], "kind": z["kind"]}
            out.append(s)
    return out


# --------------------------------------------------------- target trend

TT_MULTS = [5, 10, 15, 20, 25]


def tt_series(d: Series) -> dict:
    n = len(d.c)
    band = [x * 0.8 for x in sma(atr(d.c, 200), 200)]
    hi = sma([x["h"] for x in d.c], CFG["tt_len"])
    lo = sma([x["l"] for x in d.c], CFG["tt_len"])
    upper = [hi[i] + band[i] for i in range(n)]
    lower = [lo[i] - band[i] for i in range(n)]
    trend: list[bool | None] = [None] * n
    t: bool | None = None
    for i in range(1, n):
        c, p = d.close[i], d.close[i - 1]
        if p <= upper[i - 1] and c > upper[i]:
            t = True
        elif p >= lower[i - 1] and c < lower[i]:
            t = False
        trend[i] = t
    return {"trend": trend, "upper": upper, "lower": lower, "band": band}


def tt_flips(d: Series) -> list[dict]:
    s = tt_series(d)
    out = []
    for i in range(1, len(d.c)):
        if s["trend"][i] is None or s["trend"][i] == s["trend"][i - 1]:
            continue
        out.append({"idx": i, "side": "LONG" if s["trend"][i] else "SHORT",
                    "stop": s["lower"][i] if s["trend"][i] else s["upper"][i],
                    "band": s["band"][i]})
    return out


def scan_tt(sym: str, d: Series) -> list[dict]:
    out = []
    for f in tt_flips(d):
        i = f["idx"]
        if i < 210 or i > len(d.c) - 2:
            continue
        long = f["side"] == "LONG"
        band = f["band"]
        if not (band > 0):
            continue
        stop = f["stop"]
        why = [
            f"Target Trend({CFG['tt_len']}) flipped {'up' if long else 'down'}",
            "close crossed above SMA(high) + band" if long
            else "close crossed below SMA(low) - band",
            "stop rides the opposite band",
        ]
        sig = build_signal(sym, d, i, "LONG" if long else "SHORT",
                           f"TT {CFG['tt_len']}", why, stop, "tt",
                           unit=band, mults=TT_MULTS)
        if sig:
            out.append(sig)
    return out


# ----------------------------------------------------------- chart patterns

PAT_NAMES = {
    "dbot": "Double bottom", "dtop": "Double top",
    "ihs": "Inverted head & shoulders", "hs": "Head & shoulders",
    "fwedge": "Falling wedge", "rwedge": "Rising wedge",
    "atri": "Ascending triangle", "dtri": "Descending triangle",
    "stri": "Symmetrical triangle", "rect": "Rectangle",
    "bflag": "Bull flag", "sflag": "Bear flag",
    "bpen": "Bullish pennant", "spen": "Bearish pennant",
    "cup": "Cup and handle",
}


def pat_line(a: dict, b: dict):
    dx = b["i"] - a["i"]
    if not dx:
        return None
    m = (b["price"] - a["price"]) / dx
    return (m, lambda x: a["price"] + m * (x - a["i"]))


def broke_up(d: Series, i: int, lvl: float) -> bool:
    return d.close[i] > lvl and d.close[i - 1] <= lvl


def broke_down(d: Series, i: int, lvl: float) -> bool:
    return d.close[i] < lvl and d.close[i - 1] >= lvl


def pat_at(d: Series, i: int, hi: list[dict], lo: list[dict]) -> dict | None:
    """One pattern found at bar i, or nothing. `hi`/`lo` are the recent
    confirmed pivots, oldest first."""
    px, a = d.close[i], d.atr[i]
    if not (a > 0) or not (px > 0):
        return None
    tol = CFG["pat_tol"] / 100
    flat = CFG["pat_flat"] / 100
    min_h = CFG["pat_min_height"] * a

    def near(x: float, y: float) -> bool:
        return abs(x - y) / ((x + y) / 2) <= tol

    def wide(a1: dict, b1: dict) -> bool:
        # Three bars apart is not a double bottom, it is two candles. Noise
        # throws up plenty of those, and counting them is how a pattern
        # scanner ends up finding a pattern every time you look.
        return abs(b1["i"] - a1["i"]) >= CFG["pat_min_span"]

    def out(kind, side, level, stop, height, why):
        if not (height >= min_h):
            return None
        # A stop on the wrong side of the entry is not a stop.
        if (stop >= px) if side == "LONG" else (stop <= px):
            return None
        return {"kind": kind, "side": side, "idx": i, "level": level,
                "stop": stop, "height": height,
                "why": [PAT_NAMES[kind] + " - broke "
                        + ("up" if side == "LONG" else "down")] + why}

    H, L = hi[-3:], lo[-3:]

    # ---- double bottom / double top
    if len(L) >= 2 and len(H) >= 1:
        l1, l2 = L[-2], L[-1]
        mids = [h for h in H if l1["i"] < h["i"] < l2["i"]]
        if mids and wide(l1, l2) and near(l1["price"], l2["price"]) \
                and broke_up(d, i, mids[-1]["price"]):
            base = min(l1["price"], l2["price"])
            r = out("dbot", "LONG", mids[-1]["price"], base - 0.25 * a,
                    mids[-1]["price"] - base,
                    [f"two lows at {rnd(base, px)} held",
                     "broke the high between them"])
            if r:
                return r
    if len(H) >= 2 and len(L) >= 1:
        h1, h2 = H[-2], H[-1]
        mids = [x for x in L if h1["i"] < x["i"] < h2["i"]]
        if mids and wide(h1, h2) and near(h1["price"], h2["price"]) \
                and broke_down(d, i, mids[-1]["price"]):
            top = max(h1["price"], h2["price"])
            r = out("dtop", "SHORT", mids[-1]["price"], top + 0.25 * a,
                    top - mids[-1]["price"],
                    [f"two highs at {rnd(top, px)} rejected",
                     "broke the low between them"])
            if r:
                return r

    # ---- head and shoulders, both ways
    if len(H) >= 3 and len(L) >= 2:
        s1, head, s2 = H[-3:]
        necks = [x for x in L if s1["i"] < x["i"] < s2["i"]][-2:]
        if len(necks) == 2 and wide(s1, s2) and near(s1["price"], s2["price"]) \
                and head["price"] > max(s1["price"], s2["price"]) * (1 + tol):
            ln = pat_line(necks[0], necks[1])
            neck = ln[1](i) if ln else min(necks[0]["price"], necks[1]["price"])
            if broke_down(d, i, neck):
                r = out("hs", "SHORT", neck, s2["price"] + 0.25 * a,
                        head["price"] - neck,
                        [f"head at {rnd(head['price'], px)} above both shoulders",
                         "neckline broken"])
                if r:
                    return r
    if len(L) >= 3 and len(H) >= 2:
        s1, head, s2 = L[-3:]
        necks = [x for x in H if s1["i"] < x["i"] < s2["i"]][-2:]
        if len(necks) == 2 and wide(s1, s2) and near(s1["price"], s2["price"]) \
                and head["price"] < min(s1["price"], s2["price"]) * (1 - tol):
            ln = pat_line(necks[0], necks[1])
            neck = ln[1](i) if ln else max(necks[0]["price"], necks[1]["price"])
            if broke_up(d, i, neck):
                r = out("ihs", "LONG", neck, s2["price"] - 0.25 * a,
                        neck - head["price"],
                        [f"head at {rnd(head['price'], px)} below both shoulders",
                         "neckline broken"])
                if r:
                    return r

    # ---- two trendlines: wedges, triangles, rectangles
    if len(H) >= 2 and len(L) >= 2:
        hA, hB, lA, lB = H[-2], H[-1], L[-2], L[-1]
        upper, lower = pat_line(hA, hB), pat_line(lA, lB)
        if upper and lower:
            u_now, l_now = upper[1](i), lower[1](i)
            span = max(hB["i"], lB["i"]) - min(hA["i"], lA["i"])
            if span < CFG["pat_min_span"]:
                return None
            # Slope as a fraction of price across the whole shape, so "flat"
            # means the same on a $0.02 coin as on a $60,000 one.
            u_slope = upper[0] * span / px
            l_slope = lower[0] * span / px
            start = min(hA["i"], lA["i"])
            wA = abs(upper[1](start) - lower[1](start))
            wB = abs(u_now - l_now)
            converging = 0 < wB < wA * 0.75
            u_flat, l_flat = abs(u_slope) < flat, abs(l_slope) < flat
            height = wA

            def why2(t):
                return [t, "measured from the widest part of the shape"]

            if converging and u_slope < -flat and l_slope < -flat \
                    and broke_up(d, i, u_now):
                r = out("fwedge", "LONG", u_now, min(lB["price"], l_now) - 0.25 * a,
                        height, why2("both edges sloping down and closing in"))
                if r:
                    return r
            if converging and u_slope > flat and l_slope > flat \
                    and broke_down(d, i, l_now):
                r = out("rwedge", "SHORT", l_now, max(hB["price"], u_now) + 0.25 * a,
                        height, why2("both edges sloping up and closing in"))
                if r:
                    return r
            if u_flat and l_slope > flat and broke_up(d, i, u_now):
                r = out("atri", "LONG", u_now, min(lB["price"], l_now) - 0.25 * a,
                        height, why2("flat highs, rising lows"))
                if r:
                    return r
            if l_flat and u_slope < -flat and broke_down(d, i, l_now):
                r = out("dtri", "SHORT", l_now, max(hB["price"], u_now) + 0.25 * a,
                        height, why2("flat lows, falling highs"))
                if r:
                    return r
            if converging and u_slope < -flat and l_slope > flat:
                if broke_up(d, i, u_now):
                    r = out("stri", "LONG", u_now, l_now - 0.25 * a, height,
                            why2("range squeezing from both sides"))
                    if r:
                        return r
                if broke_down(d, i, l_now):
                    r = out("stri", "SHORT", l_now, u_now + 0.25 * a, height,
                            why2("range squeezing from both sides"))
                    if r:
                        return r
            if u_flat and l_flat:
                if broke_up(d, i, u_now):
                    r = out("rect", "LONG", u_now, l_now - 0.25 * a, height,
                            why2("range broken to the upside"))
                    if r:
                        return r
                if broke_down(d, i, l_now):
                    r = out("rect", "SHORT", l_now, u_now + 0.25 * a, height,
                            why2("range broken to the downside"))
                    if r:
                        return r

    # ---- flags and pennants
    cons, imp = CFG["pat_flag_pause"], CFG["pat_flag_run"]
    s_i, m_i = i - cons - imp, i - cons
    if s_i >= 1:
        pole_lo = min(d.c[k]["l"] for k in range(s_i, m_i + 1))
        pole_hi = max(d.c[k]["h"] for k in range(s_i, m_i + 1))
        # The consolidation stops at the bar *before* this one. Including the
        # current bar would mean the price had to break a high it was itself
        # setting, so a flag could never fire.
        run_lo = min(d.c[k]["l"] for k in range(m_i, i))
        run_hi = max(d.c[k]["h"] for k in range(m_i, i))
        pole, pause = pole_hi - pole_lo, run_hi - run_lo
        up = d.close[m_i] > d.close[s_i]
        strong = pole >= CFG["pat_flag_atr"] * a
        # A "flag" as tall as its own pole is just a range, and calling it a
        # flag would be the app flattering the chart.
        shallow = 0 < pause < pole * 0.55
        if strong and shallow:
            tight = pause < pole * 0.3
            if up and broke_up(d, i, run_hi):
                r = out("bpen" if tight else "bflag", "LONG", run_hi,
                        run_lo - 0.25 * a, pole,
                        [f"a {round(pole / a)}xATR run up, then a tight pause",
                         "broke out of the pause the same way"])
                if r:
                    return r
            if (not up) and broke_down(d, i, run_lo):
                r = out("spen" if tight else "sflag", "SHORT", run_lo,
                        run_hi + 0.25 * a, pole,
                        [f"a {round(pole / a)}xATR run down, then a tight pause",
                         "broke out of the pause the same way"])
                if r:
                    return r

    # ---- cup and handle
    W = CFG["pat_cup_bars"]
    if i - W >= 1:
        low_i, low_v = i - W, float("inf")
        for k in range(i - W, i + 1):
            if d.c[k]["l"] < low_v:
                low_v, low_i = d.c[k]["l"], k
        left_end = i - W + int(W * 0.25)
        right_start = i - int(W * 0.25)
        left_rim = max(d.c[k]["h"] for k in range(i - W, left_end + 1))
        right_rim = max(d.c[k]["h"] for k in range(right_start, i + 1))
        rim = min(left_rim, right_rim)
        middle = (i - W + W * 0.25) < low_i < (i - W * 0.2)
        depth = rim - low_v
        if middle and near(left_rim, right_rim) and depth > 0 \
                and broke_up(d, i, rim):
            handle = min(d.c[k]["l"] for k in range(right_start, i + 1))
            if rim - handle < depth * 0.5:
                r = out("cup", "LONG", rim, handle - 0.25 * a, depth,
                        ["rounded base with matching rims",
                         "shallow handle, then through the rim"])
                if r:
                    return r
    return None


def pat_hits(d: Series) -> list[dict]:
    c = d.c
    allp = pivots(c, CFG["pat_pivot"])
    out: list[dict] = []
    if len(allp) < 4 or len(c) < 220:
        return out
    p, last = 0, -999
    for i in range(210, len(c) - 1):
        while p < len(allp) and allp[p]["conf"] <= i:
            p += 1
        if i - last < CFG["pat_cooldown"]:
            continue
        hi: list[dict] = []
        lo: list[dict] = []
        for k in range(p - 1, -1, -1):
            if i - allp[k]["i"] > CFG["pat_max_age"]:
                break
            (hi if allp[k]["type"] == "H" else lo).insert(0, allp[k])
            if len(hi) >= 4 and len(lo) >= 4:
                break
        if len(hi) < 2 or len(lo) < 2:
            continue
        hit = pat_at(d, i, hi, lo)
        if hit:
            out.append(hit)
            last = i
    return out


def scan_pat(sym: str, d: Series) -> list[dict]:
    out = []
    for hit in pat_hits(d):
        # Targets in the pattern's own height — the classic projection — not
        # in ATR.
        sig = build_signal(sym, d, hit["idx"], hit["side"],
                           PAT_NAMES[hit["kind"]], hit["why"], hit["stop"],
                           "pat", unit=hit["height"], mults=CFG["pat_targets"])
        if sig:
            sig["pattern"] = hit["kind"]
            sig["pat_level"] = rnd(hit["level"], sig["entry"])
            out.append(sig)
    return out


# ------------------------------------------------------- open/close cross

def aggregate(c: list[dict], n: int) -> list[dict]:
    """Group candles into higher-timeframe ones. Only complete groups are
    produced, which is what keeps this non-repainting."""
    if n <= 1:
        return [dict(x, endIdx=i) for i, x in enumerate(c)]
    out = []
    for i in range(0, len(c) - n + 1, n):
        g = c[i:i + n]
        out.append({"t": g[0]["t"], "o": g[0]["o"], "c": g[-1]["c"],
                    "h": max(x["h"] for x in g), "l": min(x["l"] for x in g),
                    "v": sum(x["v"] for x in g), "endIdx": i + n - 1})
    return out


def ma_variant(kind: str, src: list[float], n: int,
               vol: list[float] | None = None) -> list[float]:
    """The moving-average family the Open/Close Cross script offers. Only the
    ones the app actually exposes are here; anything else falls back to SMA
    rather than silently computing something different from the phone."""
    kind = (kind or "SMMA").upper()
    if kind == "EMA":
        return ema(src, n)
    if kind == "WMA":
        return wma(src, n)
    if kind == "TEMA":
        e1 = ema(src, n)
        e2 = ema(e1, n)
        e3 = ema(e2, n)
        return [3 * (e1[i] - e2[i]) + e3[i] for i in range(len(src))]
    if kind == "DEMA":
        e1 = ema(src, n)
        e2 = ema(e1, n)
        return [2 * e1[i] - e2[i] for i in range(len(src))]
    if kind == "HULLMA":
        half = wma(src, max(1, round(n / 2)))
        full = wma(src, n)
        raw = [2 * half[i] - full[i] for i in range(len(src))]
        return wma(raw, max(1, round(math.sqrt(n))))
    if kind == "VWMA" and vol:
        pv = [src[i] * vol[i] for i in range(len(src))]
        a, b = sma(pv, n), sma(vol, n)
        return [a[i] / b[i] if b[i] else src[i] for i in range(len(src))]
    if kind == "SMMA":
        base = sma_win(src, n)
        o = [0.0] * len(src)
        started = False
        for i in range(len(src)):
            if not started:
                o[i] = base[i]
                started = i >= n - 1
            else:
                o[i] = (o[i - 1] * (n - 1) + src[i]) / n
        return o
    return sma(src, n)


def occ_crosses(c: list[dict]) -> list[dict]:
    n = max(1, round(CFG["occ_mult"]))
    agg = aggregate(c, n)
    if len(agg) < CFG["occ_ma_len"] + 3:
        return []
    vol = [x["v"] for x in agg]
    close_ma = ma_variant(CFG["occ_ma_type"], [x["c"] for x in agg],
                          CFG["occ_ma_len"], vol)
    open_ma = ma_variant(CFG["occ_ma_type"], [x["o"] for x in agg],
                         CFG["occ_ma_len"], vol)
    out = []
    for k in range(1, len(agg)):
        a0, b0, a1, b1 = close_ma[k - 1], open_ma[k - 1], close_ma[k], open_ma[k]
        if a0 <= b0 and a1 > b1:
            out.append({"idx": agg[k]["endIdx"], "side": "LONG"})
        elif a0 >= b0 and a1 < b1:
            out.append({"idx": agg[k]["endIdx"], "side": "SHORT"})
    return out


def scan_occ(sym: str, d: Series) -> list[dict]:
    crosses = occ_crosses(d.c)
    out = []
    for k, x in enumerate(crosses):
        i = x["idx"]
        if i < 210 or i > len(d.c) - 2:
            continue
        sig = build_signal(
            sym, d, i, x["side"],
            f"{CFG['occ_ma_type']}{CFG['occ_ma_len']}x{CFG['occ_mult']}",
            ["close/open cross"], None, "occ")
        if not sig:
            continue
        # The strategy's own exit: the pair crossing back the other way.
        if CFG["occ_exit_on_cross"] and k + 1 < len(crosses):
            sig["exit_idx"] = crosses[k + 1]["idx"]
        out.append(sig)
    return out


SCANNERS = {"confluence": scan_confluence, "ut": scan_ut, "brk": scan_brk,
            "rsi": scan_rsi, "mr": scan_mr, "mom": scan_mom,
            "occ": scan_occ, "tt": scan_tt, "sr": scan_sr, "pat": scan_pat}


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


def scan_coin(sym: str, bars: dict[str, list[dict]]) -> dict:
    """Every strategy, on every timeframe in the ladder, for one coin.

    A strategy is not right or wrong in the abstract — it is right on the
    period the coin actually moves on. Judging a coin only on whichever chart
    period happened to be configured was the old device behaviour and it was
    arbitrary. So the coin is the unit: run the whole set on 15m, 1h and 4h,
    then keep the single best signal per direction.

    Best means highest confidence, and ties go to the fresher one. Two
    timeframes agreeing does not stack into a stronger signal here; that would
    be double-counting the same price twice."""
    all_open: list[dict] = []
    all_closed: list[dict] = []
    all_pending: list[dict] = []
    for stf, ttf in CFG["tf_ladder"]:
        if not bars.get(stf) or not bars.get(ttf):
            continue
        keep_s, keep_t = CFG["signal_tf"], CFG["trend_tf"]
        CFG["signal_tf"], CFG["trend_tf"] = stf, ttf
        try:
            res = scan_symbol(sym, bars[stf], bars[ttf])
        finally:
            CFG["signal_tf"], CFG["trend_tf"] = keep_s, keep_t
        all_open.extend(res["open"])
        all_closed.extend(res["closed"])
        all_pending.extend(res.get("pending", []))

    for s in all_open:
        s["confidence"] = confidence(s)
    best: dict[tuple[str, str], dict] = {}
    for s in all_open:
        k = (s["symbol"], s["side"])
        cur = best.get(k)
        if cur is None or (s["confidence"], s["at"]) > (cur["confidence"], cur["at"]):
            best[k] = s
    for s in all_pending:
        s["confidence"] = confidence(s)
    live_keys = {(s["symbol"], s["side"]) for s in best.values()}
    bestp: dict[tuple[str, str], dict] = {}
    for s in all_pending:
        k = (s["symbol"], s["side"])
        if k in live_keys:
            continue          # it fired on another period; not pending at all
        cur = bestp.get(k)
        if cur is None or (s["confidence"], s["at"]) > (cur["confidence"], cur["at"]):
            bestp[k] = s
    return {"open": list(best.values()), "closed": all_closed,
            "pending": list(bestp.values())}


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
    open_, closed, pending = [], [], []
    bar_ms = (d.c[1]["t"] - d.c[0]["t"]) if len(d.c) > 1 else 3_600_000
    fresh_cut = d.c[-1]["t"] - CFG["max_age_bars"] * bar_ms
    for s in found:
        n, of = agreement(d, s["idx"], s["side"])
        s["agree"], s["agree_of"] = n, of

        # Every reason this one is not being published, collected rather than
        # returned from on first sight. A setup blocked by one thing is a
        # trade waiting for that thing to happen; a setup blocked by four is
        # just a bad idea. Only the first kind is worth watching.
        blocked: list[str] = []
        bias = htf_bias(h, s["at"]) if CFG["require_htf"] else None
        if bias and bias != s["side"]:
            blocked.append(f"the {CFG['trend_tf']} trend is the other way")
        if CFG["min_agree"] and n < CFG["min_agree"]:
            blocked.append(f"only {n} of {of} reads back it "
                           f"({CFG['min_agree']} needed)")

        track(s, d)
        if s.get("closed_at"):
            closed.append(s)
            continue

        stale = s["at"] < fresh_cut or (
            CFG["max_age_hours"]
            and (now_ms() - s["at"]) > CFG["max_age_hours"] * 3_600_000)
        s["chase"] = chase_check(d, s)
        if s["chase"].get("verdict") == "late":
            blocked.append("price has already run too far past the entry")

        if not blocked and not stale:
            open_.append(s)
            continue

        # The pipeline. Not published as a signal, but shown so you can see
        # what is one condition away — and, more usefully, what that condition
        # is. A stale one is not waiting for anything; it has simply missed.
        if blocked and not stale and len(blocked) == 1:
            p = dict(s)
            p["waiting_for"] = blocked[0]
            p["pending"] = True
            pending.append(p)

    # newest first, one per coin and direction
    open_.sort(key=lambda x: -x["at"])
    seen, uniq = set(), []
    for s in open_:
        k = (s["symbol"], s["side"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(s)

    pending.sort(key=lambda x: -x["at"])
    pseen, puniq = set(), []
    for s in pending:
        k = (s["symbol"], s["side"])
        if k in pseen or k in seen:      # already a real signal — not pending
            continue
        pseen.add(k)
        puniq.append(s)

    return {"open": uniq, "closed": closed, "pending": puniq}


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
COINS = ROOT / "bot" / "coins.txt"


def wanted_coins() -> tuple[list[str], int]:
    """What bot/coins.txt asks for: (named coins, top-N request).

    One symbol per line; blank lines and anything after a # ignored. Kept as
    a plain file rather than a repository variable so it can be edited in the
    browser on a phone, and so the list is visible in the repository instead
    of hidden in a settings page.

    A line of the form `TOP 100` means "the hundred biggest by turnover".
    That is deliberately not expanded into a hundred written-out names: the
    top hundred changes every week, and a list frozen on the day you wrote it
    slowly becomes a list of yesterday's coins without ever looking wrong.
    Named coins and a TOP line can be combined — yours are scanned first,
    then the biggest are added until the count is reached.

    Written loosely on purpose: `btc`, `BTC`, `BTC/USDT` and `BTCUSDT` all
    mean the same thing. A list that rejects your input because you typed it
    the wrong way is a list you stop maintaining."""
    try:
        raw = COINS.read_text()
    except Exception:                      # noqa: BLE001 - absent is normal
        return [], 0
    out: list[str] = []
    top = 0
    for line in raw.splitlines():
        txt = line.split("#")[0].strip()
        if not txt:
            continue
        m = re.match(r"^TOP\s*([0-9]{1,4})$", txt, re.I)
        if m:
            top = max(top, int(m.group(1)))
            continue
        sym = txt.upper().replace("-", "").replace("/", "").replace(" ", "")
        if not sym:
            continue
        if not sym.endswith("USDT"):
            sym += "USDT"
        if sym not in out:
            out.append(sym)
    return out, top

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


# Fewer coins per run than before, because each one now costs four downloads
# instead of two. The full list is still covered in the same wall-clock time;
# the work is just spread over more runs.
# Coins per run. Each one now costs a download per timeframe, so a wide
# ladder has to be paid for with a smaller slice or the run stops fitting
# inside the gap between runs. Set SCAN_BATCH yourself to override.
BATCH = env_int("SCAN_BATCH", 12)


# =========================================================================
# HOW OFTEN YOU WANT SIGNALS — set with the SCAN_RATE repository variable
#
# Every one of these is the same trade in a different place: more signals for
# a lower share of them being right. There is no setting that produces more
# good signals; there is only a setting that produces more signals, of which
# the same proportion or fewer are good.
#
# The knobs are the honest ones — which chart periods get looked at, how much
# confluence a setup needs, and how many independent reads have to back it.
# Nothing here changes the maths of a trade, only how selective it is.
# =========================================================================

def every_pair(tfs: list[str], gap: int = 2) -> list[tuple[str, str]]:
    """Every signal period paired with every higher trend period.

    `gap` is how many rungs above the signal the trend has to sit. One rung is
    not a trend filter — 15m filtered by 30m is the same information twice —
    so the default is two.

    This is cheap in the place that matters: the cost of a scan is downloads,
    and a period is downloaded once however many pairs use it. Twelve periods
    is twelve downloads and about fifty pairs.

    It is not cheap statistically, and that is the part to be honest about.
    Fifty attempts per coin per direction means something will almost always
    clear the bar, whether or not the coin is doing anything. The sweep keeps
    one signal per coin and side, so this raises the chance of finding
    something rather than the number of things found — but the something is
    more likely to be an accident than it would be from three attempts."""
    out = []
    for i, s in enumerate(tfs):
        for h in tfs[i + gap:]:
            out.append((s, h))
    return out


# Periods every venue in SOURCES actually serves. 8h and 3d are Binance-only
# and Binance refuses the runner, so they are left out rather than failing on
# every coin.
_COMMON_TF = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h",
              "1d", "1w"]

RATES: dict[str, dict[str, Any]] = {
    # A handful a day. Only the slower periods, and they have to be convincing.
    "calm": {"tf_ladder": [("1h", "4h"), ("4h", "1d")],
             "min_score": 7, "min_agree": 3, "require_htf": True,
             "adx_min": 22},
    # The default: three periods, trend has to agree, two of four reads.
    "normal": {"tf_ladder": [("15m", "1h"), ("1h", "4h"), ("4h", "1d")],
               "min_score": 6, "min_agree": 2, "require_htf": True,
               "adx_min": 20},
    # Eight periods rather than three, and the agreement floor down to one.
    #
    # More rungs is the honest way to find more: a coin that does nothing on
    # 15m may be setting up cleanly on 30m, and checking only three periods
    # was leaving those unseen. It costs one extra download per coin per
    # timeframe, which is why it is not the default.
    #
    # What it does not do is multiply the count: the sweep still keeps one
    # signal per coin and direction, so extra rungs raise the chance of
    # finding something on a given coin rather than stacking five versions of
    # the same idea.
    "busy": {"tf_ladder": [("5m", "15m"), ("15m", "1h"), ("30m", "2h"),
                           ("1h", "4h"), ("2h", "6h"), ("4h", "1d")],
             "min_score": 5, "min_agree": 1, "require_htf": True,
             "adx_min": 18},
    # Every period the exchange offers, from one minute to a day, with the
    # trend check still on. Eleven rungs.
    #
    # This is the honest ceiling for "look everywhere". Below about fifteen
    # minutes the strategies are mostly reading noise — a one-minute breakout
    # is a rounding error with a name — so the extra rungs raise the number of
    # signals a lot and the number of good ones very little. It is here
    # because you asked to see everything, and because the only way to find
    # out whether the fast periods work on your coins is to let them run and
    # read the Results tab in a fortnight.
    "max": {"tf_ladder": every_pair(_COMMON_TF, 2),
            "min_score": 4, "min_agree": 1, "require_htf": True,
            "adx_min": 15},
    # Everything the strategies can see, with the higher-timeframe check off.
    # This is a firehose and a bad way to trade; it is here because seeing the
    # raw output is sometimes the fastest way to understand what the thing
    # does.
    "flood": {"tf_ladder": every_pair(_COMMON_TF, 1),
              "min_score": 3, "min_agree": 0, "require_htf": False,
              "adx_min": 10},
}

RATE = (os.environ.get("SCAN_RATE") or "normal").strip().lower()
if RATE not in RATES:
    if RATE:
        print(f"SCAN_RATE={RATE!r} is not one of "
              f"{', '.join(RATES)} — using normal")
    RATE = "normal"
CFG.update(RATES[RATE])

# Every timeframe the ladder needs, downloaded once per coin. Computed after
# the rate is applied, or a faster setting would ask for candles nobody
# fetched.
TF_NEEDED = sorted({tf for pair in CFG["tf_ladder"] for tf in pair},
                   key=lambda x: ("mhdw".index(x[-1]), int(x[:-1])))

# Keep the run inside the gap between runs. Eleven downloads a coin at twelve
# coins is well over two minutes, and runs that overlap queue up and drift.
# Only applied when you have not set SCAN_BATCH yourself.
if not (os.environ.get("SCAN_BATCH") or "").strip():
    if len(TF_NEEDED) >= 9:
        BATCH = 5
    elif len(TF_NEEDED) >= 7:
        BATCH = 8
print(f"rate: {RATE} — {len(CFG['tf_ladder'])} ladders, "
      f"{len(TF_NEEDED)} downloads per coin, {BATCH} coins per run")
MIN_CONF_ALERT = env_int("MIN_CONFIDENCE", 0)


# =========================================================================
# THE BOOK
#
# Until now every run rebuilt each coin's signals from scratch, so the two
# found at 7:00 were simply replaced by whatever 7:15 found. From the outside
# that reads as signals vanishing for no reason, and it makes the list
# impossible to act on: you cannot take a trade that might not be there when
# you look again.
#
# So a published signal now has a life of its own. Later runs update it —
# price, targets hit, stop moved — and it leaves for one of exactly four
# reasons, all of which are things that actually happened:
#
#   * it hit its stop
#   * it reached its final target
#   * the same coin fired the opposite direction, which invalidates it
#   * it has sat unresolved for longer than book_hours
#
# Nothing else removes it. In particular, "this scan did not re-find it" does
# not, because a strategy that no longer sees an entry is not the same thing
# as a trade that ended.
# =========================================================================

def sig_key(s: dict) -> str:
    return f"{s['symbol']}|{s['side']}|{s['at']}|{s.get('strat', '')}"


def retrack(s: dict, d: Series) -> dict:
    """Replay a published signal against the newest candles.

    Works on a copy so a half-updated signal can never be written back if
    something throws part-way. The index is re-found by timestamp rather than
    trusted from last time, because the candle window slides: bar 300 of the
    last run is not bar 300 of this one."""
    out = dict(s)
    idx = None
    for i, b in enumerate(d.c):
        if b["t"] == s["at"]:
            idx = i
            break
    if idx is None or idx >= len(d.c) - 1:
        # The entry bar has scrolled out of the window. Keep the signal as it
        # stands rather than dropping it — losing a live trade because the
        # history got long is exactly the behaviour being fixed here.
        return out
    out["idx"] = idx
    # track() mutates, so hand it the copy and let it work.
    out.setdefault("hits", 0)
    out.setdefault("be", False)
    out["stop"] = out.get("initial_stop", out["stop"])
    for t in out.get("targets", []):
        t["hit"] = False
    track(out, d)
    out["last_price"] = d.close[-1]
    return out


def merge_book(kept: list[dict], fresh: list[dict], tracked: dict[str, dict],
               by_sym: dict) -> tuple[list[dict], list[str]]:
    book: dict[str, dict] = {}
    for s in kept:
        book[sig_key(s)] = s
    # Updated versions of things already published win over the stored copy.
    for k, s in tracked.items():
        book[k] = s
    logs = []
    added = flipped = 0
    for s in sorted(fresh, key=lambda x: x["at"]):
        if sig_key(s) in book:
            continue
        open_here = [b for b in book.values()
                     if b["symbol"] == s["symbol"] and not b.get("closed_at")]
        same = [b for b in open_here if b["side"] == s["side"]]
        if same:
            # The same coin, the same direction, already open. The trade you
            # were shown is still the trade; a second entry on top of it is
            # noise, and replacing it would be the exact behaviour being
            # fixed — the old one disappearing when a new one arrives.
            continue
        # The other direction, though, is a contradiction. Holding both at
        # once is not a hedge, it is the app failing to make up its mind.
        for b in open_here:
            if b["side"] != s["side"] and s["at"] > b["at"]:
                b["status"] = "flipped"
                b["closed_at"] = s["at"]
                flipped += 1
        still_open = [b for b in book.values()
                      if b["symbol"] == s["symbol"] and not b.get("closed_at")]
        if any(b["side"] != s["side"] for b in still_open):
            # An older signal in the other direction is already published and
            # still valid. It keeps the coin; this one is dropped rather than
            # sitting beside it saying the opposite.
            continue
        s["published"] = now_ms()
        book[sig_key(s)] = s
        added += 1

    resolved = [s for s in book.values() if s.get("closed_at")]
    live = []
    aged = 0
    for s in book.values():
        if s.get("closed_at"):
            continue
        born = s.get("published") or s["at"]
        if now_ms() - born > CFG["book_hours"] * 3_600_000:
            aged += 1
            continue
        px = by_sym.get(s["symbol"], {}).get("price")
        if px:
            s["last_price"] = px
        live.append(s)

    live.sort(key=lambda x: (-x.get("confidence", 0), -x["at"]))
    logs.append(f"book: {len(live)} live, {added} new, "
                f"{len(resolved)} resolved, {flipped} flipped, {aged} aged out")
    return live, logs


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:                      # noqa: BLE001
        return {"cursor": 0, "alerted": [], "record": {}, "runs": 0}


def save_state(s: dict) -> None:
    s["alerted"] = s.get("alerted", [])[-400:]
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=1))


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


# Printed as the very first line of every run. Two uploads that are both
# 1026 lines look identical from the outside, and a run that fails on old
# code while you are reading new code wastes an afternoon. This says which
# build actually executed.
BUILD = "S9 · 2026-08-12 · every period, every pair"


def main() -> int:
    print(f"build: {BUILD}")
    print(f"tls: {SSL_NOTE}")
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
    by_sym = {t["symbol"]: t for t in tk}

    # Your list wins if you have one. Anything in it the venue does not
    # carry is named rather than silently dropped — a coin quietly missing
    # from a list you wrote yourself is worse than one that complains.
    asked, top_n = wanted_coins()
    unknown = [s for s in asked if s not in by_sym]
    known = [s for s in asked if s in by_sym]
    if asked and not known and not top_n:
        print(f"none of the {len(asked)} coins in bot/coins.txt exist here "
              f"({SOURCE[0] if SOURCE else '?'}) — scanning by turnover instead")

    if top_n:
        # Your named coins first, then fill up with the biggest by turnover
        # until the count is reached. Naming a coin should always mean it is
        # scanned, even if it sits at number four hundred.
        universe = list(known)
        for t in tk:
            if len(universe) >= top_n:
                break
            if t["symbol"] not in universe:
                universe.append(t["symbol"])
        coin_source = (f"top {len(universe)} by turnover"
                       + (f", {len(known)} of them named" if known else ""))
    elif known:
        universe = known
        coin_source = f"bot/coins.txt ({len(known)} coins)"
    else:
        universe = [t["symbol"] for t in tk[:CFG["pairs"]]]
        coin_source = f"top {len(universe)} by turnover"

    if unknown:
        print(f"not available on this venue, skipped: {', '.join(unknown)}")
    print(f"coins: {coin_source}")

    cursor = state.get("cursor", 0) % max(1, len(universe))
    batch = [universe[(cursor + k) % len(universe)] for k in range(min(BATCH, len(universe)))]
    state["cursor"] = (cursor + len(batch)) % len(universe)

    # Carry forward what earlier runs found, so the file always describes the
    # whole universe rather than only the slice this run happened to cover.
    try:
        prev = json.loads(OUT.read_text())
    except Exception:                      # noqa: BLE001
        prev = {"signals": []}
    # Drop this batch's old entries (they are about to be replaced) and
    # anything for a coin no longer on the list. Without the second half,
    # narrowing your coins would leave the removed ones sitting in the file
    # for a day and a half, still showing on the page as if current.
    inplay = set(universe)
    kept = [s for s in prev.get("signals", [])
            if s["symbol"] not in batch and s["symbol"] in inplay]
    _ = kept

    # Anything already published for a coin in this batch gets re-tracked
    # against fresh candles: same trade, updated. That is what stops a signal
    # from disappearing simply because a later scan did not re-find it.
    open_prev = {sig_key(s): s for s in prev.get("signals", [])
                 if s["symbol"] in batch and not s.get("closed_at")}

    fresh, closed_all, failed = [], [], []
    pending_all: list[dict] = []
    skipped_tf: list[str] = []
    tracked: dict[str, dict] = {}
    for sym in batch:
        try:
            # One download per timeframe, reused as both the signal series and
            # the trend filter for the pair above it. Four requests cover three
            # complete (signal, trend) combinations — fetching each pair
            # separately would be six.
            bars: dict[str, list[dict]] = {}
            for tf in TF_NEEDED:
                try:
                    bars[tf] = klines(sym, tf, CFG["bars"])
                except Unsupported as e:
                    # One period this venue does not carry must not cost the
                    # coin. scan_coin skips any ladder whose bars are missing.
                    if tf not in skipped_tf:
                        skipped_tf.append(f"{tf}: {e}")
                except Exception as e:            # noqa: BLE001
                    if tf not in skipped_tf:
                        skipped_tf.append(f"{tf}: {e}")
            res = scan_coin(sym, bars)
            fresh.extend(res["open"])
            closed_all.extend(res["closed"])
            pending_all.extend(res.get("pending", []))
            for k, s in list(open_prev.items()):
                if s["symbol"] != sym:
                    continue
                stf = s.get("stf") or CFG["signal_tf"]
                if bars.get(stf):
                    tracked[k] = retrack(s, enrich(bars[stf]))
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

    allsig, book_log = merge_book(kept, fresh, tracked, by_sym)
    log.extend(book_log)

    payload = {
        "generated": now_ms(),
        "generated_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_seconds": round(time.time() - started, 1),
        "scanned": batch,
        "universe": len(universe),
        "coins": universe,
        "coin_source": coin_source,
        "coins_missing": unknown,
        "cursor": state["cursor"],
        "runs": state.get("runs", 0) + 1,
        "strategies": [STRAT_NAMES[k] for k in STRATS],
        "not_ported": NOT_PORTED,
        "book_hours": CFG["book_hours"],
        "config": {k: CFG[k] for k in
                   ("signal_tf", "trend_tf", "min_score", "min_agree",
                    "max_age_bars", "max_age_hours", "sl_atr",
                    "max_loss_pct", "max_lev", "book_hours")},
        "timeframes": [f"{a} / {b}" for a, b in CFG["tf_ladder"]],
        "rate": RATE,
        "failed": failed[:10],
        "skipped_timeframes": skipped_tf[:6],
        "signals": allsig[:120],
        # What is one condition away from firing, and which condition. Kept
        # separate from `signals` so nothing downstream can mistake a queue
        # for a trade.
        "pending": sorted(pending_all,
                          key=lambda x: (-x.get("confidence", 0), -x["at"]))[:40],
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


def merge_published(mine_path: str, mystate_path: str) -> int:
    """Fold this run's results into whatever is on the branch now.

    Two writers can land between one run reading signals.json and the same run
    pushing it: a second scan, or you uploading a file. Git then tries to
    rebase two JSON documents line by line, which is not a thing that can
    work — it produced a conflict marker in the middle of a signal and failed
    the job.

    So the merge happens here, where the documents mean something. Both books
    are unioned by signal key; where the same signal exists in both, this
    run's copy wins because it was tracked against newer candles. Nobody's
    findings are discarded, which is the part a `git checkout --ours` would
    have got wrong."""
    mine = json.loads(pathlib.Path(mine_path).read_text())
    try:
        theirs = json.loads(OUT.read_text())
    except Exception:                      # noqa: BLE001 - nothing there yet
        theirs = {"signals": []}

    book: dict[str, dict] = {}
    for s in theirs.get("signals", []):
        book[sig_key(s)] = s
    replaced = 0
    for s in mine.get("signals", []):
        k = sig_key(s)
        if k in book:
            replaced += 1
        book[k] = s

    # A coin this run did not look at keeps whatever the other writer said
    # about it; a coin it did look at is described by this run alone.
    scanned = set(mine.get("scanned", []))
    live = [s for s in book.values()
            if s["symbol"] not in scanned
            or sig_key(s) in {sig_key(x) for x in mine.get("signals", [])}]
    live.sort(key=lambda x: (-x.get("confidence", 0), -x["at"]))

    out = dict(mine)
    out["signals"] = live[:200]
    out["merged_with"] = theirs.get("generated")
    OUT.write_text(json.dumps(out, indent=1))

    # State is a counter and a cursor; the higher run count is the later one.
    try:
        ours_st = json.loads(pathlib.Path(mystate_path).read_text())
        theirs_st = json.loads(STATE.read_text())
        if theirs_st.get("runs", 0) > ours_st.get("runs", 0):
            ours_st["runs"] = theirs_st["runs"] + 1
        STATE.write_text(json.dumps(ours_st, indent=1))
    except Exception as e:                 # noqa: BLE001
        print(f"state merge skipped: {e}")

    print(f"merged: {len(live)} live "
          f"({replaced} updated, {len(theirs.get('signals', []))} were there)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 3 and sys.argv[1] == "--merge":
        raise SystemExit(merge_published(sys.argv[2], sys.argv[3]))
    raise SystemExit(main())
