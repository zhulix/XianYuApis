import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xianyu_bridge.accounts import AccountStore
from xianyu_bridge.events import XianyuEvent
from xianyu_bridge.runtime import BridgeRuntime


def cookies(account_id: str, token: str = "one") -> str:
    return f"unb={account_id}; _m_h5_tk={token}_123; cookie2=value"


class AccountStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AccountStore(Path(self.temp.name) / "accounts")

    def tearDown(self):
        self.temp.cleanup()

    def test_saves_multiple_accounts_with_private_permissions(self):
        self.store.save(cookies("1001"))
        self.store.save(cookies("1002"))

        self.assertEqual(["1001", "1002"], self.store.list_ids())
        self.assertEqual(0o700, self.store.root.stat().st_mode & 0o777)
        self.assertEqual(0o600, self.store.path("1001").stat().st_mode & 0o777)

    def test_refreshes_one_account_without_touching_another(self):
        self.store.save(cookies("1001", "old"))
        self.store.save(cookies("1002", "keep"))
        self.store.save(cookies("1001", "new"))

        accounts = self.store.load_all()
        self.assertIn("new_123", accounts["1001"])
        self.assertIn("keep_123", accounts["1002"])

    def test_legacy_import_does_not_overwrite_new_cookie(self):
        legacy = Path(self.temp.name) / ".cookies_str"
        legacy.write_text(cookies("1001", "legacy"), encoding="utf-8")
        self.store.save(cookies("1001", "new"))

        self.assertIsNone(self.store.import_legacy(legacy))
        self.assertIn("new_123", self.store.load_all()["1001"])

    def test_rejects_filename_cookie_account_mismatch(self):
        self.store.root.mkdir(parents=True)
        self.store.path("1001").write_text(cookies("1002"), encoding="utf-8")

        self.assertEqual({}, self.store.load_all())


class RuntimeAccountSyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_add_refresh_and_remove_accounts(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with patch.dict(
            os.environ,
            {
                "XIANYU_ACCOUNTS_DIR": str(Path(temp.name) / "accounts"),
                "XIANYU_OUTBOX_PATH": str(Path(temp.name) / "events.db"),
            },
        ):
            bridge = BridgeRuntime()

        class FakeManager:
            def __init__(self):
                self.instances = {}
                self.started = []
                self.stopped = []

            async def start(self, account_id, value):
                self.instances[account_id] = object()
                self.started.append((account_id, value))

            async def stop(self, account_id):
                self.instances.pop(account_id, None)
                self.stopped.append(account_id)

        manager = FakeManager()
        bridge.manager = manager
        bridge.account_store.save(cookies("1001", "old"))
        bridge.account_store.save(cookies("1002", "keep"))
        await bridge.sync_accounts()
        self.assertEqual(2, len(manager.started))

        bridge.account_store.save(cookies("1001", "new"))
        bridge.account_store.remove("1002")
        await bridge.sync_accounts()
        self.assertEqual("1001", manager.started[-1][0])
        self.assertIn("new_123", manager.started[-1][1])
        self.assertEqual(["1002"], manager.stopped)

    async def test_polled_order_recovers_context_from_detail_and_history(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with patch.dict(
            os.environ,
            {
                "XIANYU_ACCOUNTS_DIR": str(Path(temp.name) / "accounts"),
                "XIANYU_OUTBOX_PATH": str(Path(temp.name) / "events.db"),
            },
        ):
            bridge = BridgeRuntime()
        bridge.outbox.enqueue(XianyuEvent(
            "history", "CHAT_MESSAGE", "a1", 2_000_000_000_000,
            buyer_id="buyer-1", item_id="item-1", chat_id="chat-1",
        ))

        class FakePlatform:
            @staticmethod
            def order_detail(_order_id):
                return {"data": {"peerUserId": "buyer-1", "itemId": "item-1"}}

        class FakeManager:
            @staticmethod
            def platform(_account_id):
                return FakePlatform()

        bridge.manager = FakeManager()
        event = XianyuEvent(
            "poll", "ORDER_CREATED", "a1", 2_000_000_001_000,
            item_id="item-1", order_id="o1",
            content_type="order_status", content="WAIT_PAYMENT",
        )

        self.assertEqual("ENQUEUED", await bridge.handle_polled_order(event))
        payload = json.loads(bridge.outbox.due()[-1]["payload"])
        self.assertEqual("buyer-1", payload["buyer_id"])
        self.assertEqual("chat-1", payload["chat_id"])


if __name__ == "__main__":
    unittest.main()
