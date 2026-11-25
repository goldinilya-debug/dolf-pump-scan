"""Regenerate tests/fixtures/paper_golden.json from bot/paper_bot.py (needs pandas + numpy).
Run from the executor folder:  python tests/make_golden.py
The unit tests compare the executor against this file, so they run without pandas."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sim_harness as H  # noqa: E402


def daily_signals_pandas():
    """S06 inputs per day exactly as paper_bot computes them (pandas ewm ATR, iloc[-31])."""
    import numpy as np
    import pandas as pd
    import importlib.util
    spec = importlib.util.spec_from_file_location("paper_bot", os.path.join(H.EXE, "..", "bot", "paper_bot.py"))
    pb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pb)
    hist = H.Hist()
    out = {}
    for sym in H.SYMS:
        rows = []
        days = hist.b1d[sym]
        for k in range(40, len(days)):
            T = days[k]["time"] + 7 * 60_000  # 00:07 UTC of day k -> last closed = day k-1
            w = hist.window(sym, "1d", 120, T)
            d = pd.DataFrame(w).astype(float).sort_values("time")
            d = d[d.time + 86400e3 <= T]
            d["atr"] = pb.atr(d)
            want = int(np.sign(d.close.iloc[-1] / d.close.iloc[-31] - 1))
            rows.append([int(T), want, float(d.atr.iloc[-1])])
        out[sym] = rows
    return out


def main():
    t0, t1 = H.sim_range()
    g = {"t0": t0, "t1": t1, "signals": daily_signals_pandas()}
    for strat in ("S06", "B"):
        acc, _ = H.run_paper_sim(strat, t0, t1)
        g[strat] = {"equity": float(acc["equity"]),
                    "closed": [{"sym": c["sym"], "side": int(c["side"]), "entry": float(c["entry"]),
                                "exit": float(c["exit"]), "reason": c["reason"], "exit_t": c["exit_t"]}
                               for c in acc["closed"]],
                    "open": {k: {"side": int(p["side"]), "entry": float(p["entry"]), "stop": float(p["stop"]),
                                 "qty": float(p["qty"])} for k, p in acc["pos"].items()}}
        print(strat, len(g[strat]["closed"]), "trades, equity", g[strat]["equity"])
    with open(os.path.join(HERE, "fixtures", "paper_golden.json"), "w") as f:
        json.dump(g, f, indent=0)


if __name__ == "__main__":
    main()
