"""Shared pure-python math for all strategies (no pandas/numpy) — 1:1 port of bot/paper_bot.py helpers.

S06  TS-momentum 30d (primary): BTC-USDT, ETH-USDT, daily bars (UTC).
     want = sign(close[-1] / close[-31] - 1)  over CLOSED daily bars
     stop distance = 3 * ATR14(1d)  (Wilder EWM, alpha=1/14, adjust=False — same as pandas ewm in paper_bot)
     exit when sign flips (or becomes 0); after a stop-out re-enter on the next daily check if want != 0.
     size: q = risk * alloc / stop_dist, capped at max_lev * alloc / px, alloc = equity / 2.
B    Donchian 4h (reference, optional): 40-bar breakout in direction of EMA200, funding filter,
     stop 2 ATR, trailing 3 ATR from best price, exit on 20-bar channel.

Bars are lists of dicts: {"time": ms_open, "open", "high", "low", "close", "volume"} sorted ascending.
"""
from __future__ import annotations

import math
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

DAY_MS = 86_400_000
H4_MS = 4 * 3_600_000
TF_MS = {"1d": DAY_MS, "4h": H4_MS}

S06_LOOKBACK = 30
S06_STOP_ATR = 3.0
S06_KLINE_LIMIT = 120      # paper_bot: kl(sym, "1d", 120)
B_KLINE_LIMIT = 400        # paper_bot: kl(sym, "4h", 400)
B_ENTRY_N, B_EXIT_N = 40, 20
B_STOP_ATR, B_TRAIL_ATR = 2.0, 3.0
B_EMA = 200
B_FUND_LONG_MAX = 0.0003   # long only if funding <= +0.03%
B_FUND_SHORT_MIN = -0.0001  # short only if funding >= -0.01%


# ---------------------------------------------------------------- bars
def parse_klines(raw: list) -> list[dict]:
    """BingX /openApi/swap/v3/quote/klines rows -> sorted float dicts (dedup by time)."""
    out = {}
    for r in raw:
        t = int(float(r["time"]))
        out[t] = {"time": t, "open": float(r["open"]), "high": float(r["high"]),
                  "low": float(r["low"]), "close": float(r["close"]), "volume": float(r.get("volume", 0) or 0)}
    return [out[k] for k in sorted(out)]


def closed_only(bars: list[dict], tf: str, now_ms: int) -> list[dict]:
    """paper_bot: d[d.time + ms <= now] — keep only fully closed candles."""
    ms = TF_MS[tf]
    return [b for b in bars if b["time"] + ms <= now_ms]


def day_str(ms: int) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).strftime("%Y-%m-%d")


def iso_utc(ms: int) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")


# ---------------------------------------------------------------- indicators
def true_range(bars: list[dict]) -> list[float]:
    tr = []
    for i, b in enumerate(bars):
        if i == 0:
            tr.append(b["high"] - b["low"])  # pandas max(axis=1) skips NaN of shifted close
        else:
            pc = bars[i - 1]["close"]
            tr.append(max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc)))
    return tr


def ewm_adjust_false(vals: list[float], alpha: float) -> list[float]:
    out, prev = [], None
    for v in vals:
        prev = v if prev is None else (1 - alpha) * prev + alpha * v
        out.append(prev)
    return out


def atr(bars: list[dict], n: int = 14) -> list[float]:
    return ewm_adjust_false(true_range(bars), 1.0 / n)


def sma(vals: list[float], n: int) -> list[float | None]:
    """pandas s.rolling(n).mean() (None until n values)."""
    out, acc = [], 0.0
    for i, v in enumerate(vals):
        acc += v
        if i >= n:
            acc -= vals[i - n]
        out.append(acc / n if i >= n - 1 else None)
    return out


def ema(vals: list[float], span: int) -> list[float]:
    return ewm_adjust_false(vals, 2.0 / (span + 1))


def rolling_shifted(vals: list[float], n: int, fn) -> list[float | None]:
    """pandas: s.rolling(n).fn().shift() — value at i uses vals[i-n .. i-1]."""
    out = []
    for i in range(len(vals)):
        out.append(fn(vals[i - n:i]) if i >= n else None)
    return out


