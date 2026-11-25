"""Engine behaviour on an in-memory exchange: stops, idempotency, restart, kill-switch, halts, caps,
partial take-profits + break-even, Telegram chat filter, live guard, dry-run write block."""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import sim_harness as H  # noqa: E402
import strategies as SX  # noqa: E402
from strategies import common as C  # noqa: E402
from strategies.base import Open, Strategy, Target  # noqa: E402
import executor as E  # noqa: E402
from telegram_bot import Telegram  # noqa: E402

GOLD = json.load(open(os.path.join(HERE, "fixtures", "paper_golden.json")))
_both = [i for i, r in enumerate(GOLD["signals"]["BTC-USDT"])
         if r[1] != 0 and GOLD["signals"]["ETH-USDT"][i][1] != 0]
T0 = GOLD["signals"]["BTC-USDT"][_both[200]][0]          # some day at 00:07 UTC, both coins have a signal
WANT = {s: next(r[1] for r in GOLD["signals"][s] if r[0] == T0) for s in H.SYMS}
HIST = H.Hist()


class TG(H.NullTG):
    def __init__(self, cmds=None):
        super().__init__()
        self.cmds = list(cmds or [])

    def poll(self, offset, max_age_s=600):
        c, self.cmds = self.cmds, []
        return c, offset


def engine(d, env=None, fx=None, tg=None, strats=None):
    env = dict({"STRAT_S06": "1", "RISK_PCT": "1", "ACT_DELAY_MIN": "5"}, **(env or {}))
    cfg = E.Config.from_env(dict({"BINGX_API_KEY": "k", "BINGX_API_SECRET": "s",
                                  "DAILY_LOSS_LIMIT_PCT": "50", "TOTAL_DD_LIMIT_PCT": "90"}, **env), base_dir=d)
    fx = fx or H.FakeExchange(HIST)
    ex = E.Executor(cfg, fx, tg or TG(), strats=strats if strats is not None else SX.load_enabled(env),
                    state_path=os.path.join(d, "state.json"), sleep=lambda *_: None)
    return ex, fx


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def stops(self, fx, sym):
        return [o for o in fx.orders.values() if o["symbol"] == sym and o["type"] == "STOP_MARKET"]


class EntryAndStops(Base):
    def test_entry_places_exchange_stop(self):
        ex, fx = engine(self.d)
        fx.advance(T0); ex.cycle()
        self.assertEqual(set(fx.pos), set(H.SYMS))
        for s in H.SYMS:
            self.assertEqual(fx.pos[s]["side"], WANT[s])
            st = self.stops(fx, s)
            self.assertEqual(len(st), 1)
            self.assertEqual(st[0]["closePosition"], "true")
            self.assertEqual(st[0]["side"], "SELL" if WANT[s] == 1 else "BUY")
            rec = ex.st["positions"][s]
            self.assertAlmostEqual(float(st[0]["stopPrice"]), rec["stop"], places=6)
            self.assertEqual((rec["stop"] - rec["entry"]) * WANT[s] < 0, True)

    def test_nothing_before_close_buffer(self):
        ex, fx = engine(self.d)
        fx.advance(T0 - 5 * 60_000)  # 00:02 UTC < ACT_DELAY_MIN
        ex.cycle()
        self.assertEqual(fx.pos, {})

    def test_restart_is_idempotent(self):
        ex, fx = engine(self.d)
        fx.advance(T0); ex.cycle()
        n = len(fx.opens)
        ex2, _ = engine(self.d, fx=fx)     # restart with same state.json
        for k in range(1, 4):
            fx.advance(T0 + k * 60_000); ex2.cycle()
        self.assertEqual(len(fx.opens), n)

    def test_restart_after_state_loss_does_not_double_enter(self):
        ex, fx = engine(self.d)
        fx.advance(T0); ex.cycle()
        os.remove(os.path.join(self.d, "state.json"))
        ex2, _ = engine(self.d, fx=fx)
        fx.advance(T0 + 60_000); ex2.cycle()
        self.assertEqual(len(fx.opens), 2)            # still only the two original entries
        for s in H.SYMS:
            self.assertEqual(len(self.stops(fx, s)), 1)
            self.assertTrue(ex2.st["positions"][s].get("adopted"))

    def test_missing_stop_is_replaced(self):
        ex, fx = engine(self.d)
        fx.advance(T0); ex.cycle()
        for o in self.stops(fx, "BTC-USDT"):
            del fx.orders[o["orderId"]]
        fx.advance(T0 + 60_000); ex.cycle()
        self.assertEqual(len(self.stops(fx, "BTC-USDT")), 1)
        self.assertTrue(any("стоп-ордер не найден" in m for m in ex.tg.msgs))

    def test_pending_filled_before_crash_is_adopted(self):
        ex, fx = engine(self.d)
        fx.T = T0
        side = WANT["BTC-USDT"]
        fx.place_order({"symbol": "BTC-USDT", "side": "BUY" if side == 1 else "SELL", "positionSide": "LONG",
                        "type": "MARKET", "quantity": "0.01", "clientOrderId": "s06btc19990101l1"})
        ex.st["pending"]["BTC-USDT"] = {"cid": "s06btc19990101l1", "key": C.day_str(T0 - C.DAY_MS), "strat": "S06",
                                        "side": side, "stop_dist": 1000.0, "stop": None, "ts": T0, "targets": [],
                                        "be_after": None, "meta": {}}
        E.save_state(os.path.join(self.d, "state.json"), ex.st)
        ex2, _ = engine(self.d, fx=fx)
        fx.advance(T0); ex2.cycle()
        btc_opens = [o for o in fx.opens if o["sym"] == "BTC-USDT"]
        self.assertEqual(len(btc_opens), 1)
        self.assertEqual(len(self.stops(fx, "BTC-USDT")), 1)

    def test_stop_hit_then_reentry_next_day(self):
        ex, fx = engine(self.d)
        fx.advance(T0); ex.cycle()
        oid = self.stops(fx, "BTC-USDT")[0]["orderId"]
        fx.T = T0 + 3600_000; fx.trigger(oid); ex.cycle()
        self.assertNotIn("BTC-USDT", ex.st["positions"])
        self.assertTrue(any("сработал стоп" in m for m in ex.tg.msgs))
        fx.advance(T0 + C.DAY_MS); ex.cycle()
        want_next = next(r[1] for r in GOLD["signals"]["BTC-USDT"] if r[0] == T0 + C.DAY_MS)
        self.assertEqual("BTC-USDT" in fx.pos, want_next != 0)


