from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path

from loguru import logger

from .accounts import AccountStore
from .callback import JavaEventCallback, OutboxWorker
from .events import XianyuEvent
from .manager import XianyuConnectionManager
from .order_poll import OrderPoller, order_context_from_detail
from .outbox import EventOutbox


ROOT = Path(__file__).resolve().parent.parent


class BridgeRuntime:
    def __init__(self):
        outbox_path = os.getenv("XIANYU_OUTBOX_PATH", str(ROOT / "data" / "events.db"))
        callback_url = os.getenv("LXJ_EVENT_CALLBACK_URL", "").strip()
        callback_secret = os.getenv("LXJ_EVENT_CALLBACK_SECRET", "").strip()
        callback = JavaEventCallback(callback_url, callback_secret) if callback_url and callback_secret else None
        self.outbox = EventOutbox(outbox_path)
        self.account_store = AccountStore(
            os.getenv("XIANYU_ACCOUNTS_DIR", str(ROOT / "data" / "accounts"))
        )
        self.account_sync_interval = max(float(os.getenv("XIANYU_ACCOUNT_SYNC_INTERVAL", "5")), 2)
        self.worker = OutboxWorker(self.outbox, callback)
        self.manager = XianyuConnectionManager(self.handle_event)
        self.poller = OrderPoller(
            self.manager,
            self.handle_polled_order,
            float(os.getenv("XIANYU_ORDER_POLL_INTERVAL", "60")),
        )
        self.worker_task: asyncio.Task | None = None
        self.poller_task: asyncio.Task | None = None
        self.account_task: asyncio.Task | None = None
        self.account_stop = asyncio.Event()
        self.account_fingerprints: dict[str, str] = {}
        self._account_sync_lock = asyncio.Lock()

    async def handle_event(self, event: XianyuEvent) -> None:
        inserted = await asyncio.to_thread(self.outbox.enqueue, event)
        if inserted:
            logger.info("[闲鱼事件入队] 事件ID：{}，类型：{}", event.event_id, event.event_type)
        else:
            logger.debug("[闲鱼事件去重] 事件ID：{}", event.event_id)

    async def handle_polled_order(self, event: XianyuEvent) -> str:
        state = await asyncio.to_thread(
            self.outbox.order_state, event.account_id, str(event.order_id)
        )
        needs_context = (
            event.content == "WAIT_PAYMENT"
            and (state is None or not state.get("context_ready"))
            and (not event.buyer_id or not event.chat_id)
        )
        if needs_context:
            event = await self._enrich_polled_order(event)
        return await asyncio.to_thread(self.outbox.sync_order, event)

    async def _enrich_polled_order(self, event: XianyuEvent) -> XianyuEvent:
        buyer_id = event.buyer_id
        item_id = event.item_id
        try:
            if not buyer_id or not item_id:
                detail = await asyncio.to_thread(
                    self.manager.platform(event.account_id).order_detail, str(event.order_id)
                )
                detail_buyer_id, detail_item_id = order_context_from_detail(detail)
                buyer_id = buyer_id or detail_buyer_id
                item_id = item_id or detail_item_id
            chat_id = event.chat_id
            if not chat_id and buyer_id and item_id:
                chat_id = await asyncio.to_thread(
                    self.outbox.conversation_chat_id, event.account_id, buyer_id, item_id
                )
            enriched = replace(event, buyer_id=buyer_id, item_id=item_id, chat_id=chat_id)
            if buyer_id and item_id and chat_id:
                logger.info(
                    "[闲鱼订单上下文补全成功] 账号：{}，订单：{}，买家：{}，商品：{}，会话：{}",
                    event.account_id, event.order_id, buyer_id, item_id, chat_id,
                )
            else:
                logger.warning(
                    "[闲鱼订单上下文补全不足] 账号：{}，订单：{}，买家：{}，商品：{}，会话：{}",
                    event.account_id, event.order_id, buyer_id, item_id, chat_id,
                )
            return enriched
        except Exception as exc:
            logger.warning(
                "[闲鱼订单上下文补全失败] 账号：{}，订单：{}，原因：{}",
                event.account_id, event.order_id, exc,
            )
            return event

    async def start(self) -> None:
        await self.sync_accounts()
        if not self.worker_task or self.worker_task.done():
            self.worker.reset()
            self.worker_task = asyncio.create_task(self.worker.run(), name="xianyu-outbox")
        if not self.poller_task or self.poller_task.done():
            self.poller.reset()
            self.poller_task = asyncio.create_task(self.poller.run(), name="xianyu-order-poll")
        if not self.account_task or self.account_task.done():
            if self.account_stop.is_set():
                self.account_stop = asyncio.Event()
            self.account_task = asyncio.create_task(self._watch_accounts(), name="xianyu-account-sync")

    async def stop(self) -> None:
        self.account_stop.set()
        if self.account_task:
            await asyncio.gather(self.account_task, return_exceptions=True)
        await self.manager.stop_all()
        self.poller.stop()
        self.worker.stop()
        tasks = [task for task in (self.poller_task, self.worker_task) if task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def sync_accounts(self) -> None:
        async with self._account_sync_lock:
            accounts = await asyncio.to_thread(self.account_store.load_all)
            for account_id, cookies in accounts.items():
                fingerprint = self.account_store.fingerprint(cookies)
                if (
                    self.account_fingerprints.get(account_id) == fingerprint
                    and account_id in self.manager.instances
                ):
                    continue
                try:
                    await self.manager.start(account_id, cookies)
                    self.account_fingerprints[account_id] = fingerprint
                    logger.info("[闲鱼账号启动] 账号：{}", account_id)
                except Exception as exc:
                    logger.warning("[闲鱼账号启动失败] 账号：{}，原因：{}", account_id, exc)
            removed = set(self.account_fingerprints) - set(accounts)
            for account_id in removed:
                await self.manager.stop(account_id)
                self.account_fingerprints.pop(account_id, None)
                logger.info("[闲鱼账号停止] 账号：{}", account_id)

    async def _watch_accounts(self) -> None:
        while not self.account_stop.is_set():
            try:
                await asyncio.wait_for(self.account_stop.wait(), timeout=self.account_sync_interval)
            except TimeoutError:
                await self.sync_accounts()


runtime = BridgeRuntime()
