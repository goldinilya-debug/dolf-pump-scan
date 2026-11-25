"""Signing + HTTP behaviour of bingx_client with a mocked requests session (no network)."""
import hashlib
import hmac
import os
import sys
import unittest
from urllib.parse import urlparse, parse_qsl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

import bingx_client as B  # noqa: E402


class Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code, self.text = payload, status, str(payload)

    def json(self):
        return self._p


class FakeSession:
    def __init__(self, script):
        self.script = list(script)   # items: dict payload, or Exception instance
        self.calls = []

    def _next(self, method, url, **kw):
        self.calls.append((method, url, kw))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return Resp(item)

    def request(self, method, url, **kw):
        return self._next(method, url, **kw)

    def get(self, url, **kw):
        return self._next("GET", url, **kw)


def mk(script, **kw):
    return B.BingX("KEY", "SECRET", base=B.DEMO_BASE, session=FakeSession(script), sleep=lambda *_: None, **kw)


class SigningTests(unittest.TestCase):
    def test_doc_example_string_and_hex(self):
        # docs: params symbol=BTC-USDT, recvWindow=0, timestamp=1696751141337 ->
        # sorted signing string "recvWindow=0&symbol=BTC-USDT&timestamp=1696751141337"
        s, sig = B.sign_params({"symbol": "BTC-USDT", "recvWindow": 0, "timestamp": 1696751141337}, "SECRET_KEY")
        self.assertEqual(s, "recvWindow=0&symbol=BTC-USDT&timestamp=1696751141337")
        exp = hmac.new(b"SECRET_KEY", s.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(sig, exp)
        self.assertEqual(sig, sig.lower())
        self.assertEqual(len(sig), 64)

    def test_query_not_encoded_and_bool(self):
        q = B.build_query({"closePosition": True, "stopPrice": "60000.5", "symbol": "BTC-USDT", "timestamp": 1}, "x")
        self.assertTrue(q.startswith("closePosition=true&stopPrice=60000.5&symbol=BTC-USDT&timestamp=1&signature="))

    def test_json_values_encoded_but_signed_raw(self):
        q = B.build_query({"data": '[{"a":1}]', "timestamp": 1}, "x")
        raw, sig = B.sign_params({"data": '[{"a":1}]', "timestamp": 1}, "x")
        self.assertIn("data=%5B%7B%22a%22%3A1%7D%5D", q)
        self.assertTrue(q.endswith("signature=" + sig))

    def test_signed_request_shape(self):
        c = mk([{"code": 0, "data": [{"asset": "VST", "balance": "100", "equity": "101", "availableMargin": "99"}]}])
        c.offset_ms = 0
        bal = c.balance()
        self.assertEqual(bal["asset"], "VST")
        self.assertEqual(bal["balance"], 100.0)
        method, url, kw = c.s.calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.startswith("https://open-api-vst.bingx.com/openApi/swap/v3/user/balance?"))
        self.assertEqual(kw["headers"]["X-BX-APIKEY"], "KEY")
        qs = dict(parse_qsl(urlparse(url).query))
        sig = qs.pop("signature")
        s = "&".join(f"{k}={qs[k]}" for k in sorted(qs))
        self.assertEqual(sig, hmac.new(b"SECRET", s.encode(), hashlib.sha256).hexdigest())
        self.assertIn("timestamp", qs)
        self.assertIn("recvWindow", qs)

    def test_usdt_preferred_over_vst(self):
        c = mk([{"code": 0, "data": [{"asset": "VST", "balance": "5"}, {"asset": "USDT", "balance": "7"}]}])
        self.assertEqual(c.balance()["balance"], 7.0)


class TransportTests(unittest.TestCase):
    def test_get_retries_connection_error(self):
        c = mk([requests.ConnectionError("x"), {"code": 0, "data": {"dualSidePosition": "true"}}])
        self.assertTrue(c.dual_side())
        self.assertEqual(len(c.s.calls), 2)

    def test_post_timeout_not_retried(self):
        c = mk([requests.Timeout("t"), {"code": 0, "data": {"order": {}}}])
        with self.assertRaises(requests.Timeout):
            c.place_order({"symbol": "BTC-USDT", "type": "MARKET"})
        self.assertEqual(len(c.s.calls), 1)

    def test_business_error_raises(self):
        c = mk([{"code": 101204, "msg": "Insufficient margin"}])
        with self.assertRaises(B.BingXError) as cm:
            c.place_order({"symbol": "BTC-USDT"})
        self.assertEqual(cm.exception.code, 101204)

    def test_timestamp_error_resyncs_and_retries(self):
        c = mk([{"code": 100421, "msg": "timestamp is invalid"},
                {"code": 0, "data": {"serverTime": 1_800_000_000_000}},
                {"code": 0, "data": {"order": {"orderId": 7}}}])
        o = c.place_order({"symbol": "BTC-USDT"})
        self.assertEqual(o["orderId"], 7)
        self.assertTrue(c.s.calls[1][1].endswith("/openApi/swap/v2/server/time"))
        self.assertNotEqual(c.offset_ms, 0)

    def test_positions_parsing(self):
        c = mk([{"code": 0, "data": [
            {"symbol": "BTC-USDT", "positionAmt": "0.01", "positionSide": "SHORT", "avgPrice": "60000"},
            {"symbol": "ETH-USDT", "positionAmt": "-0.5", "positionSide": "BOTH", "avgPrice": "2000"},
            {"symbol": "SOL-USDT", "positionAmt": "0", "positionSide": "LONG", "avgPrice": "0"}]}])
        ps = {p["symbol"]: p for p in c.positions()}
        self.assertEqual(ps["BTC-USDT"]["side"], -1)
        self.assertEqual(ps["ETH-USDT"]["side"], -1)
        self.assertEqual(ps["ETH-USDT"]["qty"], 0.5)
        self.assertNotIn("SOL-USDT", ps)

    def test_no_keys(self):
        c = B.BingX("", "", base=B.DEMO_BASE, session=FakeSession([]))
        with self.assertRaises(B.BingXError):
            c.balance()


if __name__ == "__main__":
    unittest.main()
