from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class XianyuEvent:
    event_id: str
    event_type: str
    account_id: str
    occurred_at: int
    chat_id: str | None = None
    message_id: str | None = None
    buyer_id: str | None = None
    buyer_name: str | None = None
    item_id: str | None = None
    order_id: str | None = None
    paid_amount: str | None = None
    content_type: str | None = None
    content: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
