"""Full replay parity: executor engine + FakeExchange vs bot/paper_bot.py (golden file made by make_golden.py).

Same 4h bars (Binance 2024-07..2026-09 from project data/), same fill price (last closed 4h close),
same fees (0.07%/side), funding off on both sides. S06 must match trade-for-trade.
Takes ~30 s (S06) + ~70 s (Donchian). Set SKIP_SLOW=1 to skip.
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sim_harness as H  # noqa: E402

GOLD = json.load(open(os.path.join(HERE, "fixtures", "paper_golden.json")))
SLOW = os.environ.get("SKIP_SLOW") != "1"
REASON = {"S06": {"market": "momentum flip", "stop": "stop"}, "B": {"market": "exit channel 20", "stop": "stop"}}


def key(t):
    return (t["sym"], round(t["entry"], 6), t["side"])


@unittest.skipUnless(SLOW, "SKIP_SLOW=1")
class Replay(unittest.TestCase):
    def test_s06_trade_for_trade(self):
        fx, ex = H.run_executor_sim("S06", GOLD["t0"], GOLD["t1"])
        g = GOLD["S06"]
        self.assertEqual(len(fx.ledger), len(g["closed"]))
        for a, b in zip(fx.ledger, g["closed"]):
            self.assertEqual((a["sym"], a["side"]), (b["sym"], b["side"]))
            self.assertAlmostEqual(a["entry"], b["entry"], places=6)
            self.assertAlmostEqual(a["exit"], b["exit"], delta=1e-6 * b["exit"])
            # flip exits: 'momentum flip' (or 'momentum = 0'); stops: 'stop'
            self.assertTrue(b["reason"].startswith(REASON["S06"][a["reason"]].split()[0]), (a, b))
        self.assertAlmostEqual(fx.bal, g["equity"], delta=0.01)
        self.assertEqual({k: v["side"] for k, v in fx.pos.items()}, {k: v["side"] for k, v in g["open"].items()})
        for k, v in g["open"].items():
            self.assertAlmostEqual(fx.pos[k]["qty"], v["qty"], delta=1e-8)
        self.assertGreater(sum(1 for t in g["closed"] if t["reason"] == "stop"), 0)  # stops were exercised

    def test_donchian_same_trades(self):
        """Same trade set; 3 exits differ by one tick: when the new trailing stop is already through the
        price the engine closes at market (the exchange would reject such a stop), paper_bot waits for the
        next bar's open."""
        fx, ex = H.run_executor_sim("B", GOLD["t0"], GOLD["t1"])
        g = GOLD["B"]
        A = {key(t): t for t in fx.ledger}
        B_ = {key(t): t for t in g["closed"]}
        self.assertEqual(set(A), set(B_))
        for k in A:
            self.assertAlmostEqual(A[k]["exit"], B_[k]["exit"], delta=2e-4 * B_[k]["exit"])
        self.assertAlmostEqual(fx.bal, g["equity"], delta=0.5)


if __name__ == "__main__":
    unittest.main()
