import base64
import json
import unittest

from xianyu_bridge.parser import XianyuMessageParser


class XianyuMessageParserTest(unittest.TestCase):
    def test_paid_amount_only_uses_explicit_decimal_payment_fields(self):
        parser = XianyuMessageParser("a1")
        self.assertEqual("12.30", parser.find_paid_amount({"actualPaidAmount": "12.3"}))
        self.assertIsNone(parser.find_paid_amount({"price": "12.30"}))
        self.assertIsNone(parser.find_paid_amount({"paidAmount": 1230}))

    def setUp(self):
        self.parser = XianyuMessageParser("seller-1")

    def test_trade_card_extracts_stable_identifiers(self):
        card = {
            "dxCard": {
                "item": {
                    "main": {
                        "exContent": {
                            "title": "买家已拍下，待付款",
                            "targetUrl": "fleamarket://order_detail?id=2503688126356636370&role=seller",
                        }
                    }
                }
            }
        }
        message = {
            "1": {
                "2": "chat-1@goofish",
                "5": 1_700_000_000_000,
                "10": {
                    "senderUserId": "buyer-1",
                    "reminderTitle": "买家已拍下，待付款",
                    "reminderContent": "买家已拍下，待付款",
                    "bizTag": json.dumps({"messageId": "message-1", "itemId": "900052644277"}),
                },
                "6": {"3": {"5": json.dumps(card, ensure_ascii=False)}},
            }
        }

        event = self.parser.parse_message(message)

        self.assertEqual("seller-1:message-1", event.event_id)
        self.assertEqual("ORDER_CREATED", event.event_type)
        self.assertEqual("chat-1", event.chat_id)
        self.assertEqual("buyer-1", event.buyer_id)
        self.assertEqual("900052644277", event.item_id)
        self.assertEqual("2503688126356636370", event.order_id)

    def test_image_payload_uses_real_url(self):
        content = {"contentType": 2, "image": {"pics": [{"url": "https://img.example/a.png"}]}}
        encoded = base64.b64encode(json.dumps(content).encode()).decode()
        message = {
            "1": {
                "2": "chat-2@goofish",
                "10": {
                    "senderUserId": "buyer-2",
                    "reminderContent": "[图片]",
                    "extJson": json.dumps({"messageId": "message-2"}),
                },
                "6": {"3": {"1": encoded}},
            }
        }

        event = self.parser.parse_message(message)

        self.assertEqual("image", event.content_type)
        self.assertEqual("https://img.example/a.png", event.content)

    def test_all_sync_entries_are_parsed(self):
        def payload(number):
            return {
                "1": {
                    "2": f"chat-{number}@goofish",
                    "10": {
                        "senderUserId": f"buyer-{number}",
                        "reminderContent": f"hello-{number}",
                        "extJson": json.dumps({"messageId": f"message-{number}"}),
                    },
                }
            }

        envelope = {
            "body": {
                "syncPushPackage": {
                    "data": [
                        {"data": json.dumps(payload(1))},
                        {"data": base64.b64encode(json.dumps(payload(2)).encode()).decode()},
                    ]
                }
            }
        }

        events = self.parser.parse_envelope(envelope)

        self.assertEqual(["message-1", "message-2"], [event.message_id for event in events])


if __name__ == "__main__":
    unittest.main()
