"""Telegram Bot API adapter (long polling, thin HTTP client, no framework).

Design notes
------------
* One adapter instance == one bot token.  It may serve any number of chats.
* Outgoing traffic goes through a global + per-chat token bucket and honours
  ``retry_after`` from 429 responses.
* File uploads return ``file_id`` values which the engine stores in the media
  cache so identical content is never uploaded twice.
"""
from __future__ import annotations

import asyncio
import html as html_mod
import json
import shutil
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from ..logsys import get_logger
from ..media.detect import safe_filename
from ..media.processor import Prepared
from ..models import PLATFORM_TG, BridgeError, Forward, Media, MediaKind, Reply, Sender, SendResult, UnifiedMessage
from ..core.ratelimit import ChatRateLimiter
from .base import BaseAdapter, ChatInfo, OutgoingMessage, PermissionReport

log = get_logger("conn")
mlog = get_logger("message")

TG_TEXT_LIMIT = 4096
TG_CAPTION_LIMIT = 1024


class TelegramAdapter(BaseAdapter):
    platform = PLATFORM_TG

    def __init__(self, token: str, api_base: str = "https://api.telegram.org", download_limit_mb: int = 20,
                 rate_global_per_sec: float = 25, rate_chat_per_min: float = 20):
        super().__init__()
        self.token = token.strip()
        self.api_base = (api_base or "https://api.telegram.org").rstrip("/")
        self.download_limit_mb = download_limit_mb
        self._client: Optional[httpx.AsyncClient] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._stopping = False
        self._offset: Optional[int] = None
        self.limiter = ChatRateLimiter(rate_global_per_sec, rate_chat_per_min / 60.0, chat_burst=5)
        self.bridge_other_bots = False
        self.chats_seen: dict[str, ChatInfo] = {}
        self.polling_ok = False

    # ------------------------------------------------------------------ HTTP
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True)
        return self._client

    def _url(self, method: str) -> str:
        return f"{self.api_base}/bot{self.token}/{method}"

    async def api(self, method: str, params: Optional[dict[str, Any]] = None, files: Optional[dict[str, Any]] = None,
                  timeout: Optional[float] = None) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            if files:
                data = {k: (json.dumps(v) if isinstance(v, (dict, list, bool)) else str(v)) for k, v in params.items()}
                resp = await self.client.post(self._url(method), data=data, files=files, timeout=timeout or 300.0)
            else:
                resp = await self.client.post(self._url(method), json=params, timeout=timeout or 60.0)
        except httpx.HTTPError as exc:
            raise BridgeError("NETWORK", f"Telegram 网络错误: {type(exc).__name__}") from exc
        try:
            payload = resp.json()
        except ValueError:
            raise BridgeError("BAD_RESPONSE", f"Telegram 返回异常 (HTTP {resp.status_code})")
        if payload.get("ok"):
            return payload.get("result")
        code = int(payload.get("error_code", resp.status_code))
        desc = str(payload.get("description", "unknown error"))
        retry_after = (payload.get("parameters") or {}).get("retry_after")
        raise BridgeError(*classify_tg_error(code, desc), retry_after=float(retry_after) if retry_after else None)

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self._stopping = False
        me = await self.api("getMe", timeout=20)
        self.self_id = str(me["id"])
        self.self_name = me.get("username") or me.get("first_name", "bot")
        self.connected = True
        self.connected_since = time.time()
        self.last_error = ""
        log.info("Telegram Bot 已连接: @%s", self.self_name)
        self._poll_task = asyncio.create_task(self._poll_loop(), name="tg-poll")

    async def stop(self) -> None:
        self._stopping = True
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._client:
            await self._client.aclose()
            self._client = None
        self.connected = False

    async def _poll_loop(self) -> None:
        backoff = 1.0
        webhook_cleared = False
        while not self._stopping:
            try:
                updates = await self.api(
                    "getUpdates",
                    {
                        "offset": self._offset,
                        "timeout": 30,
                        "allowed_updates": ["message", "edited_message", "channel_post", "my_chat_member"],
                    },
                    timeout=45,
                )
                backoff = 1.0
                if not self.connected:
                    self.connected = True
                    self.connected_since = time.time()
                    self.reconnects += 1
                    log.info("Telegram 已重新连接")
                self.polling_ok = True
                self.last_activity = time.time()
                for upd in updates or []:
                    self._offset = int(upd["update_id"]) + 1
                    try:
                        await self._handle_update(upd)
                    except Exception:
                        log.exception("处理 Telegram 更新失败")
            except asyncio.CancelledError:
                raise
            except BridgeError as exc:
                self.polling_ok = False
                self.last_error = exc.message
                if exc.code == "UNAUTHORIZED":
                    self.connected = False
                    log.error("Telegram Token 无效，停止轮询")
                    return
                if exc.code == "CONFLICT" and not webhook_cleared:
                    webhook_cleared = True
                    try:
                        await self.api("deleteWebhook", {"drop_pending_updates": False})
                        log.warning("检测到 Webhook 冲突，已删除 Webhook")
                        continue
                    except BridgeError:
                        pass
                if self.connected:
                    log.warning("Telegram 轮询错误: %s，%ds 后重试", exc.message, int(backoff))
                self.connected = False
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            except Exception as exc:
                self.polling_ok = False
                self.last_error = str(exc)
                self.connected = False
                log.warning("Telegram 轮询异常: %s，%ds 后重试", exc, int(backoff))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    # --------------------------------------------------------------- updates
    async def _handle_update(self, upd: dict[str, Any]) -> None:
        if "my_chat_member" in upd:
            await self._handle_my_chat_member(upd["my_chat_member"])
            return
        edited = False
        msg = upd.get("message") or upd.get("channel_post")
        if msg is None and "edited_message" in upd:
            msg = upd["edited_message"]
            edited = True
        if not msg:
            return
        chat = msg.get("chat", {})
        if chat.get("type") not in ("group", "supergroup", "channel"):
            # private chats: only answer /start with a hint
            if chat.get("type") == "private" and (msg.get("text") or "").startswith("/start"):
                await self._safe_send_text(str(chat["id"]), "这是一个群组桥接机器人。\n请把我添加到群组，然后在群里发送 /bridge。")
            return
        info = ChatInfo(str(chat["id"]), chat.get("title") or str(chat["id"]), chat.get("type", "group"))
        self.chats_seen[info.chat_id] = info
        if self.on_event:
            await self.on_event("chat_seen", {"platform": PLATFORM_TG, "chat": info})

        unified = self.parse_message(msg)
        if unified is None:
            return
        if edited:
            if self.on_event:
                await self.on_event("edit", {"platform": PLATFORM_TG, "message": unified})
            return
        if self.on_message:
            await self.on_message(unified)

    async def _handle_my_chat_member(self, ev: dict[str, Any]) -> None:
        chat = ev.get("chat", {})
        new = ev.get("new_chat_member", {})
        status = new.get("status")
        info = ChatInfo(str(chat["id"]), chat.get("title") or str(chat["id"]), chat.get("type", "group"))
        self.chats_seen[info.chat_id] = info
        if self.on_event:
            await self.on_event("chat_seen", {"platform": PLATFORM_TG, "chat": info})
            if status in ("left", "kicked"):
                await self.on_event("bot_left", {"platform": PLATFORM_TG, "chat_id": info.chat_id, "reason": "机器人已被移出群组"})
            elif status == "restricted" and not new.get("can_send_messages", True):
                await self.on_event("bot_limited", {"platform": PLATFORM_TG, "chat_id": info.chat_id, "reason": "机器人被限制发言"})
            else:
                await self.on_event("bot_joined", {"platform": PLATFORM_TG, "chat_id": info.chat_id, "chat": info})

    # --------------------------------------------------------------- parsing
    def parse_message(self, msg: dict[str, Any]) -> Optional[UnifiedMessage]:
        chat = msg.get("chat", {})
        frm = msg.get("from") or {}
        sender_chat = msg.get("sender_chat")
        if frm:
            name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) or frm.get("username") or str(frm.get("id"))
            sender = Sender(id=str(frm.get("id")), name=name, platform=PLATFORM_TG, is_bot=bool(frm.get("is_bot")))
        elif sender_chat:
            sender = Sender(id=str(sender_chat.get("id")), name=sender_chat.get("title") or "匿名", platform=PLATFORM_TG)
        else:
            sender = Sender(id="0", name="未知", platform=PLATFORM_TG)

        text = msg.get("text") or msg.get("caption") or ""
        entities = msg.get("entities") or msg.get("caption_entities") or []
        text = _apply_entities(text, entities)
        is_command = text.startswith("/") or any(e.get("type") == "bot_command" and e.get("offset") == 0 for e in entities)

        media: list[Media] = []
        if msg.get("photo"):
            best = msg["photo"][-1]
            media.append(Media(kind=MediaKind.PHOTO, file_id=best["file_id"], size=best.get("file_size"),
                               width=best.get("width"), height=best.get("height"), mime="image/jpeg",
                               filename="photo.jpg", extra={"prehash": "tg:" + best.get("file_unique_id", best["file_id"])}))
        elif msg.get("animation"):
            a = msg["animation"]
            media.append(Media(kind=MediaKind.ANIMATION, file_id=a["file_id"], size=a.get("file_size"), width=a.get("width"),
                               height=a.get("height"), duration=a.get("duration"), mime=a.get("mime_type"),
                               filename=a.get("file_name") or "animation.mp4", extra={"prehash": "tg:" + a.get("file_unique_id", a["file_id"])}))
        elif msg.get("video"):
            v = msg["video"]
            media.append(Media(kind=MediaKind.VIDEO, file_id=v["file_id"], size=v.get("file_size"), width=v.get("width"),
                               height=v.get("height"), duration=v.get("duration"), mime=v.get("mime_type"),
                               filename=v.get("file_name") or "video.mp4", extra={"prehash": "tg:" + v.get("file_unique_id", v["file_id"])}))
        elif msg.get("video_note"):
            v = msg["video_note"]
            media.append(Media(kind=MediaKind.VIDEO, file_id=v["file_id"], size=v.get("file_size"), width=v.get("length"),
                               height=v.get("length"), duration=v.get("duration"), mime="video/mp4", filename="video_note.mp4",
                               extra={"prehash": "tg:" + v.get("file_unique_id", v["file_id"])}))
        elif msg.get("audio"):
            a = msg["audio"]
            media.append(Media(kind=MediaKind.AUDIO, file_id=a["file_id"], size=a.get("file_size"), duration=a.get("duration"),
                               mime=a.get("mime_type"), filename=a.get("file_name") or _audio_name(a),
                               extra={"prehash": "tg:" + a.get("file_unique_id", a["file_id"])}))
        elif msg.get("voice"):
            v = msg["voice"]
            media.append(Media(kind=MediaKind.VOICE, file_id=v["file_id"], size=v.get("file_size"), duration=v.get("duration"),
                               mime=v.get("mime_type") or "audio/ogg", filename="voice.ogg",
                               extra={"prehash": "tg:" + v.get("file_unique_id", v["file_id"])}))
        elif msg.get("sticker"):
            s = msg["sticker"]
            thumb = s.get("thumbnail") or s.get("thumb")
            m = Media(kind=MediaKind.STICKER, file_id=s["file_id"], size=s.get("file_size"), width=s.get("width"),
                      height=s.get("height"), emoji=s.get("emoji"), is_animated=bool(s.get("is_animated")),
                      is_video=bool(s.get("is_video")), mime="video/webm" if s.get("is_video") else "image/webp",
                      filename="sticker.webm" if s.get("is_video") else ("sticker.tgs" if s.get("is_animated") else "sticker.webp"),
                      extra={"prehash": "tg:" + s.get("file_unique_id", s["file_id"])})
            if thumb:
                m.extra["thumb_file_id"] = thumb.get("file_id")
            media.append(m)
        elif msg.get("document"):
            d = msg["document"]
            media.append(Media(kind=MediaKind.DOCUMENT, file_id=d["file_id"], size=d.get("file_size"), mime=d.get("mime_type"),
                               filename=safe_filename(d.get("file_name"), "file"),
                               extra={"prehash": "tg:" + d.get("file_unique_id", d["file_id"])}))

        event = None
        if msg.get("new_chat_members"):
            names = ", ".join(_user_name(u) for u in msg["new_chat_members"])
            event, text = "member_join", f"{names} 加入了群组"
        elif msg.get("left_chat_member"):
            event, text = "member_leave", f"{_user_name(msg['left_chat_member'])} 离开了群组"
        elif msg.get("new_chat_title"):
            event, text = "chat_title", f"群名称已修改为「{msg['new_chat_title']}」"
        elif msg.get("pinned_message"):
            event, text = "pinned", "置顶了一条消息"
        elif not text and not media:
            if msg.get("poll"):
                text = f"[投票] {msg['poll'].get('question', '')}"
            elif msg.get("location"):
                loc = msg["location"]
                text = f"[位置] {loc.get('latitude')}, {loc.get('longitude')}"
            elif msg.get("contact"):
                c = msg["contact"]
                text = f"[联系人] {c.get('first_name', '')} {c.get('phone_number', '')}".strip()
            elif msg.get("dice"):
                text = f"[骰子 {msg['dice'].get('emoji', '')}] {msg['dice'].get('value', '')}"
            else:
                return None  # unsupported service message

        reply = None
        r = msg.get("reply_to_message")
        if r and not (msg.get("is_topic_message") and r.get("message_id") == msg.get("message_thread_id")):
            preview = r.get("text") or r.get("caption") or _media_label(r)
            r_from = r.get("from") or {}
            reply = Reply(message_id=str(r["message_id"]), sender_name=_user_name(r_from) if r_from else (r.get("sender_chat") or {}).get("title"),
                          text_preview=(preview or "")[:60])

        forward = None
        origin = msg.get("forward_origin")
        if origin:
            t = origin.get("type")
            if t == "user":
                forward = Forward(_user_name(origin.get("sender_user", {})), "user")
            elif t == "hidden_user":
                forward = Forward(origin.get("sender_user_name", "隐藏用户"), "hidden")
            elif t == "chat":
                forward = Forward((origin.get("sender_chat") or {}).get("title", "群组"), "chat")
            elif t == "channel":
                forward = Forward((origin.get("chat") or {}).get("title", "频道"), "channel")
        elif msg.get("forward_from"):
            forward = Forward(_user_name(msg["forward_from"]), "user")
        elif msg.get("forward_from_chat"):
            forward = Forward(msg["forward_from_chat"].get("title", "频道"), "channel")
        elif msg.get("forward_sender_name"):
            forward = Forward(msg["forward_sender_name"], "hidden")

        return UnifiedMessage(
            platform=PLATFORM_TG,
            chat_id=str(chat["id"]),
            chat_title=chat.get("title") or "",
            message_id=str(msg["message_id"]),
            sender=sender,
            text=text,
            media=media,
            reply=reply,
            forward=forward,
            timestamp=float(msg.get("date") or time.time()),
            is_command=is_command,
            event=event,
            raw=msg,
        )

    # ----------------------------------------------------------- capabilities
    async def list_chats(self) -> list[ChatInfo]:
        return list(self.chats_seen.values())

    async def get_chat(self, chat_id: str) -> Optional[ChatInfo]:
        try:
            c = await self.api("getChat", {"chat_id": chat_id}, timeout=20)
        except BridgeError:
            return None
        info = ChatInfo(str(c["id"]), c.get("title") or str(c["id"]), c.get("type", "group"))
        try:
            info.member_count = int(await self.api("getChatMemberCount", {"chat_id": chat_id}, timeout=20))
        except BridgeError:
            pass
        self.chats_seen[info.chat_id] = info
        return info

    async def check_permissions(self, chat_id: str) -> PermissionReport:
        if not self.connected:
            return PermissionReport(ok=False, present=False, reason="Telegram 未连接", status="error")
        try:
            chat = await self.api("getChat", {"chat_id": chat_id}, timeout=20)
            member = await self.api("getChatMember", {"chat_id": chat_id, "user_id": int(self.self_id)}, timeout=20)
        except BridgeError as exc:
            if exc.code in ("CHAT_NOT_FOUND", "CHAT_FORBIDDEN"):
                return PermissionReport(ok=False, present=False, reason="机器人不在该群组", status="left")
            return PermissionReport(ok=False, present=False, reason=exc.message, status="error")
        status = member.get("status")
        checks: dict[str, bool] = {}
        keys = {
            "send_text": "can_send_messages", "send_photo": "can_send_photos", "send_video": "can_send_videos",
            "send_audio": "can_send_audios", "send_voice": "can_send_voice_notes", "send_document": "can_send_documents",
            "send_other": "can_send_other_messages",
        }
        if status in ("left", "kicked"):
            return PermissionReport(ok=False, present=False, reason="机器人已离开或被移出群组", status="left")
        if status in ("creator", "administrator"):
            if chat.get("type") == "channel":
                can_post = bool(member.get("can_post_messages", status == "creator"))
                checks = {k: can_post for k in keys}
            else:
                checks = {k: True for k in keys}
            checks["delete_messages"] = bool(member.get("can_delete_messages", status == "creator"))
        elif status == "restricted":
            checks = {k: bool(member.get(v, False)) for k, v in keys.items()}
            checks["delete_messages"] = False
        else:  # member
            perms = chat.get("permissions") or {}
            checks = {k: bool(perms.get(v, True)) for k, v in keys.items()}
            checks["delete_messages"] = False
        required = ["send_text"]
        ok = all(checks.get(k, False) for k in required)
        missing = [k for k, v in checks.items() if not v and k != "delete_messages"]
        reason = "" if not missing else "缺少权限: " + ", ".join(missing)
        return PermissionReport(ok=ok, present=True, muted=not checks.get("send_text", False), checks=checks,
                                reason=reason, status="authorized" if ok else "limited")

    # ---------------------------------------------------------------- sending
    async def _safe_send_text(self, chat_id: str, text: str, reply_to: Optional[str] = None) -> None:
        try:
            await self.send(OutgoingMessage(chat_id=chat_id, text=text, reply_to_message_id=reply_to))
        except Exception as exc:
            log.debug("send hint failed: %s", exc)

    def _reply_params(self, msg: OutgoingMessage) -> dict[str, Any]:
        if not msg.reply_to_message_id:
            return {}
        try:
            mid = int(msg.reply_to_message_id)
        except ValueError:
            return {}
        return {"reply_parameters": {"message_id": mid, "allow_sending_without_reply": True}}

    async def send(self, msg: OutgoingMessage) -> SendResult:
        result = SendResult(ok=True)
        html = msg.html or html_mod.escape(msg.text or "")
        deliverable = [m for m in msg.media if m.deliverable]
        fallbacks = [m.fallback_text for m in msg.media if not m.deliverable and m.fallback_text]
        if fallbacks:
            extra = "\n\n".join(html_mod.escape(t) for t in fallbacks)
            html = (html + "\n\n" + extra) if html.strip() else extra

        try:
            if not deliverable:
                if not html.strip():
                    return SendResult(ok=False, error_code="EMPTY", error="空消息", permanent=True)
                for chunk in _split_html(html, TG_TEXT_LIMIT):
                    r = await self._call_send("sendMessage", msg.chat_id, {"text": chunk, "parse_mode": "HTML", **self._reply_params(msg)})
                    result.message_ids.append(str(r["message_id"]))
                return result

            caption_html: Optional[str] = html if html.strip() else None
            if caption_html and len(caption_html) > TG_CAPTION_LIMIT:
                for chunk in _split_html(caption_html, TG_TEXT_LIMIT):
                    r = await self._call_send("sendMessage", msg.chat_id, {"text": chunk, "parse_mode": "HTML", **self._reply_params(msg)})
                    result.message_ids.append(str(r["message_id"]))
                caption_html = None

            first = True
            for prepared in deliverable:
                params: dict[str, Any] = {}
                if first:
                    params.update(self._reply_params(msg))
                    if caption_html:
                        params.update({"caption": caption_html, "parse_mode": "HTML"})
                first = False
                r, file_ref = await self._send_media(msg.chat_id, prepared, params)
                result.message_ids.append(str(r["message_id"]))
                if file_ref:
                    for key in prepared.hash_keys:
                        result.file_refs[f"{prepared.kind.value}|{key}"] = file_ref
            return result
        except BridgeError as exc:
            return SendResult(ok=False, message_ids=result.message_ids, error_code=exc.code, error=exc.message,
                              retry_after=exc.retry_after, permanent=exc.permanent, file_refs=result.file_refs)

    async def _call_send(self, method: str, chat_id: str, params: dict[str, Any], files: Optional[dict[str, Any]] = None) -> Any:
        await self.limiter.acquire(chat_id)
        params = {"chat_id": chat_id, **params}
        try:
            return await self.api(method, params, files=files)
        except BridgeError as exc:
            if exc.code == "RATE_LIMIT" and exc.retry_after:
                self.limiter.hold(chat_id, exc.retry_after)
            raise

    async def _send_media(self, chat_id: str, p: Prepared, params: dict[str, Any]) -> tuple[Any, Optional[str]]:
        method, field = {
            MediaKind.PHOTO: ("sendPhoto", "photo"),
            MediaKind.ANIMATION: ("sendAnimation", "animation"),
            MediaKind.VIDEO: ("sendVideo", "video"),
            MediaKind.AUDIO: ("sendAudio", "audio"),
            MediaKind.VOICE: ("sendVoice", "voice"),
            MediaKind.DOCUMENT: ("sendDocument", "document"),
            MediaKind.STICKER: ("sendPhoto", "photo"),
        }[p.kind]
        if p.kind == MediaKind.VIDEO:
            params.update({"width": p.width, "height": p.height, "duration": int(p.duration) if p.duration else None, "supports_streaming": True})
        elif p.kind == MediaKind.ANIMATION:
            params.update({"width": p.width, "height": p.height, "duration": int(p.duration) if p.duration else None})
        elif p.kind == MediaKind.AUDIO:
            params.update({"duration": int(p.duration) if p.duration else None, "title": Path(p.filename or "audio").stem[:64]})
        elif p.kind == MediaKind.VOICE:
            params.update({"duration": int(p.duration) if p.duration else None})

        if p.file_ref and not p.path:
            try:
                r = await self._call_send(method, chat_id, {**params, field: p.file_ref})
                return r, _extract_file_id(r, field)
            except BridgeError as exc:
                if exc.code == "FILE_ID_INVALID":
                    raise BridgeError("FILE_ID_INVALID", "缓存的 file_id 已失效，将重新上传", permanent=False)
                raise

        assert p.path
        files: dict[str, Any] = {}
        handles = []
        try:
            fh = open(p.path, "rb")
            handles.append(fh)
            files[field] = (safe_filename(p.filename, "file"), fh, p.mime or "application/octet-stream")
            if p.thumbnail_path and p.kind in (MediaKind.VIDEO, MediaKind.ANIMATION, MediaKind.DOCUMENT, MediaKind.AUDIO):
                th = open(p.thumbnail_path, "rb")
                handles.append(th)
                files["thumbnail"] = ("thumb.jpg", th, "image/jpeg")
                params["thumbnail"] = "attach://thumbnail"
            r = await self._call_send(method, chat_id, params, files=files)
        finally:
            for h in handles:
                try:
                    h.close()
                except Exception:
                    pass
        return r, _extract_file_id(r, field)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        try:
            await self.api("deleteMessage", {"chat_id": chat_id, "message_id": int(message_id)}, timeout=20)
            return True
        except (BridgeError, ValueError) as exc:
            log.debug("deleteMessage failed: %s", exc)
            return False

    # --------------------------------------------------------------- download
    async def download(self, media: Media, dest_dir: Path) -> Path:
        file_id = media.file_id
        if media.kind == MediaKind.STICKER and media.is_animated:
            # tgs: download the static thumbnail instead (lottie cannot be rendered here)
            thumb = media.extra.get("thumb_file_id")
            if not thumb:
                raise BridgeError("STICKER_TGS", "动画贴纸无预览图，无法转换", permanent=True)
            file_id = thumb
            media.is_animated = False
            media.filename = "sticker.webp"
            media.mime = "image/webp"
        if not file_id:
            raise BridgeError("NO_FILE", "消息没有可下载的媒体", permanent=True)
        if media.size and media.size > self.download_limit_mb * 1024 * 1024:
            raise BridgeError("FILE_TOO_LARGE", f"文件超过 Telegram Bot 下载上限 ({self.download_limit_mb} MB)", permanent=True)
        try:
            info = await self.api("getFile", {"file_id": file_id}, timeout=30)
        except BridgeError as exc:
            if "too big" in exc.message.lower():
                raise BridgeError("FILE_TOO_LARGE", f"文件超过 Telegram Bot 下载上限 ({self.download_limit_mb} MB)", permanent=True)
            raise
        file_path = info.get("file_path")
        if not file_path:
            raise BridgeError("NO_FILE", "Telegram 未返回文件路径", permanent=True)
        name = safe_filename(media.filename or Path(file_path).name, "file")
        dest = dest_dir / f"src_{name}"
        local = Path(file_path)
        if local.is_absolute() and local.is_file():  # local Bot API server
            shutil.copyfile(local, dest)
            return dest
        url = f"{self.api_base}/file/bot{self.token}/{file_path}"
        try:
            async with self.client.stream("GET", url, timeout=300.0) as resp:
                if resp.status_code != 200:
                    raise BridgeError("DOWNLOAD_FAILED", f"Telegram 文件下载失败 (HTTP {resp.status_code})")
                with open(dest, "wb") as fh:
                    async for chunk in resp.aiter_bytes(1024 * 256):
                        fh.write(chunk)
        except httpx.HTTPError as exc:
            raise BridgeError("NETWORK", f"Telegram 文件下载网络错误: {type(exc).__name__}") from exc
        return dest


