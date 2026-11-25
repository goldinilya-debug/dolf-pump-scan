"""Offline simulation harness: replays historical 4h bars through
   (a) bot/paper_bot.py (needs pandas; only used to (re)generate the golden fixture), and
   (b) executor.Executor wired to an in-memory FakeExchange (stdlib only).
Both sides see identical klines; fills happen at the last closed 4h close (paper_bot's px),
fees 0.07%/side like paper_bot, funding switched off on both sides (it does not drive decisions).
"""
from __future__ import annotations

import gzip
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.dirname(HERE)
sys.path.insert(0, EXE)

from strategies import common as S  # noqa: E402

H4 = S.H4_MS
DAY = S.DAY_MS
COST = 0.0005 + 0.0002
SYMS = ["BTC-USDT", "ETH-USDT"]
START_EQ = 10_000.0


def load_bars():
    with gzip.open(os.path.join(HERE, "fixtures", "bars_4h.json.gz"), "rt") as g:
        raw = json.load(g)["bars"]
    b4 = {s: [{"time": int(r[0]), "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
              for r in rows] for s, rows in raw.items()}
    b1d = {}
    for s, rows in b4.items():
        agg = {}
        for b in rows:
            k = b["time"] - b["time"] % DAY
            if k not in agg:
                agg[k] = dict(b, time=k, n=1)
            else:
                a = agg[k]
                a["high"] = max(a["high"], b["high"]); a["low"] = min(a["low"], b["low"])
                a["close"] = b["close"]; a["volume"] += b["volume"]; a["n"] += 1
        b1d[s] = [{k: v for k, v in a.items() if k != "n"} for _, a in sorted(agg.items()) if a["n"] == 6]
    return b4, b1d


class Hist:
    def __init__(self):
        self.b4, self.b1d = load_bars()
        self._idx = {}

    def window(self, sym, tf, limit, T):
        """Like the exchange: newest `limit` bars whose open time <= T (incl. the still-forming one)."""
        arr = self.b4[sym] if tf == "4h" else self.b1d[sym]
        lo, hi = 0, len(arr)
        while lo < hi:
            m = (lo + hi) // 2
            if arr[m]["time"] <= T:
                lo = m + 1
            else:
                hi = m
        return arr[max(0, lo - limit):lo]


# ---------------------------------------------------------------------- fake exchange
class FakeExchange:
    """Implements the subset of bingx_client.BingX used by Executor."""

    def __init__(self, hist: Hist, start_eq=START_EQ, dual=True):
        self.h, self.T = hist, 0
        self.bal = start_eq
        self.pos = {}      # sym -> dict(side, qty, entry, check_from)
        self.orders = {}   # id -> order dict
        self.oid = 1000
        self.ledger = []   # closed trades
        self.opens = []
        self.cids = set()
        self._dual = dual
        self.filled = {}
        self.px_override = {}

    # time
    def now_ms(self): return self.T
    def maybe_sync(self, *a, **k): pass
    def sync_time(self): return 0

    # market data
    def klines(self, sym, tf, limit):
        return [dict(r) for r in self.h.window(sym, tf, limit, self.T)]

    def last_close(self, sym):
        if sym in self.px_override:
            return self.px_override[sym]
        w = S.closed_only(self.h.window(sym, "4h", 3, self.T), "4h", self.T)
        return w[-1]["close"]

    def contract(self, sym):
        return {"symbol": sym, "quantityPrecision": 10, "pricePrecision": 8, "tradeMinQuantity": 0, "tradeMinUSDT": 0}

    def funding_rate(self, sym): return 0.0001
    def mark_price(self, sym): return self.last_close(sym)

    # account
    def balance(self):
        upnl = sum(p["side"] * p["qty"] * (self.last_close(s) - p["entry"]) for s, p in self.pos.items())
        return {"asset": "VST", "balance": self.bal, "equity": self.bal + upnl, "availableMargin": 1e12,
                "unrealizedProfit": upnl}

    def positions(self, symbol=None):
        out = []
        for s, p in self.pos.items():
            if symbol and s != symbol:
                continue
            out.append({"symbol": s, "side": p["side"], "qty": p["qty"], "positionSide": "LONG" if p["side"] == 1 else "SHORT",
                        "avgPrice": p["entry"], "unrealizedProfit": 0.0, "markPrice": self.last_close(s), "positionId": s})
        return out

    def dual_side(self): return self._dual
    def margin_type(self, sym): return "ISOLATED"
    def set_margin_type(self, *a): return {}
    def leverage(self, sym): return {"longLeverage": 3, "shortLeverage": 3}
    def set_leverage(self, *a): return {}

    def open_orders(self, symbol=None):
        return [dict(o) for o in self.orders.values() if symbol is None or o["symbol"] == symbol]

    def get_order(self, sym, order_id=None, client_order_id=None):
        for o in self.filled.values():
            if (order_id is not None and o["orderId"] == order_id) or (client_order_id and o.get("clientOrderId") == client_order_id):
                return dict(o)
        from bingx_client import BingXError
        raise BingXError(109414, "order not exist", "/fake")

    def _close(self, sym, px, reason):
        p = self.pos.pop(sym)
        fee = p["qty"] * px * COST
        pnl = p["side"] * p["qty"] * (px - p["entry"])
        self.bal += pnl - fee
        self.ledger.append({"sym": sym, "side": p["side"], "entry": p["entry"], "exit": px, "qty": p["qty"],
                            "reason": reason, "exit_T": self.T})
        for k in [k for k, o in self.orders.items() if o["symbol"] == sym]:
            del self.orders[k]  # closePosition stop orders vanish with the position

    def place_order(self, p):
        sym, typ = p["symbol"], p["type"]
        self.oid += 1
        if typ in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            o = {"orderId": self.oid, "symbol": sym, "type": typ, "side": p["side"], "positionSide": p["positionSide"],
                 "stopPrice": p["stopPrice"], "origQty": p["quantity"],
                 "closePosition": p.get("closePosition", "false")}
            self.orders[self.oid] = o
            return dict(o)
        assert typ == "MARKET"
        cid = p.get("clientOrderId")
        if cid:
            assert cid not in self.cids, "duplicate clientOrderId"
            self.cids.add(cid)
        px, q = self.last_close(sym), float(p["quantity"])
        side = 1 if p["side"] == "BUY" else -1
        cur = self.pos.get(sym)
        if cur and cur["side"] != side:  # reducing
            assert abs(q - cur["qty"]) < 1e-9
            self._close(sym, px, "market")
        else:
            assert cur is None, "executor tried to add to an existing position"
            self.bal -= q * px * COST
            self.pos[sym] = {"side": side, "qty": q, "entry": px, "check_from": self.T - self.T % H4}
            self.opens.append({"sym": sym, "side": side, "entry": px, "qty": q, "T": self.T})
        o = {"orderId": self.oid, "symbol": sym, "status": "FILLED", "avgPrice": str(px), "executedQty": str(q),
             "clientOrderId": cid, "type": "MARKET"}
        self.filled[self.oid] = o
        return dict(o)

    def trigger(self, oid):
        """Manually fire a resting STOP/TP order at its trigger price (unit tests)."""
        o = self.orders.pop(oid)
        sym, px = o["symbol"], float(o["stopPrice"])
        self.filled[oid] = dict(o, status="FILLED", updateTime=self.T)
        p = self.pos[sym]
        q = p["qty"] if o.get("closePosition") == "true" or o["type"] == "STOP_MARKET" else min(p["qty"], float(o["origQty"]))
        if q >= p["qty"] - 1e-12:
            self._close(sym, px, "stop" if o["type"] == "STOP_MARKET" else "tp")
        else:
            fee = q * px * COST
            self.bal += p["side"] * q * (px - p["entry"]) - fee
            p["qty"] -= q
            self.ledger.append({"sym": sym, "side": p["side"], "entry": p["entry"], "exit": px, "qty": q,
                                "reason": "tp-partial", "exit_T": self.T})

    def cancel_order(self, sym, oid):
        self.orders.pop(oid, None)

    def cancel_all(self, sym):
        for k in [k for k, o in self.orders.items() if o["symbol"] == sym]:
            del self.orders[k]

    def close_all_positions(self, sym):
        if sym in self.pos:
            self._close(sym, self.last_close(sym), "closeall")

    # stop engine — identical gap logic to paper_bot.check_stops
    def advance(self, T):
        self.T = T
        for sym in list(self.pos):
            p = self.pos[sym]
            stops = [o for o in self.orders.values() if o["symbol"] == sym and o["type"] == "STOP_MARKET"]
            closed = S.closed_only(self.h.window(sym, "4h", 50, T), "4h", T)
            todo = [b for b in closed if b["time"] >= p["check_from"]]
            if not stops:
                if todo:
                    p["check_from"] = todo[-1]["time"] + H4
                continue
            stop = float(stops[0]["stopPrice"])
            for b in todo:
                hit = (p["side"] == 1 and b["low"] <= stop) or (p["side"] == -1 and b["high"] >= stop)
                if hit:
                    gap = (p["side"] == 1 and b["open"] < stop) or (p["side"] == -1 and b["open"] > stop)
                    so = stops[0]
                    self.filled[so["orderId"]] = dict(so, status="FILLED", updateTime=b["time"] + H4 // 2)
                    self._close(sym, b["open"] if gap else stop, "stop")
                    break
                p["check_from"] = b["time"] + H4


class NullTG:
    enabled = False
    def __init__(self): self.msgs = []
    def send(self, text, throttle_key=None, throttle_s=0): self.msgs.append(text); return False
    def poll(self, offset, max_age_s=600): return [], offset


def run_executor_sim(strategy="S06", t0=None, t1=None, tmpdir=None, dual=True):
    import tempfile
    from executor import Config, Executor
    hist = Hist()
    fx = FakeExchange(hist, dual=dual)
    d = tmpdir or tempfile.mkdtemp()
    import strategies as SX
    env = {"STRAT_S06": "1" if strategy == "S06" else "0", "STRAT_DONCHIAN": "1" if strategy == "B" else "0",
           "RISK_PCT": "1", "ACT_DELAY_MIN": "5"}
    cfg = Config(api_key="k", api_secret="s", mode="demo", risk_pct=1.0, max_leverage=3,
                 daily_loss_limit_pct=100, total_dd_limit_pct=100, max_open_positions=10, max_total_risk_pct=100,
                 act_delay_min=5, base_dir=d, env=env)
    ex = Executor(cfg, fx, NullTG(), strats=SX.load_enabled(env), state_path=os.path.join(d, "state.json"),
                  sleep=lambda *_: None)
    T = t0
    while T <= t1:
        fx.advance(T)
        ex.cycle()
        T += H4
    return fx, ex


def sim_range():
    hist = Hist()
    first = hist.b4["BTC-USDT"][0]["time"]
    last = hist.b4["BTC-USDT"][-1]["time"]
    t0 = first - first % DAY + 45 * DAY + 7 * 60_000   # start after warm-up, at hh:07 like paper_bot's cron
    t1 = last - last % DAY + 7 * 60_000
    return t0, t1


# ---------------------------------------------------------------------- paper side (pandas)
def run_paper_sim(strategy="S06", t0=None, t1=None):
    import importlib.util
    import pandas as pd
    spec = importlib.util.spec_from_file_location("paper_bot", os.path.join(EXE, "..", "bot", "paper_bot.py"))
    pb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pb)
    hist = Hist()
    clock = {"T": t0}

    def kl(sym, tf, limit=400):
        rows = hist.window(sym, tf, limit, clock["T"])
        d = pd.DataFrame(rows).astype(float).sort_values("time")
        ms = {"4h": 4 * 3600e3, "1d": 86400e3}[tf]
        d = d[d.time + ms <= clock["T"]]
        d["t"] = pd.to_datetime(d.time, unit="ms", utc=True)
        return d.set_index("t")

    pb.kl = kl
    pb.fund = lambda sym: 0.0001
    s = {k: dict(equity=START_EQ, pos={}, closed=[]) for k in ("S06", "B", "S15")} | {"meta": {}}
    events = []
    T = t0
    while T <= t1:
        clock["T"] = T
        bars4 = {m: kl(m, "4h", 400) for m in pb.MAJ}
        ev = []
        if strategy == "S06":
            pb.step_s06(s, bars4, ev)
        else:
            pb.step_b(s, bars4, ev)
        events += ev
        T += H4
    return s[strategy], events
