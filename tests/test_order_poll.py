import unittest

from xianyu_bridge.order_poll import order_context_from_detail, orders_from_response


class OrderPollTest(unittest.TestCase):
    def test_extracts_stable_order_events_from_nested_response(self):
        response = {
            "ret": ["SUCCESS::调用成功"],
            "data": {
                "orders": [
                    {
                        "bizOrderId": "123456789012345",
                        "itemId": "778899",
                        "buyerUserId": "buyer-1",
                        "orderStatus": "WAIT_SELLER_SEND_GOODS",
                        "actualPaidAmount": "18.80",
                    },
                    {
                        "orderId": "223456789012345",
                        "statusText": "交易关闭",
                    },
                ]
            },
        }

        events = orders_from_response("account-1", response)

        self.assertEqual(2, len(events))
        self.assertEqual("ORDER_PAID", events[0].event_type)
        self.assertEqual("account-1:order:123456789012345:PAID", events[0].event_id)
        self.assertEqual("778899", events[0].item_id)
        self.assertEqual("18.80", events[0].paid_amount)
        self.assertEqual("ORDER_CLOSED", events[1].event_type)

    def test_ignores_unknown_containers_and_deduplicates_nested_order(self):
        order = {"orderId": "123456789012345", "status": "WAIT_BUYER_PAY"}
        response = {"data": {"order": order, "copy": order}}

        events = orders_from_response("a", response)

        self.assertEqual(1, len(events))
        self.assertEqual("ORDER_CREATED", events[0].event_type)

    def test_extracts_buyer_and_item_from_seller_order_detail(self):
        detail = {
            "data": {
                "raw": {
                    "data": {
                        "orderId": "o1",
                        "peerUserId": "buyer-1",
                        "itemId": "item-1",
                    }
                }
            }
        }

        self.assertEqual(("buyer-1", "item-1"), order_context_from_detail(detail))


if __name__ == "__main__":
    unittest.main()
