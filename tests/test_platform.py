import unittest

from xianyu_bridge.platform import XianyuPlatformClient, XianyuPlatformError


class FakeCookies:
    def get(self, key, default=""):
        return "token_123" if key == "_m_h5_tk" else default


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.cookies = FakeCookies()
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.responses.pop(0))


class FakeApi:
    def __init__(self, responses):
        self.session = FakeSession(responses)
        self.refreshes = 0

    def refresh_token(self):
        self.refreshes += 1


class PlatformClientTest(unittest.TestCase):
    def test_list_items_uses_seller_goods_search(self):
        api = FakeApi([{
            "ret": ["SUCCESS::调用成功"],
            "data": {"code": "success", "data": {"itemSearchResponseList": []}},
        }])

        XianyuPlatformClient(api).list_items()

        url, call = api.session.calls[0]
        self.assertIn("seller.pc.common.item.search", url)
        self.assertIn('"bizType":"commonPro"', call["data"]["data"])
        self.assertEqual("https://seller.goofish.com", call["headers"]["Origin"])

    def test_read_refreshes_expired_token_once(self):
        api = FakeApi([
            {"ret": ["FAIL_SYS_TOKEN_EXPIRED::令牌过期"]},
            {"ret": ["SUCCESS::调用成功"], "data": {"orders": []}},
        ])

        result = XianyuPlatformClient(api).list_sold_orders()

        self.assertIn("data", result)
        self.assertEqual(1, api.refreshes)
        self.assertEqual(2, len(api.session.calls))

    def test_write_does_not_auto_replay_on_token_expiry(self):
        api = FakeApi([{"ret": ["FAIL_SYS_TOKEN_EXPIRED::令牌过期"]}])

        with self.assertRaises(XianyuPlatformError):
            XianyuPlatformClient(api).confirm_delivery("123")

        self.assertEqual(0, api.refreshes)
        self.assertEqual(1, len(api.session.calls))
        url, call = api.session.calls[0]
        self.assertIn("consign.dummy", url)
        self.assertIn('"orderId":"123"', call["data"]["data"])

    def test_cancel_order_uses_seller_close_api_with_fixed_reason(self):
        api = FakeApi([{"ret": ["SUCCESS::调用成功"]}])

        XianyuPlatformClient(api).cancel_order("123")

        url, call = api.session.calls[0]
        self.assertIn("mtop.taobao.idle.trade.merchant.close.by.seller/2.0", url)
        self.assertEqual(
            '{"tid":"123","bizOrderId":"123","closeReason":"其他原因"}',
            call["data"]["data"],
        )
        self.assertEqual("https://seller.goofish.com", call["headers"]["Origin"])
        self.assertEqual("COMMONPRO", call["headers"]["idle_site_biz_code"])

    def test_cancel_order_does_not_auto_replay_on_token_expiry(self):
        api = FakeApi([{"ret": ["FAIL_SYS_TOKEN_EXPIRED::令牌过期"]}])

        with self.assertRaises(XianyuPlatformError):
            XianyuPlatformClient(api).cancel_order("123")

        self.assertEqual(0, api.refreshes)
        self.assertEqual(1, len(api.session.calls))


if __name__ == "__main__":
    unittest.main()
