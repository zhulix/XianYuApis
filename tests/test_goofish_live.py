import asyncio
import json
import unittest

from goofish_live import XianyuLive
from message import make_text


class FakeWebSocket:
    def __init__(self, live: XianyuLive, response_factory=None):
        self.live = live
        self.response_factory = response_factory
        self.sent = []

    async def send(self, raw: str):
        request = json.loads(raw)
        self.sent.append(request)
        if self.response_factory:
            response = self.response_factory(request)
            self.live._dispatch_mid_response(response)


class XianyuLiveSendMessageTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.live = XianyuLive("unb=seller; _m_h5_tk=test_1; cookie2=test")

    async def test_send_message_waits_for_matching_success_response(self):
        websocket = FakeWebSocket(self.live, lambda request: {
            "code": 200,
            "headers": {"mid": request["headers"]["mid"]},
            "body": {},
        })

        result = await self.live.send_msg(
            websocket, "chat-1", "buyer-1", make_text("hello"), "stable-uuid"
        )

        self.assertTrue(result["success"])
        self.assertEqual("stable-uuid", websocket.sent[0]["body"][0]["uuid"])
        self.assertEqual({}, self.live._pending_mid_futures)

    async def test_send_message_returns_failure_for_business_rejection(self):
        websocket = FakeWebSocket(self.live, lambda request: {
            "code": 200,
            "headers": {"mid": request["headers"]["mid"]},
            "body": {"reason": "CSI_FORBID", "moreInfo": "消息被风控拦截"},
        })

        result = await self.live.send_msg(websocket, "chat-1", "buyer-1", make_text("hello"))

        self.assertFalse(result["success"])
        self.assertIn("CSI_FORBID", result["error"])

    async def test_send_message_returns_failure_for_non_200_response(self):
        websocket = FakeWebSocket(self.live, lambda request: {
            "code": 500,
            "headers": {"mid": request["headers"]["mid"]},
            "body": {},
        })

        result = await self.live.send_msg(websocket, "chat-1", "buyer-1", make_text("hello"))

        self.assertFalse(result["success"])
        self.assertIn("500", result["error"])

    async def test_send_message_timeout_is_unknown_not_success(self):
        self.live.SEND_RESPONSE_TIMEOUT_SECONDS = 0.01
        websocket = FakeWebSocket(self.live)

        result = await self.live.send_msg(websocket, "chat-1", "buyer-1", make_text("hello"))

        self.assertFalse(result["success"])
        self.assertTrue(result["unknown"])
        self.assertEqual({}, self.live._pending_mid_futures)

    async def test_disconnect_fails_pending_request(self):
        future = asyncio.get_running_loop().create_future()
        self.live._pending_mid_futures["mid-1"] = future

        self.live._fail_pending_mid_requests("断开")

        with self.assertRaisesRegex(ConnectionError, "断开"):
            await future
        self.assertEqual({}, self.live._pending_mid_futures)

    async def test_create_chat_waits_for_response_and_returns_chat_id(self):
        websocket = FakeWebSocket(self.live, lambda request: {
            "code": 200,
            "headers": {"mid": request["headers"]["mid"]},
            "body": {"data": {"conversationId": "chat-1@goofish"}},
        })

        result = await self.live.create_chat(websocket, "buyer-1", "item-1")

        self.assertTrue(result["success"])
        self.assertEqual("chat-1", result["chatId"])
        self.assertEqual("buyer-1@goofish", websocket.sent[0]["body"][0]["pairFirst"])
        self.assertEqual({}, self.live._pending_mid_futures)

    async def test_create_chat_does_not_report_success_without_chat_id(self):
        websocket = FakeWebSocket(self.live, lambda request: {
            "code": 200,
            "headers": {"mid": request["headers"]["mid"]},
            "body": {"success": True},
        })

        result = await self.live.create_chat(websocket, "buyer-1", "item-1")

        self.assertFalse(result["success"])
        self.assertIn("会话ID", result["error"])


if __name__ == "__main__":
    unittest.main()
