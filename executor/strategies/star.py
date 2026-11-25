"""STAR — morning/evening star reversal on 4h, 15 coins (reversal/scan.py + reversal/README.md rules).
Ported (signal logic 1:1 with reversal/scan.py) but DISABLED by default and NOT parity-tested against
reversal/bt.py — treat as experimental until a backtest comparison is added.

LONG: c1 red body >= 0.8 ATR; c2 doji body <= 0.35 ATR; c3 green body >= 0.8 ATR and close > mid of c1 body;
      pattern low <= 40-bar low (rolling 40 min shifted 3 at c1). ATR = SMA14 of true range (not Wilder!).
      Entry at market after the c3 close, stop = pattern low - 0.1 ATR, take-profit 1R (100%), time exit 30 bars.
SHORT: mirror at the 40-bar high (weaker edge per README).
"""
from __future__ import annotations

from . import common as C
from .base import Close, Info, NotReady, Open, Strategy, Target


class Star(Strategy):
    name = "STAR"
    tag = "str"
    title = "Звезда 4h (разворот)"
    timeframe = "4h"
    default_symbols = [s + "-USDT" for s in "BTC ETH SOL XRP DOGE ADA AVAX LINK SUI BNB LTC NEAR DOT AAVE BCH".split()]
    capital_divisor = 1
    max_hold_bars = 30
    long_only = False

    def __init__(self, env):
        super().__init__(env)
        self.long_only = str(env.get("STAR_LONG_ONLY", "0")).strip() in ("1", "true", "yes")

    @staticmethod
    def detect(bars: list) -> tuple[int, float] | None:
        """Return (side, stop) if the last closed bar completes a star, else None."""
        n = len(bars)
        if n < 50:
            return None
        o = [b["open"] for b in bars]; c = [b["close"] for b in bars]
        h = [b["high"] for b in bars]; l = [b["low"] for b in bars]
        a = C.sma(C.true_range(bars), 14)
        body = [c[k] - o[k] for k in range(n)]
        i = n - 1; j = i - 2
        if None in (a[i], a[i - 1], a[j]) or j - 42 < 0:
            return None
        lo40 = min(l[j - 42:j - 2]); hi40 = max(h[j - 42:j - 2])   # rolling(40).min().shift(3) at j
        if abs(body[i - 1]) > 0.35 * a[i - 1]:
            return None
        mid = (o[j] + c[j]) / 2
        if body[j] < -0.8 * a[j] and body[i] > 0.8 * a[i] and c[i] > mid and min(l[j:i + 1]) <= lo40:
            return 1, min(l[j:i + 1]) - 0.1 * a[i]
        if body[j] > 0.8 * a[j] and body[i] < -0.8 * a[i] and c[i] < mid and max(h[j:i + 1]) >= hi40:
            return -1, max(h[j:i + 1]) + 0.1 * a[i]
        return None

    def step(self, ctx, sym, key, pos, rec):
        bars = ctx.klines(sym, "4h", 120)
        last = bars[-1]
        if str(last["time"]) != key:
            raise NotReady(f"newest closed 4h bar {last['time']} != {key}")
        if pos and rec:
            opened = int(rec.get("opened_bar") or last["time"])
            if (last["time"] - opened) // C.H4_MS >= self.max_hold_bars:
                return [Close(f"time exit {self.max_hold_bars} bars")]
            return []
        if pos is not None:
            return []
        d = self.detect(bars)
        if not d or (self.long_only and d[0] == -1):
            return []
        side, stop = d
        return [Info(f"⭐ STAR {sym}: {'утренняя' if side == 1 else 'вечерняя'} звезда, close {last['close']}, стоп {stop:.6g}, тейк 1R"),
                Open(side=side, stop=stop, targets=[Target(frac=1.0, r=1.0)], reason="star",
                     meta={"opened_bar": last["time"] + C.H4_MS})]
