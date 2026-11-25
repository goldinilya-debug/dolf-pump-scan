"""Common interface every strategy module implements.

The engine (executor.py) owns the exchange: sizing, orders, stops, take-profits, break-even, risk caps,
kill-switch, state. A strategy only reads market data through `ctx` and returns a list of actions.

Lifecycle, every minute:
    if strategy.due(now_ms):
        for sym in strategy.symbols:
            key = strategy.period_key(now_ms, sym)     # id of the bar/day to act on, cheap, no I/O
            if key already processed for (strategy, sym): skip
            actions = strategy.step(ctx, sym, key, pos, rec)   # may raise NotReady -> retry next minute
            engine executes actions; when all succeed the key is marked processed

`pos` = live exchange position of THIS strategy on `sym` (None if flat or if another strategy owns the symbol),
`rec` = the engine's record for it (entry, stop, qty, targets, be, plus any strategy fields set via meta).
A strategy may update its own fields in `rec` (e.g. trailing 'best'); the engine persists them.

Env per strategy NAME (upper-case, e.g. S06, DONCHIAN):
    STRAT_<NAME>=1           enable
    RISK_<NAME>=1            % risk per trade, of the strategy's allocation (default RISK_PCT)
    CAPITAL_<NAME>=1         share of account balance the strategy may size from (default 1.0)
    MAXPOS_<NAME>=2          max concurrent positions of this strategy (default len(symbols))
    SYMBOLS_<NAME>=A-USDT,B-USDT  override the symbol list
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import common as C


class NotReady(Exception):
    """Data for `key` is not available yet (exchange lag). Engine retries next minute, key stays open."""


@dataclass
class Target:
    """Partial take-profit. Either r (multiple of the initial risk |entry-stop|) or an absolute price."""
    frac: float
    r: float | None = None
    price: float | None = None


@dataclass
class Open:
    side: int                         # +1 long, -1 short
    stop_dist: float | None = None    # stop = fill price -/+ stop_dist   (S06, Donchian)
    stop: float | None = None         # or an absolute stop price          (Star, Pumps)
    targets: list = field(default_factory=list)   # [Target, ...]; fractions should sum to <= 1
    be_after: int | None = None       # move stop to entry after this many targets filled (1 = after T1)
    entries: list | None = None       # ladder [(price, frac)] — NOT supported by the engine yet (Pumps)
    meta: dict = field(default_factory=dict)      # copied into rec
    reason: str = ""


@dataclass
class Close:
    reason: str


@dataclass
class MoveStop:
    stop: float
    reason: str = ""


@dataclass
class Info:
    text: str


class Strategy:
    name = "BASE"            # env suffix: STRAT_<name>
    tag = "x"                # clientOrderId prefix, 1-4 lowercase letters
    title = ""
    timeframe = "1d"
    default_symbols: list = []
    implemented = True       # False -> cannot be enabled (stub)
    capital_divisor = None   # alloc = balance * capital / divisor (None -> len(symbols)); paper_bot: eq/2

    def __init__(self, env: dict):
        n = self.name
        syms = str(env.get(f"SYMBOLS_{n}", "")).strip()
        self.symbols = [s.strip().upper() for s in syms.split(",") if s.strip()] if syms else list(self.default_symbols)
        self.risk_pct = _f(env, f"RISK_{n}", _f(env, "RISK_PCT", 1.0))
        self.capital = _f(env, f"CAPITAL_{n}", 1.0)
        self.max_positions = int(_f(env, f"MAXPOS_{n}", len(self.symbols)))
        self.delay_ms = int(_f(env, "ACT_DELAY_MIN", 5)) * 60_000

    # ---------------------------------------------------------------- schedule
    @property
    def period_ms(self) -> int:
        return C.TF_MS[self.timeframe]

    def due(self, now_ms: int) -> bool:
        """Default: act from <bar close + ACT_DELAY_MIN> on (and keep retrying until processed)."""
        return (now_ms % self.period_ms) >= self.delay_ms

    def period_key(self, now_ms: int, sym: str) -> str:
        """Open time (ms) of the bar that closed most recently."""
        return str(now_ms - now_ms % self.period_ms - self.period_ms)

    # ---------------------------------------------------------------- sizing
    def alloc(self, balance: float) -> float:
        div = self.capital_divisor or max(1, len(self.symbols))
        return balance * self.capital / div

    # ---------------------------------------------------------------- logic
    def step(self, ctx, sym: str, key: str, pos: dict | None, rec: dict | None) -> list:
        raise NotImplementedError

    def on_closed(self, ctx, sym: str, rec: dict, t_close_ms: int, reason: str):
        """Hook: the engine saw this strategy's position disappear (stop/TP/liquidation/manual)."""


def _f(env, k, d):
    v = env.get(k, "")
    return float(v) if str(v).strip() != "" else float(d)
