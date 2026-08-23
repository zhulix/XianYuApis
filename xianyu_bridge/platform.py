from __future__ import annotations

import json
import time
from typing import Any

from goofish_apis import UA, XianyuApis
from utils.goofish_utils import generate_sign


_CLOSE_ORDER_REASON = "其他原因"


class XianyuPlatformError(RuntimeError):
    def __init__(self, operation: str, response: dict[str, Any]):
        self.operation = operation
        self.response = response
        ret = response.get("ret") if isinstance(response, dict) else None
        super().__init__(f"{operation}失败: {ret or response}")


class UnsupportedXianyuOperation(XianyuPlatformError):
    pass


class XianyuPlatformClient:
    """闲鱼卖家侧 HTTP 能力。

    写操作不做网络异常自动重放；调用方需先查询平台状态再决定是否重试。
    """

    def __init__(self, api: XianyuApis, timeout: float = 20):
        self.api = api
        self.timeout = timeout

    def list_sold_orders(self, page: int = 1, size: int = 20, query_code: str = "ALL") -> dict[str, Any]:
        return self._call(
            "mtop.taobao.idle.trade.merchant.sold.get",
            "1.0",
            {
                "pageNumber": page,
                "rowsPerPage": size,
                "orderIds": "",
                "queryCode": query_code,
                "orderSearchParam": "{}",
            },
            referer="https://seller.goofish.com/",
            seller=True,
            response_type="json",
            retry_token=True,
        )

    def order_detail(self, order_id: str) -> dict[str, Any]:
        return self._call(
            "mtop.idle.web.trade.order.detail",
            "1.0",
            {"tid": str(order_id)},
            spm="a21ybx.order-detail.0.0",
            retry_token=True,
        )

    def list_items(self, page: int = 1, size: int = 20) -> dict[str, Any]:
        result = self._call(
            "mtop.alibaba.idle.seller.pc.common.item.search",
            "1.0",
            {
                "pageNo": page,
                "pageSize": size,
                "bizType": "commonPro",
                "searchRequest": "{}",
            },
            referer="https://seller.goofish.com/?site=COMMONPRO#/seller-item/goods-manage",
            seller=True,
            response_type="json",
            retry_token=True,
            extra_params={"needLoginPC": "true", "showErrorToast": "true"},
        )
        data = result.get("data") or {}
        if data.get("code") != "success":
            raise XianyuPlatformError("查询商品列表", result)
        return result

    def confirm_delivery(self, order_id: str) -> dict[str, Any]:
        result = self._call(
            "mtop.taobao.idle.logistic.consign.dummy",
            "1.0",
            {"orderId": str(order_id), "tradeText": "", "picList": [], "newUnconsign": True},
            retry_token=False,
        )
        return result

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return self._call(
            "mtop.taobao.idle.trade.merchant.close.by.seller",
            "2.0",
            {
                "tid": str(order_id),
                "bizOrderId": str(order_id),
                "closeReason": _CLOSE_ORDER_REASON,
            },
            referer="https://seller.goofish.com/?site=COMMONPRO",
            seller=True,
            spm="a21107h.44911108.0.0",
            retry_token=False,
        )

    def offline_item(self, item_id: str) -> dict[str, Any]:
        return self._call(
            "mtop.alibaba.idle.seller.pc.item.batch.offline",
            "1.0",
            {"itemIds": str(item_id)},
            referer="https://seller.goofish.com/?site=COMMONPRO",
            seller=True,
            spm="a21107h.42826273.0.0",
            retry_token=False,
            extra_params={"needLoginPC": "true", "showErrorToast": "true"},
        )

    def change_price(self, order_id: str, amount: str) -> dict[str, Any]:
        raise UnsupportedXianyuOperation(
            "订单改价",
            {"ret": [f"UNSUPPORTED::尚未验证闲鱼改价协议, order={order_id}, amount={amount}"]},
        )

    def refund(self, order_id: str, amount: str, reason: str) -> dict[str, Any]:
        raise UnsupportedXianyuOperation(
            "主动退款",
            {"ret": [f"UNSUPPORTED::尚未验证卖家主动退款协议, order={order_id}"]},
        )

    def _call(
        self,
        api_name: str,
        version: str,
        payload: dict[str, Any],
        *,
        referer: str = "https://www.goofish.com/",
        seller: bool = False,
        spm: str | None = None,
        response_type: str = "originaljson",
        retry_token: bool = False,
        extra_params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        data_val = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        timestamp = str(int(time.time() * 1000))
        token = self.api.session.cookies.get("_m_h5_tk", "").split("_")[0]
        params = {
            "jsv": "2.7.2",
            "appKey": "34839810",
            "t": timestamp,
            "sign": generate_sign(timestamp, token, data_val),
            "v": version,
            "type": response_type,
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": api_name,
            "sessionOption": "AutoLoginOnly",
        }
        if spm:
            params["spm_cnt"] = spm
        if extra_params:
            params.update(extra_params)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": UA,
            "Origin": "https://seller.goofish.com" if seller else "https://www.goofish.com",
            "Referer": referer,
        }
        if seller:
            headers["idle_site_biz_code"] = "COMMONPRO"
        url = f"https://h5api.m.goofish.com/h5/{api_name}/{version}/"
        response = self.api.session.post(
            url, params=params, headers=headers, data={"data": data_val}, timeout=self.timeout
        )
        response.raise_for_status()
        result = response.json()
        if self._success(result):
            return result
        if retry_token and self._token_expired(result):
            self.api.refresh_token()
            return self._call(
                api_name,
                version,
                payload,
                referer=referer,
                seller=seller,
                spm=spm,
                response_type=response_type,
                retry_token=False,
                extra_params=extra_params,
            )
        raise XianyuPlatformError(api_name, result)

    @staticmethod
    def _success(result: dict[str, Any]) -> bool:
        return any("SUCCESS" in str(item) for item in result.get("ret", []))

    @staticmethod
    def _token_expired(result: dict[str, Any]) -> bool:
        joined = " ".join(map(str, result.get("ret", []))).upper()
        return "TOKEN" in joined and ("EXPIRED" in joined or "EXOIRED" in joined or "令牌过期" in joined)


