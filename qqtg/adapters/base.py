"""Adapter interface.  Adapters translate platform events into
``UnifiedMessage`` objects and deliver ``OutgoingMessage`` objects."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ..media.processor import Prepared
from ..models import Media, SendResult, UnifiedMessage


@dataclass
class OutgoingMessage:
    chat_id: str
    text: str = ""  # already formatted, plain text
    html: str = ""  # Telegram: formatted HTML (optional)
    media: list[Prepared] = field(default_factory=list)
    reply_to_message_id: Optional[str] = None
    # Text to send when there is no media (or media failed).  Adapters send
    # ``text`` as the caption of the first media item when possible.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatInfo:
    chat_id: str
    title: str
    chat_type: str = "group"
    member_count: Optional[int] = None


@dataclass
class PermissionReport:
    ok: bool
    present: bool = True
    muted: bool = False
    checks: dict[str, bool] = field(default_factory=dict)  # e.g. {"send_text": True, ...}
    reason: str = ""
    status: str = "authorized"  # suggested chat status: authorized | left | limited | error


MessageHandler = Callable[[UnifiedMessage], Awaitable[None]]
EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class BaseAdapter:
    platform: str = ""

    def __init__(self) -> None:
        self.on_message: Optional[MessageHandler] = None
        self.on_event: Optional[EventHandler] = None  # recall / member events / chat status
        self.connected: bool = False
        self.self_id: str = ""
        self.self_name: str = ""
        self.last_error: str = ""
        self.last_activity: float = 0.0
        self.connected_since: float = 0.0
        self.reconnects: int = 0

    # lifecycle
    async def start(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def stop(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    # capabilities
    async def list_chats(self) -> list[ChatInfo]:  # pragma: no cover - interface
        return []

    async def check_permissions(self, chat_id: str) -> PermissionReport:  # pragma: no cover
        return PermissionReport(ok=self.connected)

    async def send(self, msg: OutgoingMessage) -> SendResult:  # pragma: no cover - interface
        raise NotImplementedError

    async def delete_message(self, chat_id: str, message_id: str) -> bool:  # pragma: no cover
        return False

    async def download(self, media: Media, dest_dir: Path) -> Path:  # pragma: no cover - interface
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "connected": self.connected,
            "self_id": self.self_id,
            "self_name": self.self_name,
            "last_error": self.last_error,
            "last_activity": self.last_activity,
            "connected_since": self.connected_since,
            "reconnects": self.reconnects,
            "uptime": (time.time() - self.connected_since) if self.connected and self.connected_since else 0,
        }
