import json
import os
import secrets
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from xianyu_bridge.api import _message_uuid, app
from xianyu_bridge.accounts import AccountStore
from xianyu_bridge.runtime import runtime
from xianyu_bridge.signature import sign


class SignatureApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["XIANYU_INTERNAL_SECRET"] = "test-secret"
        cls.accounts_temp = tempfile.TemporaryDirectory()
        cls.original_store = runtime.account_store
        runtime.account_store = AccountStore(cls.accounts_temp.name)
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        runtime.account_store = cls.original_store
        cls.accounts_temp.cleanup()

    def request(self, method: str, path: str, payload=None, nonce=None):
        body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        nonce = nonce or secrets.token_hex(8)
        headers = {
            "Content-Type": "application/json",
            "X-Xianyu-Timestamp": timestamp,
            "X-Xianyu-Nonce": nonce,
            "X-Xianyu-Signature": sign("test-secret", timestamp, nonce, method, path, body),
        }
        return self.client.request(method, path, content=body, headers=headers)

    def test_health_does_not_require_signature(self):
        response = self.client.get("/health")
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["success"])
        self.assertTrue(response.json()["capabilities"]["cancel"])
        self.assertFalse(response.json()["capabilities"]["changePrice"])

    def test_message_idempotency_key_is_stable_and_account_scoped(self):
        first = _message_uuid("a1", "meal-link:1")
        self.assertEqual(first, _message_uuid("a1", "meal-link:1"))
        self.assertNotEqual(first, _message_uuid("a2", "meal-link:1"))

    def test_protected_endpoint_accepts_valid_signature_and_rejects_replay(self):
        nonce = secrets.token_hex(8)
        first = self.request("GET", "/internal/accounts/not-running/status", nonce=nonce)
        second = self.request("GET", "/internal/accounts/not-running/status", nonce=nonce)
        self.assertEqual(200, first.status_code)
        self.assertEqual(409, second.status_code)

    def test_invalid_signature_does_not_consume_nonce(self):
        nonce = secrets.token_hex(8)
        path = "/internal/accounts/not-running/status"
        timestamp = str(int(time.time()))
        bad = self.client.get(path, headers={
            "X-Xianyu-Timestamp": timestamp,
            "X-Xianyu-Nonce": nonce,
            "X-Xianyu-Signature": "bad",
        })
        good = self.request("GET", path, nonce=nonce)
        self.assertEqual(401, bad.status_code)
        self.assertEqual(200, good.status_code)

    def test_change_price_is_explicitly_unsupported(self):
        class FakePlatform:
            def change_price(self, order_id, amount):
                from xianyu_bridge.platform import UnsupportedXianyuOperation
                raise UnsupportedXianyuOperation("订单改价", {"ret": ["UNSUPPORTED"]})

        with patch("xianyu_bridge.api.runtime.manager.platform", return_value=FakePlatform()):
            response = self.request(
                "POST", "/internal/accounts/a1/orders/o1/price", {"amount": "10.00"}
            )
        self.assertEqual(501, response.status_code)

    def test_items_are_normalized_for_selection(self):
        class FakePlatform:
            def list_items(self, page, size):
                return {"data": {"data": {"total": 2, "itemSearchResponseList": [
                    {"itemId": "1001", "title": "在卖商品", "itemStatus": "0"},
                    {"itemId": "1002", "title": "下架商品", "itemStatus": "-2"},
                ]}}}

        with patch("xianyu_bridge.api.runtime.manager.platform", return_value=FakePlatform()):
            response = self.request("GET", "/internal/accounts/a1/items")

        self.assertEqual(200, response.status_code)
        self.assertEqual("在卖", response.json()["data"]["list"][0]["statusName"])
        self.assertEqual("下架", response.json()["data"]["list"][1]["statusName"])

    def test_deliver_checks_order_state_and_is_idempotent(self):
        class FakePlatform:
            delivered = 0

            def __init__(self, status):
                self.status = status

            def order_detail(self, order_id):
                return {"data": {"status": self.status}}

            def confirm_delivery(self, order_id):
                self.delivered += 1
                return {"ret": ["SUCCESS"]}

        unpaid = FakePlatform("WAIT_BUYER_PAY")
        with patch("xianyu_bridge.api.runtime.manager.platform", return_value=unpaid):
            response = self.request("POST", "/internal/accounts/a1/orders/o1/deliver", {})
        self.assertEqual(409, response.status_code)
        self.assertEqual(0, unpaid.delivered)

        shipped = FakePlatform("SHIPPED")
        with patch("xianyu_bridge.api.runtime.manager.platform", return_value=shipped):
            response = self.request("POST", "/internal/accounts/a1/orders/o1/deliver", {})
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["data"]["confirm"]["alreadyDelivered"])
        self.assertEqual(0, shipped.delivered)

        paid = FakePlatform("WAIT_SELLER_SEND_GOODS")
        with patch("xianyu_bridge.api.runtime.manager.platform", return_value=paid):
            response = self.request("POST", "/internal/accounts/a1/orders/o1/deliver", {})
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, paid.delivered)

    def test_cancel_only_allows_waiting_payment_and_is_idempotent(self):
        class FakePlatform:
            cancelled = 0

            def __init__(self, status):
                self.status = status

            def order_detail(self, order_id):
                return {"data": {"status": self.status}}

            def cancel_order(self, order_id):
                self.cancelled += 1
                return {"ret": ["SUCCESS"]}

        unpaid = FakePlatform("WAIT_BUYER_PAY")
        with patch("xianyu_bridge.api.runtime.manager.platform", return_value=unpaid):
            response = self.request("POST", "/internal/accounts/a1/orders/o1/cancel", {})
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, unpaid.cancelled)

        closed = FakePlatform("TRADE_CLOSED")
        with patch("xianyu_bridge.api.runtime.manager.platform", return_value=closed):
            response = self.request("POST", "/internal/accounts/a1/orders/o1/cancel", {})
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["data"]["alreadyCancelled"])
        self.assertEqual(0, closed.cancelled)

        paid = FakePlatform("WAIT_SELLER_SEND_GOODS")
        with patch("xianyu_bridge.api.runtime.manager.platform", return_value=paid):
            response = self.request("POST", "/internal/accounts/a1/orders/o1/cancel", {})
        self.assertEqual(409, response.status_code)
        self.assertEqual(0, paid.cancelled)


if __name__ == "__main__":
    unittest.main()
