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
from .platform import UnsupportedXianyuOperation, XianyuPlatformError, summarize_order_status
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


class DeliverRequest(BaseModel):
    itemId: str | None = None
    buyerId: str | None = None
    chatId: str | None = None
    pickupCode: str | None = None
    idempotencyKey: str | None = Field(default=None, min_length=1, max_length=200)


class CancelRequest(BaseModel):
    """卖家关闭待付款订单。

    闲鱼卖家端只接受预设关闭原因；当前协议固定使用“其他原因”，不允许调用方
    透传任意文案。
    """


class PriceRequest(BaseModel):
    amount: str
    quoteText: str | None = None


class RefundRequest(BaseModel):
    amount: str
    reason: str


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
            "orderQuery": True,
            "deliver": True,
            "cancel": True,
            "offline": True,
            "changePrice": False,
            "refund": False,
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


@app.get("/internal/accounts/{account_id}/orders")
async def list_orders(account_id: str, _: Protected, page: int = 1, size: int = 20, queryCode: str = "ALL"):
    try:
        return {"success": True, "data": await _thread(runtime.manager.platform(account_id).list_sold_orders, page, size, queryCode)}
    except Exception as exc:
        _platform_error(exc)


@app.get("/internal/accounts/{account_id}/items")
async def list_items(account_id: str, _: Protected, page: int = 1, size: int = 100):
    try:
        limit = min(max(size, 1), 100)
        platform = runtime.manager.platform(account_id)
        raw_items = []
        total = 0
        current_page = max(page, 1)
        while len(raw_items) < limit:
            raw = await _thread(platform.list_items, current_page, min(20, limit - len(raw_items)))
            data = raw.get("data", {}).get("data", {})
            batch = data.get("itemSearchResponseList", [])
            total = int(data.get("total") or len(batch))
            raw_items.extend(batch)
            if not batch or len(raw_items) >= total:
                break
            current_page += 1
        items = []
        for item in raw_items:
            item_id = item.get("itemId")
            if not item_id:
                continue
            status = str(item.get("itemStatus", ""))
            items.append({
                "itemId": str(item_id),
                "title": item.get("title") or f"商品 {item_id}",
                "status": status,
                "statusName": "在卖" if status in {"0", "-9"} else "下架",
                "imageUrl": item.get("picUrl") or item.get("imageUrl"),
            })
        return {"success": True, "data": {"list": items, "total": total}}
    except Exception as exc:
        _platform_error(exc)


@app.get("/internal/accounts/{account_id}/orders/{order_id}")
async def order_detail(account_id: str, order_id: str, _: Protected):
    try:
        raw = await _thread(runtime.manager.platform(account_id).order_detail, order_id)
        return {"success": True, "data": {"summary": summarize_order_status(raw), "raw": raw}}
    except Exception as exc:
        _platform_error(exc)


@app.post("/internal/accounts/{account_id}/orders/{order_id}/deliver")
async def deliver(account_id: str, order_id: str, req: DeliverRequest, _: Protected):
    client = runtime.manager.platform(account_id)
    try:
        detail = await _thread(client.order_detail, order_id)
        summary = summarize_order_status(detail)
        if summary["status"] == "SHIPPED":
            confirm = {"alreadyDelivered": True}
        elif summary["status"] != "PAID":
            raise HTTPException(status_code=409, detail=f"订单状态不允许发货: {summary['status']}")
        else:
            confirm = await _thread(client.confirm_delivery, order_id)
        message_result = None
        if req.pickupCode and req.chatId and req.buyerId:
            live = _live(account_id)
            if not live.ws:
                raise HTTPException(status_code=502, detail="闲鱼已发货，但 WebSocket 未连接，取餐码未发送")
            message_result = await live.send_msg(
                live.ws,
                req.chatId,
                req.buyerId,
                make_text(f"已为您下单，取餐码：{req.pickupCode}"),
                _message_uuid(account_id, req.idempotencyKey),
            )
        return {"success": True, "data": {"confirm": confirm, "message": message_result}}
    except HTTPException:
        raise
    except Exception as exc:
        _platform_error(exc)


@app.post("/internal/accounts/{account_id}/orders/{order_id}/cancel")
async def cancel_order(account_id: str, order_id: str, _req: CancelRequest, _: Protected):
    try:
        client = runtime.manager.platform(account_id)
        detail = await _thread(client.order_detail, order_id)
        status = summarize_order_status(detail)["status"]
        if status == "CLOSED":
            data = {"alreadyCancelled": True}
        elif status != "WAIT_PAYMENT":
            raise HTTPException(status_code=409, detail=f"订单状态不允许取消: {status}")
        else:
            data = await _thread(client.cancel_order, order_id)
        return {"success": True, "data": data}
    except HTTPException:
        raise
    except Exception as exc:
        _platform_error(exc)


@app.post("/internal/accounts/{account_id}/items/{item_id}/offline")
async def offline_item(account_id: str, item_id: str, _: Protected):
    try:
        data = await _thread(runtime.manager.platform(account_id).offline_item, item_id)
        return {"success": True, "data": data}
    except Exception as exc:
        _platform_error(exc)


@app.post("/internal/accounts/{account_id}/orders/{order_id}/price")
async def change_price(account_id: str, order_id: str, req: PriceRequest, _: Protected):
    try:
        return {"success": True, "data": runtime.manager.platform(account_id).change_price(order_id, req.amount)}
    except Exception as exc:
        _platform_error(exc)


@app.post("/internal/accounts/{account_id}/orders/{order_id}/refund")
async def refund(account_id: str, order_id: str, req: RefundRequest, _: Protected):
    try:
        return {"success": True, "data": runtime.manager.platform(account_id).refund(order_id, req.amount, req.reason)}
    except Exception as exc:
        _platform_error(exc)


async def _thread(function, *args):
    return await asyncio.to_thread(function, *args)


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


def _platform_error(exc: Exception):
    if isinstance(exc, UnsupportedXianyuOperation):
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    if isinstance(exc, XianyuPlatformError):
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc
