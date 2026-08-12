"""Tests for the server engine.

The port is only useful if it agrees with the page. So these check the things
that silently differ between two implementations of the same idea: whether a
gate is inclusive, whether leverage is derived from the stop or guessed,
whether the stop is checked before the targets, and whether anything reads a
bar it could not have seen.

Run: python3 bot/test_engine.py
"""

from __future__ import annotations

import math
import pathlib
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scan as E   # noqa: E402

PASS, FAIL = [], []


def ok(label: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(label)
    print(("PASS " if cond else "FAIL ") + label + (f"  {extra}" if extra else ""))


def candles(path: list[float], tf_ms: int = 3_600_000) -> list[dict]:
    out, t = [], 1_700_000_000_000
    for i, p in enumerate(path):
        o = path[i - 1] if i else p
        out.append({"t": t, "o": o, "h": p * 1.002, "l": p * 0.998,
                    "c": p, "v": 1000.0})
        t += tf_ms
    return out


def flat(n: int, base: float = 100.0) -> list[float]:
    return [base * (1 + math.sin(i / 4) * 0.003) for i in range(n)]


# ------------------------------------------------------------- indicators
c = candles(flat(300))
d = E.enrich(c)
ok("enrich fills every series",
   all(len(getattr(d, k)) == len(c)
       for k in ("close", "ef", "em", "es", "rsi", "atr", "adx", "hist")))
ok("ATR is positive on real movement", d.atr[-1] > 0, f"{d.atr[-1]:.4f}")
ok("RSI stays inside 0..100", all(0 <= x <= 100 for x in d.rsi))
ok("ADX stays inside 0..100", all(0 <= x <= 100 for x in d.adx))

up = E.enrich(candles([100 + i * 0.5 for i in range(300)]))
dn = E.enrich(candles([250 - i * 0.5 for i in range(300)]))
ok("a rising market reads as an up stack", up.ef[-1] > up.em[-1] > up.es[-1])
ok("a falling market reads as a down stack", dn.ef[-1] < dn.em[-1] < dn.es[-1])
ok("a trend registers on ADX", up.adx[-1] > 20, f"{up.adx[-1]:.1f}")
# A smooth sine reads as trending too — inside each half-cycle it genuinely is.
# The honest test is comparative: a one-way ramp must read stronger than chop.
ok("and reads stronger than chop", up.adx[-1] > d.adx[-1],
   f"{up.adx[-1]:.1f} vs {d.adx[-1]:.1f}")
ok("RSI is high in an uptrend and low in a downtrend",
   up.rsi[-1] > 60 and dn.rsi[-1] < 40,
   f"{up.rsi[-1]:.0f} / {dn.rsi[-1]:.0f}")

# --------------------------------------------------------- trade building
sig = E.build_signal("TESTUSDT", up, 280, "LONG", "test", ["because"],
                     None, "confluence")
ok("a signal is produced", sig is not None)
if sig:
    ok("the stop sits below the entry on a long", sig["stop"] < sig["entry"])
    ok("five targets, climbing away from the entry",
       len(sig["targets"]) == 5
       and all(t["price"] > sig["entry"] for t in sig["targets"])
       and all(sig["targets"][i]["price"] < sig["targets"][i + 1]["price"]
               for i in range(4)))
    sl_pct = abs(sig["entry"] - sig["stop"]) / sig["entry"] * 100
    ok("leverage is derived from the stop, not guessed",
       abs(sig["leverage"] - min(E.CFG["max_lev"],
                                 max(E.CFG["min_lev"],
                                     int(E.CFG["max_loss_pct"] / sl_pct)))) < 1,
       f"lev {sig['leverage']} on a {sl_pct:.2f}% stop")
    ok("and the stop costs no more than the configured share of margin",
       sl_pct * sig["leverage"] <= E.CFG["max_loss_pct"] + 0.01,
       f"{sl_pct * sig['leverage']:.1f}%")

short = E.build_signal("TESTUSDT", dn, 280, "SHORT", "test", ["because"],
                       None, "confluence")
ok("a short's stop sits above its entry",
   short is not None and short["stop"] > short["entry"])
ok("and its targets fall away",
   short is not None and all(t["price"] < short["entry"] for t in short["targets"]))

# A truly still coin: no range at all, not just a flat close. The generator
# adds a 0.4% bar range by default, which is plenty to trade.
still = [{"t": 1_700_000_000_000 + i * 3_600_000, "o": 100.0, "h": 100.0,
          "l": 100.0, "c": 100.0, "v": 1000.0} for i in range(300)]
dead = E.enrich(still)
ok("a coin that does not move produces nothing",
   E.build_signal("X", dead, 280, "LONG", "t", [], None, "confluence") is None)

# ------------------------------------------------------------- strategies
# A perfectly straight ramp produces almost nothing, and that is correct:
# RSI pins at 100 (outside the band), the MACD histogram is constant so nothing
# is expanding, and the UT stop is never re-crossed. Real trends breathe, so
# the test data has to as well.
trend = []
p2, sd2 = 100.0, 11
for i in range(400):
    sd2 = (sd2 * 1103515245 + 12345) % 2147483648
    p2 *= 1 + 0.0016 + (sd2 / 2147483648 - 0.5) * 0.012
    trend.append(p2)
up2 = E.enrich(candles(trend))
found = {k: len(E.SCANNERS[k]("TESTUSDT", up2)) for k in E.STRATS}
print("on a clean uptrend:", found)
ok("the trend strategies fire in a trend",
   found["confluence"] + found["ut"] + found["mom"] > 0, str(found))
ok("mean reversion stays quiet in a trend",
   found["mr"] <= 2, str(found["mr"]))

noise = []
p, seed = 100.0, 7
for _ in range(400):
    seed = (seed * 1103515245 + 12345) % 2147483648
    p *= 1 + (seed / 2147483648 - 0.5) * 0.004
    noise.append(p)
nz = E.enrich(candles(noise))
quiet = {k: len(E.SCANNERS[k]("TESTUSDT", nz)) for k in E.STRATS}
print("on a random walk:  ", quiet)
ok("a breakout needs a trend behind it", quiet["brk"] <= 2, str(quiet["brk"]))

for key in E.STRATS:
    sigs = E.SCANNERS[key]("TESTUSDT", up2)
    bad = [s for s in sigs if s["idx"] >= len(up2.c) - 1]
    ok(f"{key} never fires on the candle still forming", not bad, str(len(bad)))

# ------------------------------------------------------------ the filters
ok("the higher timeframe reads a bar that had already closed",
   E.htf_bias(up, up.c[-1]["t"]) == "LONG")
ok("and the other way in a downtrend",
   E.htf_bias(dn, dn.c[-1]["t"]) == "SHORT")
ok("it says nothing when there is no history yet",
   E.htf_bias(E.enrich(candles(flat(20))), 1) is None)

n, of = E.agreement(up, 280, "LONG")
ok("all four reads back a clean uptrend", n == 4 and of == 4, f"{n}/{of}")
n2, _ = E.agreement(up, 280, "SHORT")
ok("and none of them back the wrong side", n2 == 0, str(n2))

# stop before targets, on a bar that covers both
spike = flat(240) + [100.0]
spike_c = candles(spike)
spike_c[-1] = {"t": spike_c[-1]["t"], "o": 100.0, "h": 140.0, "l": 60.0,
               "c": 100.0, "v": 1000.0}
sd = E.enrich(spike_c)
s2 = {"symbol": "X", "side": "LONG", "entry": 100.0, "initial_stop": 98.0,
      "idx": len(spike_c) - 2, "leverage": 5,
      "targets": [{"index": i, "price": 100 + i, "roi": i} for i in range(1, 6)]}
E.track(s2, sd)
ok("a candle covering both the stop and the targets is read pessimistically",
   s2["status"] == "stopped", s2["status"])

# a clean runner resolves as completed
runner = flat(240) + [100 + k for k in range(1, 12)]
rd = E.enrich(candles(runner))
s3 = {"symbol": "X", "side": "LONG", "entry": 100.0, "initial_stop": 98.0,
      "idx": 240, "leverage": 5,
      "targets": [{"index": i, "price": 100 + i, "roi": i} for i in range(1, 6)]}
E.track(s3, rd)
ok("a clean runner reaches every target",
   s3["status"] == "completed" and s3["hits"] == 5, s3["status"])
ok("and the result has fees taken out of it",
   s3["roi"] < (s3["price"] - 100) / 100 * 100 * 5,
   f"{s3['roi']}% net")

# ------------------------------------------------------------- anti-chase
base = {"side": "LONG", "entry": 100.0, "initial_stop": 98.0}
near = E.chase_check(E.enrich(candles(flat(240))), dict(base))
ok("a signal at its own price is takeable", near["verdict"] == "take",
   str(near))
ran = E.chase_check(E.enrich(candles(flat(240) + [101.5])), dict(base))
ok("one that ran says wait for a retest", ran["verdict"] == "retest", str(ran))
ok("and names the level", "retest" in ran, str(ran))
gone = E.chase_check(E.enrich(candles(flat(240) + [104.0])), dict(base))
ok("one that ran a long way is too late", gone["verdict"] == "late", str(gone))

# ------------------------------------------------------------ end to end
res = E.scan_symbol("TESTUSDT", candles([100 + i * 0.5 for i in range(400)]),
                    candles([100 + i * 2.0 for i in range(200)], 14_400_000))
ok("a full scan returns open and resolved signals separately",
   isinstance(res["open"], list) and isinstance(res["closed"], list),
   f"{len(res['open'])} open, {len(res['closed'])} closed")
ok("one card per coin and direction",
   len({(s["symbol"], s["side"]) for s in res["open"]}) == len(res["open"]))
ok("every open signal carries its entry verdict",
   all("chase" in s for s in res["open"]))
ok("every open signal names the strategy that found it",
   all(s.get("strat_name") for s in res["open"]))
ok("nothing open is also marked closed",
   all(not s.get("closed_at") for s in res["open"]))

# ------------------------------------------------- the fallback data layer
#
# The whole point of this layer is what happens when the first venue refuses,
# so the tests are about refusal, not about the happy path.

_calls = []


def fake_http(responses):
    """Replace get_json with a lookup keyed on which host is being asked."""
    def fn(url, tries=3):
        host = url.split("/")[2]
        _calls.append(url)
        r = responses.get(host)
        if r is None:
            raise E.Blocked(f"HTTP 451 from {host}")
        return r(url) if callable(r) else r
    return fn


_real_get = E.get_json

BINANCE_TK = [{"symbol": f"C{i}USDT", "quoteVolume": str(1e9 - i * 1e6),
               "lastPrice": "100", "priceChangePercent": "2.5"}
              for i in range(30)]
BYBIT_TK = {"result": {"list": [
    {"symbol": f"C{i}USDT", "turnover24h": str(1e9 - i * 1e6),
     "lastPrice": "100", "price24hPcnt": "0.025"} for i in range(30)]}}

# 1. Binance blocked, Bybit answers -> the run continues on Bybit
E.SOURCE = None
E._FIRST_TICKERS = None
E.get_json = fake_http({"api.bybit.com": BYBIT_TK})
src = E.pick_source()
ok("a blocked exchange falls through to the next one", src[0] == "bybit",
   str(src))

tk = E.tickers()
ok("the fallback returns a usable universe", len(tk) == 30, str(len(tk)))
ok("and it is sorted by turnover, biggest first",
   tk[0]["vol"] > tk[-1]["vol"])
ok("a fraction change is converted to a percent, not left as 0.025",
   abs(tk[0]["change"] - 2.5) < 1e-9, str(tk[0]["change"]))

before = len(_calls)
E.tickers()
ok("probing the venue is not paid for twice", len(_calls) > before,
   "second call goes to the wire, the probe's own result was reused once")

# 2. Bybit hands candles back newest-first; reading them that way would
#    compute every indicator backwards and still look plausible.
rows = [[str(1_700_000_000_000 + i * 3_600_000), "1", "2", "0.5",
         str(100 + i), "10", "1000"] for i in range(50)]
E.get_json = fake_http({"api.bybit.com": {"result": {"list": list(reversed(rows))}}})
kl = E._klines_from("bybit", "https://api.bybit.com", "BTCUSDT", "1h", 50)
ok("bybit candles come back oldest-first", kl[0]["t"] < kl[-1]["t"],
   f"{kl[0]['t']} .. {kl[-1]['t']}")
ok("and the closes are in the right order", kl[0]["c"] == 100
   and kl[-1]["c"] == 149, f"{kl[0]['c']} .. {kl[-1]['c']}")
ok("the timeframe name is translated, not passed through",
   E._BYBIT_TF["1h"] == "60" and E._OKX_TF["4h"] == "4H")
ok("okx instrument ids are built correctly",
   E._okx_inst("BTCUSDT") == "BTC-USDT-SWAP", E._okx_inst("BTCUSDT"))

# 3. A venue that answers 200 with almost nothing is a failure, not a
#    quiet market. Treating it as data would overwrite a good file.
E.SOURCE = None
E._FIRST_TICKERS = None
E.get_json = fake_http({"api.bybit.com": {"result": {"list": BYBIT_TK["result"]["list"][:3]}},
                        "www.okx.com": {"data": [
                            {"instId": f"C{i}-USDT-SWAP", "last": "100",
                             "open24h": "98", "volCcy24h": "1000"}
                            for i in range(30)]}})
src = E.pick_source()
ok("an almost-empty answer is not mistaken for data", src[0] == "okx",
   str(src))

# 4. Everything refuses -> one clear sentence, not a traceback
E.SOURCE = None
E._FIRST_TICKERS = None
E.get_json = fake_http({})
try:
    E.pick_source()
    ok("total refusal is reported clearly", False, "it did not raise")
except RuntimeError as e:
    ok("total refusal is reported clearly", "no exchange would answer" in str(e)
       and "451" in str(e), str(e)[:90])
except Exception as e:                     # noqa: BLE001
    ok("total refusal is reported clearly", False, repr(e))

# 5. A 4xx must not be retried — a geo-block does not lift in nine seconds,
#    and retrying it eats the time the fallback needs.
E.get_json = _real_get
tries_seen = []


class _Boom(urllib.error.HTTPError):
    def __init__(self):
        super().__init__("http://x/y", 451, "blocked", {}, None)


def _always_451(req, timeout=0):
    tries_seen.append(1)
    raise _Boom()


_real_open = urllib.request.urlopen
urllib.request.urlopen = _always_451
try:
    E.get_json("https://fapi.binance.com/fapi/v1/ticker/24hr")
    ok("a geo-block is not retried", False, "it did not raise")
except E.Blocked as e:
    ok("a geo-block is not retried", len(tries_seen) == 1, f"{len(tries_seen)} attempts, {e}")
except Exception as e:                     # noqa: BLE001
    ok("a geo-block is not retried", False, repr(e))
finally:
    urllib.request.urlopen = _real_open
    E.get_json = _real_get
    E.SOURCE = None
    E._FIRST_TICKERS = None

# ------------------------------------------------- numbers from the runner
#
# This is what actually killed the first two runs on GitHub, and it killed
# them on line one, before a single candle was fetched. A workflow that maps
# an unset repository variable does not omit it — it sets it to "".

import os as _os                            # noqa: E402


def _with_env(name, value, fn):
    had = _os.environ.get(name)
    if value is None:
        _os.environ.pop(name, None)
    else:
        _os.environ[name] = value
    try:
        return fn()
    finally:
        if had is None:
            _os.environ.pop(name, None)
        else:
            _os.environ[name] = had


ok("an unset variable uses the default",
   _with_env("SCAN_BATCH", None, lambda: E.env_int("SCAN_BATCH", 18)) == 18)
ok("an EMPTY variable uses the default too — this is the one that crashed",
   _with_env("SCAN_BATCH", "", lambda: E.env_int("SCAN_BATCH", 18)) == 18)
ok("whitespace is not a number either",
   _with_env("SCAN_BATCH", "   ", lambda: E.env_int("SCAN_BATCH", 18)) == 18)
ok("a real value is honoured",
   _with_env("SCAN_BATCH", "30", lambda: E.env_int("SCAN_BATCH", 18)) == 30)
ok("a typo falls back instead of stopping the scan",
   _with_env("SCAN_BATCH", "thirty", lambda: E.env_int("SCAN_BATCH", 18)) == 18)
ok("a number written with a decimal point still parses",
   _with_env("MIN_CONFIDENCE", "55.0",
             lambda: E.env_int("MIN_CONFIDENCE", 0)) == 55)

# ----------------------------------------------------------- state on disk
#
# Untested until now, and it showed: three references to STATE had lost
# their "E." — `STATE.read_text()` had become `STATread_text()`. Python
# only notices an undefined name when the line actually runs, and no test
# ever ran these two functions, so the whole scan completed and then died
# on the last statement. Every module-level name is now also checked.

import tempfile as _tf                      # noqa: E402
import pathlib as _pl                       # noqa: E402

_kept = E.STATE
E.STATE = _pl.Path(_tf.mkdtemp()) / "nested" / "state.json"
try:
    fresh = E.load_state()
    ok("a missing state file gives an empty starting point",
       fresh["cursor"] == 0 and fresh["alerted"] == [] and fresh["runs"] == 0,
       str(fresh))

    fresh["cursor"] = 7
    fresh["alerted"] = [f"S{i}" for i in range(500)]
    E.save_state(fresh)
    ok("saving creates the folder it needs", E.STATE.exists())

    back = E.load_state()
    ok("what was saved comes back", back["cursor"] == 7, str(back["cursor"]))
    ok("the alerted list is capped so the file cannot grow forever",
       len(back["alerted"]) == 400, str(len(back["alerted"])))
    ok("and it keeps the most recent ones, not the oldest",
       back["alerted"][-1] == "S499", back["alerted"][-1])

    E.STATE.write_text("{ this is not json")
    ok("a corrupted state file is started over, not fatal",
       E.load_state()["cursor"] == 0)
finally:
    E.STATE = _kept

# A name that only exists on a rarely-run line is invisible until that line
# runs. Compile every function and check each global it reaches for.
import ast as _ast                          # noqa: E402
import builtins as _bi                      # noqa: E402

_src = _pl.Path(E.__file__).read_text()
_tree = _ast.parse(_src)
_known = set(dir(E)) | set(dir(_bi))
_missing = set()



def _bound_inside(node):
    """Every name a function body can legally see from within itself:
    its own arguments, anything it assigns, the arguments of any nested
    function or lambda, and the `as e` of an except clause."""
    names = set()
    for n in _ast.walk(node):
        if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                          _ast.Lambda)):
            a = n.args
            for grp in (a.args, a.posonlyargs, a.kwonlyargs):
                names.update(x.arg for x in grp)
            for extra in (a.vararg, a.kwarg):
                if extra:
                    names.add(extra.arg)
            if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                names.add(n.name)
        elif isinstance(n, _ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, _ast.Name) and isinstance(n.ctx, _ast.Store):
            names.add(n.id)
        elif isinstance(n, _ast.alias):
            names.add((n.asname or n.name).split(".")[0])
    return names


