from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from loguru import logger

from goofish_live import XianyuLive
from .events import XianyuEvent
from .platform import XianyuPlatformClient


class XianyuConnectionManager:
    def __init__(self, event_handler: Callable[[XianyuEvent], Awaitable[None]]):
        self.event_handler = event_handler
        self.instances: dict[str, XianyuLive] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.errors: dict[str, str] = {}
        self.started_at: dict[str, float] = {}

    async def start(self, account_id: str, cookies: str) -> None:
        account_id = str(account_id)
        await self.stop(account_id)
        live = XianyuLive(cookies, self.event_handler)
        if live.myid != account_id:
            logger.info("[闲鱼账号标识校正] 请求账号：{}，Cookie账号：{}", account_id, live.myid)
            account_id = live.myid
            await self.stop(account_id)
        self.instances[account_id] = live
        self.started_at[account_id] = time.time()
        self.errors.pop(account_id, None)
        self.tasks[account_id] = asyncio.create_task(self._run(account_id, live), name=f"xianyu-{account_id}")

    async def _run(self, account_id: str, live: XianyuLive) -> None:
        delay = 1
        while self.instances.get(account_id) is live:
            try:
                await live.main()
                self.errors[account_id] = "连接正常结束"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors[account_id] = f"{type(exc).__name__}: {exc}"
                logger.warning("[闲鱼连接断开] 账号：{}，{}秒后重连，原因：{}", account_id, delay, exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def stop(self, account_id: str) -> None:
        account_id = str(account_id)
        self.instances.pop(account_id, None)
        task = self.tasks.pop(account_id, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.started_at.pop(account_id, None)
        self.errors.pop(account_id, None)

    async def stop_all(self) -> None:
        for account_id in list(self.tasks):
            await self.stop(account_id)

    def get(self, account_id: str) -> XianyuLive:
        live = self.instances.get(str(account_id))
        if not live:
            raise KeyError(f"账号 {account_id} 未启动")
        return live

    def platform(self, account_id: str) -> XianyuPlatformClient:
        return XianyuPlatformClient(self.get(account_id).xianyu)

    def status(self, account_id: str) -> dict:
        live = self.instances.get(str(account_id))
        task = self.tasks.get(str(account_id))
        return {
            "accountId": str(account_id),
            "running": bool(task and not task.done()),
            "connected": bool(live and live.ws),
            "startedAt": self.started_at.get(str(account_id)),
            "lastError": self.errors.get(str(account_id)),
        }

    def statuses(self) -> list[dict]:
        return [self.status(account_id) for account_id in self.instances]