# ---------------------------------------------------------------- helpers

def classify_tg_error(code: int, desc: str) -> tuple[str, str, bool]:
    d = desc.lower()
    if code == 401:
        return "UNAUTHORIZED", "Telegram Bot Token 无效", True
    if code == 409:
        return "CONFLICT", "另一个实例正在使用此 Bot（或设置了 Webhook）", False
    if code == 429:
        return "RATE_LIMIT", "Telegram 限流，稍后重试", False
    if code == 413 or "entity too large" in d or "file is too big" in d:
        return "FILE_TOO_LARGE", "文件超过 Telegram Bot 允许的大小", True
    if code == 403:
        return "CHAT_FORBIDDEN", "机器人被移出群组或无发言权限", True
    if "chat not found" in d:
        return "CHAT_NOT_FOUND", "找不到目标群组（机器人可能未加入）", True
    if "not enough rights" in d or "have no rights" in d:
        return "NO_PERMISSION", "机器人在目标群组权限不足", True
    if "wrong file identifier" in d or "file_id" in d or "wrong remote file" in d or "file reference" in d:
        return "FILE_ID_INVALID", "文件标识无效", False
    if "wrong type of the web page content" in d or "failed to get http url content" in d:
        return "BAD_MEDIA", "Telegram 无法读取媒体内容", True
    if "image_process_failed" in d or "photo_invalid_dimensions" in d or "photo should be uploaded" in d:
        return "BAD_MEDIA", "Telegram 无法处理该图片", True
    if "message is too long" in d or "caption is too long" in d:
        return "TEXT_TOO_LONG", "消息过长", True
    if "can't parse entities" in d:
        return "BAD_HTML", "消息格式化失败", True
    if "message to delete not found" in d or "message can't be deleted" in d:
        return "DELETE_FAILED", "消息无法删除", True
    if code >= 500:
        return "SERVER_ERROR", f"Telegram 服务器错误 ({code})", False
    if code == 400:
        return "BAD_REQUEST", f"Telegram 拒绝请求: {desc}", True
    return "TG_ERROR", f"Telegram 错误 {code}: {desc}", False