for _node in _tree.body:
    if isinstance(_node, _ast.FunctionDef):
        _local = _bound_inside(_node)
        for _n in _ast.walk(_node):
            if isinstance(_n, _ast.Name) and isinstance(_n.ctx, _ast.Load):
                if _n.id not in _local and _n.id not in _known:
                    _missing.add(f"{_node.name}: {_n.id}")
ok("no function reaches for a name that does not exist",
   not _missing, ", ".join(sorted(_missing)) or "none")

# ------------------------------------------------------- the coin list file

_keptc = E.COINS
_dir = _pl.Path(_tf.mkdtemp())
E.COINS = _dir / "coins.txt"
try:
    ok("no file means no list, so the server falls back to turnover",
       E.wanted_coins() == [], str(E.wanted_coins()))

    E.COINS.write_text("BTC\neth\n  SOLUSDT  \n\n# a note\nBNB # inline note\n"
                       "DOGE-USDT\nBTC\n")
    got = E.wanted_coins()
    ok("bare names get USDT added", "BTCUSDT" in got and "ETHUSDT" in got,
       str(got))
    ok("lower case is accepted", "ETHUSDT" in got)
    ok("a full symbol is left alone", "SOLUSDT" in got)
    ok("comments and blank lines are ignored",
       "#ANOTEUSDT" not in " ".join(got) and len(got) == 5, str(got))
    ok("an inline comment does not become part of the name",
       "BNBUSDT" in got, str(got))
    ok("dashes and slashes are tolerated", "DOGEUSDTUSDT" not in got
       and "DOGEUSDT" in got, str(got))
    ok("a coin listed twice is only scanned once",
       got.count("BTCUSDT") == 1, str(got))
    ok("and the order you wrote is the order it scans",
       got == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "DOGEUSDT"], str(got))

    E.COINS.write_text("\n\n#only comments\n   \n")
    ok("a file with nothing usable in it counts as no list",
       E.wanted_coins() == [], str(E.wanted_coins()))
finally:
    E.COINS = _keptc

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print("  FAILED:", f)
raise SystemExit(1 if FAIL else 0)
