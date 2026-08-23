from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

from .events import XianyuEvent


TERMINAL_ORDER_STATUSES = {"FINISHED", "CLOSED", "REFUNDED"}
ORDER_STATUSES = {
    "WAIT_PAYMENT", "PAID", "SHIPPED", "FINISHED", "CLOSED", "REFUNDED",
}
EVENT_ORDER_STATUSES = {
    "ORDER_CREATED": "WAIT_PAYMENT",
    "ORDER_PAID": "PAID",
    "ORDER_CLOSED": "CLOSED",
    "ORDER_REFUNDED": "REFUNDED",
}


class EventOutbox:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS event_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    create_time INTEGER NOT NULL,
                    delivered_time INTEGER
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbox_due ON event_outbox(status, next_retry_at, id)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS order_sync_state (
                    account_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    last_status TEXT NOT NULL,
                    state_version INTEGER NOT NULL DEFAULT 1,
                    last_seen_at INTEGER NOT NULL,
                    terminal_at INTEGER,
                    context_ready INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (account_id, order_id)
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(order_sync_state)").fetchall()
            }
            if "context_ready" not in columns:
                connection.execute(
                    "ALTER TABLE order_sync_state ADD COLUMN context_ready INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_context (
                    account_id TEXT NOT NULL,
                    buyer_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    PRIMARY KEY (account_id, buyer_id, item_id)
                )
                """
            )
            self._backfill_order_state(connection)
            self._backfill_conversation_context(connection)
            connection.commit()

    @staticmethod
    def _backfill_order_state(connection: sqlite3.Connection) -> None:
        """用已有 Outbox 建立水位，升级后不重放历史订单事件。"""
        if connection.execute("SELECT 1 FROM order_sync_state LIMIT 1").fetchone():
            return
        rows = connection.execute(
            "SELECT payload, create_time FROM event_outbox ORDER BY id"
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            account_id = payload.get("account_id")
            order_id = payload.get("order_id")
            status = EventOutbox._event_order_status(
                payload.get("event_type"), payload.get("content_type"), payload.get("content")
            )
            if not account_id or not order_id or not status:
                continue
            EventOutbox._observe_order_state(
                connection,
                str(account_id),
                str(order_id),
                status,
                EventOutbox._event_time(payload.get("occurred_at"), int(row["create_time"])),
                EventOutbox._has_context(payload),
            )

    @staticmethod
    def _backfill_conversation_context(connection: sqlite3.Connection) -> None:
        """从历史事件恢复买家、商品与会话关系，并标记已有完整订单上下文。"""
        if connection.execute("SELECT 1 FROM conversation_context LIMIT 1").fetchone():
            return
        rows = connection.execute(
            "SELECT payload, create_time FROM event_outbox ORDER BY id"
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if not EventOutbox._has_context(payload):
                continue
            account_id = str(payload["account_id"])
            buyer_id = str(payload["buyer_id"])
            item_id = str(payload["item_id"])
            chat_id = str(payload["chat_id"])
            seen_at = EventOutbox._event_time(
                payload.get("occurred_at"), int(row["create_time"])
            )
            EventOutbox._observe_conversation_context(
                connection, account_id, buyer_id, item_id, chat_id, seen_at
            )
            order_id = payload.get("order_id")
            if order_id:
                connection.execute(
                    """
                    UPDATE order_sync_state SET context_ready=1
                    WHERE account_id=? AND order_id=?
                    """,
                    (account_id, str(order_id)),
                )

    def enqueue(self, event: XianyuEvent) -> bool:
        payload = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO event_outbox(event_id, payload, create_time)
                VALUES (?, ?, ?)
                """,
                (event.event_id, payload, int(time.time())),
            )
            context_ready = self._has_context(event)
            if context_ready:
                self._observe_conversation_context(
                    connection,
                    str(event.account_id),
                    str(event.buyer_id),
                    str(event.item_id),
                    str(event.chat_id),
                    self._event_time(event.occurred_at, int(time.time())),
                )
            status = self._event_order_status(event.event_type, event.content_type, event.content)
            if event.order_id and status:
                self._observe_order_state(
                    connection,
                    str(event.account_id),
                    str(event.order_id),
                    status,
                    self._event_time(event.occurred_at, int(time.time())),
                    context_ready,
                )
            connection.commit()
            return cursor.rowcount == 1

    @staticmethod
    def _event_order_status(event_type: Any, content_type: Any, content: Any) -> str | None:
        status = EVENT_ORDER_STATUSES.get(str(event_type))
        if status:
            return status
        content_status = str(content) if content is not None else None
        if content_type == "order_status" and content_status in ORDER_STATUSES:
            return content_status
        return None

    @staticmethod
    def _event_time(occurred_at: Any, fallback: int) -> int:
        try:
            value = int(occurred_at)
        except (TypeError, ValueError):
            return fallback
        if value <= 0:
            return fallback
        return value // 1000 if value > 10_000_000_000 else value

    @staticmethod
    def _has_context(event: Any) -> bool:
        if isinstance(event, dict):
            values = (event.get("buyer_id"), event.get("item_id"), event.get("chat_id"))
        else:
            values = (event.buyer_id, event.item_id, event.chat_id)
        return all(value not in (None, "") for value in values)

    @staticmethod
    def _observe_conversation_context(
        connection: sqlite3.Connection,
        account_id: str,
        buyer_id: str,
        item_id: str,
        chat_id: str,
        seen_at: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO conversation_context(account_id, buyer_id, item_id, chat_id, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(account_id, buyer_id, item_id) DO UPDATE SET
                chat_id=excluded.chat_id,
                last_seen_at=excluded.last_seen_at
            WHERE excluded.last_seen_at >= conversation_context.last_seen_at
            """,
            (account_id, buyer_id, item_id, chat_id, seen_at),
        )

    @staticmethod
    def _observe_order_state(
        connection: sqlite3.Connection,
        account_id: str,
        order_id: str,
        status: str,
        seen_at: int,
        context_ready: bool,
    ) -> None:
        row = connection.execute(
            """
            SELECT last_status, state_version, last_seen_at, context_ready
            FROM order_sync_state WHERE account_id=? AND order_id=?
            """,
            (account_id, order_id),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO order_sync_state(
                    account_id, order_id, last_status, state_version, last_seen_at, terminal_at,
                    context_ready
                ) VALUES (?, ?, ?, 1, ?, ?, ?)
                """,
                (account_id, order_id, status, seen_at,
                 seen_at if status in TERMINAL_ORDER_STATUSES else None, int(context_ready)),
            )
            return
        if row["last_status"] == status:
            connection.execute(
                """
                UPDATE order_sync_state
                SET last_seen_at=MAX(last_seen_at, ?), context_ready=MAX(context_ready, ?)
                WHERE account_id=? AND order_id=?
                """,
                (seen_at, int(context_ready), account_id, order_id),
            )
            return
        if seen_at < int(row["last_seen_at"]):
            return
        connection.execute(
            """
            UPDATE order_sync_state
            SET last_status=?, state_version=?, last_seen_at=?, terminal_at=?,
                context_ready=MAX(context_ready, ?)
            WHERE account_id=? AND order_id=?
            """,
            (
                status,
                int(row["state_version"]) + 1,
                seen_at,
                seen_at if status in TERMINAL_ORDER_STATUSES else None,
                int(context_ready),
                account_id,
                order_id,
            ),
        )

    def sync_order(self, event: XianyuEvent) -> str:
        """原子比较订单状态并入队，返回 ENQUEUED/BASELINED/UNCHANGED。"""
        if not event.order_id or not event.content:
            raise ValueError("订单轮询事件缺少 order_id 或 content")
        account_id = str(event.account_id)
        order_id = str(event.order_id)
        status = str(event.content)
        now = int(time.time())
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            context_ready = self._has_context(event)
            if context_ready:
                self._observe_conversation_context(
                    connection,
                    account_id,
                    str(event.buyer_id),
                    str(event.item_id),
                    str(event.chat_id),
                    self._event_time(event.occurred_at, now),
                )
            row = connection.execute(
                """
                SELECT last_status, state_version, context_ready
                FROM order_sync_state WHERE account_id=? AND order_id=?
                """,
                (account_id, order_id),
            ).fetchone()
            if row is not None and row["last_status"] == status:
                if status == "WAIT_PAYMENT" and not row["context_ready"] and context_ready:
                    versioned_event = replace(
                        event,
                        event_id=f"{account_id}:order:{order_id}:v{row['state_version']}:{status}:CONTEXT",
                    )
                    payload = json.dumps(
                        versioned_event.to_dict(), ensure_ascii=False, separators=(",", ":")
                    )
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO event_outbox(event_id, payload, create_time)
                        VALUES (?, ?, ?)
                        """,
                        (versioned_event.event_id, payload, now),
                    )
                    connection.execute(
                        """
                        UPDATE order_sync_state
                        SET last_seen_at=MAX(last_seen_at, ?), context_ready=1
                        WHERE account_id=? AND order_id=?
                        """,
                        (now, account_id, order_id),
                    )
                    connection.commit()
                    return "ENQUEUED" if cursor.rowcount == 1 else "UNCHANGED"
                connection.execute(
                    """
                    UPDATE order_sync_state SET last_seen_at=MAX(last_seen_at, ?)
                    WHERE account_id=? AND order_id=?
                    """,
                    (now, account_id, order_id),
                )
                connection.commit()
                return "UNCHANGED"

            version = 1 if row is None else int(row["state_version"]) + 1
            terminal_at = now if status in TERMINAL_ORDER_STATUSES else None
            connection.execute(
                """
                INSERT INTO order_sync_state(
                    account_id, order_id, last_status, state_version, last_seen_at, terminal_at,
                    context_ready
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, order_id) DO UPDATE SET
                    last_status=excluded.last_status,
                    state_version=excluded.state_version,
                    last_seen_at=excluded.last_seen_at,
                    terminal_at=excluded.terminal_at,
                    context_ready=MAX(order_sync_state.context_ready, excluded.context_ready)
                """,
                (account_id, order_id, status, version, now, terminal_at, int(context_ready)),
            )

            # 首次看到的历史终态只建立基线；活跃订单及后续变化才产生事件。
            if row is None and status in TERMINAL_ORDER_STATUSES:
                connection.commit()
                return "BASELINED"

            versioned_event = replace(
                event,
                event_id=f"{account_id}:order:{order_id}:v{version}:{status}",
            )
            payload = json.dumps(
                versioned_event.to_dict(), ensure_ascii=False, separators=(",", ":")
            )
            connection.execute(
                """
                INSERT INTO event_outbox(event_id, payload, create_time)
                VALUES (?, ?, ?)
                """,
                (versioned_event.event_id, payload, now),
            )
            connection.commit()
            return "ENQUEUED"

    def order_state(self, account_id: str, order_id: str) -> dict[str, Any] | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM order_sync_state WHERE account_id=? AND order_id=?",
                (str(account_id), str(order_id)),
            ).fetchone()
        return dict(row) if row else None

    def conversation_chat_id(self, account_id: str, buyer_id: str, item_id: str) -> str | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT chat_id FROM conversation_context
                WHERE account_id=? AND buyer_id=? AND item_id=?
                """,
                (str(account_id), str(buyer_id), str(item_id)),
            ).fetchone()
        return str(row["chat_id"]) if row else None

    def due(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, event_id, payload, attempts
                FROM event_outbox
                WHERE status = 'PENDING' AND next_retry_at <= ?
                ORDER BY id LIMIT ?
                """,
                (int(time.time()), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def delivered(self, row_id: int) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "UPDATE event_outbox SET status='DELIVERED', delivered_time=?, last_error=NULL WHERE id=?",
                (int(time.time()), row_id),
            )
            connection.commit()

    def failed(self, row_id: int, attempts: int, error: str) -> None:
        delay = min(300, 2 ** min(attempts, 8))
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE event_outbox
                SET attempts=?, next_retry_at=?, last_error=?
                WHERE id=? AND status='PENDING'
                """,
                (attempts, int(time.time()) + delay, error[:1000], row_id),
            )
            connection.commit()

    def stats(self) -> dict[str, int]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) count FROM event_outbox GROUP BY status"
            ).fetchall()
        result = {"PENDING": 0, "DELIVERED": 0}
        result.update({str(row["status"]): int(row["count"]) for row in rows})
        return result