def _extract_file_id(result: Any, field: str) -> Optional[str]:
    if not isinstance(result, dict):
        return None
    if field == "photo":
        photos = result.get("photo")
        if photos:
            return photos[-1].get("file_id")
        return None
    obj = result.get(field)
    if isinstance(obj, dict):
        return obj.get("file_id")
    # sendAnimation may return document for some inputs
    doc = result.get("document")
    if isinstance(doc, dict):
        return doc.get("file_id")
    return None


def _user_name(u: dict[str, Any]) -> str:
    return " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or u.get("username") or str(u.get("id", "?"))


def _audio_name(a: dict[str, Any]) -> str:
    title = a.get("title")
    performer = a.get("performer")
    if title and performer:
        return safe_filename(f"{performer} - {title}.mp3")
    if title:
        return safe_filename(f"{title}.mp3")
    return "audio.mp3"


def _media_label(m: dict[str, Any]) -> str:
    for key, label in (("photo", "[图片]"), ("video", "[视频]"), ("animation", "[动画]"), ("audio", "[音频]"),
                       ("voice", "[语音]"), ("document", "[文件]"), ("sticker", "[贴纸]"), ("video_note", "[视频消息]")):
        if m.get(key):
            return label
    return ""


def _apply_entities(text: str, entities: list[dict[str, Any]]) -> str:
    """Render text_link entities as ``text (url)`` so links survive in plain text.
    Offsets are UTF-16 code units."""
    links = [e for e in entities if e.get("type") == "text_link" and e.get("url")]
    if not links or not text:
        return text
    utf16 = text.encode("utf-16-le")
    out: list[bytes] = []
    pos = 0
    for e in sorted(links, key=lambda x: x["offset"]):
        start = e["offset"] * 2
        end = start + e["length"] * 2
        if start < pos:
            continue
        out.append(utf16[pos:end])
        out.append(f" ({e['url']})".encode("utf-16-le"))
        pos = end
    out.append(utf16[pos:])
    return b"".join(out).decode("utf-16-le", errors="ignore")


def _split_html(text: str, limit: int) -> list[str]:
    """Split long text at line boundaries.  The formatter only produces <b>/<i>
    tags around the header line, so cutting at newlines keeps the HTML valid."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
