"""OneBot v11 adapter (NapCat / LLOneBot / Lagrange / go-cqhttp ...).

Two connection modes are supported:

* ``forward``  – the bridge connects to the OneBot WebSocket server
  (NapCat "正向 WebSocket", e.g. ``ws://127.0.0.1:3001``).  Default.
* ``reverse``  – the OneBot implementation connects to the bridge at
  ``ws://<bridge>/onebot/v11/ws``.  The web app hands the socket over via
  :meth:`OneBotAdapter.attach_reverse`.

Everything QQ specific stays in this file; the rest of the system only sees
``UnifiedMessage`` objects.
"""
from __future__ import annotations

import asyncio
import base64
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Optional, Protocol

import httpx
import websockets

from ..logsys import get_logger
from ..media.detect import safe_filename
from ..media.processor import Prepared
from ..models import PLATFORM_QQ, BridgeError, Media, MediaKind, Reply, Sender, SendResult, UnifiedMessage
from ..core.ratelimit import ChatRateLimiter
from .base import BaseAdapter, ChatInfo, OutgoingMessage, PermissionReport

log = get_logger("conn")

QQ_BASE64_LIMIT = 10 * 1024 * 1024  # above this we hand a local path to OneBot instead of base64

# A few very common built-in QQ faces so they read naturally on Telegram.
FACE_TEXT = {
    0: "惊讶", 1: "撇嘴", 2: "色", 3: "发呆", 4: "得意", 5: "流泪", 6: "害羞", 7: "闭嘴", 8: "睡", 9: "大哭",
    10: "尴尬", 11: "发怒", 12: "调皮", 13: "呲牙", 14: "微笑", 15: "难过", 16: "酷", 18: "抓狂", 19: "吐",
    20: "偷笑", 21: "可爱", 22: "白眼", 23: "傲慢", 24: "饥饿", 25: "困", 26: "惊恐", 27: "流汗", 28: "憨笑",
    29: "悠闲", 30: "奋斗", 31: "咒骂", 32: "疑问", 33: "嘘", 34: "晕", 35: "折磨", 36: "衰", 37: "骷髅",
    38: "敲打", 39: "再见", 41: "发抖", 42: "爱情", 43: "跳跳", 46: "猪头", 49: "拥抱", 53: "蛋糕",
    56: "刀", 59: "便便", 60: "咖啡", 63: "玫瑰", 64: "凋谢", 66: "爱心", 67: "心碎", 74: "太阳", 75: "月亮",
    76: "赞", 77: "踩", 78: "握手", 79: "胜利", 85: "飞吻", 86: "怄火", 89: "西瓜", 96: "冷汗", 97: "擦汗",
    98: "抠鼻", 99: "鼓掌", 100: "糗大了", 101: "坏笑", 102: "左哼哼", 103: "右哼哼", 104: "哈欠", 105: "鄙视",
    106: "委屈", 107: "快哭了", 108: "阴险", 109: "左亲亲", 110: "吓", 111: "可怜", 112: "菜刀", 113: "啤酒",
    114: "篮球", 115: "乒乓", 116: "示爱", 117: "瓢虫", 118: "抱拳", 119: "勾引", 120: "拳头", 121: "差劲",
    122: "爱你", 123: "NO", 124: "OK", 125: "转圈", 129: "挥手", 144: "喝彩", 146: "爆筋", 147: "棒棒糖",
    171: "茶", 172: "眨眼睛", 173: "泪奔", 174: "无奈", 175: "卖萌", 176: "小纠结", 177: "喷血", 178: "斜眼笑",
    179: "doge", 180: "惊喜", 181: "骚扰", 182: "笑哭", 183: "我最美", 187: "幽灵", 201: "点赞", 212: "托腮",
    214: "啵啵", 222: "抱抱", 227: "拍手", 264: "捂脸", 265: "敲击", 266: "哦哟", 267: "头秃", 268: "问号脸",
    269: "暗中观察", 270: "emm", 271: "吃瓜", 272: "呵呵哒", 273: "我酸了", 277: "汪汪", 281: "无眼笑",
    282: "敬礼", 284: "面无表情", 285: "摸鱼", 287: "哦", 289: "睁眼", 290: "敲开心", 293: "摸锦鲤",
    294: "期待", 297: "拜谢", 298: "元宝", 299: "牛啊", 305: "右亲亲", 306: "牛气冲天", 307: "喵喵",
    311: "打call", 312: "变形", 314: "仔细分析", 317: "菜汪", 318: "崇拜", 319: "比心", 320: "庆祝",
    323: "嫌弃", 324: "吃糖", 326: "生气", 332: "举牌牌", 333: "烟花", 334: "虎虎生威", 336: "豹富",
    337: "花朵脸", 338: "我想开了", 339: "舔屏", 341: "打招呼", 342: "酸Q", 343: "我方了", 344: "大怨种",
    345: "红包多多", 346: "你真棒棒",
}


