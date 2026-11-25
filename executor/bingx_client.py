"""Minimal BingX USDT-M perpetual REST client (requests + stdlib only).

Signing (BingX docs, Quick Start -> Signature, verified 2026-11-25 at https://bingx-api.github.io/docs-v3/):
  * all params + timestamp(ms) sorted by key (ASCII asc) -> "k1=v1&k2=v2..." (unencoded)
  * signature = HMAC-SHA256(secret, that string) as lowercase hex
  * sent as query string "...&signature=<hex>", header X-BX-APIKEY
  * values need URL-encoding only if they contain '[' or '{' (never the case here)
  * timestamp must be within 5 s of server time (recvWindow widens this) -> we sync with
    GET /openApi/swap/v2/server/time
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from urllib.parse import quote

import requests

log = logging.getLogger("bingx")

LIVE_BASE = "https://open-api.bingx.com"
DEMO_BASE = "https://open-api-vst.bingx.com"

# endpoints (USDT-M perpetual)
EP_SERVER_TIME = "/openApi/swap/v2/server/time"
EP_KLINES = "/openApi/swap/v3/quote/klines"
EP_CONTRACTS = "/openApi/swap/v2/quote/contracts"
EP_PREMIUM = "/openApi/swap/v2/quote/premiumIndex"
EP_BALANCE = "/openApi/swap/v3/user/balance"
EP_POSITIONS = "/openApi/swap/v2/user/positions"
EP_POSITION_MODE = "/openApi/swap/v1/positionSide/dual"
EP_MARGIN_TYPE = "/openApi/swap/v2/trade/marginType"
EP_LEVERAGE = "/openApi/swap/v2/trade/leverage"
EP_ORDER = "/openApi/swap/v2/trade/order"
EP_OPEN_ORDERS = "/openApi/swap/v2/trade/openOrders"
EP_CANCEL_ALL = "/openApi/swap/v2/trade/allOpenOrders"
EP_CLOSE_ALL = "/openApi/swap/v2/trade/closeAllPositions"

RETRYABLE_CODES = {100410, 100500, 100503, 80012}  # rate limit / system busy / service unavailable
TIME_CODES = {100421, 109400}


class BingXError(Exception):
    def __init__(self, code, msg, path=""):
        super().__init__(f"BingX {path} code={code} msg={msg}")
        self.code, self.msg, self.path = code, msg, path


def sign_params(params: dict, secret: str) -> tuple[str, str]:
    """Return (sorted unencoded param string, lowercase hex signature)."""
    items = sorted((k, v) for k, v in params.items() if v is not None)
    s = "&".join(f"{k}={_val(v)}" for k, v in items)
    sig = hmac.new(secret.encode("utf-8"), s.encode("utf-8"), hashlib.sha256).hexdigest()
    return s, sig


def _val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def build_query(params: dict, secret: str) -> str:
    s, sig = sign_params(params, secret)
    if "[" in s or "{" in s:  # doc rule: encode values only when JSON present
        items = sorted((k, v) for k, v in params.items() if v is not None)
        s_sent = "&".join(f"{k}={quote(_val(v), safe='')}" for k, v in items)
    else:
        s_sent = s
    return f"{s_sent}&signature={sig}"


class BingX:
    def __init__(self, api_key: str, api_secret: str, base: str, public_base: str = LIVE_BASE,
                 session: requests.Session | None = None, recv_window: int = 10_000, timeout: float = 15.0,
                 sleep=time.sleep):
        self.key, self.secret = api_key or "", api_secret or ""
        self.base, self.public_base = base.rstrip("/"), public_base.rstrip("/")
        self.s = session or requests.Session()
        self.recv_window, self.timeout = recv_window, timeout
        self.offset_ms = 0
        self.last_sync = 0.0
        self.sleep = sleep

    # ------------------------------------------------------------ time
    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.offset_ms

    def server_time(self, base: str | None = None) -> int:
        d = self._public(EP_SERVER_TIME, {}, base=base or self.base)
        return int(d["serverTime"])

    def sync_time(self) -> int:
        t0 = time.time()
        st = self.server_time()
        t1 = time.time()
        local_mid = int((t0 + t1) / 2 * 1000)
        self.offset_ms = st - local_mid
        self.last_sync = t1
        log.info("time sync: server-local offset %d ms", self.offset_ms)
        return self.offset_ms

    def maybe_sync(self, every_s: float = 1800):
        if time.time() - self.last_sync > every_s:
            self.sync_time()

    # ------------------------------------------------------------ transport
    def _parse(self, r: requests.Response, path: str):
        try:
            j = r.json()
        except ValueError:
            raise BingXError(r.status_code, r.text[:200], path)
        code = j.get("code", 0)
        if code not in (0, "0"):
            raise BingXError(int(code) if str(code).lstrip("-").isdigit() else code, j.get("msg", ""), path)
        return j.get("data", j)

    def _public(self, path: str, params: dict, base: str | None = None, retries: int = 4):
        url = (base or self.public_base) + path
        last = None
        for i in range(retries):
            try:
                r = self.s.get(url, params=params, timeout=self.timeout)
                return self._parse(r, path)
            except BingXError as e:
                last = e
                if e.code not in RETRYABLE_CODES and not (isinstance(e.code, int) and 500 <= e.code < 600):
                    raise
            except requests.RequestException as e:
                last = e
            self.sleep(2 + 3 * i)
        raise last if isinstance(last, Exception) else RuntimeError(path)

    def signed(self, method: str, path: str, params: dict | None = None, retries: int | None = None):
        """Signed call. GET retries freely; POST/DELETE retry only on errors where the request
        certainly was not executed (HTTP-level rejection / rate limit / timestamp), never on timeouts."""
        if not self.key or not self.secret:
            raise BingXError("nokey", "BINGX_API_KEY / BINGX_API_SECRET not set", path)
        method = method.upper()
        idempotent = method == "GET"
        retries = retries if retries is not None else (4 if idempotent else 2)
        last = None
        for i in range(retries):
            p = dict(params or {})
            p["timestamp"] = self.now_ms()
            p["recvWindow"] = self.recv_window
            url = f"{self.base}{path}?{build_query(p, self.secret)}"
            try:
                r = self.s.request(method, url, headers={"X-BX-APIKEY": self.key}, timeout=self.timeout)
                return self._parse(r, path)
            except BingXError as e:
                last = e
                if e.code in TIME_CODES or "timestamp" in str(e.msg).lower():
                    log.warning("timestamp rejected, resyncing: %s", e)
                    try:
                        self.sync_time()
                    except Exception as se:  # noqa
                        log.warning("resync failed: %s", se)
                elif e.code in RETRYABLE_CODES:
                    pass
                else:
                    raise
            except (requests.ConnectionError,) as e:
                # connection refused/reset before response: safe to retry only for GET
                last = e
                if not idempotent:
                    raise
            except requests.Timeout as e:
                last = e
                if not idempotent:
                    raise  # caller must verify whether the order went through
            except requests.RequestException as e:
                last = e
                if not idempotent:
                    raise
            self.sleep(1.5 + 2 * i)
        raise last if isinstance(last, Exception) else RuntimeError(path)

    # ------------------------------------------------------------ market data (public)
    def klines(self, symbol: str, interval: str, limit: int):
        return self._public(EP_KLINES, {"symbol": symbol, "interval": interval, "limit": limit})

    def contract(self, symbol: str) -> dict:
        try:
            d = self._public(EP_CONTRACTS, {"symbol": symbol}, base=self.base)
        except Exception:
            d = self._public(EP_CONTRACTS, {"symbol": symbol})
        rows = d if isinstance(d, list) else [d]
        for c in rows:
            if c.get("symbol") == symbol:
                return c
        raise BingXError("nosymbol", f"{symbol} not in contracts", EP_CONTRACTS)

    def funding_rate(self, symbol: str) -> float:
        d = self._public(EP_PREMIUM, {"symbol": symbol})
        if isinstance(d, list):
            d = d[0]
        return float(d["lastFundingRate"])

    def mark_price(self, symbol: str) -> float:
        d = self._public(EP_PREMIUM, {"symbol": symbol}, base=self.base)
        if isinstance(d, list):
            d = d[0]
        return float(d["markPrice"])

    # ------------------------------------------------------------ account (signed)
    def balance(self) -> dict:
        """Returns {'asset','balance','equity','availableMargin',...} for USDT (live) or VST (demo)."""
        d = self.signed("GET", EP_BALANCE)
        rows = d if isinstance(d, list) else [d.get("balance", d)]
        pick = None
        for pref in ("USDT", "VST"):
            for r in rows:
                if r.get("asset") == pref:
                    pick = r
                    break
            if pick:
                break
        if pick is None and rows:
            pick = rows[0]
        if not pick:
            raise BingXError("nobal", f"unexpected balance payload {str(d)[:200]}", EP_BALANCE)
        out = dict(pick)
        for k in ("balance", "equity", "availableMargin", "unrealizedProfit", "usedMargin", "freezedMargin"):
            if k in out:
                out[k] = float(out[k] or 0)
        return out

    def positions(self, symbol: str | None = None) -> list[dict]:
        p = {"symbol": symbol} if symbol else {}
        d = self.signed("GET", EP_POSITIONS, p) or []
        out = []
        for r in d:
            amt = float(r.get("positionAmt") or 0)
            if amt == 0:
                continue
            ps = str(r.get("positionSide", "")).upper()
            side = 1 if ps == "LONG" else -1 if ps == "SHORT" else (1 if amt > 0 else -1)
            out.append({
                "symbol": r["symbol"], "side": side, "qty": abs(amt), "positionSide": ps,
                "avgPrice": float(r.get("avgPrice") or 0), "isolated": r.get("isolated"),
                "leverage": r.get("leverage"), "unrealizedProfit": float(r.get("unrealizedProfit") or 0),
                "positionId": r.get("positionId"), "markPrice": float(r.get("markPrice") or 0) or None,
            })
        return out

    def dual_side(self) -> bool:
        d = self.signed("GET", EP_POSITION_MODE)
        return str(d.get("dualSidePosition", "false")).lower() == "true"

    def margin_type(self, symbol: str) -> str:
        return str(self.signed("GET", EP_MARGIN_TYPE, {"symbol": symbol}).get("marginType", ""))

    def set_margin_type(self, symbol: str, mtype: str = "ISOLATED"):
        return self.signed("POST", EP_MARGIN_TYPE, {"symbol": symbol, "marginType": mtype})

    def leverage(self, symbol: str) -> dict:
        return self.signed("GET", EP_LEVERAGE, {"symbol": symbol})

    def set_leverage(self, symbol: str, side: str, lev: int):
        return self.signed("POST", EP_LEVERAGE, {"symbol": symbol, "side": side, "leverage": int(lev)})

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        d = self.signed("GET", EP_OPEN_ORDERS, {"symbol": symbol} if symbol else {})
        return (d or {}).get("orders", []) if isinstance(d, dict) else (d or [])

    def get_order(self, symbol: str, order_id=None, client_order_id: str | None = None) -> dict:
        p = {"symbol": symbol}
        if order_id is not None:
            p["orderId"] = order_id
        if client_order_id:
            p["clientOrderId"] = client_order_id
        d = self.signed("GET", EP_ORDER, p)
        return d.get("order", d) if isinstance(d, dict) else d

    def place_order(self, params: dict) -> dict:
        d = self.signed("POST", EP_ORDER, params)
        return d.get("order", d) if isinstance(d, dict) else d

    def cancel_order(self, symbol: str, order_id) -> dict:
        return self.signed("DELETE", EP_ORDER, {"symbol": symbol, "orderId": order_id})

    def cancel_all(self, symbol: str):
        return self.signed("DELETE", EP_CANCEL_ALL, {"symbol": symbol})

    def close_all_positions(self, symbol: str):
        return self.signed("POST", EP_CLOSE_ALL, {"symbol": symbol})
