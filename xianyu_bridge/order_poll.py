from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from .events import XianyuEvent
from .manager import XianyuConnectionManager
from .platform import summarize_order_status
from .parser import XianyuMessageParser


_ORDER_KEYS = ("orderId", "bizOrderId", "bizOrderIdStr", "order_id", "tid")
_ITEM_KEYS = ("itemId", "item_id")
_BUYER_KEYS = ("buyerId", "buyerUserId", "buyer_id", "peerUserId")
_CHAT_KEYS = ("chatId", "conversationId", "cid")


def orders_from_response(account_id: str, response: dict[str, Any]) -> list[XianyuEvent]:
    """从卖家订单列表的易变结构中提取可去重的订单状态事件。"""
    events: list[XianyuEvent] = []
    seen: set[str] = set()
    for candidate in _dicts(response.get("data", response)):
        order_id = _first(candidate, _ORDER_KEYS)
        if not order_id or order_id in seen:
            continue
        summary = summarize_order_status(candidate)
        status = summary["status"]
        if status == "UNKNOWN":
            continue
        seen.add(order_id)
        event_type = {
            "WAIT_PAYMENT": "ORDER_CREATED",
            "PAID": "ORDER_PAID",
            "REFUNDED": "ORDER_REFUNDED",
            "CLOSED": "ORDER_CLOSED",
        }.get(status, "ORDER_UPDATED")
        events.append(
            XianyuEvent(
                event_id=f"{account_id}:order:{order_id}:{status}",
                event_type=event_type,
                account_id=str(account_id),
                occurred_at=int(time.time() * 1000),
                chat_id=_first(candidate, _CHAT_KEYS),
                buyer_id=_first(candidate, _BUYER_KEYS),
                item_id=_first(candidate, _ITEM_KEYS),
                order_id=order_id,
                paid_amount=XianyuMessageParser.find_paid_amount(candidate),
                content_type="order_status",
                content=status,
                raw_payload=candidate,
            )
        )
    return events


def order_context_from_detail(response: dict[str, Any]) -> tuple[str | None, str | None]:
    """从卖家订单详情提取买家和商品；卖家视角的 peerUserId 即买家 ID。"""
    buyer_id = _first_nested(response.get("data", response), _BUYER_KEYS)
    item_id = _first_nested(response.get("data", response), _ITEM_KEYS)
    return buyer_id, item_id


class OrderPoller:
    def __init__(
        self,
        manager: XianyuConnectionManager,
        event_handler: Callable[[XianyuEvent], Awaitable[str]],
        interval: float = 60,
    ):
        self.manager = manager
        self.event_handler = event_handler
        self.interval = max(interval, 5)
        self._stop = asyncio.Event()

    async def run(self) -> None:
        while not self._stop.is_set():
            for account_id in list(self.manager.instances):
                started_at = time.monotonic()
                try:
                    response = await asyncio.to_thread(
                        self.manager.platform(account_id).list_sold_orders, 1, 50, "ALL"
                    )
                    events = orders_from_response(account_id, response)
                    counts = {"ENQUEUED": 0, "BASELINED": 0, "UNCHANGED": 0}
                    for event in events:
                        result = await self.event_handler(event)
                        counts[result] = counts.get(result, 0) + 1
                    logger.info(
                        "[闲鱼订单补偿完成] 账号：{}，扫描：{}，变化：{}，基线：{}，未变：{}，耗时：{:.0f}ms",
                        account_id,
                        len(events),
                        counts["ENQUEUED"],
                        counts["BASELINED"],
                        counts["UNCHANGED"],
                        (time.monotonic() - started_at) * 1000,
                    )
                except Exception as exc:
                    logger.warning("[闲鱼订单轮询失败] 账号：{}，原因：{}", account_id, exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    def reset(self) -> None:
        if self._stop.is_set():
            self._stop = asyncio.Event()


def _dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _dicts(item)


def _first(value: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        item = value.get(key)
        if item not in (None, ""):
            return str(item)
    return None


def _first_nested(value: Any, keys: tuple[str, ...]) -> str | None:
    for candidate in _dicts(value):
        result = _first(candidate, keys)
        if result:
            return result
    return None
