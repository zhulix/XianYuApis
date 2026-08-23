import base64
import json
import asyncio
import time
from collections.abc import Awaitable, Callable

from loguru import logger
import websockets
from goofish_apis import XianyuApis

from utils.goofish_utils import generate_mid, generate_uuid, trans_cookies, generate_device_id, \
    get_session_cookies_str
from message import Message
from xianyu_bridge.events import XianyuEvent
from xianyu_bridge.parser import XianyuMessageParser


def ws_connect(url, headers):
    """兼容 websockets 新旧版本的连接头参数名（>=14 为 additional_headers）。"""
    try:
        return websockets.connect(url, additional_headers=headers,
                                  ping_interval=20, ping_timeout=120)
    except TypeError:
        return websockets.connect(url, extra_headers=headers,
                                  ping_interval=20, ping_timeout=120)


class XianyuLive:
    SEND_RESPONSE_TIMEOUT_SECONDS = 10

    def __init__(self, cookies_str, event_handler: Callable[[XianyuEvent], Awaitable[None]] | None = None):
        self.base_url = 'wss://wss-goofish.dingtalk.com/'
        self.cookies_str = cookies_str
        self.cookies = trans_cookies(cookies_str)
        self.myid = self.cookies['unb']
        self.device_id = generate_device_id(self.myid)
        self.xianyu = XianyuApis(self.cookies, self.device_id)
        self.ws = None
        self.event_handler = event_handler
        self.parser = XianyuMessageParser(self.myid)
        self._pending_mid_futures: dict[str, asyncio.Future] = {}

    def _dispatch_mid_response(self, message: dict) -> bool:
        headers = message.get("headers") or {}
        mid = str(headers.get("mid") or "")
        future = self._pending_mid_futures.pop(mid, None)
        if future is None or future.done():
            return False
        future.set_result(message)
        return True

    def _fail_pending_mid_requests(self, reason: str):
        futures = list(self._pending_mid_futures.values())
        self._pending_mid_futures.clear()
        for future in futures:
            if not future.done():
                future.set_exception(ConnectionError(reason))

    @staticmethod
    def _send_response_error(response: dict) -> str | None:
        code = response.get("code")
        body = response.get("body") or {}
        if not isinstance(body, dict):
            body = {}
        reason = body.get("reason") or body.get("message") or body.get("errorMessage") or body.get("error")
        more_info = body.get("moreInfo")
        if reason:
            return "：".join(str(value) for value in (reason, more_info) if value)
        try:
            success = int(code) == 200
        except (TypeError, ValueError):
            success = False
        if not success:
            return f"闲鱼返回非成功状态：{code or '缺少状态码'}"
        return None

    async def list_all_conversations(self, cid):
        headers = {
            "Cookie": get_session_cookies_str(self.xianyu.session),
            "Host": "wss-goofish.dingtalk.com",
            "Connection": "Upgrade",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "Origin": "https://www.goofish.com",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        async with ws_connect(self.base_url, headers) as websocket:
            asyncio.create_task(self.init(websocket))
            send_mid = generate_mid()
            msg = {
                "lwp": "/r/MessageManager/listUserMessages",
                "headers": {
                    "mid": send_mid
                },
                "body": [
                    f"{cid}@goofish",
                    False,
                    9007199254740991,
                    20,
                    False
                ]
            }
            user_message_models = []
            async for message in websocket:
                try:
                    message = json.loads(message)
                    ack = {
                        "code": 200,
                        "headers": {
                            "mid": message["headers"]["mid"] if "mid" in message["headers"] else generate_mid(),
                            "sid": message["headers"]["sid"] if "sid" in message["headers"] else '',
                        }
                    }
                    if 'app-key' in message["headers"]:
                        ack["headers"]["app-key"] = message["headers"]["app-key"]
                    if 'ua' in message["headers"]:
                        ack["headers"]["ua"] = message["headers"]["ua"]
                    if 'dt' in message["headers"]:
                        ack["headers"]["dt"] = message["headers"]["dt"]
                    await websocket.send(json.dumps(ack))
                except Exception as e:
                    pass
                try:
                    if 'lwp' in message and message['lwp'] == "/s/vulcan":
                        await websocket.send(json.dumps(msg))
                    recv_mid = message["headers"]["mid"] if "mid" in message["headers"] else ''
                    if recv_mid == send_mid:
                        logger.info(f"user history message: {message}")
                        has_more = message["body"]["hasMore"] == 1
                        next_cursor = message["body"]["nextCursor"]
                        for user_message in message["body"]["userMessageModels"]:
                            send_user_name = user_message["message"]["extension"]["reminderTitle"]
                            send_user_id = user_message["message"]["extension"]["senderUserId"]
                            send_message_base64 = user_message["message"]["content"]["custom"]["data"]
                            send_message_json = json.loads(base64.b64decode(send_message_base64).decode('utf-8'))
                            user_message_models.insert(0, {
                                "send_user_id": send_user_id,
                                "send_user_name": send_user_name,
                                "message": send_message_json
                            })
                        if has_more:
                            logger.info(f"has more history messages, next cursor: {next_cursor}")
                            send_mid = generate_mid()
                            msg["headers"]["mid"] = send_mid
                            msg["body"][2] = next_cursor
                            await websocket.send(json.dumps(msg))
                        else:
                            return user_message_models
                except Exception as e:
                    return user_message_models

    async def create_chat(self, ws, toid, item_id='891198795482'):
        mid = generate_mid()
        msg = {
            "lwp": "/r/SingleChatConversation/create",
            "headers": {
                "mid": mid
            },
            "body": [
                {
                    "pairFirst": f"{toid}@goofish",
                    "pairSecond": f"{self.myid}@goofish",
                    "bizType": "1",
                    "extension": {
                        "itemId": item_id
                    },
                    "ctx": {
                        "appVersion": "1.0",
                        "platform": "web"
                    }
                }
            ]
        }
        await ws.send(json.dumps(msg))
        return {"success": True, "mid": mid}

    async def send_msg(self, ws, cid, toid, message: Message, message_uuid: str | None = None):
        msg_type = message["type"]
        mid = generate_mid()
        msg = {
            "lwp": "/r/MessageSend/sendByReceiverScope",
            "headers": {
                "mid": mid
            },
            "body": [
                {
                    "uuid": message_uuid or generate_uuid(),
                    "cid": f"{cid}@goofish",
                    "conversationType": 1,
                    "content": {
                        "contentType": 101,
                        "custom": {
                            "type": None,
                            "data": None
                        }
                    },
                    "redPointPolicy": 0,
                    "extension": {
                        "extJson": "{}"
                    },
                    "ctx": {
                        "appVersion": "1.0",
                        "platform": "web"
                    },
                    "mtags": {},
                    "msgReadStatusSetting": 1
                },
                {
                    "actualReceivers": [
                        f"{toid}@goofish",
                        f"{self.myid}@goofish"
                    ]
                }
            ]
        }
        if msg_type == "text":
            payload = {
                "contentType": 1,
                "text": {
                    "text": message["text"]
                }
            }
            text_base64 = str(base64.b64encode(json.dumps(payload).encode('utf-8')), 'utf-8')
            msg["body"][0]["content"]["custom"]["type"] = 1
            msg["body"][0]["content"]["custom"]["data"] = text_base64
        elif msg_type == "image":
            payload = {
                "contentType": 2,
                "image": {
                    "pics": [
                        {
                            "type": 0,
                            "url": message["image_url"],
                            "width": message["width"],
                            "height": message["height"]
                        }
                    ]
                }
            }
            image_base64 = str(base64.b64encode(json.dumps(payload).encode('utf-8')), 'utf-8')
            msg["body"][0]["content"]["custom"]["type"] = 2
            msg["body"][0]["content"]["custom"]["data"] = image_base64
        elif msg_type == "audio":
            # TODO: handle audio message
            logger.error(f"不支持的消息类型: {msg_type}")
            return {"success": False, "error": f"不支持的消息类型: {msg_type}"}
        else:
            logger.error(f"不支持的消息类型: {msg_type}")
            return {"success": False, "error": f"不支持的消息类型: {msg_type}"}
        loop = asyncio.get_running_loop()
        response_future = loop.create_future()
        self._pending_mid_futures[mid] = response_future
        try:
            await ws.send(json.dumps(msg))
            response = await asyncio.wait_for(
                asyncio.shield(response_future),
                timeout=self.SEND_RESPONSE_TIMEOUT_SECONDS,
            )
            error = self._send_response_error(response)
            if error:
                logger.warning("[闲鱼消息发送被拒绝] 账号：{}，消息MID：{}，原因：{}", self.myid, mid, error)
                return {
                    "success": False,
                    "mid": mid,
                    "code": response.get("code"),
                    "error": error,
                }
            logger.info("[闲鱼消息发送确认] 账号：{}，消息MID：{}", self.myid, mid)
            return {"success": True, "mid": mid, "code": response.get("code")}
        except TimeoutError:
            error = "等待闲鱼消息发送确认超时，发送结果未知"
            logger.warning("[闲鱼消息发送结果未知] 账号：{}，消息MID：{}，原因：{}", self.myid, mid, error)
            return {"success": False, "mid": mid, "unknown": True, "error": error}
        except Exception as exc:
            error = f"等待闲鱼消息发送确认失败：{exc}"
            logger.warning("[闲鱼消息发送失败] 账号：{}，消息MID：{}，原因：{}", self.myid, mid, error)
            return {"success": False, "mid": mid, "unknown": True, "error": error}
        finally:
            pending = self._pending_mid_futures.pop(mid, None)
            if pending is not None and not pending.done():
                pending.cancel()

    async def init(self, ws):
        data = self.xianyu.get_token()
        token = data['data']['accessToken'] if 'data' in data and 'accessToken' in data['data'] else ''
        if not token:
            raise RuntimeError('获取闲鱼 WebSocket token 失败')
        msg = {
            "lwp": "/reg",
            "headers": {
                "cache-header": "app-key token ua wv",
                "app-key": "444e9908a51d1cb236a27862abc769c9",
                "token": token,
                "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 DingTalk(2.1.5) OS(Windows/10) Browser(Chrome/133.0.0.0) DingWeb/2.1.5 IMPaaS DingWeb/2.1.5",
                "dt": "j",
                "wv": "im:3,au:3,sy:6",
                "sync": "0,0;0;0;",
                "did": self.device_id,
                "mid": generate_mid()
            }
        }
        await ws.send(json.dumps(msg))
        current_time = int(time.time() * 1000)
        msg = {
            "lwp": "/r/SyncStatus/ackDiff",
            "headers": {"mid": generate_mid()},
            "body": [
                {
                    "pipeline": "sync",
                    "tooLong2Tag": "PNM,1",
                    "channel": "sync",
                    "topic": "sync",
                    "highPts": 0,
                    "pts": current_time * 1000,
                    "seq": 0,
                    "timestamp": current_time
                }
            ]
        }
        await ws.send(json.dumps(msg))
        logger.info('init')

    async def heart_beat(self, ws):
        while True:
            msg = {
                "lwp": "/!",
                "headers": {
                    "mid": generate_mid()
                 }
            }
            await ws.send(json.dumps(msg))
            await asyncio.sleep(15)

    async def user_alive(self):
        while True:
            await asyncio.sleep(600)
            try:
                await asyncio.to_thread(self.xianyu.refresh_token)
                logger.info("[闲鱼Token刷新完成] 账号：{}", self.myid)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[闲鱼Token刷新失败] 账号：{}，原因：{}", self.myid, exc)

    async def main(self):
        headers = {
            "Cookie": get_session_cookies_str(self.xianyu.session),
            "Host": "wss-goofish.dingtalk.com",
            "Connection": "Upgrade",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "Origin": "https://www.goofish.com",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        async with ws_connect(self.base_url, headers) as websocket:
            self.ws = websocket
            tasks = [
                asyncio.create_task(self.init(websocket), name=f"xianyu-init-{self.myid}"),
                asyncio.create_task(self.heart_beat(websocket), name=f"xianyu-heartbeat-{self.myid}"),
                asyncio.create_task(self.user_alive(), name=f"xianyu-token-{self.myid}"),
            ]
            try:
                async for raw in websocket:
                    message = json.loads(raw)
                    self._dispatch_mid_response(message)
                    headers_in = message.get("headers") or {}
                    ack = {
                        "code": 200,
                        "headers": {
                            "mid": headers_in.get("mid", generate_mid()),
                            "sid": headers_in.get("sid", ""),
                        },
                    }
                    for key in ("app-key", "ua", "dt"):
                        if key in headers_in:
                            ack["headers"][key] = headers_in[key]
                    await websocket.send(json.dumps(ack))
                    await self.handle_message(message, websocket)
            finally:
                self.ws = None
                self._fail_pending_mid_requests("闲鱼 WebSocket 连接已断开")
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def handle_message(self, message, websocket):
        events = self.parser.parse_envelope(message)
        for event in events:
            if event.buyer_id == self.myid:
                continue
            logger.info(
                "[闲鱼事件接收] 账号：{}，类型：{}，事件ID：{}，会话ID：{}，订单ID：{}",
                self.myid, event.event_type, event.event_id, event.chat_id, event.order_id,
            )
            if self.event_handler:
                await self.event_handler(event)


if __name__ == '__main__':
    cookies_str = r''
    xianyuLive = XianyuLive(cookies_str)

    # 1 获取全部聊天记录
    # cid = '47812870000'
    # all_messages = asyncio.run(xianyuLive.list_all_conversations(cid))
    # for message in all_messages:
    #     print(message)

    # 2 常驻进程 用于接收消息和自动回复
    asyncio.run(xianyuLive.main())
