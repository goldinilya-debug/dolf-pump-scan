"""BingX USDT-M perpetual executor with pluggable strategies (strategies/ folder).

    python executor.py --check     read-only: server time, balance, positions, orders, Telegram test
    python executor.py --dry-run   compute current signals and sizes, print the plan, place nothing
    python executor.py             run the loop (wakes every minute)

Strategies are switched on in .env: STRAT_S06=1 (default), STRAT_DONCHIAN / STRAT_STAR / STRAT_PUMPS = 0.
MODE=demo (VST) by default; MODE=live also needs CONFIRM_LIVE=YES.
Only `requests` and `python-dotenv` are needed; everything else is the standard library.

The engine owns everything that touches money: sizing (per-strategy risk budget), isolated margin and
leverage, market entries with idempotent clientOrderId, the exchange-side STOP_MARKET (verified every
minute and re-placed if missing), partial take-profits (TAKE_PROFIT_MARKET), break-even moves,
global caps (concurrent positions, total open risk), daily-loss and drawdown halts, kill-switch.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import time
from dataclasses import dataclass, field

import requests

import strategies as SX
from strategies import common as S
from strategies.base import Close, Info, MoveStop, NotReady, Open, Strategy, Target
from bingx_client import BingX, BingXError, DEMO_BASE, LIVE_BASE
from telegram_bot import Telegram

HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("executor")
STOP_TYPES = ("STOP_MARKET", "STOP")
TP_TYPES = ("TAKE_PROFIT_MARKET", "TAKE_PROFIT")


# ====================================================================== config
def _f(env, k, d):
    v = env.get(k, "")
    return float(v) if str(v).strip() != "" else float(d)


def _b(env, k, d):
    v = str(env.get(k, "")).strip().lower()
    return d if v == "" else v in ("1", "true", "yes", "y", "on")


@dataclass
class Config:
    api_key: str = ""
    api_secret: str = ""
    mode: str = "demo"
    confirm_live: bool = False
    tg_token: str = ""
    tg_chat: str = ""
    risk_pct: float = 1.0
    max_leverage: int = 3
    daily_loss_limit_pct: float = 3.0
    total_dd_limit_pct: float = 10.0
    max_open_positions: int = 4
    max_total_risk_pct: float = 3.0
    act_delay_min: int = 5
    stop_working_type: str = "CONTRACT_PRICE"
    exit_on_flip_when_halted: bool = True
    market_data_base: str = LIVE_BASE
    base_dir: str = HERE
    env: dict = field(default_factory=dict)

    @property
    def base(self) -> str:
        return LIVE_BASE if self.mode == "live" else DEMO_BASE

    @classmethod
    def from_env(cls, env: dict, base_dir: str = HERE) -> "Config":
        mode = str(env.get("MODE", "demo")).strip().lower() or "demo"
        if mode not in ("demo", "live"):
            raise SystemExit(f"MODE must be demo or live, got {mode!r}")
        return cls(
            api_key=str(env.get("BINGX_API_KEY", "")).strip(),
            api_secret=str(env.get("BINGX_API_SECRET", "")).strip(),
            mode=mode,
            confirm_live=str(env.get("CONFIRM_LIVE", "")).strip() == "YES",
            tg_token=str(env.get("TG_TOKEN", "")).strip(),
            tg_chat=str(env.get("TG_CHAT", "")).strip(),
            risk_pct=_f(env, "RISK_PCT", 1),
            max_leverage=int(_f(env, "MAX_LEVERAGE", 3)),
            daily_loss_limit_pct=_f(env, "DAILY_LOSS_LIMIT_PCT", 3),
            total_dd_limit_pct=_f(env, "TOTAL_DD_LIMIT_PCT", 10),
            max_open_positions=int(_f(env, "MAX_OPEN_POSITIONS", 4)),
            max_total_risk_pct=_f(env, "MAX_TOTAL_RISK_PCT", 3),
            act_delay_min=int(_f(env, "ACT_DELAY_MIN", 5)),
            stop_working_type=str(env.get("STOP_WORKING_TYPE", "CONTRACT_PRICE")).strip().upper() or "CONTRACT_PRICE",
            exit_on_flip_when_halted=_b(env, "EXIT_ON_FLIP_WHEN_HALTED", True),
            market_data_base=str(env.get("MARKET_DATA_BASE", LIVE_BASE)).strip() or LIVE_BASE,
            base_dir=base_dir,
            env=dict(env),
        )

    def validate(self, strats: list, need_keys: bool = True) -> list[str]:
        errs = []
        if need_keys and (not self.api_key or not self.api_secret):
            errs.append("BINGX_API_KEY / BINGX_API_SECRET are empty in .env")
        if not (1 <= self.max_leverage <= 10):
            errs.append("MAX_LEVERAGE must be 1..10")
        if not strats:
            errs.append("no strategy enabled (STRAT_S06=1 ...)")
        tags = set()
        for s in strats:
            if not s.implemented:
                errs.append(f"STRAT_{s.name}=1 but {s.name} is only a stub (not implemented yet)")
            if not (0 < s.risk_pct <= 5):
                errs.append(f"RISK_{s.name} must be in (0, 5]")
            if not s.symbols:
                errs.append(f"{s.name}: empty symbol list")
            if s.tag in tags:
                errs.append(f"duplicate strategy tag {s.tag}")
            tags.add(s.tag)
        if self.stop_working_type not in ("CONTRACT_PRICE", "MARK_PRICE", "INDEX_PRICE"):
            errs.append("STOP_WORKING_TYPE must be CONTRACT_PRICE / MARK_PRICE / INDEX_PRICE")
        return errs


# ====================================================================== state
def default_state() -> dict:
    return {"version": 2, "done": {}, "attempts": {}, "blocked": {}, "strat": {},
            "positions": {}, "pending": {},
            "risk": {"day": None, "day_start_equity": None, "peak_equity": None,
                     "halt_daily_day": None, "halt_dd": False, "tg_stop": False},
            "tg_offset": None, "closeall_req": 0, "last_cycle": None}


def load_state(path: str) -> dict:
    st = default_state()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            disk = json.load(f)
        for k, v in disk.items():
            if isinstance(v, dict) and isinstance(st.get(k), dict):
                st[k].update(v)
            else:
                st[k] = v
    return st


def save_state(path: str, st: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


# ====================================================================== strategy context
class Ctx:
    """What a strategy may touch: closed bars, funding, clock, its own persistent dict."""

    def __init__(self, engine: "Executor", strat: Strategy):
        self.e, self.strat = engine, strat
        self.state = engine.st["strat"].setdefault(strat.name, {})

    @property
    def now_ms(self) -> int:
        return self.e.now_ms()

    def klines(self, sym: str, tf: str, limit: int) -> list[dict]:
        k = (sym, tf, limit)
        if k not in self.e._kcache:
            raw = self.e.c.klines(sym, tf, limit)
            self.e._kcache[k] = S.closed_only(S.parse_klines(raw), tf, self.now_ms)
        return self.e._kcache[k]

    def funding(self, sym: str) -> float:
        return self.e.c.funding_rate(sym)


# ====================================================================== engine
class Executor:
    def __init__(self, cfg: Config, client: BingX, tg: Telegram, strats: list | None = None,
                 state_path: str | None = None, dry_run: bool = False, sleep=time.sleep):
        self.cfg, self.c, self.tg = cfg, client, tg
        self.strats: list[Strategy] = strats if strats is not None else SX.load_enabled(cfg.env)
        self.by_name = {s.name: s for s in self.strats}
        self.state_path = state_path or os.path.join(cfg.base_dir, "state.json")
        self.st = load_state(self.state_path)
        self.dry = dry_run
        self.sleep = sleep
        self._contracts: dict = {}
        self._dual: bool | None = None
        self._margin_ok: set = set()
        self._last_snapshot: dict = {}
        self._kcache: dict = {}

    # ------------------------------------------------------------ helpers
    @property
    def all_symbols(self) -> list[str]:
        out = []
        for s in self.strats:
            out += [x for x in s.symbols if x not in out]
        return out

    def save(self):
        if not self.dry:
            save_state(self.state_path, self.st)

    def now_ms(self) -> int:
        return self.c.now_ms()

    def notify(self, text: str, key: str | None = None, throttle_s: float = 1800):
        tag = "DEMO" if self.cfg.mode == "demo" else "LIVE"
        self.tg.send(f"[{tag}] {text}", throttle_key=key, throttle_s=throttle_s)

    def contract(self, sym: str) -> dict:
        if sym not in self._contracts:
            self._contracts[sym] = self.c.contract(sym)
        return self._contracts[sym]

    def qprec(self, sym) -> int:
        return int(self.contract(sym).get("quantityPrecision", 4))

    def pprec(self, sym) -> int:
        return int(self.contract(sym).get("pricePrecision", 2))

    def dual(self) -> bool:
        if self._dual is None:
            self._dual = self.c.dual_side()
        return self._dual

    def pos_side_param(self, side: int) -> str:
        return ("LONG" if side == 1 else "SHORT") if self.dual() else "BOTH"

    def halt_reasons(self) -> list[str]:
        r, rk = [], self.st["risk"]
        if os.path.exists(os.path.join(self.cfg.base_dir, "STOP")):
            r.append("файл STOP")
        if rk.get("tg_stop"):
            r.append("команда /stop")
        if rk.get("halt_daily_day") == S.day_str(self.now_ms()):
            r.append(f"дневной лимит убытка {self.cfg.daily_loss_limit_pct}%")
        if rk.get("halt_dd"):
            r.append(f"лимит общей просадки {self.cfg.total_dd_limit_pct}%")
        return r

    def snapshot(self) -> dict:
        bal = self.c.balance()
        syms = set(self.all_symbols) | set(self.st["positions"])
        pos = {p["symbol"]: p for p in self.c.positions() if p["symbol"] in syms}
        self._last_snapshot = {"bal": bal, "pos": pos}
        return self._last_snapshot

    @staticmethod
    def side_txt(side: int) -> str:
        return "LONG" if side == 1 else "SHORT"

    # ------------------------------------------------------------ risk
    def update_risk(self, equity: float):
        rk = self.st["risk"]
        today = S.day_str(self.now_ms())
        if rk.get("day") != today:
            if rk.get("halt_daily_day") and rk["halt_daily_day"] != today:
                self.notify("✅ Новый день UTC: дневной лимит убытка сброшен, новые входы снова разрешены.")
                rk["halt_daily_day"] = None
            rk["day"], rk["day_start_equity"] = today, equity
        if rk.get("peak_equity") is None or equity > rk["peak_equity"]:
            rk["peak_equity"] = equity
        ds, pk = rk["day_start_equity"], rk["peak_equity"]
        if ds and equity <= ds * (1 - self.cfg.daily_loss_limit_pct / 100) and rk.get("halt_daily_day") != today:
            rk["halt_daily_day"] = today
            self.notify(f"🛑 Дневной лимит убытка: equity {equity:.2f} vs начало дня {ds:.2f} "
                        f"(−{(1 - equity / ds) * 100:.2f}%). Новые входы остановлены до 00:00 UTC. "
                        f"Открытые позиции и их стопы не трогаю.")
        if pk and equity <= pk * (1 - self.cfg.total_dd_limit_pct / 100) and not rk.get("halt_dd"):
            rk["halt_dd"] = True
            self.notify(f"🛑 Лимит общей просадки: equity {equity:.2f} vs пик {pk:.2f} "
                        f"(−{(1 - equity / pk) * 100:.2f}%). Новые входы остановлены до команды /resume. "
                        f"Позиции не закрываю (закрыть всё: /closeall).")

    def open_risk_usd(self) -> float:
        tot = 0.0
        for rec in self.st["positions"].values():
            if rec.get("be"):
                continue
            tot += abs(float(rec["entry"]) - float(rec["stop"])) * float(rec.get("qty") or 0)
        return tot

    def caps_block(self, strat: Strategy, new_risk_usd: float, equity: float, pos_map: dict) -> str | None:
        n_open = len(set(pos_map) | set(self.st["pending"]) | set(self.st["positions"]))
        if n_open >= self.cfg.max_open_positions:
            return f"лимит одновременных позиций {self.cfg.max_open_positions}"
        mine = sum(1 for r in self.st["positions"].values() if r.get("strat") == strat.name)
        mine += sum(1 for p in self.st["pending"].values() if p.get("strat") == strat.name)
        if mine >= strat.max_positions:
            return f"лимит позиций стратегии {strat.name} = {strat.max_positions}"
        cap = self.cfg.max_total_risk_pct / 100 * equity
        if self.open_risk_usd() + new_risk_usd > cap + 1e-9:
            return (f"общий открытый риск {self.open_risk_usd():.2f} + {new_risk_usd:.2f} > "
                    f"{self.cfg.max_total_risk_pct}% equity ({cap:.2f})")
        return None

    # ------------------------------------------------------------ exchange setup
    def ensure_margin(self, sym: str) -> bool:
        if sym in self._margin_ok:
            return True
        try:
            mt = self.c.margin_type(sym)
            if mt.upper() != "ISOLATED":
                self.c.set_margin_type(sym, "ISOLATED")
                log.info("%s margin type %s -> ISOLATED", sym, mt)
            lev = self.c.leverage(sym)
            sides = ["LONG", "SHORT"] if self.dual() else ["BOTH"]
            cur = {"LONG": lev.get("longLeverage"), "SHORT": lev.get("shortLeverage"), "BOTH": lev.get("longLeverage")}
            for sd in sides:
                if cur.get(sd) is None or int(float(cur[sd])) != self.cfg.max_leverage:
                    self.c.set_leverage(sym, sd, self.cfg.max_leverage)
                    log.info("%s leverage %s %s -> %s", sym, sd, cur.get(sd), self.cfg.max_leverage)
            self._margin_ok.add(sym)
            return True
        except Exception as e:
            log.exception("ensure_margin %s", sym)
            self.notify(f"⚠️ {sym}: не удалось выставить изолированную маржу/плечо {self.cfg.max_leverage}x: {e}. "
                        f"Вход пропущен.", key=f"margin-{sym}")
            return False

    # ------------------------------------------------------------ orders
    def _orders_of(self, sym: str, side: int, orders: list, types: tuple) -> list:
        close_side = "SELL" if side == 1 else "BUY"
        out = []
        for o in orders:
            if o.get("symbol", sym) != sym or str(o.get("type", "")).upper() not in types:
                continue
            if str(o.get("side", "")).upper() != close_side:
                continue
            if self.dual() and str(o.get("positionSide", "")).upper() not in ("", self.pos_side_param(side)):
                continue
            out.append(o)
        return out

    def _place_reduce(self, sym: str, side: int, qty: float, price: float, typ: str, close_all: bool) -> dict | None:
        params = {"symbol": sym, "side": "SELL" if side == 1 else "BUY", "positionSide": self.pos_side_param(side),
                  "type": typ, "quantity": S.fmt_num(qty, self.qprec(sym)),
                  "stopPrice": S.fmt_num(S.round_price(price, self.pprec(sym)), self.pprec(sym)),
                  "workingType": self.cfg.stop_working_type}
        if close_all:
            params["closePosition"] = "true"
        elif not self.dual():
            params["reduceOnly"] = "true"
        types = STOP_TYPES if typ == "STOP_MARKET" else TP_TYPES
        last = None
        for i in range(3):
            try:
                o = self.c.place_order(params)
                log.info("%s placed %s %s", typ, sym, params)
                return o
            except (requests.Timeout, requests.ConnectionError) as e:
                last = e
                self.sleep(3)
                want = params["stopPrice"]
                found = [o for o in self._orders_of(sym, side, self.c.open_orders(sym), types)
                         if S.fmt_num(float(o.get("stopPrice") or 0), self.pprec(sym)) == want]
                if found:
                    return found[0]
            except BingXError as e:
                last = e
                log.warning("%s place failed %s: %s", typ, sym, e)
                self.sleep(2 + 2 * i)
        log.error("%s placement failed for %s: %s", typ, sym, last)
        return None

    def place_stop(self, sym, side, qty, stop):
        return self._place_reduce(sym, side, qty, stop, "STOP_MARKET", close_all=True)

    def place_tp(self, sym, side, qty, price):
        return self._place_reduce(sym, side, qty, price, "TAKE_PROFIT_MARKET", close_all=False)

    def market(self, sym: str, side: int, qty: float, reduce: bool, cid: str | None = None,
               pos_side: int | None = None) -> dict:
        ps = pos_side if pos_side is not None else side
        p = {"symbol": sym, "side": "BUY" if side == 1 else "SELL", "positionSide": self.pos_side_param(ps),
             "type": "MARKET", "quantity": S.fmt_num(qty, self.qprec(sym))}
        if reduce and not self.dual():
            p["reduceOnly"] = "true"
        if cid:
            p["clientOrderId"] = cid
        try:
            return self.c.place_order(p)
        except (requests.Timeout, requests.ConnectionError):
            if not cid:
                raise
            self.sleep(3)
            return self.c.get_order(sym, client_order_id=cid)  # did it go through?

    def fill_info(self, sym: str, order: dict, fallback_px: float) -> tuple[float, float]:
        oid = order.get("orderId") or order.get("orderID")
        if str(order.get("status", "")).upper() == "FILLED" and float(order.get("avgPrice") or 0) > 0:
            return float(order["avgPrice"]), float(order.get("executedQty") or 0)
        for _ in range(6):
            try:
                if oid is not None:
                    o = self.c.get_order(sym, order_id=oid)
                    if str(o.get("status", "")).upper() == "FILLED" and float(o.get("avgPrice") or 0) > 0:
                        return float(o["avgPrice"]), float(o.get("executedQty") or 0)
            except BingXError as e:
                log.warning("order query %s: %s", sym, e)
            self.sleep(1)
        for p in self.c.positions(sym):
            if p["symbol"] == sym and p["avgPrice"] > 0:
                return p["avgPrice"], p["qty"]
        return fallback_px, float(order.get("executedQty") or 0)

    def cancel_types(self, sym: str, types: tuple):
        try:
            for o in self.c.open_orders(sym):
                if str(o.get("type", "")).upper() in types:
                    self.c.cancel_order(sym, o["orderId"])
        except Exception as e:
            log.warning("cancel %s %s: %s", types, sym, e)

    def close_position(self, sym: str, pos: dict, reason: str) -> bool:
        if self.dry:
            print(f"  [dry-run] would CLOSE {sym} {pos['side']:+d} qty={pos['qty']} ({reason})")
            return True
        try:
            o = self.market(sym, -pos["side"], pos["qty"], reduce=True, pos_side=pos["side"])
            px, _ = self.fill_info(sym, o, pos.get("markPrice") or pos["avgPrice"])
        except Exception as e:
            log.exception("close %s", sym)
            self.notify(f"❌ Ошибка закрытия {sym} ({reason}): {e}", key=f"closeerr-{sym}", throttle_s=600)
            return False
        self.cancel_types(sym, STOP_TYPES + TP_TYPES)
        rec = self.st["positions"].pop(sym, None)
        pnl = pos["side"] * pos["qty"] * (px - pos["avgPrice"])
        self.notify(f"🔁 Закрыл {sym} {self.side_txt(pos['side'])} qty {pos['qty']} по ~{px} "
                    f"(вход {pos['avgPrice']}), причина: {reason}. PnL ≈ {pnl:+.2f} без комиссий.")
        log.info("closed %s %s px=%s reason=%s rec=%s", sym, pos, px, reason, rec)
        self.save()
        return True

    def move_stop(self, sym: str, pos: dict, rec: dict, new_stop: float, reason: str) -> bool:
        new_stop = S.round_price(new_stop, self.pprec(sym))
        if new_stop == float(rec["stop"]):
            return True
        old = rec["stop"]
        mark = pos.get("markPrice") or self.c.mark_price(sym)
        if (pos["side"] == 1 and mark <= new_stop) or (pos["side"] == -1 and mark >= new_stop):
            return self.close_position(sym, pos, f"price already beyond new stop {new_stop}")
        so = self.place_stop(sym, pos["side"], pos["qty"], new_stop)  # new first, then remove the old one(s)
        if not so:
            # exchange may allow only one closePosition stop per side: cancel old, place new, restore old on failure
            self.cancel_types(sym, STOP_TYPES)
            so = self.place_stop(sym, pos["side"], pos["qty"], new_stop)
            if not so:
                back = self.place_stop(sym, pos["side"], pos["qty"], float(old))
                rec["stop_oid"] = (back or {}).get("orderId")
        if not so:
            self.notify(f"⚠️ {sym}: не удалось перенести стоп {old} → {new_stop}; старый стоп остаётся.",
                        key=f"mvstop-{sym}", throttle_s=600)
            return False
        new_id = so.get("orderId") or so.get("orderID")
        for o in self._orders_of(sym, pos["side"], self.c.open_orders(sym), STOP_TYPES):
            if o.get("orderId") != new_id:
                try:
                    self.c.cancel_order(sym, o["orderId"])
                except Exception as e:
                    log.warning("old stop cancel %s: %s", sym, e)
        rec["stop"], rec["stop_oid"] = new_stop, new_id
        self.save()
        self.notify(f"↗️ {sym}: стоп {old} → {new_stop} ({reason})")
        return True

    # ------------------------------------------------------------ entry
    def enter(self, strat: Strategy, sym: str, act: Open, key: str, pos_map: dict) -> bool:
        """Returns True when the key may be marked processed (entered, or deliberately skipped)."""
        cfg = self.cfg
        if act.entries:
            self.notify(f"⚠️ {strat.name} {sym}: лестничные входы пока не поддерживаются движком — пропуск.",
                        key=f"ladder-{strat.name}")
            return True
        owner = self.st["positions"].get(sym, {}).get("strat")
        if sym in pos_map or sym in self.st["pending"]:
            self.notify(f"⏭ {strat.name} {sym}: символ уже занят ({owner or 'позиция на бирже'}) — вход пропущен.",
                        key=f"busy-{strat.name}-{sym}-{key}", throttle_s=86400)
            return True
        if not self.ensure_margin(sym):
            return False
        bal = self.c.balance()
        equity = float(bal.get("equity") or bal["balance"])
        alloc = strat.alloc(bal["balance"])  # paper_bot sizes from realized equity (no uPnL)
        px = self.c.mark_price(sym)
        stop_dist = act.stop_dist if act.stop_dist is not None else abs(px - float(act.stop))
        if stop_dist <= 0 or (act.stop is not None and (act.stop - px) * act.side >= 0):
            self.notify(f"⚠️ {strat.name} {sym}: стоп {act.stop} по неправильную сторону от цены {px} — пропуск.",
                        key=f"badstop-{sym}-{key}")
            return True
        q_raw = S.position_size(alloc, strat.risk_pct / 100, px, stop_dist, cfg.max_leverage)
        avail = float(bal.get("availableMargin") or bal["balance"])
        if q_raw * px / cfg.max_leverage > 0.95 * avail:
            q_raw = 0.95 * avail * cfg.max_leverage / px
            self.notify(f"⚠️ {sym}: не хватает свободной маржи, размер урезан до {q_raw:.6f}", key=f"cut-{sym}")
        c = self.contract(sym)
        qty = S.floor_qty(q_raw, self.qprec(sym))
        min_q, min_usdt = float(c.get("tradeMinQuantity") or 0), float(c.get("tradeMinUSDT") or 0)
        if qty <= 0 or qty < min_q or qty * px < min_usdt:
            self.notify(f"⚠️ {strat.name} {sym}: размер {q_raw:.6f} меньше минимума биржи ({min_q} / {min_usdt} USDT) — пропуск.",
                        key=f"min-{sym}-{key}")
            return True
        why = self.caps_block(strat, qty * stop_dist, equity, pos_map)
        if why:
            self.notify(f"⏭ {strat.name} {sym}: вход {self.side_txt(act.side)} пропущен — {why}.",
                        key=f"cap-{strat.name}-{sym}-{key}", throttle_s=86400)
            return True
        akey = f"{strat.name}:{sym}"
        att = self.st["attempts"].setdefault(akey, {"key": key, "n": 0})
        if att["key"] != key:
            att.update(key=key, n=0)
        att["n"] += 1
        stamp = key.replace("-", "") if "-" in key else S.iso_utc(int(key))[:13].replace("-", "").replace(" ", "")
        cid = f"{strat.tag}{sym.split('-')[0].lower()}{stamp}{'l' if act.side == 1 else 's'}{att['n']}"[:40]
        self.st["pending"][sym] = {"cid": cid, "key": key, "strat": strat.name, "side": act.side,
                                   "stop_dist": stop_dist, "stop": act.stop, "ts": self.now_ms(),
                                   "targets": [t.__dict__ for t in act.targets], "be_after": act.be_after,
                                   "meta": act.meta}
        self.save()
        self.notify(f"📤 {strat.name}: ордер {sym} {'BUY/LONG' if act.side == 1 else 'SELL/SHORT'} market qty {qty} "
                    f"(~{qty * px:.0f} USDT, риск {strat.risk_pct}% от {alloc:.0f} ≈ {qty * stop_dist:.2f}, "
                    f"стоп-дистанция {stop_dist:.6g})")
        try:
            o = self.market(sym, act.side, qty, reduce=False, cid=cid)
        except BingXError as e:  # definite rejection: nothing opened
            log.exception("entry %s", sym)
            self.notify(f"❌ Биржа отклонила вход {sym}: {e}", key=f"entryerr-{sym}", throttle_s=600)
            self.st["pending"].pop(sym, None)
            self.save()
            return False
        except Exception as e:  # unknown outcome: keep pending, resolve_pending() checks by clientOrderId
            log.exception("entry %s (unknown outcome)", sym)
            self.notify(f"⚠️ Сетевая ошибка при входе {sym}: {e}. Проверю ордер {cid} в следующую минуту.",
                        key=f"entrynet-{sym}", throttle_s=600)
            return False
        entry, filled = self.fill_info(sym, o, px)
        self._after_fill(sym, self.st["pending"][sym], entry, filled or qty)
        return True

    def _after_fill(self, sym: str, pd_: dict, entry: float, qty: float):
        side = pd_["side"]
        stop_abs = pd_.get("stop")
        stop = S.round_price(float(stop_abs) if stop_abs is not None else S.stop_price(entry, side, pd_["stop_dist"]),
                             self.pprec(sym))
        rec = {"strat": pd_["strat"], "side": side, "qty": qty, "qty0": qty, "entry": entry, "stop": stop,
               "risk_dist": abs(entry - stop), "key": pd_["key"], "opened_at": S.iso_utc(self.now_ms()),
               "best": entry, "be": False, "be_after": pd_.get("be_after"), "targets": []}
        rec.update(pd_.get("meta") or {})
        self.st["positions"][sym] = rec
        self.st["pending"].pop(sym, None)
        self.save()
        self.notify(f"✅ {rec['strat']}: вход {sym} {self.side_txt(side)} qty {qty} по {entry}. Ставлю стоп {stop}.")
        mark = self.c.mark_price(sym)
        if (side == 1 and mark <= stop) or (side == -1 and mark >= stop):
            self.notify(f"⚠️ {sym}: цена {mark} уже за стопом {stop} — закрываю по рынку.")
            self.close_position(sym, {"side": side, "qty": qty, "avgPrice": entry, "markPrice": mark}, "price beyond stop")
            return
        so = self.place_stop(sym, side, qty, stop)
        if not so:
            self.notify(f"🚨 {sym}: стоп НЕ поставился после 3 попыток — аварийно закрываю позицию.")
            self.close_position(sym, {"side": side, "qty": qty, "avgPrice": entry, "markPrice": mark},
                                "stop placement failed")
            return
        rec["stop_oid"] = so.get("orderId") or so.get("orderID")
        self.notify(f"🛡 Стоп {sym} STOP_MARKET {stop} (closePosition, {self.cfg.stop_working_type}) стоит на бирже.")
        self._place_targets(sym, rec, pd_.get("targets") or [])
        self.save()

    def _place_targets(self, sym: str, rec: dict, targets: list):
        if not targets:
            return
        side, entry, risk = rec["side"], rec["entry"], rec["risk_dist"]
        qp, min_q = self.qprec(sym), float(self.contract(sym).get("tradeMinQuantity") or 0)
        left = rec["qty0"]
        for i, t in enumerate(targets):
            t = t if isinstance(t, dict) else t.__dict__
            price = float(t["price"]) if t.get("price") is not None else entry + side * float(t["r"]) * risk
            last = i == len(targets) - 1
            q = left if last and sum(float(x["frac"] if isinstance(x, dict) else x.frac) for x in targets) >= 0.999 \
                else S.floor_qty(rec["qty0"] * float(t["frac"]), qp)
            q = min(S.floor_qty(q, qp), left)
            tgt = {"i": i + 1, "price": S.round_price(price, self.pprec(sym)), "qty": q, "frac": float(t["frac"]),
                   "oid": None, "done": False}
            if q <= 0 or q < min_q:
                tgt["skipped"] = "below min qty"
                rec["targets"].append(tgt)
                continue
            o = self.place_tp(sym, side, q, tgt["price"])
            tgt["oid"] = (o or {}).get("orderId")
            rec["targets"].append(tgt)
            left = max(0.0, left - q)
            self.notify(f"🎯 {sym}: тейк T{i + 1} {tgt['price']} на {q}" + ("" if o else " — НЕ поставился, повторю"))

    # ------------------------------------------------------------ restart safety
    def resolve_pending(self):
        for sym, pd_ in list(self.st["pending"].items()):
            try:
                o = self.c.get_order(sym, client_order_id=pd_["cid"])
            except BingXError as e:
                log.info("pending %s %s not found on exchange (%s) — dropping", sym, pd_["cid"], e)
                self.st["pending"].pop(sym, None)
                continue
            except Exception as e:
                log.warning("pending check %s failed: %s (will retry)", sym, e)
                continue
            status = str(o.get("status", "")).upper()
            if status == "FILLED":
                self.notify(f"♻️ После перезапуска: ордер {pd_['cid']} по {sym} был исполнен — подхватываю позицию.")
                self._after_fill(sym, pd_, float(o.get("avgPrice") or 0), float(o.get("executedQty") or 0))
                self.st["done"].setdefault(pd_["strat"], {})[sym] = pd_["key"]
            elif status in ("CANCELLED", "CANCELED", "EXPIRED", "REJECTED", "FAILED"):
                self.st["pending"].pop(sym, None)
            elif self.now_ms() - int(pd_.get("ts") or 0) > 10 * 60_000:
                self.notify(f"⚠️ {sym}: ордер {pd_['cid']} в статусе {status} >10 мин — снимаю отметку, позицию проверит сверка.",
                            key=f"pendstale-{sym}")
                self.st["pending"].pop(sym, None)
        self.save()

    # ------------------------------------------------------------ reconcile: stops, targets, BE
    def _close_time(self, sym: str, rec: dict) -> tuple[int, str]:
        for key, why in (("stop_oid", "stop"),):
            oid = rec.get(key)
            if oid in (None, ""):
                continue
            try:
                o = self.c.get_order(sym, order_id=oid)
                if str(o.get("status", "")).upper() == "FILLED":
                    return int(o.get("updateTime") or o.get("time") or 0) or self.now_ms() - 60_000, why
            except Exception as e:
                log.info("stop order lookup %s: %s", sym, e)
        if rec.get("targets") and all(t.get("done") or t.get("skipped") for t in rec["targets"][:-1]):
            return self.now_ms() - 60_000, "take-profit"
        return self.now_ms() - 60_000, "unknown"

    def reconcile(self, pos_map: dict):
        syms = (set(pos_map) & set(self.all_symbols)) | set(self.st["positions"])
        for sym in sorted(syms):
            p = pos_map.get(sym)
            rec = self.st["positions"].get(sym)
            orders = self.c.open_orders(sym)
            if p is None:
                if rec is not None:
                    self.st["positions"].pop(sym)
                    t_close, why = self._close_time(sym, rec)
                    st = self.by_name.get(rec.get("strat"))
                    if st:
                        st.on_closed(Ctx(self, st), sym, rec, t_close, why)
                    label = {"stop": f"сработал стоп {rec.get('stop')}" + (" (безубыток)" if rec.get("be") else ""),
                             "take-profit": "закрыта тейком"}.get(why, "закрыта (стоп/тейк/вручную/ликвидация)")
                    self.notify(f"🧯 {rec.get('strat')} {sym}: позиции больше нет — {label}. Вход был {rec.get('entry')}.")
                for o in orders:  # leftovers of a closed position
                    if str(o.get("type", "")).upper() in STOP_TYPES + TP_TYPES:
                        try:
                            self.c.cancel_order(sym, o["orderId"])
                        except Exception as e:
                            log.warning("orphan cancel %s: %s", sym, e)
                continue
            if sym in self.st["pending"]:
                continue
            if rec is None or rec.get("side") != p["side"]:
                rec = self.adopt(sym, p)
            self._check_targets(sym, p, rec, orders)
            stops = self._orders_of(sym, p["side"], orders, STOP_TYPES)
            if not stops:
                mark = p.get("markPrice") or self.c.mark_price(sym)
                if (p["side"] == 1 and mark <= rec["stop"]) or (p["side"] == -1 and mark >= rec["stop"]):
                    self.notify(f"🚨 {sym}: стопа на бирже нет, а цена {mark} уже за уровнем {rec['stop']} — закрываю.")
                    self.close_position(sym, p, "missing stop, price beyond level")
                    continue
                self.notify(f"⚠️ {sym}: стоп-ордер не найден — ставлю заново {rec['stop']}.", key=f"restop-{sym}",
                            throttle_s=300)
                so = self.place_stop(sym, p["side"], p["qty"], rec["stop"])
                if so:
                    rec["stop_oid"] = so.get("orderId") or so.get("orderID")
                else:
                    self.notify(f"🚨 {sym}: не удалось восстановить стоп. Повторю через минуту.", key=f"restopfail-{sym}",
                                throttle_s=600)
            elif len(stops) > 1:
                keep = min(stops, key=lambda o: abs(float(o.get("stopPrice") or 0) - rec["stop"]))
                for o in stops:
                    if o is not keep:
                        try:
                            self.c.cancel_order(sym, o["orderId"])
                        except Exception as e:
                            log.warning("dup stop cancel %s: %s", sym, e)
            rec["qty"] = p["qty"]
        self.save()

    def _check_targets(self, sym: str, p: dict, rec: dict, orders: list):
        tg = rec.get("targets") or []
        if not tg:
            return
        open_ids = {o.get("orderId") for o in self._orders_of(sym, p["side"], orders, TP_TYPES)}
        eps = 10 ** -self.qprec(sym) / 2
        filled_q = sum(t["qty"] for t in tg if t.get("done"))
        for t in tg:
            if t.get("done") or t.get("skipped"):
                continue
            if t.get("oid") in open_ids:
                continue
            # order gone: filled if the position shrank by its quantity, otherwise re-place it
            if p["qty"] <= rec["qty0"] - filled_q - t["qty"] + eps:
                t["done"] = True
                filled_q += t["qty"]
                self.notify(f"🎯 {rec.get('strat')} {sym}: тейк T{t['i']} {t['price']} исполнен ({t['qty']}). Остаток {p['qty']}.")
            else:
                o = self.place_tp(sym, p["side"], t["qty"], t["price"])
                t["oid"] = (o or {}).get("orderId")
        n_done = sum(1 for t in tg if t.get("done"))
        if rec.get("be_after") and n_done >= rec["be_after"] and not rec.get("be"):
            be = rec["entry"]
            if self.move_stop(sym, p, rec, be, f"безубыток после T{rec['be_after']}"):
                rec["be"] = True

    def adopt(self, sym: str, p: dict) -> dict:
        """Position on the exchange that state.json doesn't know (crash, manual trade): protect it with 3 ATR(1d)."""
        try:
            bars = S.closed_only(S.parse_klines(self.c.klines(sym, "1d", S.S06_KLINE_LIMIT)), "1d", self.now_ms())
            sd = S.s06_signal(bars)["stop_dist"]
        except Exception:
            sd = 0.1 * p["avgPrice"]
        stop = S.round_price(S.stop_price(p["avgPrice"], p["side"], sd), self.pprec(sym))
        owner = next((s.name for s in self.strats if sym in s.symbols), "adopted")
        rec = {"strat": owner, "side": p["side"], "qty": p["qty"], "qty0": p["qty"], "entry": p["avgPrice"], "stop": stop,
               "risk_dist": sd, "key": None, "opened_at": S.iso_utc(self.now_ms()), "best": p["avgPrice"],
               "be": False, "be_after": None, "targets": [], "adopted": True}
        self.st["positions"][sym] = rec
        self.notify(f"♻️ {sym}: на бирже найдена позиция {self.side_txt(p['side'])} {p['qty']} @ {p['avgPrice']}, "
                    f"которой не было в state.json. Беру под контроль ({owner}), стоп {stop} (3 ATR 1d).")
        return rec

    # ------------------------------------------------------------ strategy loop
    def run_strategies(self, pos_map: dict, halted: list[str]):
        now = self.now_ms()
        self._kcache = {}
        done = self.st["done"]
        for strat in self.strats:
            if not strat.due(now):
                continue
            ctx = Ctx(self, strat)
            for sym in strat.symbols:
                key = strat.period_key(now, sym)
                if done.get(strat.name, {}).get(sym) == key or sym in self.st["pending"]:
                    continue
                rec = self.st["positions"].get(sym)
                mine = rec is not None and rec.get("strat") == strat.name
                pos = pos_map.get(sym) if mine else None
                if sym in pos_map and not mine and rec is None:
                    pos = None  # unknown position — reconcile adopts it next minute
                try:
                    acts = strat.step(ctx, sym, key, pos, rec if mine else None)
                except NotReady as e:
                    self.notify(f"⏳ {strat.name} {sym}: {e}", key=f"lag-{strat.name}-{sym}-{key}", throttle_s=7200)
                    continue
                if self._execute(strat, sym, key, acts, pos, rec if mine else None, pos_map, halted):
                    done.setdefault(strat.name, {})[sym] = key
                    self.save()

    def _execute(self, strat, sym, key, acts, pos, rec, pos_map, halted) -> bool:
        bkey = f"{strat.name}:{sym}"
        first_try = self.st["blocked"].get(bkey) != key
        ok = True
        for a in acts:
            if isinstance(a, Info):
                if first_try:
                    self.notify(a.text)
            elif isinstance(a, Close):
                if pos is None:
                    continue
                if halted and not self.cfg.exit_on_flip_when_halted:
                    self.notify(f"⏸ {sym}: сигнал на выход, но торговля остановлена ({', '.join(halted)}) — не закрываю.",
                                key=f"haltexit-{sym}-{key}", throttle_s=86400)
                    return False
                if not self.close_position(sym, pos, a.reason):
                    return False
                pos_map.pop(sym, None)
                pos = None
            elif isinstance(a, MoveStop):
                if pos is not None and rec is not None and not self.move_stop(sym, pos, rec, a.stop, a.reason):
                    ok = False
            elif isinstance(a, Open):
                if halted:
                    if first_try:
                        self.notify(f"⏸ {strat.name} {sym}: вход {self.side_txt(a.side)} заблокирован ({', '.join(halted)}). "
                                    f"Если снять остановку до следующего сигнала — войду при следующей проверке.")
                        self.st["blocked"][bkey] = key
                        self.save()
                    return False
                att = self.st["attempts"].get(bkey, {})
                if att.get("key") == key and att.get("n", 0) >= 3:
                    self.notify(f"❌ {strat.name} {sym}: 3 неудачные попытки входа ({key}) — пропускаю этот сигнал.",
                                key=f"giveup-{bkey}-{key}", throttle_s=86400)
                    return True
                if self.dry:
                    print(f"  [dry-run] would OPEN {strat.name} {sym} {self.side_txt(a.side)} {a}")
                    continue
                if not self.enter(strat, sym, a, key, pos_map):
                    return False
        return ok

    # ------------------------------------------------------------ telegram commands
    def status_text(self) -> str:
        snap = self._last_snapshot or self.snapshot()
        bal = snap["bal"]
        lines = [f"Режим: {self.cfg.mode.upper()} ({self.cfg.base})",
                 "Стратегии: " + ", ".join(f"{s.name} (риск {s.risk_pct}%)" for s in self.strats),
                 f"Баланс {bal.get('asset')}: {bal['balance']:.2f}, equity {bal.get('equity', 0):.2f}, "
                 f"свободно {bal.get('availableMargin', 0):.2f}",
                 f"Открытый риск: {self.open_risk_usd():.2f} (лимит {self.cfg.max_total_risk_pct}% equity), "
                 f"позиций {len(snap['pos'])}/{self.cfg.max_open_positions}"]
        rk = self.st["risk"]
        if rk.get("day_start_equity"):
            lines.append(f"Начало дня UTC: {rk['day_start_equity']:.2f}; пик: {rk.get('peak_equity') or 0:.2f}")
        for sym in self.all_symbols if len(self.all_symbols) <= 4 else sorted(snap["pos"]):
            p, rec = snap["pos"].get(sym), self.st["positions"].get(sym, {})
            if p:
                tp = ", ".join(f"T{t['i']} {t['price']}{'✓' if t.get('done') else ''}" for t in rec.get("targets", []))
                lines.append(f"{sym} [{rec.get('strat')}]: {self.side_txt(p['side'])} {p['qty']} @ {p['avgPrice']}, "
                             f"uPnL {p['unrealizedProfit']:+.2f}, стоп {rec.get('stop')}{' (БУ)' if rec.get('be') else ''}"
                             + (f", тейки {tp}" if tp else ""))
            else:
                lines.append(f"{sym}: нет позиции")
        lines.append(f"Обработано: {json.dumps(self.st['done'], ensure_ascii=False)[:300]}")
        h = self.halt_reasons()
        lines.append("Остановка входов: " + (", ".join(h) if h else "нет"))
        return "\n".join(lines)

    def handle_commands(self):
        cmds, off = self.tg.poll(self.st.get("tg_offset"))
        if off != self.st.get("tg_offset"):
            self.st["tg_offset"] = off
            self.save()
        for cmd in cmds:
            log.info("TG command: %s", cmd)
            head = cmd.split()[0]
            if head == "/status":
                try:
                    self.snapshot()
                except Exception as e:
                    log.warning("status snapshot: %s", e)
                self.notify(self.status_text())
            elif head == "/stop":
                self.st["risk"]["tg_stop"] = True
                self.notify("⏸ Принято /stop: новые входы остановлены. Позиции и стопы не трогаю. "
                            "Возобновить: /resume. Закрыть всё: /closeall.")
            elif head == "/resume":
                rk = self.st["risk"]
                rk["tg_stop"] = False
                if rk.get("halt_dd"):
                    rk["halt_dd"] = False
                    try:
                        rk["peak_equity"] = self.snapshot()["bal"].get("equity")
                    except Exception:
                        rk["peak_equity"] = None
                msg = "▶️ /resume: входы разрешены."
                if os.path.exists(os.path.join(self.cfg.base_dir, "STOP")):
                    msg += " Но файл STOP в папке ещё лежит — удалите его."
                if rk.get("halt_daily_day") == S.day_str(self.now_ms()):
                    msg += " Дневной лимит убытка действует до 00:00 UTC."
                self.notify(msg)
            elif head == "/closeall":
                self.st["closeall_req"] = time.time()
                self.notify("⚠️ Закрыть ВСЕ позиции бота по рынку и снять стопы/тейки? Отправьте /confirm_closeall в течение 2 минут.")
            elif head == "/confirm_closeall":
                if time.time() - float(self.st.get("closeall_req") or 0) <= 120:
                    self.st["closeall_req"] = 0
                    self.closeall()
                else:
                    self.notify("Нет свежего запроса /closeall (или прошло больше 2 минут).")
            elif head in ("/help", "/start"):
                self.notify("Команды: /status, /stop (стоп новых входов), /resume, /closeall (закрыть всё, с подтверждением).")
            self.save()

    def closeall(self):
        self.st["risk"]["tg_stop"] = True
        res = []
        syms = sorted(set(self.all_symbols) | set(self.st["positions"]))
        held = set()
        try:
            held = {p["symbol"] for p in self.c.positions()}
        except Exception as e:
            log.warning("closeall positions read: %s", e)
        for sym in syms:
            if held and sym not in held and sym not in self.st["positions"]:
                continue
            try:
                self.c.close_all_positions(sym)
                self.c.cancel_all(sym)
                self.st["positions"].pop(sym, None)
                res.append(f"{sym}: ok")
            except Exception as e:
                res.append(f"{sym}: ошибка {e}")
        self.save()
        self.notify("🧹 /closeall выполнен: " + ("; ".join(res) or "позиций не было") + ". Входы остановлены (/resume чтобы продолжить).")

    # ------------------------------------------------------------ one minute
    def cycle(self):
        self.c.maybe_sync()
        self.handle_commands()
        self.resolve_pending()
        snap = self.snapshot()
        self.update_risk(float(snap["bal"].get("equity") or snap["bal"]["balance"]))
        self.reconcile(snap["pos"])
        now = self.now_ms()
        if any(s.due(now) and any(self.st["done"].get(s.name, {}).get(x) != s.period_key(now, x) for x in s.symbols)
               for s in self.strats):
            snap = self.snapshot()  # fresh positions right before acting
            self.run_strategies(snap["pos"], self.halt_reasons())
        self.st["last_cycle"] = S.iso_utc(now)
        self.save()

    def run_forever(self):
        self.c.sync_time()
        try:
            snap = self.snapshot()
            pos_txt = ", ".join(f"{s} {'L' if p['side'] == 1 else 'S'} {p['qty']}" for s, p in snap["pos"].items()) or "нет"
            bal_txt = f"{snap['bal']['balance']:.2f} {snap['bal'].get('asset')}"
        except Exception as e:
            pos_txt, bal_txt = "?", f"ошибка чтения: {e}"
        h = self.halt_reasons()
        self.notify("🚀 Исполнитель запущен. Стратегии: " +
                    "; ".join(f"{s.name} {','.join(s.symbols[:4])}{'…' if len(s.symbols) > 4 else ''} риск {s.risk_pct}%"
                              for s in self.strats) +
                    f". Плечо ≤{self.cfg.max_leverage}x, лимиты: день {self.cfg.daily_loss_limit_pct}%, "
                    f"просадка {self.cfg.total_dd_limit_pct}%, позиций ≤{self.cfg.max_open_positions}, "
                    f"общий риск ≤{self.cfg.max_total_risk_pct}%. Баланс {bal_txt}. Позиции: {pos_txt}. "
                    f"Стоп входов: {', '.join(h) if h else 'нет'}.")
        while True:
            try:
                self.cycle()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log.exception("cycle error")
                self.notify(f"❌ Ошибка цикла: {type(e).__name__}: {str(e)[:300]}",
                            key=f"err-{type(e).__name__}-{str(e)[:60]}")
            t = time.time()
            self.sleep(60 - (t % 60) + 2)


