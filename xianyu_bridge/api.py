from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from message import make_text
from .runtime import runtime
from .signature import verify


class NonceCache:
    def __init__(self):
        self.values: dict[str, int] = {}
        self.lock = threading.Lock()

    def use(self, nonce: str, now: int) -> bool:
        with self.lock:
            self.values = {key: expires for key, expires in self.values.items() if expires > now}
            if nonce in self.values:
                return False
            self.values[nonce] = now + 300
            return True


nonce_cache = NonceCache()


async def require_signature(request: Request) -> None:
    secret = os.getenv("XIANYU_INTERNAL_SECRET", "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="XIANYU_INTERNAL_SECRET 未配置")
    timestamp = request.headers.get("X-Xianyu-Timestamp", "")
    nonce = request.headers.get("X-Xianyu-Nonce", "")
    signature = request.headers.get("X-Xianyu-Signature", "")
    try:
        number = int(timestamp)
    except ValueError:
        raise HTTPException(status_code=401, detail="时间戳无效") from None
    now = int(time.time())
    if abs(now - number) > 300:
        raise HTTPException(status_code=401, detail="请求已过期")
    if not nonce:
        raise HTTPException(status_code=401, detail="nonce 缺失")
    body = await request.body()
    if not verify(secret, timestamp, nonce, request.method, request.url.path, body, signature):
        raise HTTPException(status_code=401, detail="签名无效")
    if not nonce_cache.use(nonce, now):
        raise HTTPException(status_code=409, detail="nonce 已使用")


Protected = Annotated[None, Depends(require_signature)]


class SendMessageRequest(BaseModel):
    chatId: str = Field(min_length=1)
    buyerId: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=5000)
    idempotencyKey: str | None = Field(default=None, min_length=1, max_length=200)


class CreateChatRequest(BaseModel):
    buyerId: str = Field(min_length=1)
    itemId: str = Field(min_length=1)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await runtime.start()
    try:
        yield
    finally:
        await runtime.stop()


app = FastAPI(title="Xianyu Bridge", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "success": True,
        "capabilities": {
            "multiAccount": True,
            "messages": True,
            "createChat": True,
        },
        "accounts": await _account_statuses(),
        "outbox": await asyncio.to_thread(runtime.outbox.stats),
    }


@app.get("/internal/accounts/{account_id}/status")
async def account_status(account_id: str, _: Protected):
    return {"success": True, "data": runtime.manager.status(account_id)}


@app.get("/internal/accounts")
async def accounts(_: Protected):
    return {"success": True, "data": await _account_statuses()}


@app.post("/internal/accounts/{account_id}/messages")
async def send_message(account_id: str, req: SendMessageRequest, _: Protected):
    live = _live(account_id)
    if not live.ws:
        raise HTTPException(status_code=409, detail="闲鱼 WebSocket 未连接")
    result = await live.send_msg(
        live.ws,
        req.chatId,
        req.buyerId,
        make_text(req.message),
        _message_uuid(account_id, req.idempotencyKey),
    )
    return {"success": bool(result.get("success")), "data": result}


@app.post("/internal/accounts/{account_id}/chats")
async def create_chat(account_id: str, req: CreateChatRequest, _: Protected):
    live = _live(account_id)
    if not live.ws:
        raise HTTPException(status_code=409, detail="闲鱼 WebSocket 未连接")
    result = await live.create_chat(live.ws, req.buyerId, req.itemId)
    return {"success": bool(result.get("success")), "data": result}


async def _account_statuses() -> list[dict]:
    stored = await asyncio.to_thread(runtime.account_store.list_ids)
    active = set(runtime.manager.instances)
    return [
        {**runtime.manager.status(account_id), "configured": account_id in stored}
        for account_id in sorted(set(stored) | active)
    ]


def _live(account_id: str):
    try:
        return runtime.manager.get(account_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


def _message_uuid(account_id: str, idempotency_key: str | None) -> str | None:
    if not idempotency_key:
        return None
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"xianyu:{account_id}:{idempotency_key}"))
