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

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print("  FAILED:", f)
raise SystemExit(1 if FAIL else 0)
