"""DOLF серия 2 — бумажные шорты пампов на BingX. Запуск каждые 15 мин (GitHub Actions).
Только бумага: никаких ордеров. Состояние: state.json, журнал: journal.csv.
Секреты (env): TG_TOKEN, TG_CHAT, OPENMARKET_KEY.
"""
import os, json, time, csv, math, datetime as dt
import urllib.request, urllib.parse

BX = "https://open-api.bingx.com"
OM = "https://api.openmarket.xyz/v1/points"
STATE, JOURNAL = "state.json", "journal.csv"

# ---- правила (серия 2) ----
MIN_VOL = 3_000_000      # 24ч оборот USDT
MIN_PUMP = 15.0          # цена за 24ч, %
OI_DROP = 0.05           # OI ниже максимума 12ч на ≥5%
BTC4H_MAX = 1.0          # BTC за 4ч не выше +1% (плоский/слабый)
GRID = [(1.01, 0.3), (1.03, 0.3), (1.05, 0.4)]  # E1..E3: цена×k, доля
STOP_PAD = 1.03          # стоп = E3×1.03 (над верхней заявкой)
MIN_STOP_PCT = 1.5       # стоп от средней не ближе 1.5%
TGT_R = [(1.5, 0.4), (2.0, 0.3), (3.0, 0.3)]
ORDER_TTL_H, HOLD_MAX_H = 24, 48
MAX_NEW_PER_DAY, DAY_LIMIT_R = 2, -2.0
MAKER, TAKER = 0.0002, 0.0005
START_BAL, RISK = 300.0, 0.01
WATCH_PCT = 8.0          # копим свой OI BingX для монет с ростом ≥8%
EXCLUDE = ("NCSK", "NCFX", "NCCO", "NCSI")   # акции/форекс/сырьё BingX

def get(url, params=None, headers=None, tries=3):
    if params: url += "?" + urllib.parse.urlencode(params)
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers or {"User-Agent": "pumpbot"})
            with urllib.request.urlopen(req, timeout=20) as r: return json.loads(r.read())
        except Exception as e:
            if i == tries - 1: raise
            time.sleep(2)

def tg(text):
    tok, chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT")
    if not tok or not chat: print("TG:", text); return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try: urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=20)
    except Exception as e: print("tg error", e)

def now(): return int(time.time())
def iso(t): return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")