def summarize_order_status(result: dict[str, Any]) -> dict[str, Any]:
    """从变化较频繁的订单详情结构中生成稳定状态摘要。"""
    strings: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str) and len(value) <= 200:
            strings.append(value)

    visit(result.get("data", result))
    text = " ".join(strings)
    upper = text.upper()
    status = "UNKNOWN"
    if any(word in text for word in ("退款成功", "已退款")) or any(
        word in upper for word in ("REFUND_SUCCESS", "REFUNDED")
    ):
        status = "REFUNDED"
    elif any(word in text for word in ("交易关闭", "订单关闭", "已取消")) or any(
        word in upper for word in ("TRADE_CLOSED", "CLOSED", "CANCELLED", "CANCELED")
    ):
        status = "CLOSED"
    elif any(word in text for word in ("交易成功", "已完成")) or any(
        word in upper for word in ("TRADE_FINISHED", "FINISHED", "COMPLETED")
    ):
        status = "FINISHED"
    elif any(word in text for word in ("已发货", "待收货")) or any(
        word in upper for word in ("WAIT_BUYER_CONFIRM_GOODS", "SHIPPED", "DELIVERED")
    ):
        status = "SHIPPED"
    elif any(word in text for word in ("已付款", "待发货", "等待卖家发货")) or any(
        word in upper for word in ("WAIT_SELLER_SEND_GOODS", "WAIT_SELLER_DELIVER", "PAID")
    ):
        status = "PAID"
    elif any(word in text for word in ("待付款", "等待买家付款")) or any(
        word in upper for word in ("WAIT_BUYER_PAY", "WAIT_PAYMENT", "UNPAID")
    ):
        status = "WAIT_PAYMENT"
    from .parser import XianyuMessageParser

    return {
        "status": status,
        "statusText": text[:1000],
        "paidAmount": XianyuMessageParser.find_paid_amount(result.get("data", result)),
    }
