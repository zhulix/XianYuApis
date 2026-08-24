from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from loguru import logger

from utils.goofish_utils import decrypt
from .events import XianyuEvent


_ORDER_PATTERNS = (
    re.compile(r"(?:bizOrderId|orderId|order_id|biz_order_id|tid)(?:=|%3D|[\"':])+(\d{10,})", re.I),
    re.compile(r"(?:order_detail\?id=|adjust_price\?.*?bizOrderId=)(\d{10,})", re.I),
)
_ITEM_PATTERNS = (
    re.compile(r"(?:itemId|item_id)[=\"':%3D]+(\d{6,})", re.I),
    re.compile(r"(?:item\?id=|item/)(\d{6,})", re.I),
)


class XianyuMessageParser:
    """将闲鱼同步包统一转换为稳定事件。

    未知卡片不丢弃：只要能拿到会话或消息标识，就以 CHAT_MESSAGE 交给上层。
    """

    def __init__(self, account_id: str):
        self.account_id = str(account_id)

    def parse_envelope(self, envelope: dict[str, Any]) -> list[XianyuEvent]:
        payloads = self._payloads(envelope)
        events: list[XianyuEvent] = []
        for payload in payloads:
            decoded = self.decode_payload(payload)
            if not isinstance(decoded, dict):
                continue
            event = self.parse_message(decoded)
            if event:
                events.append(event)
        return events

    def _payloads(self, envelope: dict[str, Any]) -> list[Any]:
        try:
            data = envelope["body"]["syncPushPackage"]["data"]
            return [entry.get("data") if isinstance(entry, dict) else entry for entry in data]
        except (KeyError, TypeError):
            return [envelope]

    def decode_payload(self, payload: Any) -> dict[str, Any] | None:
        if isinstance(payload, dict):
            return payload
        if not isinstance(payload, str) or not payload:
            return None
        for loader in (self._json, self._base64_json, self._decrypt_json):
            try:
                result = loader(payload)
                if isinstance(result, dict):
                    return result
            except Exception:
                continue
        logger.warning("[闲鱼消息解码失败] 账号：{}，长度：{}", self.account_id, len(payload))
        return None

    @staticmethod
    def _json(value: str) -> Any:
        return json.loads(value)

    @staticmethod
    def _base64_json(value: str) -> Any:
        return json.loads(base64.b64decode(value).decode("utf-8"))

    @staticmethod
    def _decrypt_json(value: str) -> Any:
        return json.loads(decrypt(value))

    def parse_message(self, message: dict[str, Any]) -> XianyuEvent | None:
        root = message.get("1") if isinstance(message.get("1"), dict) else message
        extension = root.get("10") if isinstance(root, dict) and isinstance(root.get("10"), dict) else {}
        if not extension and isinstance(message.get("4"), dict):
            extension = message["4"]

        chat_id = self._clean_id((root.get("2") if isinstance(root, dict) else None) or message.get("2"))
        buyer_id = self._clean_id(extension.get("senderUserId")) or self._nested_sender(root)
        buyer_name = extension.get("senderNick") or extension.get("reminderTitle")
        content, content_type = self._content(root, extension)
        message_id = self._find_named(message, {"messageId", "msg_id", "msgId"})
        item_id = self._find_named(message, {"itemId", "item_id"}) or self._regex_find(message, _ITEM_PATTERNS)
        order_id = self._find_named(message, {"bizOrderId", "orderId", "order_id", "tid"})
        order_id = order_id or self._regex_find(message, _ORDER_PATTERNS)
        paid_amount = self.find_paid_amount(message)
        occurred_at = self._timestamp(root, message)
        event_type = self._event_type(content, order_id, buyer_name)

        if not any((chat_id, message_id, item_id, order_id, content)):
            return None
        stable_id = message_id or self._stable_id(message, occurred_at)
        return XianyuEvent(
            event_id=f"{self.account_id}:{stable_id}",
            event_type=event_type,
            account_id=self.account_id,
            occurred_at=occurred_at,
            chat_id=chat_id,
            message_id=message_id,
            buyer_id=buyer_id,
            buyer_name=str(buyer_name) if buyer_name else None,
            item_id=str(item_id) if item_id else None,
            order_id=str(order_id) if order_id else None,
            paid_amount=paid_amount,
            content_type=content_type,
            content=content,
            raw_payload=message,
        )

    def _content(self, root: Any, extension: dict[str, Any]) -> tuple[str | None, str | None]:
        reminder = extension.get("reminderContent")
        candidates: list[Any] = []
        if isinstance(root, dict):
            content = root.get("6")
            content = content.get("3") if isinstance(content, dict) else None
            if isinstance(content, dict):
                candidates.extend((content.get("5"), content.get("1"), content.get("2")))
        for candidate in candidates:
            decoded = self._decode_content(candidate)
            if not decoded:
                continue
            images = list(self._find_urls(decoded))
            if images:
                return "\n".join(images), "image"
            text = self._find_named(decoded, {"text"})
            if text:
                return str(text), "text"
            card_text = self._card_text(decoded)
            if card_text:
                return card_text, "card"
        if reminder:
            marker = str(reminder)
            return marker, "image" if "[图片]" in marker else "text"
        return None, None

    def _decode_content(self, candidate: Any) -> Any:
        if isinstance(candidate, (dict, list)):
            return candidate
        if not isinstance(candidate, str) or not candidate:
            return None
        try:
            return json.loads(candidate)
        except Exception:
            try:
                return json.loads(base64.b64decode(candidate).decode("utf-8"))
            except Exception:
                return None

    def _event_type(self, content: str | None, order_id: str | None, sender: Any) -> str:
        # 订单状态已改由闲管家订单推送负责；聊天消息中的 order_id 仅作为
        # 会话上下文保留，不能再根据文案猜测订单状态。
        return "CHAT_MESSAGE"

    @staticmethod
    def _nested_sender(root: Any) -> str | None:
        if not isinstance(root, dict) or not isinstance(root.get("1"), dict):
            return None
        return XianyuMessageParser._clean_id(root["1"].get("1"))

    @staticmethod
    def _clean_id(value: Any) -> str | None:
        if value is None:
            return None
        result = str(value).split("@", 1)[0].strip()
        return result or None

    @staticmethod
    def _timestamp(root: Any, message: dict[str, Any]) -> int:
        value = root.get("5") if isinstance(root, dict) else message.get("5")
        try:
            number = int(value)
            return number if number > 10_000_000_000 else number * 1000
        except (TypeError, ValueError):
            return int(time.time() * 1000)

    @staticmethod
    def _stable_id(message: dict[str, Any], occurred_at: int) -> str:
        raw = json.dumps(message, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(raw.encode()).hexdigest()}:{occurred_at}"

    @classmethod
    def _find_named(cls, value: Any, names: set[str]) -> str | None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in names and item not in (None, ""):
                    return str(item)
                if isinstance(item, str) and key in {"bizTag", "extJson", "args", "metadata"}:
                    try:
                        found = cls._find_named(json.loads(item), names)
                        if found:
                            return found
                    except Exception:
                        pass
                found = cls._find_named(item, names)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = cls._find_named(item, names)
                if found:
                    return found
        return None

    @classmethod
    def find_paid_amount(cls, value: Any) -> str | None:
        """只接受明确标识为实付金额的人民币元字段，不从商品价格或文本猜测。"""
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = key.lower().replace("_", "")
                if normalized in {"paidamount", "actualpaidamount", "payamountyuan", "paidamountyuan"}:
                    amount = cls._decimal_yuan(item)
                    if amount is not None:
                        return amount
                found = cls.find_paid_amount(item)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = cls.find_paid_amount(item)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _decimal_yuan(value: Any) -> str | None:
        if isinstance(value, dict):
            value = value.get("value") or value.get("amount")
        if not isinstance(value, (str, float, Decimal)):
            return None
        text = str(value).strip().replace("¥", "").replace("￥", "")
        if not text or (isinstance(value, str) and "." not in text):
            return None
        try:
            amount = Decimal(text)
        except InvalidOperation:
            return None
        if amount < 0:
            return None
        return f"{amount.quantize(Decimal('0.01')):.2f}"

    @staticmethod
    def _regex_find(value: Any, patterns: Iterable[re.Pattern[str]]) -> str | None:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        for pattern in patterns:
            match = pattern.search(raw)
            if match:
                return match.group(1)
        return None

    @classmethod
    def _find_urls(cls, value: Any) -> Iterable[str]:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"url", "imageUrl", "image_url"} and isinstance(item, str) and item.startswith("http"):
                    yield item
                else:
                    yield from cls._find_urls(item)
        elif isinstance(value, list):
            for item in value:
                yield from cls._find_urls(item)

    @classmethod
    def _card_text(cls, value: Any) -> str | None:
        parts: list[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"title", "desc", "text", "content"} and isinstance(item, str):
                    parts.append(item)
                else:
                    nested = cls._card_text(item)
                    if nested:
                        parts.append(nested)
        elif isinstance(value, list):
            for item in value:
                nested = cls._card_text(item)
                if nested:
                    parts.append(nested)
        return " ".join(dict.fromkeys(parts)) or None
