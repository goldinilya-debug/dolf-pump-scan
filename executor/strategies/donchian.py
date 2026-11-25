"""DONCHIAN — strategy B from bot/paper_bot.py (reference). Ported and parity-tested, DISABLED by default.

  * every 4h after the bar close: 40-bar breakout in the direction of EMA200, funding filter
    (long only if funding <= +0.03%, short only if >= -0.01%)
  * stop 2 ATR14(4h); trailing: after each closed bar best = max/min, stop ratchets to best -/+ 3 ATR
  * exit when close crosses the 20-bar channel; no entry on the bar where a stop was hit
Correlation with S06 is 0.4-0.5 and both trade BTC/ETH: the engine allows one position per symbol,
so with both enabled whichever strategy holds a symbol first owns it. For a clean comparison run
Donchian in a second folder with a sub-account key (STRAT_S06=0, STRAT_DONCHIAN=1).
"""
from __future__ import annotations

from . import common as C
from .base import Close, Info, MoveStop, NotReady, Open, Strategy


class Donchian(Strategy):
    name = "DONCHIAN"
    tag = "dch"
    title = "Дончиан 4h (эталон B)"
    timeframe = "4h"
    default_symbols = ["BTC-USDT", "ETH-USDT"]
    capital_divisor = 2

    def step(self, ctx, sym, key, pos, rec):
        bars = ctx.klines(sym, "4h", C.B_KLINE_LIMIT)
        last = bars[-1]
        if str(last["time"]) != key:
            raise NotReady(f"newest closed 4h bar {last['time']} != {key}")
        ind = C.b_indicators(bars)
        px = last["close"]
        acts = []
        if pos and rec:
            checked = int(rec.get("checked") or 0)
            best, stop = float(rec.get("best") or rec["entry"]), float(rec["stop"])
            for i, b in enumerate(bars):
                if b["time"] >= checked:
                    best, stop = C.b_trail(pos["side"], best, stop, b, ind["atr"][i])
            rec["checked"], rec["best"] = last["time"] + C.H4_MS, best
            if C.b_exit_channel(pos["side"], px, ind):
                acts.append(Close("exit channel 20"))
            elif stop != float(rec["stop"]):
                acts.append(MoveStop(stop, "trailing 3 ATR"))
            return acts
        if pos is None:
            stopped = int(ctx.state.get("stopped_at", {}).get(sym) or 0)
            if stopped and stopped - stopped % C.H4_MS >= last["time"]:
                return acts  # paper_bot: a stop inside this bar -> no new entry on the same bar
            f = ctx.funding(sym)
            sig = C.b_entry_signal(bars, ind, f)
            if sig:
                acts.append(Info(f"📊 DONCHIAN {sym}: пробой 40×4h → {'LONG' if sig == 1 else 'SHORT'} (funding {f:.5f})"))
                acts.append(Open(side=sig, stop_dist=C.B_STOP_ATR * ind["atr"][-1], reason="Donchian breakout",
                                 meta={"checked": last["time"] + C.H4_MS}))
        return acts

    def on_closed(self, ctx, sym, rec, t_close_ms, reason):
        ctx.state.setdefault("stopped_at", {})[sym] = t_close_ms