def klines(sym, interval, limit):
    d = get(BX + "/openApi/swap/v3/quote/klines", dict(symbol=sym, interval=interval, limit=limit))["data"]
    return sorted([(int(k["time"]) // 1000, float(k["open"]), float(k["high"]), float(k["low"]), float(k["close"])) for k in d])

def om_oi(coin):
    key = os.getenv("OPENMARKET_KEY")
    if not key: return None
    t = now()
    try:
        r = get(OM, {"type": "OPEN_INTEREST_AGG", "exchange": "BINANCE_FUTURES", "rawSymbol": coin + "USDT",
                     "interval": "FIFTEEN_MINUTES", "from": t - 12 * 3600, "period": 12 * 3600},
                {"X-OpenMarket-Key": key, "User-Agent": "pumpbot"})
        c = [p["Point"]["close"] for s in r.get("series", []) for p in s["points"]]
        return c if len(c) >= 24 else None
    except Exception as e:
        print("om", coin, e); return None

def bx_oi(sym):
    try: return float(get(BX + "/openApi/swap/v2/quote/openInterest", {"symbol": sym})["data"]["openInterest"])
    except Exception: return None

def load():
    if os.path.exists(STATE): return json.load(open(STATE))
    return {"bal": START_BAL, "trades": [], "oi": {}, "last_check": now() - 900, "summary_day": ""}

def journal(t):
    new = not os.path.exists(JOURNAL)
    with open(JOURNAL, "a", newline="") as f:
        w = csv.writer(f)
        if new: w.writerow(["id","coin","opened","closed","avg","stop","filled_share","R_paper","R_real","fee_R","funding_R","R_net","exits","oi_src"])
        w.writerow([t["id"], t["coin"], iso(t["t0"]), iso(t.get("tclose", now())), t.get("avg"), t["stop"], round(t.get("filled", 0), 2),
                    round(t["R_paper"], 3), round(t["R_real"], 3), round(t["fee_R"], 3), round(t["fund_R"], 3),
                    round(t["R_real"] - t["fee_R"] + t["fund_R"], 3), ";".join(t["exits"]), t["oi_src"] + (" | вне лимита" if t.get("ol") else "")])

# ---- ведение открытых сделок по 1m-свечам ----
def manage(t, S):
    sym = t["sym"]; since = t["checked"]
    ks = [k for k in klines(sym, "1m", 1000) if k[0] >= since]
    for ts, o, h, l, c in ks:
        t["checked"] = ts + 60
        if t["state"] == "pending" and ts > t["t0"] + ORDER_TTL_H * 3600 and t["filled"] == 0:
            t["state"] = "expired"; return "expired"
        # исполнение лимиток (шорт: цена дошла вверх до E)
        if t["state"] in ("pending", "open") and not t.get("be"):
            for e in t["grid"]:
                if not e["f"] and h >= e["p"]:
                    e["f"] = True; t["state"] = "open"
                    t["fee_R"] += e["w"] * MAKER * e["p"] / t["plan_avg_dist"]  # комиссия входа (лимитка)
                    if not t.get("t_fill"): t["t_fill"] = ts
        filled = [e for e in t["grid"] if e["f"]]
        if not filled: continue
        t["filled"] = sum(e["w"] for e in filled)
        t["avg"] = sum(e["p"] * e["w"] for e in filled) / t["filled"]
        risk_unit = t["plan_avg_dist"]  # 1R = расстояние план-средняя → стоп (на всю сетку)
        stop = t["avg"] if t.get("be") else t["stop"]
        # стоп первым (консервативно)
        if h >= stop:
            gap = o if o > stop else stop
            worst = max(gap, c) if c > stop else gap
            rem = t["rem"]
            t["R_paper"] += rem * t["filled"] * (t["avg"] - stop) / risk_unit
            t["R_real"] += rem * t["filled"] * (t["avg"] - worst) / risk_unit
            t["fee_R"] += rem * t["filled"] * TAKER * worst / risk_unit
            t["exits"].append(f"{iso(ts)}|{worst:.6g}|{rem:.2f}|{'БУ' if t.get('be') else 'стоп'}")
            t["rem"] = 0; t["state"] = "closed"; t["tclose"] = ts; return "closed"
        for i, (r_mult, share) in enumerate(TGT_R):
            if i in t["hit"]: continue
            tp = t["avg"] - r_mult * (t["stop"] - t["avg"])
            if l <= tp:
                t["hit"].append(i); part = share
                t["R_paper"] += part * t["filled"] * (t["avg"] - tp) / risk_unit
                t["R_real"] += part * t["filled"] * (t["avg"] - tp) / risk_unit
                t["fee_R"] += part * t["filled"] * MAKER * tp / risk_unit
                t["rem"] -= part; t["exits"].append(f"{iso(ts)}|{tp:.6g}|{part:.2f}|T{i+1}")
                if i == 0:
                    t["be"] = True  # БУ, неисполненные снимаем
                if t["rem"] <= 1e-9:
                    t["state"] = "closed"; t["tclose"] = ts; return "closed"
        if t.get("t_fill") and ts > t["t_fill"] + HOLD_MAX_H * 3600:
            rem = t["rem"]
            t["R_paper"] += rem * t["filled"] * (t["avg"] - c) / risk_unit
            t["R_real"] += rem * t["filled"] * (t["avg"] - c) / risk_unit
            t["fee_R"] += rem * t["filled"] * TAKER * c / risk_unit
            t["exits"].append(f"{iso(ts)}|{c:.6g}|{rem:.2f}|48ч"); t["rem"] = 0
            t["state"] = "closed"; t["tclose"] = ts; return "closed"
    # фандинг: шорт получает при плюсе, платит при минусе
    if t["state"] == "open":
        try:
            pi = get(BX + "/openApi/swap/v2/quote/premiumIndex", {"symbol": sym})["data"]
            nft = int(pi["nextFundingTime"]) // 1000; iv = int(pi.get("fundingIntervalHours", 8)) * 3600
            last_settle = nft - iv
            if last_settle > t.get("fund_ts", t["t_fill"]) and last_settle >= t["t_fill"]:
                rate = float(pi["lastFundingRate"])
                t["fund_R"] += rate * t["rem"] * t["filled"] * t["avg"] / t["plan_avg_dist"]
                t["fund_ts"] = last_settle
        except Exception as e: print("funding", e)
    return None

def main():
    first = not os.path.exists(STATE)
    S = load(); t_now = now(); today = iso(t_now)[:10]
    if first: tg("✅ DOLF серия 2: бумажный сканер шортов пампов запущен (BingX, каждые 15 мин). Пишу только при сигнале, исполнении, закрытии и раз в день сводку.")
    tick = get(BX + "/openApi/swap/v2/quote/ticker")["data"]
    msgs = []
    # 1) ведение
    for t in S["trades"]:
        if t["state"] in ("pending", "open"):
            was = t["state"]; ev = manage(t, S); tag = " (вне лимита)" if t.get("ol") else ""
            if t["state"] == "open" and was == "pending":
                msgs.append(f"🟡 {t['coin']}{tag} шорт: исполнена лимитка, средняя {t['avg']:.6g}, стоп {t['stop']:.6g}")
            if ev == "closed":
                net = t["R_real"] - t["fee_R"] + t["fund_R"]
                if t.get("ol"):
                    S["ol_R"] = S.get("ol_R", 0.0) + net; journal(t)
                    msgs.append(f"⚪ {t['coin']}{tag} закрыт: бумага {t['R_paper']:+.2f}R, реально {net:+.2f}R (комиссии {t['fee_R']:.2f}R). В баланс не идёт, вне лимита всего {S['ol_R']:+.2f}R")
                else:
                    S["bal"] += net * t["risk_usd"]; journal(t)
                    msgs.append(f"⚪ {t['coin']} закрыт: бумага {t['R_paper']:+.2f}R, реально {net:+.2f}R (комиссии {t['fee_R']:.2f}R). Баланс {S['bal']:.2f}$")
            elif ev == "expired":
                msgs.append(f"⏳ {t['coin']}{tag}: лимитки не исполнились за 24ч, снято")
    # 2) свой OI BingX для растущих монет
    for x in tick:
        sym = x["symbol"]
        if not sym.endswith("-USDT") or sym.startswith(EXCLUDE): continue
        if float(x["priceChangePercent"]) >= WATCH_PCT and float(x["quoteVolume"]) >= MIN_VOL:
            v = bx_oi(sym)
            if v: S["oi"].setdefault(sym, []).append([t_now, v])
    for sym in list(S["oi"]):
        S["oi"][sym] = [p for p in S["oi"][sym] if p[0] > t_now - 13 * 3600]
        if not S["oi"][sym]: del S["oi"][sym]
    # 3) скан
    btc = klines("BTC-USDT", "4h", 2); btc4h = (btc[-1][4] / btc[-1][1] - 1) * 100
    day_trades = [t for t in S["trades"] if iso(t["t0"])[:10] == today and t["state"] != "rejected" and not t.get("ol")]
    day_R = sum(t["R_real"] - t["fee_R"] for t in S["trades"] if t["state"] == "closed" and not t.get("ol") and iso(t.get("tclose", 0))[:10] == today)
    active = {t["sym"] for t in S["trades"] if t["state"] in ("pending", "open")}
    for x in tick:
        sym = x["symbol"]
        if not sym.endswith("-USDT") or sym.startswith(EXCLUDE) or sym in active: continue
        chg, vol = float(x["priceChangePercent"]), float(x["quoteVolume"])
        if chg < MIN_PUMP or vol < MIN_VOL: continue
        if any(t["sym"] == sym and t["t0"] > t_now - 24 * 3600 for t in S["trades"]): continue
        coin = sym.split("-")[0]
        series = om_oi(coin); src = "OpenMarket/Binance"
        if not series:
            own = [v for _, v in S["oi"].get(sym, [])]
            if len(own) >= 40: series, src = own, "BingX (свой)"
        if not series:
            print("нет OI", sym); continue
        drop = 1 - series[-1] / max(series)
        if drop < OI_DROP or btc4h > BTC4H_MAX: continue
        ol = len(day_trades) >= MAX_NEW_PER_DAY or day_R <= DAY_LIMIT_R   # лимит дня: сделку всё равно ведём, но отдельно (вне лимита)
        last, high = float(x["lastPrice"]), float(x["highPrice"])
        grid = [{"p": last * k, "w": w, "f": False} for k, w in GRID]
        stop = grid[-1]["p"] * STOP_PAD
        avg = sum(e["p"] * e["w"] for e in grid)
        if (stop / avg - 1) * 100 < MIN_STOP_PCT: continue
        risk_usd = S["bal"] * RISK
        t = {"id": len(S["trades"]) + 1, "sym": sym, "coin": coin, "t0": t_now, "checked": t_now, "state": "pending",
             "grid": grid, "stop": stop, "plan_avg_dist": stop - avg, "filled": 0, "rem": 1.0, "hit": [], "exits": [],
             "R_paper": 0.0, "R_real": 0.0, "fee_R": 0.0, "fund_R": 0.0, "risk_usd": risk_usd, "oi_src": src,
             "pump": chg, "oi_drop": round(drop * 100, 1), "btc4h": round(btc4h, 2), "ol": ol}
        S["trades"].append(t)
        if not ol: day_trades.append(t)
        tps = [avg - r * (stop - avg) for r, _ in TGT_R]
        msgs.append(f"🔴 БУМАГА шорт {coin}{' (вне лимита)' if ol else ''} (BingX): памп +{chg:.0f}%, OI −{drop*100:.1f}% от макс 12ч ({src}), BTC 4ч {btc4h:+.1f}%\n"
                    f"Сетка: {', '.join(f'{e['p']:.6g}' for e in grid)} (30/30/40)\nСтоп {stop:.6g} ({(stop/avg-1)*100:.1f}%)\n"
                    f"Цели {', '.join(f'{p:.6g}' for p in tps)} (40/30/30), БУ после T1\nРиск 1R = {risk_usd:.2f}$. Это не сигнал на реал." + ("\n⚠️ Вне лимита дня: сделка ведётся для статистики, в баланс не идёт." if ol else ""))
    # 4) сводка раз в день (после 18:00 UTC)
    if dt.datetime.now(dt.timezone.utc).hour >= 18 and S["summary_day"] != today:
        S["summary_day"] = today
        closed = [t for t in S["trades"] if t["state"] == "closed" and not t.get("ol")]
        n_ol = sum(1 for t in S["trades"] if t["state"] == "closed" and t.get("ol"))
        net = sum(t["R_real"] - t["fee_R"] + t["fund_R"] for t in closed)
        msgs.append(f"📊 Серия 2: закрыто {len(closed)}, реально {net:+.2f}R, бумага {sum(t['R_paper'] for t in closed):+.2f}R, баланс {S['bal']:.2f}$. Открыто/ждут: {len(active)}" + (f". Вне лимита: закрыто {n_ol}, {S.get('ol_R', 0.0):+.2f}R" if n_ol else ""))
    S["last_check"] = t_now
    json.dump(S, open(STATE, "w"), indent=1)
    for m in msgs: tg(m)
    print(f"ok {iso(t_now)} msgs={len(msgs)} btc4h={btc4h:.2f}")

if __name__ == "__main__":
    main()