class WSLike(Protocol):
    async def send_text(self, data: str) -> None: ...
    async def recv_text(self) -> str: ...
    async def close(self) -> None: ...


class _ClientWS:
    def __init__(self, ws: Any):
        self.ws = ws

    async def send_text(self, data: str) -> None:
        await self.ws.send(data)

    async def recv_text(self) -> str:
        data = await self.ws.recv()
        return data if isinstance(data, str) else data.decode("utf-8", errors="replace")

    async def close(self) -> None:
        await self.ws.close()


class _StarletteWS:
    def __init__(self, ws: Any):
        self.ws = ws

    async def send_text(self, data: str) -> None:
        await self.ws.send_text(data)

    async def recv_text(self) -> str:
        return await self.ws.receive_text()

    async def close(self) -> None:
        try:
            await self.ws.close()
        except Exception:
            pass


class OneBotAdapter(BaseAdapter):
    platform = PLATFORM_QQ
    kind = "onebot"
    kind_label = "个人账号 (OneBot v11)"

    def __init__(self, mode: str = "forward", ws_url: str = "ws://127.0.0.1:3001", access_token: str = "",
                 rate_chat_per_sec: float = 1.5):
        super().__init__()
        self.mode = mode if mode in ("forward", "reverse") else "forward"
        self.ws_url = ws_url.strip()
        self.access_token = access_token.strip()
        self._ws: Optional[WSLike] = None
        self._task: Optional[asyncio.Task] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stopping = False
        self._pending: dict[str, asyncio.Future] = {}
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._dispatch_task: Optional[asyncio.Task] = None
        self._http: Optional[httpx.AsyncClient] = None
        self.limiter = ChatRateLimiter(10, rate_chat_per_sec, chat_burst=3)
        self.groups: dict[str, ChatInfo] = {}
        self.last_heartbeat = 0.0
        self.impl_name = ""

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._stopping = False
        if self._dispatch_task is None or self._dispatch_task.done():
            self._dispatch_task = asyncio.create_task(self._dispatch_loop(), name="onebot-dispatch")
        if self.mode == "forward":
            self._task = asyncio.create_task(self._forward_loop(), name="onebot-forward")
        else:
            log.info("OneBot 反向 WebSocket 模式：等待 QQ 客户端连接 /onebot/v11/ws")

    async def stop(self) -> None:
        self._stopping = True
        for t in (self._task, self._reader_task, self._dispatch_task):
            if t:
                t.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._http:
            await self._http.aclose()
            self._http = None
        self.connected = False

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=True,
                                           headers={"User-Agent": "Mozilla/5.0 QQTG-Bridge"})
        return self._http

    async def _forward_loop(self) -> None:
        backoff = 1.0
        while not self._stopping:
            headers = {}
            if self.access_token:
                headers["Authorization"] = f"Bearer {self.access_token}"
            try:
                async with websockets.connect(self.ws_url, additional_headers=headers, ping_interval=20, ping_timeout=20,
                                              max_size=64 * 1024 * 1024, open_timeout=15) as ws:
                    backoff = 1.0
                    await self._on_connected(_ClientWS(ws))
                    await self._read_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = _describe_ws_error(exc)
                if self.connected:
                    log.warning("QQ (OneBot) 连接断开: %s", self.last_error)
                else:
                    log.debug("OneBot connect failed: %s", self.last_error)
            finally:
                if self.connected:
                    self.connected = False
                    self._fail_pending("连接断开")
            if self._stopping:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def attach_reverse(self, ws: Any) -> None:
        """Serve a reverse WebSocket connection (called from the web app)."""
        if self._ws is not None:
            log.warning("已有 OneBot 反向连接，关闭旧连接")
            try:
                await self._ws.close()
            except Exception:
                pass
        await self._on_connected(_StarletteWS(ws))
        try:
            await self._read_loop()
        finally:
            self.connected = False
            self._fail_pending("连接断开")
            self._ws = None
            log.warning("QQ (OneBot 反向) 连接已断开")

    async def _on_connected(self, ws: WSLike) -> None:
        self._ws = ws
        self.connected = True
        self.connected_since = time.time()
        self.last_activity = time.time()
        self.last_error = ""
        self.reconnects += 1 if self.self_id else 0
        # identify + prefetch groups in the background (needs the read loop running)
        asyncio.create_task(self._after_connect())

    async def _after_connect(self) -> None:
        await asyncio.sleep(0.2)
        try:
            info = await self.call("get_login_info", timeout=20)
            self.self_id = str(info.get("user_id", ""))
            self.self_name = str(info.get("nickname", ""))
            try:
                ver = await self.call("get_version_info", timeout=10)
                self.impl_name = f"{ver.get('app_name', '')} {ver.get('app_version', '')}".strip()
            except BridgeError:
                pass
            log.info("QQ 已连接: %s (%s) via %s", self.self_name, self.self_id, self.impl_name or "OneBot v11")
            await self.list_chats()
            if self.on_event:
                await self.on_event("qq_connected", {"platform": PLATFORM_QQ})
        except Exception as exc:
            log.warning("OneBot 初始化失败: %s", exc)

    def _fail_pending(self, reason: str) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(BridgeError("DISCONNECTED", f"QQ 连接不可用: {reason}"))
        self._pending.clear()

    async def _read_loop(self) -> None:
        assert self._ws is not None
        ws = self._ws
        while not self._stopping:
            raw = await ws.recv_text()
            self.last_activity = time.time()
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if "echo" in data and data.get("echo") in self._pending:
                fut = self._pending.pop(data["echo"])
                if not fut.done():
                    fut.set_result(data)
                continue
            # Events are dispatched sequentially by a separate task so handlers
            # can call the OneBot API (which needs this read loop) without deadlocking.
            self._events.put_nowait(data)

    async def _dispatch_loop(self) -> None:
        while True:
            data = await self._events.get()
            try:
                await self._handle_event(data)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("处理 QQ 事件失败")
            finally:
                self._events.task_done()

    # ---------------------------------------------------------------- API
    async def call(self, action: str, params: Optional[dict[str, Any]] = None, timeout: float = 30.0) -> Any:
        if not self._ws or not self.connected:
            raise BridgeError("DISCONNECTED", "QQ 未连接")
        echo = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[echo] = fut
        try:
            await self._ws.send_text(json.dumps({"action": action, "params": params or {}, "echo": echo}, ensure_ascii=False))
            resp = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(echo, None)
            raise BridgeError("TIMEOUT", f"QQ 接口 {action} 超时")
        except BridgeError:
            raise
        except Exception as exc:
            self._pending.pop(echo, None)
            raise BridgeError("DISCONNECTED", f"QQ 连接错误: {type(exc).__name__}")
        if resp.get("status") == "ok" or resp.get("retcode") == 0:
            return resp.get("data")
        wording = resp.get("wording") or resp.get("message") or resp.get("msg") or f"retcode {resp.get('retcode')}"
        raise BridgeError(*classify_qq_error(int(resp.get("retcode") or -1), str(wording)))

    # -------------------------------------------------------------- events
    async def _handle_event(self, ev: dict[str, Any]) -> None:
        post_type = ev.get("post_type")
        if post_type == "meta_event":
            if ev.get("meta_event_type") == "heartbeat":
                self.last_heartbeat = time.time()
            elif ev.get("meta_event_type") == "lifecycle" and ev.get("self_id"):
                self.self_id = str(ev["self_id"])
            return
        if post_type == "message_sent":
            return  # our own outgoing messages (NapCat option) – loop protection layer 1
        if post_type == "message":
            if ev.get("message_type") != "group":
                return
            unified = self.parse_message(ev)
            if unified is None:
                return
            if unified.sender.id == self.self_id:
                return
            gid = unified.chat_id
            if gid not in self.groups:
                self.groups[gid] = ChatInfo(gid, unified.chat_title or gid)
            if self.on_event:
                await self.on_event("chat_seen", {"platform": PLATFORM_QQ, "chat": self.groups[gid]})
            if self.on_message:
                await self.on_message(unified)
            return
        if post_type == "notice":
            nt = ev.get("notice_type")
            gid = str(ev.get("group_id", ""))
            if nt == "group_recall" and self.on_event:
                await self.on_event("recall", {"platform": PLATFORM_QQ, "chat_id": gid, "message_id": str(ev.get("message_id")),
                                               "user_id": str(ev.get("user_id")), "operator_id": str(ev.get("operator_id"))})
            elif nt in ("group_increase", "group_decrease") and self.on_event:
                uid = str(ev.get("user_id"))
                if uid == self.self_id:
                    if nt == "group_increase":
                        await self.list_chats()
                        await self.on_event("bot_joined", {"platform": PLATFORM_QQ, "chat_id": gid, "chat": self.groups.get(gid) or ChatInfo(gid, gid)})
                    else:
                        self.groups.pop(gid, None)
                        await self.on_event("bot_left", {"platform": PLATFORM_QQ, "chat_id": gid, "reason": "机器人已退出/被移出群"})
                    return
                name = uid
                try:
                    info = await self.call("get_group_member_info", {"group_id": int(gid), "user_id": int(uid), "no_cache": True}, timeout=10)
                    name = info.get("card") or info.get("nickname") or uid
                except (BridgeError, ValueError):
                    try:
                        info = await self.call("get_stranger_info", {"user_id": int(uid)}, timeout=10)
                        name = info.get("nickname") or uid
                    except (BridgeError, ValueError):
                        pass
                text = f"{name} 加入了 QQ 群" if nt == "group_increase" else f"{name} 离开了 QQ 群"
                await self.on_event("member_event", {"platform": PLATFORM_QQ, "chat_id": gid, "text": text,
                                                     "event": "member_join" if nt == "group_increase" else "member_leave"})
            elif nt == "group_ban" and str(ev.get("user_id")) == self.self_id and self.on_event:
                if int(ev.get("duration") or 0) > 0:
                    await self.on_event("bot_limited", {"platform": PLATFORM_QQ, "chat_id": gid, "reason": "机器人已被禁言"})
                else:
                    await self.on_event("bot_joined", {"platform": PLATFORM_QQ, "chat_id": gid, "chat": self.groups.get(gid) or ChatInfo(gid, gid)})

    # ------------------------------------------------------------- parsing
    def parse_message(self, ev: dict[str, Any]) -> Optional[UnifiedMessage]:
        sender_raw = ev.get("sender") or {}
        uid = str(ev.get("user_id") or sender_raw.get("user_id") or "")
        name = sender_raw.get("card") or sender_raw.get("nickname") or uid
        sender = Sender(id=uid, name=str(name), platform=PLATFORM_QQ)
        gid = str(ev.get("group_id"))
        segments = ev.get("message")
        if isinstance(segments, str):
            segments = [{"type": "text", "data": {"text": _decode_cq_text(segments)}}]
        if not isinstance(segments, list):
            segments = []

        text_parts: list[str] = []
        media: list[Media] = []
        reply: Optional[Reply] = None
        for seg in segments:
            t = seg.get("type")
            d = seg.get("data") or {}
            if t == "text":
                text_parts.append(str(d.get("text", "")))
            elif t == "face":
                label = None
                raw = d.get("raw") or {}
                if isinstance(raw, dict):
                    label = raw.get("faceText")
                if not label:
                    try:
                        label = FACE_TEXT.get(int(d.get("id")))
                    except (TypeError, ValueError):
                        label = None
                text_parts.append(f"[{label.strip('[]')}]" if label else "[表情]")
            elif t == "image":
                file = str(d.get("file") or "")
                url = d.get("url") or (file if file.startswith("http") else None)
                summary = str(d.get("summary") or "")
                is_sticker = str(d.get("sub_type", "0")) not in ("0", "None", "") or "表情" in summary
                m = Media(kind=MediaKind.PHOTO, url=url, file_id=file or None, size=_int(d.get("file_size")),
                          filename=safe_filename(file if "." in file and not file.startswith("http") else "image.jpg"),
                          extra={"sticker": is_sticker})
                if file and not file.startswith("http"):
                    m.extra["prehash"] = "qq:" + file.split(".")[0]
                media.append(m)
            elif t == "mface":
                url = d.get("url")
                m = Media(kind=MediaKind.PHOTO, url=url, filename="sticker.gif", extra={"sticker": True})
                if d.get("emoji_id"):
                    m.extra["prehash"] = "qqmface:" + str(d["emoji_id"])
                if url:
                    media.append(m)
                else:
                    text_parts.append(f"[{(d.get('summary') or '动画表情').strip('[]')}]")
            elif t == "record":
                media.append(Media(kind=MediaKind.VOICE, url=d.get("url") if str(d.get("url", "")).startswith("http") else None,
                                   file_id=d.get("file"), size=_int(d.get("file_size")), filename="voice.amr"))
            elif t == "video":
                file = str(d.get("file") or "")
                media.append(Media(kind=MediaKind.VIDEO, url=d.get("url") if str(d.get("url", "")).startswith("http") else None,
                                   file_id=file or d.get("file_id"), size=_int(d.get("file_size")),
                                   filename=safe_filename(d.get("file_name") or (file if "." in file else "video.mp4"), "video.mp4")))
            elif t == "file":
                fname = safe_filename(d.get("file_name") or d.get("name") or d.get("file"), "file")
                media.append(Media(kind=MediaKind.DOCUMENT, url=d.get("url") if str(d.get("url", "")).startswith("http") else None,
                                   file_id=d.get("file_id") or d.get("file"), size=_int(d.get("file_size")), filename=fname,
                                   extra={"busid": d.get("busid")}))
            elif t == "at":
                target = str(d.get("qq", ""))
                if target == "all":
                    text_parts.append("@全体成员 ")
                else:
                    text_parts.append(f"@{d.get('name') or target} ")
            elif t == "reply":
                rid = str(d.get("id", ""))
                if rid:
                    reply = Reply(message_id=rid)
            elif t == "forward":
                text_parts.append("[合并转发消息]")
                if d.get("id"):
                    ev.setdefault("_forward_id", d["id"])  # expanded later by fetch_forward_text()
            elif t == "json":
                text_parts.append(_json_card_text(d.get("data")))
            elif t == "xml":
                text_parts.append("[XML 卡片消息]")
            elif t == "share":
                text_parts.append(f"{d.get('title', '')} {d.get('url', '')}".strip())
            elif t == "poke":
                text_parts.append("[戳一戳]")
            elif t == "dice":
                text_parts.append(f"[骰子] {d.get('result', '')}".strip())
            elif t == "rps":
                text_parts.append("[猜拳]")
            elif t == "location":
                text_parts.append(f"[位置] {d.get('title') or ''} {d.get('lat', '')},{d.get('lon', '')}".strip())
            elif t == "music":
                text_parts.append(f"[音乐] {d.get('title') or d.get('url') or ''}".strip())
            elif t == "markdown":
                text_parts.append(str(d.get("content", "")))
            elif t == "contact":
                text_parts.append("[推荐联系人]")
            else:
                text_parts.append(f"[{t}]")

        text = "".join(text_parts).strip()
        if not text and not media:
            return None
        return UnifiedMessage(
            platform=PLATFORM_QQ,
            chat_id=gid,
            chat_title=(self.groups.get(gid).title if gid in self.groups else ""),
            message_id=str(ev.get("message_id")),
            sender=sender,
            text=text,
            media=media,
            reply=reply,
            timestamp=float(ev.get("time") or time.time()),
            is_command=text.startswith("/"),
            raw=ev,
        )

    async def enrich_reply(self, msg: UnifiedMessage) -> None:
        """Fill in reply sender/preview via get_msg (best effort)."""
        if not msg.reply or msg.reply.text_preview is not None:
            return
        try:
            data = await self.call("get_msg", {"message_id": int(msg.reply.message_id)}, timeout=10)
        except (BridgeError, ValueError):
            return
        s = data.get("sender") or {}
        msg.reply.sender_name = s.get("card") or s.get("nickname") or str(s.get("user_id", ""))
        inner = self.parse_message({**data, "group_id": msg.chat_id, "message_type": "group"})
        if inner:
            msg.reply.text_preview = inner.summary(60)

    async def fetch_forward_text(self, msg: UnifiedMessage, limit: int = 8) -> None:
        fid = (msg.raw or {}).get("_forward_id") if isinstance(msg.raw, dict) else None
        if not fid:
            return
        try:
            data = await self.call("get_forward_msg", {"id": fid, "message_id": fid}, timeout=20)
        except BridgeError:
            return
        nodes = data.get("messages") or data.get("message") or []
        lines: list[str] = []
        for node in nodes[:limit]:
            content = node.get("content") if isinstance(node, dict) else None
            if isinstance(content, dict):
                content = content.get("message")
            if content is None and isinstance(node, dict) and node.get("data"):
                content = node["data"].get("content")
            snd = (node.get("sender") or {}) if isinstance(node, dict) else {}
            who = snd.get("card") or snd.get("nickname") or (node.get("data") or {}).get("name") or "?"
            inner = self.parse_message({"message": content, "sender": snd, "user_id": snd.get("user_id"), "group_id": msg.chat_id, "message_id": 0})
            lines.append(f"- {who}: {inner.summary(60) if inner else '[消息]'}")
        if len(nodes) > limit:
            lines.append(f"… 共 {len(nodes)} 条")
        if lines:
            msg.text = msg.text.replace("[合并转发消息]", "[合并转发消息]\n" + "\n".join(lines), 1)

    # -------------------------------------------------------- capabilities
    async def list_chats(self) -> list[ChatInfo]:
        try:
            groups = await self.call("get_group_list", {"no_cache": True}, timeout=30)
        except BridgeError as exc:
            log.debug("get_group_list failed: %s", exc)
            return list(self.groups.values())
        result: list[ChatInfo] = []
        for g in groups or []:
            info = ChatInfo(str(g.get("group_id")), str(g.get("group_name") or g.get("group_id")), "group", _int(g.get("member_count")))
            self.groups[info.chat_id] = info
            result.append(info)
        return result

    async def check_permissions(self, chat_id: str) -> PermissionReport:
        if not self.connected:
            return PermissionReport(ok=False, present=False, reason="QQ 未连接", status="error")
        try:
            info = await self.call("get_group_member_info", {"group_id": int(chat_id), "user_id": int(self.self_id or 0), "no_cache": True}, timeout=20)
        except (BridgeError, ValueError) as exc:
            reason = exc.message if isinstance(exc, BridgeError) else str(exc)
            if isinstance(exc, BridgeError) and exc.code in ("NOT_IN_GROUP", "QQ_API_FAILED"):
                return PermissionReport(ok=False, present=False, reason="机器人不在该群", status="left")
            return PermissionReport(ok=False, present=False, reason=reason, status="error")
        shut_up = _int(info.get("shut_up_timestamp")) or 0
        muted = shut_up > time.time()
        checks = {"send_text": not muted, "send_photo": not muted, "send_video": not muted, "send_voice": not muted,
                  "send_document": not muted, "delete_messages": info.get("role") in ("owner", "admin")}
        return PermissionReport(ok=not muted, present=True, muted=muted, checks=checks,
                                reason="机器人被禁言" if muted else "", status="limited" if muted else "authorized")

    # ---------------------------------------------------------------- send
    async def send(self, msg: OutgoingMessage) -> SendResult:
        result = SendResult(ok=True)
        try:
            gid = int(msg.chat_id)
        except ValueError:
            return SendResult(ok=False, error_code="BAD_CHAT", error="无效的 QQ 群号", permanent=True)

        segments: list[dict[str, Any]] = []
        if msg.reply_to_message_id:
            segments.append({"type": "reply", "data": {"id": msg.reply_to_message_id}})
        text = msg.text or ""
        fallbacks = [m.fallback_text for m in msg.media if not m.deliverable and m.fallback_text]
        if fallbacks:
            text = (text + "\n\n" if text.strip() else "") + "\n\n".join(fallbacks)
        if text.strip():
            segments.append({"type": "text", "data": {"text": text}})

        inline: list[Prepared] = []
        files: list[Prepared] = []
        for p in msg.media:
            if not p.deliverable:
                continue
            if p.kind in (MediaKind.PHOTO, MediaKind.ANIMATION, MediaKind.STICKER, MediaKind.VOICE, MediaKind.VIDEO):
                inline.append(p)
            else:
                files.append(p)

        try:
            # Voice and video cannot be combined with text in one QQ message.
            standalone = [p for p in inline if p.kind in (MediaKind.VOICE, MediaKind.VIDEO)]
            combined = [p for p in inline if p.kind not in (MediaKind.VOICE, MediaKind.VIDEO)]
            for p in combined:
                segments.append({"type": "image", "data": {"file": _file_ref(p), "summary": "[图片]"}})
            if len(segments) > (1 if msg.reply_to_message_id else 0):
                mid = await self._send_segments(gid, msg.chat_id, segments)
                result.message_ids.append(mid)
            for p in standalone:
                seg_type = "record" if p.kind == MediaKind.VOICE else "video"
                mid = await self._send_segments(gid, msg.chat_id, [{"type": seg_type, "data": {"file": _file_ref(p)}}])
                result.message_ids.append(mid)
            for p in files:
                mid = await self._send_file(gid, msg.chat_id, p)
                if mid:
                    result.message_ids.append(mid)
            if not result.message_ids:
                return SendResult(ok=False, error_code="EMPTY", error="空消息", permanent=True)
            return result
        except BridgeError as exc:
            return SendResult(ok=False, message_ids=result.message_ids, error_code=exc.code, error=exc.message,
                              permanent=exc.permanent, retry_after=exc.retry_after)

    async def _send_segments(self, gid: int, chat_key: str, segments: list[dict[str, Any]]) -> str:
        await self.limiter.acquire(chat_key)
        data = await self.call("send_group_msg", {"group_id": gid, "message": segments}, timeout=120)
        return str((data or {}).get("message_id", ""))

    async def _send_file(self, gid: int, chat_key: str, p: Prepared) -> str:
        assert p.path
        name = safe_filename(p.filename, "file")
        await self.limiter.acquire(chat_key)
        # Preferred: file segment (works over WS without a shared filesystem).
        try:
            data = await self.call("send_group_msg", {"group_id": gid, "message": [{"type": "file", "data": {"file": _file_ref(p), "name": name}}]}, timeout=600)
            return str((data or {}).get("message_id", ""))
        except BridgeError as exc:
            if exc.code in ("DISCONNECTED", "TIMEOUT"):
                raise
            log.debug("file segment unsupported (%s), trying upload_group_file", exc.message)
        await self.call("upload_group_file", {"group_id": gid, "file": str(Path(p.path).resolve()), "name": name}, timeout=600)
        return ""

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        try:
            await self.call("delete_msg", {"message_id": int(message_id)}, timeout=20)
            return True
        except (BridgeError, ValueError) as exc:
            log.debug("delete_msg failed: %s", exc)
            return False

    # ------------------------------------------------------------ download
    async def download(self, media: Media, dest_dir: Path) -> Path:
        name = safe_filename(media.filename, "file")
        dest = dest_dir / f"src_{name}"
        # 1. direct URL
        if media.url and media.url.startswith("http"):
            try:
                await self._download_url(media.url, dest)
                return dest
            except BridgeError as exc:
                log.debug("direct download failed (%s), trying OneBot API", exc.message)
        # 2. ask the OneBot implementation
        payload: Optional[dict[str, Any]] = None
        try:
            if media.kind == MediaKind.VOICE and media.file_id:
                payload = await self.call("get_record", {"file": media.file_id, "file_id": media.file_id, "out_format": "mp3"}, timeout=120)
                dest = dest_dir / "src_voice.mp3"
            elif media.kind == MediaKind.PHOTO and media.file_id:
                payload = await self.call("get_image", {"file": media.file_id, "file_id": media.file_id}, timeout=120)
            elif media.file_id:
                if media.kind == MediaKind.DOCUMENT and media.extra.get("busid") is not None:
                    try:
                        u = await self.call("get_group_file_url", {"group_id": int(media.extra.get("group_id", 0) or 0), "file_id": media.file_id, "busid": int(media.extra["busid"])}, timeout=60)
                        if u and u.get("url"):
                            await self._download_url(u["url"], dest)
                            return dest
                    except (BridgeError, ValueError):
                        pass
                payload = await self.call("get_file", {"file": media.file_id, "file_id": media.file_id}, timeout=300)
        except BridgeError as exc:
            raise BridgeError("DOWNLOAD_FAILED", f"无法从 QQ 获取媒体: {exc.message}", permanent=exc.permanent)
        if not payload:
            raise BridgeError("DOWNLOAD_FAILED", "无法从 QQ 获取媒体", permanent=True)
        if payload.get("base64"):
            dest.write_bytes(base64.b64decode(payload["base64"]))
            return dest
        if payload.get("url") and str(payload["url"]).startswith("http"):
            await self._download_url(payload["url"], dest)
            return dest
        local = payload.get("file")
        if local:
            lp = Path(str(local).replace("file://", ""))
            if lp.is_file():
                shutil.copyfile(lp, dest)
                return dest
            # last resort: ask for base64 through the (NapCat) download API
            try:
                b64 = await self.call("get_file", {"file": local, "file_id": local}, timeout=120)
                if b64 and b64.get("base64"):
                    dest.write_bytes(base64.b64decode(b64["base64"]))
                    return dest
            except BridgeError:
                pass
        raise BridgeError("DOWNLOAD_FAILED", "QQ 客户端返回的文件路径在本机不可访问（请让 NapCat 与 Bridge 共享目录，或开启 base64 返回）", permanent=True)

    async def _download_url(self, url: str, dest: Path) -> None:
        try:
            async with self.http.stream("GET", url, timeout=300.0) as resp:
                if resp.status_code != 200:
                    raise BridgeError("DOWNLOAD_FAILED", f"媒体下载失败 (HTTP {resp.status_code})")
                with open(dest, "wb") as fh:
                    async for chunk in resp.aiter_bytes(1024 * 256):
                        fh.write(chunk)
        except httpx.HTTPError as exc:
            raise BridgeError("NETWORK", f"媒体下载网络错误: {type(exc).__name__}") from exc

    def status(self) -> dict[str, Any]:
        s = super().status()
        s.update({"mode": self.mode, "impl": self.impl_name, "groups": len(self.groups), "last_heartbeat": self.last_heartbeat})
        return s


