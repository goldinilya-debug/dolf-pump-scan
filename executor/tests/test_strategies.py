"""Strategy math: sizing, rounding, S06 signal vs paper_bot golden values, Star detector, registry."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import sim_harness as H  # noqa: E402
import strategies as SX  # noqa: E402
from strategies import common as C  # noqa: E402
from strategies.base import Open, Target  # noqa: E402
from strategies.star import Star  # noqa: E402
from strategies.pumps import Pumps  # noqa: E402

GOLD = json.load(open(os.path.join(HERE, "fixtures", "paper_golden.json")))


class Sizing(unittest.TestCase):
    def test_risk_based(self):
        # alloc 5000, risk 1% = 50 USDT, stop 3000 away -> 0.016667
        self.assertAlmostEqual(C.position_size(5000, 0.01, 60000, 3000, 3), 50 / 3000)

    def test_leverage_cap(self):
        # tiny stop would want 50/10 = 5 BTC, cap = 3*5000/60000 = 0.25
        self.assertAlmostEqual(C.position_size(5000, 0.01, 60000, 10, 3), 0.25)

    def test_degenerate(self):
        self.assertEqual(C.position_size(5000, 0.01, 60000, 0, 3), 0.0)

    def test_rounding(self):
        self.assertEqual(C.floor_qty(0.0123999, 4), 0.0123)
        self.assertEqual(C.floor_qty(1.0, 2), 1.0)
        self.assertEqual(C.round_price(60000.05, 1), 60000.1)
        self.assertEqual(C.fmt_num(1e-05, 6), "0.000010")
        self.assertEqual(C.fmt_num(0.1 + 0.2, 4), "0.3000")

    def test_alloc_like_paper(self):
        s06 = SX.S06({})
        self.assertEqual(s06.alloc(10_000), 5_000)   # paper_bot: equity / 2
        s06b = SX.S06({"CAPITAL_S06": "0.5", "RISK_S06": "0.7"})
        self.assertEqual(s06b.alloc(10_000), 2_500)
        self.assertEqual(s06b.risk_pct, 0.7)
        self.assertEqual(SX.S06({"RISK_PCT": "2"}).risk_pct, 2.0)


class S06Parity(unittest.TestCase):
    def test_signal_every_day_matches_paper_bot(self):
        """want and ATR for every day 2024-08..2026-09, BTC and ETH, vs pandas paper_bot (golden)."""
        hist = H.Hist()
        n = 0
        for sym in H.SYMS:
            for T, want, atr in GOLD["signals"][sym]:
                bars = C.closed_only(hist.window(sym, "1d", C.S06_KLINE_LIMIT, T), "1d", T)
                sig = C.s06_signal(bars)
                self.assertEqual(sig["want"], want, (sym, T))
                self.assertAlmostEqual(sig["atr"], atr, delta=1e-9 * atr)
                n += 1
        self.assertGreater(n, 1400)

    def test_step_actions(self):
        hist = H.Hist()
        fx = H.FakeExchange(hist)
        T = GOLD["signals"]["BTC-USDT"][300][0]
        fx.T = T

        class Ctx:
            state = {}
            now_ms = T
            def klines(self, sym, tf, limit):
                return C.closed_only(hist.window(sym, tf, limit, T), tf, T)

        s = SX.S06({})
        key = s.period_key(T, "BTC-USDT")
        acts = s.step(Ctx(), "BTC-USDT", key, None, None)
        want = GOLD["signals"]["BTC-USDT"][300][1]
        opens = [a for a in acts if isinstance(a, Open)]
        if want:
            self.assertEqual(opens[0].side, want)
            self.assertAlmostEqual(opens[0].stop_dist, 3 * GOLD["signals"]["BTC-USDT"][300][2], places=6)
        # opposite position -> Close then Open
        acts2 = s.step(Ctx(), "BTC-USDT", key, {"side": -want or 1, "qty": 1}, {})
        kinds = [type(a).__name__ for a in acts2]
        self.assertIn("Close", kinds)


class StarTests(unittest.TestCase):
    def _bars(self):
        bars, t, p = [], 0, 100.0
        for i in range(60):   # gentle downtrend with small ranges
            o = p; c = p - 0.3
            bars.append({"time": t, "open": o, "high": o + 0.5, "low": c - 0.5, "close": c, "volume": 1}); t += C.H4_MS; p = c
        # c1: big red to a new low, c2: doji, c3: big green closing above c1 mid
        bars.append({"time": t, "open": p, "high": p + 0.2, "low": p - 4.2, "close": p - 4.0, "volume": 1}); t += C.H4_MS
        q = p - 4.0
        bars.append({"time": t, "open": q, "high": q + 0.3, "low": q - 0.6, "close": q + 0.05, "volume": 1}); t += C.H4_MS
        bars.append({"time": t, "open": q + 0.05, "high": q + 3.6, "low": q - 0.1, "close": q + 3.5, "volume": 1})
        return bars

    def test_morning_star(self):
        bars = self._bars()
        d = Star.detect(bars)
        self.assertIsNotNone(d)
        side, stop = d
        self.assertEqual(side, 1)
        self.assertLess(stop, min(b["low"] for b in bars[-3:]))

    def test_no_signal_on_trend(self):
        self.assertIsNone(Star.detect(self._bars()[:-3]))


class Registry(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual([s.name for s in SX.load_enabled({})], ["S06"])

    def test_flags(self):
        names = [s.name for s in SX.load_enabled({"STRAT_S06": "0", "STRAT_DONCHIAN": "1", "STRAT_STAR": "1"})]
        self.assertEqual(names, ["DONCHIAN", "STAR"])

    def test_pumps_is_stub(self):
        p = Pumps({})
        self.assertFalse(p.implemented)
        plan = Pumps.plan(1.0)
        self.assertEqual([round(e[0], 4) for e in plan.entries], [1.01, 1.03, 1.05])
        self.assertAlmostEqual(plan.stop, 1.05 * 1.03)
        self.assertEqual([t.r for t in plan.targets], [1.5, 2.0, 3.0])
        self.assertEqual(plan.be_after, 1)
        from executor import Config
        errs = Config().validate([p], need_keys=False)
        self.assertTrue(any("stub" in e for e in errs))


if __name__ == "__main__":
    unittest.main()
