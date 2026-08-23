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

    def test_order_sync_baselines_terminal_and_enqueues_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = EventOutbox(Path(directory) / "events.db")
            terminal = XianyuEvent(
                "old", "ORDER_UPDATED", "a1", 1,
                order_id="o1", content_type="order_status", content="FINISHED",
            )
            self.assertEqual("BASELINED", outbox.sync_order(terminal))
            self.assertEqual("UNCHANGED", outbox.sync_order(terminal))
            self.assertEqual([], outbox.due())

            changed = XianyuEvent(
                "new", "ORDER_REFUNDED", "a1", 2,
                order_id="o1", content_type="order_status", content="REFUNDED",
            )
            self.assertEqual("ENQUEUED", outbox.sync_order(changed))
            rows = outbox.due()
            self.assertEqual(1, len(rows))
            self.assertEqual("a1:order:o1:v2:REFUNDED", rows[0]["event_id"])
            self.assertEqual("REFUNDED", outbox.order_state("a1", "o1")["last_status"])

    def test_order_sync_enqueues_first_active_order(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = EventOutbox(Path(directory) / "events.db")
            event = XianyuEvent(
                "poll", "ORDER_PAID", "a1", 1,
                order_id="o2", content_type="order_status", content="PAID",
            )
            self.assertEqual("ENQUEUED", outbox.sync_order(event))
            self.assertEqual("a1:order:o2:v1:PAID", outbox.due()[0]["event_id"])

    def test_websocket_order_created_prevents_duplicate_poll_event(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = EventOutbox(Path(directory) / "events.db")
            websocket = XianyuEvent(
                "message-1", "ORDER_CREATED", "a1", 2_000_000_000_000,
                chat_id="chat-1", buyer_id="buyer-1", item_id="item-1", order_id="o1",
                content_type="image", content="https://example.com/order-card.zip",
            )
            polled = XianyuEvent(
                "poll-1", "ORDER_CREATED", "a1", 2_000_000_001_000,
                item_id="item-1", order_id="o1",
                content_type="order_status", content="WAIT_PAYMENT",
            )

            self.assertTrue(outbox.enqueue(websocket))
            self.assertEqual("UNCHANGED", outbox.sync_order(polled))
            self.assertEqual(["message-1"], [row["event_id"] for row in outbox.due()])
            self.assertEqual("WAIT_PAYMENT", outbox.order_state("a1", "o1")["last_status"])

    def test_same_chat_tracks_each_order_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = EventOutbox(Path(directory) / "events.db")
            for order_id in ("o1", "o2"):
                outbox.enqueue(XianyuEvent(
                    f"message-{order_id}", "ORDER_CREATED", "a1", 2_000_000_000_000,
                    chat_id="chat-1", buyer_id="buyer-1", item_id="item-1",
                    order_id=order_id,
                ))

            self.assertEqual("WAIT_PAYMENT", outbox.order_state("a1", "o1")["last_status"])
            self.assertEqual("WAIT_PAYMENT", outbox.order_state("a1", "o2")["last_status"])

    def test_incomplete_polled_order_is_replayed_once_after_context_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = EventOutbox(Path(directory) / "events.db")
            incomplete = XianyuEvent(
                "poll", "ORDER_CREATED", "a1", 2_000_000_000_000,
                item_id="item-1", order_id="o1",
                content_type="order_status", content="WAIT_PAYMENT",
            )
            enriched = XianyuEvent(
                "poll", "ORDER_CREATED", "a1", 2_000_000_001_000,
                buyer_id="buyer-1", item_id="item-1", chat_id="chat-1", order_id="o1",
                content_type="order_status", content="WAIT_PAYMENT",
            )

            self.assertEqual("ENQUEUED", outbox.sync_order(incomplete))
            self.assertEqual("ENQUEUED", outbox.sync_order(enriched))
            self.assertEqual("UNCHANGED", outbox.sync_order(enriched))
            event_ids = [row["event_id"] for row in outbox.due()]
            self.assertEqual(
                [
                    "a1:order:o1:v1:WAIT_PAYMENT",
                    "a1:order:o1:v1:WAIT_PAYMENT:CONTEXT",
                ],
                event_ids,
            )
            self.assertEqual(1, outbox.order_state("a1", "o1")["context_ready"])

    def test_conversation_context_is_backfilled_from_history(self):
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

    def test_websocket_status_change_prevents_duplicate_paid_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = EventOutbox(Path(directory) / "events.db")
            outbox.enqueue(XianyuEvent(
                "created", "ORDER_CREATED", "a1", 2_000_000_000_000, order_id="o1",
            ))
            outbox.enqueue(XianyuEvent(
                "paid", "ORDER_PAID", "a1", 2_000_000_010_000, order_id="o1",
            ))
            polled = XianyuEvent(
                "poll", "ORDER_PAID", "a1", 2_000_000_011_000,
                order_id="o1", content_type="order_status", content="PAID",
            )

            self.assertEqual("UNCHANGED", outbox.sync_order(polled))
            state = outbox.order_state("a1", "o1")
            self.assertEqual("PAID", state["last_status"])
            self.assertEqual(2, state["state_version"])

    def test_delayed_websocket_event_does_not_regress_order_state(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = EventOutbox(Path(directory) / "events.db")
            outbox.enqueue(XianyuEvent(
                "paid", "ORDER_PAID", "a1", 2_000_000_010_000, order_id="o1",
            ))
            outbox.enqueue(XianyuEvent(
                "created", "ORDER_CREATED", "a1", 2_000_000_000_000, order_id="o1",
            ))

            state = outbox.order_state("a1", "o1")
            self.assertEqual("PAID", state["last_status"])
            self.assertEqual(1, state["state_version"])

    def test_non_order_status_content_does_not_pollute_order_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            outbox = EventOutbox(path)
            outbox.enqueue(XianyuEvent(
                "updated", "ORDER_UPDATED", "a1", 1,
                order_id="o1", content_type="image", content="https://example.com/card.zip",
            ))

            self.assertIsNone(outbox.order_state("a1", "o1"))
            self.assertIsNone(EventOutbox(path).order_state("a1", "o1"))

    def test_existing_order_card_backfills_normalized_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            outbox = EventOutbox(path)
            event = XianyuEvent(
                "message-1", "ORDER_CREATED", "a1", 2_000_000_000_000,
                chat_id="chat-1", buyer_id="buyer-1", item_id="item-1", order_id="o1",
                content_type="image", content="https://example.com/order-card.zip",
            )
            outbox.enqueue(event)
            with sqlite3.connect(path) as connection:
                connection.execute("DELETE FROM order_sync_state")
                connection.commit()

            reopened = EventOutbox(path)
            self.assertEqual("WAIT_PAYMENT", reopened.order_state("a1", "o1")["last_status"])

    def test_existing_outbox_backfills_order_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            outbox = EventOutbox(path)
            event = XianyuEvent(
                "a1:order:o3:PAID", "ORDER_PAID", "a1", 1,
                order_id="o3", content_type="order_status", content="PAID",
            )
            outbox.enqueue(event)
            with sqlite3.connect(path) as connection:
                connection.execute("DELETE FROM order_sync_state")
                connection.commit()

            reopened = EventOutbox(path)
            self.assertEqual("PAID", reopened.order_state("a1", "o3")["last_status"])
            self.assertEqual("UNCHANGED", reopened.sync_order(event))
            self.assertEqual(1, len(reopened.due()))


if __name__ == "__main__":
    unittest.main()