# ---------------------------------------------------------------- helpers

def classify_qq_error(retcode: int, wording: str) -> tuple[str, str, bool]:
    w = wording.lower()
    if "禁言" in wording or "shut" in w or "mute" in w:
        return "MUTED", "机器人在该群被禁言", True
    if "不在" in wording or "not in" in w or "no such group" in w or "群不存在" in wording:
        return "NOT_IN_GROUP", "机器人不在该群", True
    if "too large" in w or "过大" in wording or "超过" in wording:
        return "FILE_TOO_LARGE", "文件超过 QQ 允许的大小", True
    if "风控" in wording or "risk" in w or "1200" in wording:
        return "RISK_CONTROL", "QQ 风控，消息被拦截", True
    if "timeout" in w or "超时" in wording:
        return "TIMEOUT", f"QQ 接口超时: {wording}", False
    if retcode == 1404 or "not found" in w or "不存在" in wording:
        return "QQ_API_FAILED", f"QQ 接口失败: {wording}", True
    return "QQ_API_FAILED", f"QQ 接口失败: {wording}", True


def _file_ref(p: Prepared) -> str:
    assert p.path
    size = p.size or Path(p.path).stat().st_size
    if size <= QQ_BASE64_LIMIT:
        with open(p.path, "rb") as fh:
            return "base64://" + base64.b64encode(fh.read()).decode("ascii")
    return "file://" + str(Path(p.path).resolve())


def _int(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _decode_cq_text(s: str) -> str:
    return s.replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")


def _json_card_text(data: Any) -> str:
    try:
        obj = json.loads(data) if isinstance(data, str) else (data or {})
    except ValueError:
        return "[卡片消息]"
    prompt = obj.get("prompt") or obj.get("desc") or ""
    meta = obj.get("meta") or {}
    url = ""
    title = ""
    for v in meta.values() if isinstance(meta, dict) else []:
        if isinstance(v, dict):
            url = v.get("jumpUrl") or v.get("qqdocurl") or v.get("url") or url
            title = v.get("title") or v.get("desc") or title
    parts = [p for p in ("[卡片消息]", prompt.strip("[]") if prompt else "", title, url) if p]
    return " ".join(dict.fromkeys(parts))


def _describe_ws_error(exc: Exception) -> str:
    name = type(exc).__name__
    if isinstance(exc, (ConnectionRefusedError, OSError)) and not isinstance(exc, TimeoutError):
        return "连接被拒绝（OneBot WebSocket 服务未启动或地址错误）"
    if "401" in str(exc) or "403" in str(exc):
        return "认证失败（access_token 不正确）"
    return f"{name}: {exc}" if str(exc) else name