# ====================================================================== CLI
def setup_logging(base_dir: str, verbose: bool = False):
    os.makedirs(os.path.join(base_dir, "logs"), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(os.path.join(base_dir, "logs", "executor.log"),
                                              maxBytes=2_000_000, backupCount=10, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers[:] = [fh, sh]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


class SingleInstance:
    def __init__(self, path):
        self.path, self.fh = path, None

    def __enter__(self):
        self.fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("Другой экземпляр executor.py уже запущен (executor.lock занят).")
        return self

    def __exit__(self, *a):
        try:
            self.fh.close()
        except Exception:
            pass


def build(cfg: Config) -> tuple[BingX, Telegram]:
    return (BingX(cfg.api_key, cfg.api_secret, base=cfg.base, public_base=cfg.market_data_base),
            Telegram(cfg.tg_token, cfg.tg_chat))


def run_check(cfg: Config, client: BingX, tg: Telegram, strats: list) -> int:
    ok = True
    print(f"MODE={cfg.mode}  trade base={cfg.base}  market data={cfg.market_data_base}")
    print("strategies:", ", ".join(f"{s.name} risk={s.risk_pct}% syms={len(s.symbols)}" for s in strats))
    try:
        print(f"server time OK, offset {client.sync_time()} ms")
    except Exception as e:
        ok = False
        print("server time FAILED:", e)
    syms = []
    for s in strats:
        syms += [x for x in s.symbols if x not in syms]
    try:
        b = client.balance()
        print(f"balance: {b.get('asset')} balance={b['balance']:.4f} equity={b.get('equity')} available={b.get('availableMargin')}")
        print("position mode:", "hedge (dual)" if client.dual_side() else "one-way")
        for p in client.positions():
            print(f"position: {p}")
        for sym in syms[:6]:
            c = client.contract(sym)
            print(f"{sym}: qtyPrec={c.get('quantityPrecision')} pricePrec={c.get('pricePrecision')} "
                  f"minQty={c.get('tradeMinQuantity')} minUSDT={c.get('tradeMinUSDT')}")
            try:
                print(f"   margin type: {client.margin_type(sym)}; leverage: {client.leverage(sym)}")
            except Exception as e:
                print("   margin/leverage read failed:", e)
            for o in client.open_orders(sym):
                print(f"   open order: {o.get('type')} {o.get('side')} {o.get('positionSide')} qty={o.get('origQty')} "
                      f"stop={o.get('stopPrice')} id={o.get('orderId')}")
    except Exception as e:
        ok = False
        print("account read FAILED:", e)
    if tg.enabled:
        sent = tg.send(f"[{cfg.mode.upper()}] ✅ Тест связи: executor.py --check ({'OK' if ok else 'есть ошибки'})")
        print("telegram:", "sent" if sent else "FAILED (check TG_TOKEN / TG_CHAT, write /start to the bot first)")
        ok = ok and sent
    else:
        print("telegram: TG_TOKEN/TG_CHAT not set — skipped")
    print("CHECK", "OK" if ok else "FAILED")
    return 0 if ok else 1


class _DryClient:
    """Wraps the real client for --dry-run: reads pass through, anything that writes raises."""

    def __init__(self, c):
        self._c = c

    def __getattr__(self, k):
        if k in ("place_order", "cancel_order", "cancel_all", "close_all_positions", "set_margin_type", "set_leverage"):
            raise RuntimeError(f"dry-run: {k} is blocked")
        return getattr(self._c, k)


def run_dry(cfg: Config, client: BingX, strats: list, equity_arg: float | None) -> int:
    try:
        client.sync_time()
    except Exception as e:
        print("time sync failed (using local clock):", e)
    now = client.now_ms()
    pos_map, balance = {}, equity_arg
    if cfg.api_key and cfg.api_secret:
        try:
            b = client.balance()
            balance = balance or b["balance"]
            pos_map = {p["symbol"]: p for p in client.positions()}
        except Exception as e:
            print("account read failed (using --equity):", e)
    balance = balance or 10_000.0
    print(f"DRY RUN  MODE={cfg.mode}  balance={balance:.2f}  now={S.iso_utc(now)}  maxLev={cfg.max_leverage}")
    eng = Executor(cfg, _DryClient(client), Telegram("", ""), strats=strats, dry_run=True,
                   state_path=os.path.join(cfg.base_dir, "state.json"))
    for strat in strats:
        ctx = Ctx(eng, strat)
        print(f"\n=== {strat.name} — {strat.title}  risk {strat.risk_pct}% of alloc {strat.alloc(balance):.2f}")
        for sym in strat.symbols:
            key = strat.period_key(now, sym)
            rec = eng.st["positions"].get(sym)
            mine = rec is not None and rec.get("strat") == strat.name
            p = pos_map.get(sym)
            try:
                acts = strat.step(ctx, sym, key, p if (mine or (p and rec is None)) else None, rec if mine else None)
            except NotReady as e:
                print(f"{sym}: not ready ({e})")
                continue
            except Exception as e:
                print(f"{sym}: error {e}")
                continue
            print(f"\n{sym}  key={key}  exchange position: {p if p else 'none'}")
            for a in acts:
                if isinstance(a, Info):
                    print("  " + a.text)
                elif isinstance(a, Open):
                    try:
                        px = client.mark_price(sym)
                    except Exception:
                        px = ctx.klines(sym, strat.timeframe, 3)[-1]["close"]
                    sd = a.stop_dist if a.stop_dist is not None else abs(px - a.stop)
                    q = S.position_size(strat.alloc(balance), strat.risk_pct / 100, px, sd, cfg.max_leverage)
                    try:
                        qp = int(client.contract(sym).get("quantityPrecision", 4))
                    except Exception:
                        qp = 4
                    stop = a.stop if a.stop is not None else S.stop_price(px, a.side, sd)
                    print(f"  ACTION: OPEN {eng.side_txt(a.side)} price≈{px} qty={S.floor_qty(q, qp)} (raw {q:.6f}) "
                          f"notional≈{q * px:.2f} stop≈{stop:.6g} risk≈{q * sd:.2f}"
                          + (f" targets={[(t.r, t.frac) for t in a.targets]}" if a.targets else "")
                          + (f" BE after T{a.be_after}" if a.be_after else ""))
                elif isinstance(a, Close):
                    print(f"  ACTION: CLOSE ({a.reason})")
                elif isinstance(a, MoveStop):
                    print(f"  ACTION: MOVE STOP → {a.stop:.6g} ({a.reason})")
            if not any(isinstance(a, (Open, Close, MoveStop)) for a in acts):
                print("  ACTION: none (hold / flat)")
    print("\nNo orders were sent.")
    return 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="BingX strategy executor")
    ap.add_argument("--check", action="store_true", help="read-only connectivity check + Telegram test")
    ap.add_argument("--dry-run", action="store_true", help="compute signals and sizes, place nothing")
    ap.add_argument("--equity", type=float, default=None, help="equity for --dry-run without API keys")
    ap.add_argument("--env", default=os.path.join(HERE, ".env"))
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args(argv)

    from dotenv import dotenv_values
    env = {**{k: v for k, v in os.environ.items() if k.isupper()},
           **{k: v for k, v in dotenv_values(a.env).items() if v is not None}}
    cfg = Config.from_env(env)
    setup_logging(cfg.base_dir, a.v)
    strats = SX.load_enabled(env)
    errs = cfg.validate(strats, need_keys=not a.dry_run)
    if errs:
        for e in errs:
            log.error("config: %s", e)
        return 2
    if cfg.mode == "live" and not cfg.confirm_live:
        if a.check or a.dry_run:
            log.warning("MODE=live without CONFIRM_LIVE=YES: read-only mode allowed, trading loop would refuse to start")
        else:
            log.error("MODE=live requires CONFIRM_LIVE=YES in .env — refusing to start")
            return 2
    client, tg = build(cfg)
    if a.check:
        return run_check(cfg, client, tg, strats)
    if a.dry_run:
        return run_dry(cfg, client, strats, a.equity)
    with SingleInstance(os.path.join(cfg.base_dir, "executor.lock")):
        ex = Executor(cfg, client, tg, strats=strats)
        try:
            ex.run_forever()
        except KeyboardInterrupt:
            ex.notify("⏹ Исполнитель остановлен вручную (Ctrl+C). Стопы на бирже остаются.")
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
