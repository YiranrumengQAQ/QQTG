"""Unified message model shared by every adapter.

Adapters never talk to each other.  They only produce / consume
``UnifiedMessage`` objects; the bridge core does routing, loop protection,
media conversion and formatting in between.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

PLATFORM_QQ = "qq"
PLATFORM_TG = "telegram"

PLATFORM_LABEL = {PLATFORM_QQ: "QQ", PLATFORM_TG: "TG"}


class MediaKind(str, Enum):
    PHOTO = "photo"
    ANIMATION = "animation"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"
    STICKER = "sticker"


# Order matters: text is highest priority in the queue, big files lowest.
KIND_PRIORITY = {
    "text": 0,
    MediaKind.PHOTO.value: 1,
    MediaKind.STICKER.value: 1,
    MediaKind.ANIMATION.value: 2,
    MediaKind.VOICE.value: 2,
    MediaKind.AUDIO.value: 3,
    MediaKind.VIDEO.value: 3,
    MediaKind.DOCUMENT.value: 4,
}


@dataclass
class Sender:
    id: str
    name: str
    platform: str
    is_bot: bool = False


@dataclass
class Media:
    kind: MediaKind
    # Exactly one of the following is usually present when the message is
    # produced by an adapter.  The media processor resolves it to ``path``.
    url: Optional[str] = None
    file_id: Optional[str] = None  # platform specific reference (TG file_id / QQ file id)
    path: Optional[str] = None  # local temp path after download
    mime: Optional[str] = None
    size: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[float] = None
    filename: Optional[str] = None
    thumbnail_path: Optional[str] = None
    emoji: Optional[str] = None  # sticker emoji
    is_animated: bool = False  # tgs sticker
    is_video: bool = False  # webm sticker
    fallback_text: Optional[str] = None  # used when media cannot be delivered
    extra: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        labels = {
            MediaKind.PHOTO: "图片",
            MediaKind.ANIMATION: "动画",
            MediaKind.VIDEO: "视频",
            MediaKind.AUDIO: "音频",
            MediaKind.VOICE: "语音",
            MediaKind.DOCUMENT: "文件",
            MediaKind.STICKER: "贴纸",
        }
        return labels.get(self.kind, "媒体")


@dataclass
class Reply:
    message_id: str
    sender_name: Optional[str] = None
    text_preview: Optional[str] = None


@dataclass
class Forward:
    source_name: str
    source_type: str = ""  # user / channel / chat / hidden


@dataclass
class UnifiedMessage:
    platform: str
    chat_id: str
    message_id: str
    sender: Sender
    text: str = ""
    media: list[Media] = field(default_factory=list)
    reply: Optional[Reply] = None
    forward: Optional[Forward] = None
    chat_title: str = ""
    timestamp: float = field(default_factory=time.time)
    is_command: bool = False
    event: Optional[str] = None  # e.g. "member_join" for group events
    raw: Any = None

    @property
    def kind(self) -> str:
        if self.media:
            return self.media[0].kind.value
        return "text"

    @property
    def priority(self) -> int:
        return KIND_PRIORITY.get(self.kind, 3)

    def summary(self, limit: int = 80) -> str:
        parts: list[str] = []
        if self.text:
            parts.append(self.text.replace("\n", " "))
        for m in self.media:
            parts.append(f"[{m.describe()}]")
        s = " ".join(parts) or "(空消息)"
        return s if len(s) <= limit else s[: limit - 1] + "…"


@dataclass
class SendResult:
    ok: bool
    message_ids: list[str] = field(default_factory=list)
    error_code: str = ""
    error: str = ""
    retry_after: Optional[float] = None
    file_refs: dict[str, str] = field(default_factory=dict)  # media hash -> platform file ref
    permanent: bool = False  # do not retry


class BridgeError(Exception):
    def __init__(self, code: str, message: str, permanent: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.permanent = permanent
        self.retry_after = retry_after