class Halts(Base):
    def test_stop_file_blocks_entries_until_removed(self):
        open(os.path.join(self.d, "STOP"), "w").close()
        ex, fx = engine(self.d)
        fx.advance(T0); ex.cycle()
        self.assertEqual(fx.pos, {})
        self.assertTrue(any("заблокирован" in m for m in ex.tg.msgs))
        os.remove(os.path.join(self.d, "STOP"))
        fx.advance(T0 + 60_000); ex.cycle()
        self.assertEqual(set(fx.pos), set(H.SYMS))

    def test_telegram_stop_and_resume(self):
        ex, fx = engine(self.d, tg=TG(["/stop"]))
        fx.advance(T0); ex.cycle()
        self.assertEqual(fx.pos, {})
        ex.tg.cmds = ["/resume"]
        fx.advance(T0 + 60_000); ex.cycle()
        self.assertEqual(set(fx.pos), set(H.SYMS))

    def test_stop_does_not_close_positions_but_closeall_does(self):
        ex, fx = engine(self.d)
        fx.advance(T0); ex.cycle()
        ex.tg.cmds = ["/stop"]; fx.advance(T0 + 60_000); ex.cycle()
        self.assertEqual(set(fx.pos), set(H.SYMS))
        ex.tg.cmds = ["/closeall"]; fx.advance(T0 + 120_000); ex.cycle()
        self.assertEqual(set(fx.pos), set(H.SYMS))   # needs confirmation
        ex.tg.cmds = ["/confirm_closeall"]; fx.advance(T0 + 180_000); ex.cycle()
        self.assertEqual(fx.pos, {})
        self.assertEqual(fx.orders, {})
        self.assertTrue(ex.st["risk"]["tg_stop"])

    def test_daily_loss_halt(self):
        ex, fx = engine(self.d, env={"DAILY_LOSS_LIMIT_PCT": "3"})
        fx.advance(T0 - 6 * 60_000); ex.cycle()      # 00:01: records day-start equity, no trading yet
        fx.bal *= 0.96                                 # -4% (e.g. realised loss)
        fx.advance(T0); ex.cycle()
        self.assertEqual(ex.st["risk"]["halt_daily_day"], C.day_str(T0))
        self.assertEqual(fx.pos, {})
        self.assertTrue(any("Дневной лимит" in m for m in ex.tg.msgs))

    def test_total_dd_halt_sticky(self):
        ex, fx = engine(self.d, env={"TOTAL_DD_LIMIT_PCT": "10"})
        fx.advance(T0 - 6 * 60_000); ex.cycle()
        fx.bal *= 0.85
        fx.advance(T0 - 5 * 60_000 - C.DAY_MS + C.DAY_MS); ex.cycle()
        self.assertTrue(ex.st["risk"]["halt_dd"])
        fx.advance(T0 + C.DAY_MS); ex.cycle()
        self.assertEqual(fx.pos, {})


