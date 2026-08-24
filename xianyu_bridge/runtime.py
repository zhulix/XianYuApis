from __future__ import annotations

import asyncio
import os
from pathlib import Path

from loguru import logger

from .accounts import AccountStore
from .callback import JavaEventCallback, OutboxWorker
from .events import XianyuEvent
from .manager import XianyuConnectionManager
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
        self.worker_task: asyncio.Task | None = None
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

    async def start(self) -> None:
        await self.sync_accounts()
        if not self.worker_task or self.worker_task.done():
            self.worker.reset()
            self.worker_task = asyncio.create_task(self.worker.run(), name="xianyu-outbox")
        if not self.account_task or self.account_task.done():
            if self.account_stop.is_set():
                self.account_stop = asyncio.Event()
            self.account_task = asyncio.create_task(self._watch_accounts(), name="xianyu-account-sync")

    async def stop(self) -> None:
        self.account_stop.set()
        if self.account_task:
            await asyncio.gather(self.account_task, return_exceptions=True)
        await self.manager.stop_all()
        self.worker.stop()
        tasks = [task for task in (self.worker_task,) if task]
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
