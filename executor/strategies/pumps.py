"""PUMPS — DOLF series 2: short pumped alts (pump_bot/scan.py, repo goldinilya-debug/dolf-pump-scan).
STUB: rules recorded here, engine pieces it needs are not finished, so it cannot be enabled yet.

Rules (pump_bot/scan.py, paper only):
  universe  BingX USDT-M perps, 24h turnover >= 3M USDT, price +15% in 24h, OI >= 5% below its 12h max
            (OpenMarket Binance OI or own BingX OI history), BTC 4h change <= +1%; stocks/FX excluded
  entries   SHORT ladder of limit orders at price x1.01 / x1.03 / x1.05 with weights 30/30/40, TTL 24h
  stop      E3 x 1.03 (above the top order); skip if stop is < 1.5% from the planned average
  targets   1.5R / 2R / 3R taking 40/30/30%; after T1: stop -> break-even, unfilled entries cancelled
  time      exit after 48h in position; max 2 new trades/day; day limit -2R; risk 1% per trade
  check     every 15 minutes, positions managed on 1m candles

Missing for live use: ladder LIMIT entries with TTL in the engine (Open.entries), OI data source
(OPENMARKET_KEY or a local BingX OI history), the 15-min universe scan across all perps.
The engine already supports what the exit side needs: partial take-profits (Target) and break-even (be_after).
"""
from __future__ import annotations

from .base import Open, Strategy, Target

GRID = [(1.01, 0.3), (1.03, 0.3), (1.05, 0.4)]
STOP_PAD = 1.03
MIN_STOP_PCT = 1.5
TGT_R = [(1.5, 0.4), (2.0, 0.3), (3.0, 0.3)]


class Pumps(Strategy):
    name = "PUMPS"
    tag = "pmp"
    title = "Шорт пампов DOLF (заготовка)"
    timeframe = "15m"
    default_symbols = []        # dynamic universe
    implemented = False

    @property
    def period_ms(self) -> int:
        return 15 * 60_000

    @staticmethod
    def plan(price: float) -> Open:
        """The order plan the strategy would emit for a qualifying coin (used by tests/docs)."""
        grid = [(price * k, w) for k, w in GRID]
        stop = grid[-1][0] * STOP_PAD
        return Open(side=-1, stop=stop, entries=grid, be_after=1,
                    targets=[Target(frac=w, r=r) for r, w in TGT_R], reason="pump short")

    def step(self, ctx, sym, key, pos, rec):
        raise NotImplementedError("PUMPS is a stub: ladder entries and the OI scan are not implemented yet")