class Caps(Base):
    def test_max_open_positions(self):
        ex, fx = engine(self.d, env={"MAX_OPEN_POSITIONS": "1"})
        fx.advance(T0); ex.cycle()
        self.assertEqual(len(fx.pos), 1)
        self.assertTrue(any("лимит одновременных позиций" in m for m in ex.tg.msgs))

    def test_total_risk_cap(self):
        ex, fx = engine(self.d, env={"MAX_TOTAL_RISK_PCT": "0.6"})   # each S06 trade risks 0.5% of balance
        fx.advance(T0); ex.cycle()
        self.assertEqual(len(fx.pos), 1)
        self.assertTrue(any("общий открытый риск" in m for m in ex.tg.msgs))


class OneShot(Strategy):
    """Test strategy: one long on BTC with two partial targets and break-even after T1."""
    name = "TEST"; tag = "tst"; timeframe = "4h"; default_symbols = ["BTC-USDT"]; capital_divisor = 1

    def step(self, ctx, sym, key, pos, rec):
        if pos or ctx.state.get("fired"):
            return []
        ctx.state["fired"] = True
        px = ctx.klines(sym, "4h", 5)[-1]["close"]
        return [Open(side=1, stop=px * 0.98, targets=[Target(frac=0.5, r=1.0), Target(frac=0.5, r=2.0)], be_after=1)]


class TargetsAndBE(Base):
    def test_partial_tp_then_breakeven_then_tp2(self):
        ex, fx = engine(self.d, env={"STRAT_S06": "0"}, strats=[OneShot({"RISK_PCT": "1"})])
        fx.advance(T0); ex.cycle()
        rec = ex.st["positions"]["BTC-USDT"]
        tps = [o for o in fx.orders.values() if o["type"] == "TAKE_PROFIT_MARKET"]
        self.assertEqual(len(tps), 2)
        self.assertAlmostEqual(float(tps[0]["stopPrice"]), rec["entry"] + rec["risk_dist"], delta=0.01)
        self.assertAlmostEqual(float(tps[1]["stopPrice"]), rec["entry"] + 2 * rec["risk_dist"], delta=0.01)
        self.assertAlmostEqual(float(tps[0]["origQty"]) + float(tps[1]["origQty"]), rec["qty0"], places=8)
        fx.px_override["BTC-USDT"] = rec["entry"] * 1.015
        fx.T += 60_000; fx.trigger(tps[0]["orderId"]); ex.cycle()
        rec = ex.st["positions"]["BTC-USDT"]
        self.assertTrue(rec["targets"][0]["done"])
        self.assertTrue(rec["be"])
        st = self.stops(fx, "BTC-USDT")
        self.assertEqual(len(st), 1)
        self.assertAlmostEqual(float(st[0]["stopPrice"]), rec["entry"], delta=0.01)
        fx.T += 60_000; fx.trigger(tps[1]["orderId"]); ex.cycle()
        self.assertNotIn("BTC-USDT", fx.pos)
        self.assertNotIn("BTC-USDT", ex.st["positions"])
        self.assertEqual(fx.orders, {})


class TelegramFilter(unittest.TestCase):
    def test_only_own_chat_and_fresh(self):
        import time as _t

        class R:
            def __init__(self, j): self.j = j
            def json(self): return self.j

        now = _t.time()
        upd = {"ok": True, "result": [
            {"update_id": 1, "message": {"chat": {"id": 111}, "date": now, "text": "/stop"}},
            {"update_id": 2, "message": {"chat": {"id": 999}, "date": now, "text": "/closeall"}},
            {"update_id": 3, "message": {"chat": {"id": 111}, "date": now - 3600, "text": "/closeall"}},
            {"update_id": 4, "message": {"chat": {"id": 111}, "date": now, "text": "/status@my_bot"}}]}

        class S:
            def get(self, url, params=None, timeout=None): return R(upd)

        tg = Telegram("T", "111", session=S())
        cmds, off = tg.poll(None)
        self.assertEqual(cmds, ["/stop", "/status"])
        self.assertEqual(off, 5)


class Guards(unittest.TestCase):
    def test_live_requires_confirm(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, ".env")
        with open(p, "w") as f:
            f.write("MODE=live\nBINGX_API_KEY=a\nBINGX_API_SECRET=b\n")
        self.assertEqual(E.main(["--env", p]), 2)
        shutil.rmtree(d, ignore_errors=True)

    def test_dry_client_blocks_writes(self):
        dc = E._DryClient(H.FakeExchange(HIST))
        with self.assertRaises(RuntimeError):
            dc.place_order({})
        with self.assertRaises(RuntimeError):
            dc.set_leverage("BTC-USDT", "LONG", 3)

    def test_default_config_is_demo_s06(self):
        cfg = E.Config.from_env({})
        self.assertEqual(cfg.mode, "demo")
        self.assertEqual(cfg.base, "https://open-api-vst.bingx.com")
        self.assertEqual([s.name for s in SX.load_enabled({})], ["S06"])


if __name__ == "__main__":
    unittest.main()