def sign(x: float) -> int:
    if x is None or math.isnan(x):
        return 0
    return 1 if x > 0 else (-1 if x < 0 else 0)


# ---------------------------------------------------------------- S06
def s06_signal(daily_closed: list[dict], lookback: int = S06_LOOKBACK, stop_mult: float = S06_STOP_ATR) -> dict:
    """daily_closed: closed 1d bars exactly as paper_bot sees them (limit=120 request, open bar removed)."""
    if len(daily_closed) < lookback + 1:
        raise ValueError(f"need >= {lookback + 1} closed daily bars, got {len(daily_closed)}")
    a = atr(daily_closed)
    c_last = daily_closed[-1]["close"]
    c_ref = daily_closed[-1 - lookback]["close"]
    want = sign(c_last / c_ref - 1)
    return {
        "day": day_str(daily_closed[-1]["time"]),
        "want": want,
        "close": c_last,
        "ref_close": c_ref,
        "ret": c_last / c_ref - 1,
        "atr": a[-1],
        "stop_dist": stop_mult * a[-1],
    }


def position_size(alloc: float, risk_frac: float, px: float, stop_dist: float, max_lev: float) -> float:
    """paper_bot.size(): min(risk*alloc/stop_dist, 3*alloc/px) with 3 -> max_lev."""
    if stop_dist <= 0 or px <= 0 or alloc <= 0:
        return 0.0
    return min(risk_frac * alloc / stop_dist, max_lev * alloc / px)


def stop_price(entry: float, side: int, stop_dist: float) -> float:
    return entry - side * stop_dist


# ---------------------------------------------------------------- B (Donchian 4h)
def b_indicators(bars4: list[dict]) -> dict:
    highs = [b["high"] for b in bars4]
    lows = [b["low"] for b in bars4]
    closes = [b["close"] for b in bars4]
    return {
        "hi": rolling_shifted(highs, B_ENTRY_N, max),
        "lo": rolling_shifted(lows, B_ENTRY_N, min),
        "xl": rolling_shifted(lows, B_EXIT_N, min),
        "xs": rolling_shifted(highs, B_EXIT_N, max),
        "atr": atr(bars4),
        "e200": ema(closes, B_EMA),
    }


def _gt(a, b):  # pandas comparisons with NaN are False
    return a is not None and b is not None and a > b


def _lt(a, b):
    return a is not None and b is not None and a < b


def b_entry_signal(bars4: list[dict], ind: dict, funding: float, i: int = -1) -> int:
    px = bars4[i]["close"]
    hi, lo, e = ind["hi"][i], ind["lo"][i], ind["e200"][i]
    sig = 0
    if _gt(px, hi) and px > e and funding <= B_FUND_LONG_MAX:
        sig = 1
    if _lt(px, lo) and px < e and funding >= B_FUND_SHORT_MIN:
        sig = -1
    return sig


def b_trail(side: int, best: float, stop: float, bar: dict, bar_atr: float) -> tuple[float, float]:
    """paper_bot step_b: after the bar did NOT hit the stop, update best and ratchet the stop."""
    if side == 1:
        best = max(best, bar["high"])
        stop = max(stop, best - B_TRAIL_ATR * bar_atr)
    else:
        best = min(best, bar["low"])
        stop = min(stop, best + B_TRAIL_ATR * bar_atr)
    return best, stop


def b_exit_channel(side: int, px: float, ind: dict, i: int = -1) -> bool:
    return (side == 1 and _lt(px, ind["xl"][i])) or (side == -1 and _gt(px, ind["xs"][i]))


# ---------------------------------------------------------------- exchange rounding
def _dec(x) -> Decimal:
    return Decimal(repr(float(x))) if not isinstance(x, Decimal) else x


def floor_qty(q: float, precision: int) -> float:
    if q <= 0:
        return 0.0
    step = Decimal(1).scaleb(-int(precision))
    return float(_dec(q).quantize(step, rounding=ROUND_DOWN))


def round_price(p: float, precision: int) -> float:
    step = Decimal(1).scaleb(-int(precision))
    return float(_dec(p).quantize(step, rounding=ROUND_HALF_UP))


def fmt_num(x: float, precision: int) -> str:
    """Plain decimal string with fixed precision, never scientific notation."""
    return f"{float(x):.{int(precision)}f}"
