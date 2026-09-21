"""Render a ``UnifiedMessage`` for the target platform.

Three display modes (per bridge, default from global settings)::

    simple    张三
              你好

    standard  TG · 张三

              你好

    full      TG · Minecraft 玩家群 · 张三
              2026-09-21 11:32

              你好
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from ..models import UnifiedMessage, platform_label


@dataclass
class Rendered:
    text: str  # plain text
    html: str  # HTML (Telegram)


def _time_str(ts: float, tz_name: str) -> str:
    try:
        tz = ZoneInfo(tz_name or "Asia/Shanghai")
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M")


def _header(msg: UnifiedMessage, mode: str, tz_name: str) -> list[str]:
    label = platform_label(msg.platform)
    name = msg.sender.name or msg.sender.id
    if mode == "simple":
        return [name]
    if mode == "full":
        parts = [label]
        if msg.chat_title:
            parts.append(msg.chat_title)
        parts.append(name)
        return [" · ".join(parts), _time_str(msg.timestamp, tz_name)]
    return [f"{label} · {name}"]


def render(msg: UnifiedMessage, mode: str = "standard", tz_name: str = "Asia/Shanghai", include_reply_preview: bool = True,
           body_override: str | None = None) -> Rendered:
    body = msg.text if body_override is None else body_override
    if msg.event:
        label = platform_label(msg.platform)
        text = f"{label} · 系统\n{body}" if mode != "simple" else body
        return Rendered(text=text, html=f"<i>{html.escape(text)}</i>")

    header_lines = _header(msg, mode, tz_name)
    meta_lines: list[str] = []
    if msg.forward:
        meta_lines.append(f"[转发自 {msg.forward.source_name}]")
    if msg.reply and include_reply_preview:
        who = msg.reply.sender_name or ""
        preview = (msg.reply.text_preview or "").replace("\n", " ")
        if who or preview:
            meta_lines.append(f"↪ {who}{'：' if who and preview else ''}{preview}")

    plain_parts = ["\n".join(header_lines)]
    html_parts = ["<b>" + html.escape(header_lines[0]) + "</b>" + ("\n" + html.escape("\n".join(header_lines[1:])) if len(header_lines) > 1 else "")]
    if meta_lines:
        plain_parts.append("\n".join(meta_lines))
        html_parts.append("<i>" + html.escape("\n".join(meta_lines)) + "</i>")
    if body:
        sep = "\n" if mode == "simple" else "\n\n"
        plain = "\n".join(plain_parts) + sep + body
        htm = "\n".join(html_parts) + sep + html.escape(body)
    else:
        plain = "\n".join(plain_parts)
        htm = "\n".join(html_parts)
    return Rendered(text=plain, html=htm)


def render_edit(msg: UnifiedMessage, mode: str, tz_name: str) -> Rendered:
    return render(msg, mode, tz_name, include_reply_preview=False, body_override="[消息已修改]\n" + (msg.text or msg.summary()))
