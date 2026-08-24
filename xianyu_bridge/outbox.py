from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .events import XianyuEvent


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
            self._backfill_conversation_context(connection)
            connection.commit()

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
            connection.commit()
            return cursor.rowcount == 1

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
