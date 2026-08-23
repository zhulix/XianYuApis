from __future__ import annotations

import asyncio
import secrets
import time

import httpx
from loguru import logger

from .outbox import EventOutbox
from .signature import sign


class JavaEventCallback:
    def __init__(self, url: str, secret: str, timeout: float = 10):
        self.url = url
        self.secret = secret
        self.timeout = timeout

    async def send(self, payload: str, event_id: str) -> None:
        body = payload.encode()
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        path = httpx.URL(self.url).raw_path.decode()
        headers = {
            "Content-Type": "application/json",
            "X-Xianyu-Timestamp": timestamp,
            "X-Xianyu-Nonce": nonce,
            "X-Xianyu-Event-Id": event_id,
            "X-Xianyu-Signature": sign(self.secret, timestamp, nonce, "POST", path, body),
        }
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            response = await client.post(self.url, content=body, headers=headers)
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError:
            return
        if isinstance(data, dict) and data.get("code") not in (None, 0, 200):
            raise RuntimeError(f"业务回调失败: {data.get('code')} {data.get('msg') or data.get('message')}")


class OutboxWorker:
    def __init__(self, outbox: EventOutbox, callback: JavaEventCallback | None):
        self.outbox = outbox
        self.callback = callback
        self._stop = asyncio.Event()

    async def run(self) -> None:
        while not self._stop.is_set():
            if self.callback:
                for row in await asyncio.to_thread(self.outbox.due):
                    attempts = int(row["attempts"]) + 1
                    try:
                        await self.callback.send(row["payload"], row["event_id"])
                        await asyncio.to_thread(self.outbox.delivered, row["id"])
                        logger.info("[闲鱼事件回调成功] 事件ID：{}", row["event_id"])
                    except Exception as exc:
                        await asyncio.to_thread(self.outbox.failed, row["id"], attempts, str(exc))
                        logger.warning("[闲鱼事件回调失败] 事件ID：{}，原因：{}", row["event_id"], exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    def reset(self) -> None:
        if self._stop.is_set():
            self._stop = asyncio.Event()
