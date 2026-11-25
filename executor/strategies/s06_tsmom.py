"""S06 — time-series momentum 30d on BTC-USDT and ETH-USDT (primary strategy, tournament winner).

Exact port of bot/paper_bot.py step_s06():
  * once a day after the 00:00 UTC daily close: closed 1d bars from a limit=120 klines request
  * want = sign(close[-1] / close[-31] - 1)
  * position on the other side (or want == 0) -> close ("momentum flip")
  * flat and want != 0 -> open, stop = fill -/+ 3 * ATR14(1d) (Wilder EWM), sized 1% of (balance / 2)
  * after a stop-out: re-entry at the next daily check if want != 0 (falls out of the rules above)
No take-profit, no break-even (the paper strategy has none).
"""
from __future__ import annotations

from . import common as C
from .base import Close, Info, NotReady, Open, Strategy


class S06(Strategy):
    name = "S06"
    tag = "s06"
    title = "TS-моментум 30д"
    timeframe = "1d"
    default_symbols = ["BTC-USDT", "ETH-USDT"]
    capital_divisor = 2          # paper_bot: size(acc["equity"] / 2, ...)
    lookback = C.S06_LOOKBACK
    stop_atr = C.S06_STOP_ATR

    def period_key(self, now_ms: int, sym: str) -> str:
        return C.day_str(now_ms - C.DAY_MS)     # the UTC day that closed last

    def step(self, ctx, sym, key, pos, rec):
        bars = ctx.klines(sym, "1d", C.S06_KLINE_LIMIT)
        sig = C.s06_signal(bars, self.lookback, self.stop_atr)
        if sig["day"] != key:
            raise NotReady(f"newest closed daily bar is {sig['day']}, waiting for {key}")
        want = sig["want"]
        wtxt = {1: "LONG", -1: "SHORT", 0: "FLAT"}[want]
        cur = ("LONG " if pos["side"] == 1 else "SHORT ") + str(pos["qty"]) if pos else "нет позиции"
        acts = [Info(f"📊 S06 {sym} за {sig['day']}: close {sig['close']} vs 30д назад {sig['ref_close']} "
                     f"({sig['ret'] * 100:+.2f}%) → {wtxt}. ATR {sig['atr']:.2f}, стоп 3 ATR = {sig['stop_dist']:.2f}. "
                     f"Сейчас: {cur}.")]
        if pos and pos["side"] != want:
            acts.append(Close("momentum flip" if want else "momentum = 0"))
            pos = None
        if pos is None and want != 0:
            acts.append(Open(side=want, stop_dist=sig["stop_dist"], reason=f"S06 {wtxt}",
                             meta={"signal_day": sig["day"]}))
        return acts
