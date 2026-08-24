import sqlite3
import tempfile
import unittest
from pathlib import Path

from xianyu_bridge.events import XianyuEvent
from xianyu_bridge.outbox import EventOutbox


class EventOutboxTest(unittest.TestCase):
    def test_enqueue_is_idempotent_and_tracks_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = EventOutbox(Path(directory) / "events.db")
            event = XianyuEvent(
                event_id="seller:message-1",
                event_type="CHAT_MESSAGE",
                account_id="seller",
                occurred_at=1,
            )
            self.assertTrue(outbox.enqueue(event))
            self.assertFalse(outbox.enqueue(event))
            rows = outbox.due()
            self.assertEqual(1, len(rows))
            outbox.delivered(rows[0]["id"])
            self.assertEqual({"PENDING": 0, "DELIVERED": 1}, outbox.stats())

    def test_failure_is_scheduled_not_lost(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = EventOutbox(Path(directory) / "events.db")
            outbox.enqueue(XianyuEvent("e1", "CHAT_MESSAGE", "a1", 1))
            row = outbox.due()[0]
            outbox.failed(row["id"], 1, "network")
            self.assertEqual([], outbox.due())
            self.assertEqual(1, outbox.stats()["PENDING"])

    def test_chat_context_is_stored_by_account_buyer_and_product(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            outbox = EventOutbox(path)
            outbox.enqueue(XianyuEvent(
                "message-1", "CHAT_MESSAGE", "a1", 2_000_000_000_000,
                buyer_id="buyer-1", item_id="item-1", chat_id="chat-1",
            ))

            self.assertEqual("chat-1", outbox.conversation_chat_id("a1", "buyer-1", "item-1"))
            self.assertEqual(
                "chat-1", EventOutbox(path).conversation_chat_id("a1", "buyer-1", "item-1")
            )

    def test_order_id_in_chat_payload_does_not_create_order_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            outbox = EventOutbox(path)
            outbox.enqueue(XianyuEvent(
                "message-1", "CHAT_MESSAGE", "a1", 1,
                buyer_id="buyer-1", item_id="item-1", chat_id="chat-1", order_id="order-1",
            ))

            with self.assertRaises(Exception):
                # The old state table is intentionally not part of the Sidecar schema.
                with sqlite3.connect(path) as connection:
                    connection.execute("SELECT 1 FROM order_sync_state")


if __name__ == "__main__":
    unittest.main()
