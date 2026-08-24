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
        self.assertTrue(response.json()["capabilities"]["createChat"])
        self.assertNotIn("cancel", response.json()["capabilities"])
        self.assertNotIn("changePrice", response.json()["capabilities"])

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

    def test_legacy_order_and_item_routes_are_removed(self):
        for method, path in (
            ("GET", "/internal/accounts/a1/orders"),
            ("GET", "/internal/accounts/a1/items"),
            ("GET", "/internal/accounts/a1/orders/o1"),
            ("POST", "/internal/accounts/a1/orders/o1/price"),
            ("POST", "/internal/accounts/a1/orders/o1/deliver"),
            ("POST", "/internal/accounts/a1/orders/o1/cancel"),
            ("POST", "/internal/accounts/a1/items/i1/offline"),
            ("POST", "/internal/accounts/a1/orders/o1/refund"),
        ):
            self.assertEqual(404, self.request(method, path, {}).status_code)


if __name__ == "__main__":
    unittest.main()
