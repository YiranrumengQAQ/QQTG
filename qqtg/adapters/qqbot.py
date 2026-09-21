"""QQ 官方机器人 adapter（QQ 开放平台 q.qq.com，Bot API v2）。

与内置的 OneBot 个人号 adapter 并列的另一种 QQ 接入方式：

* 鉴权：AppID + AppSecret（面板可手输，也可扫码授权获取），通过
  ``POST /app/getAppAccessToken`` 换取 2 小时有效的 access_token，过期自动刷新。
* 事件：连接 ``/gateway/bot`` 返回的 WebSocket 网关，IDENTIFY 订阅
  ``GROUP_AND_C2C_EVENT (1<<25)``，处理 ``GROUP_AT_MESSAGE_CREATE``、
  ``GROUP_ADD_ROBOT``、``GROUP_DEL_ROBOT``、``GROUP_MSG_REJECT/RECEIVE``。
* 发送：``POST /v2/groups/{group_openid}/messages``（文本）+
  ``POST /v2/groups/{group_openid}/files``（富媒体，需要一个公网可访问的
  媒体 URL，由本机 ``/qqbot/media`` 带签名端点提供）。
* 平台限制（会在权限检查与错误信息中体现）：
  - 群内只能收到 @机器人 的消息（除非群主开启全量消息）；
  - 被动回复：收到消息后 5 分钟内、每条消息最多回复 5 次（msg_seq 递增）；
  - 主动消息需要平台配额（一般每群每月 4 条）。
"""
from __future__ import annotations

import asyncio
import itertools
import json
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import websockets

from ..logsys import get_logger
from ..media.detect import safe_filename
from ..media.processor import Prepared
from ..models import PLATFORM_QQ, BridgeError, Media, MediaKind, Reply, Sender, SendResult, UnifiedMessage
from ..core.ratelimit import ChatRateLimiter
from .base import BaseAdapter, ChatInfo, OutgoingMessage, PermissionReport

log = get_logger("conn")

INTENT_GROUP_AND_C2C = 1 << 25
PASSIVE_WINDOW = 290.0  # official passive window is 5 min; refresh before that
PASSIVE_MAX_REPLIES = 5

# OpenAPI size limits (server side, mirrored here for fast clear errors)
OFFICIAL_SIZE_LIMIT = {
    1: 10 * 1024 * 1024,    # image
    2: 100 * 1024 * 1024,   # video
    3: 10 * 1024 * 1024,    # voice (silk)
    4: 100 * 1024 * 1024,   # file
}

# 富媒体 file_type：1 图片 2 视频 3 语音 4 文件
def official_file_type(kind: MediaKind) -> int:
    if kind in (MediaKind.PHOTO, MediaKind.ANIMATION, MediaKind.STICKER):
        return 1
    if kind == MediaKind.VIDEO:
        return 2
    if kind == MediaKind.VOICE:
        return 4  # 官方语音仅接受 silk；退回为文件发送
    return 4


def classify_official_error(status_code: int, payload: dict[str, Any]) -> tuple[str, str, bool]:
    """Map an official OpenAPI error to (code, 中文说明, permanent)."""
    code = str(payload.get("code") or status_code)
    raw = str(payload.get("message") or payload.get("msg") or payload.get("wording") or "")
    low = raw.lower()
    if status_code in (401, 403) or "token" in low and ("invalid" in low or "expire" in low or "无效" in raw):
        return "UNAUTHORIZED", f"QQ 官方机器人鉴权失败: {raw or code}", True
    if "msg_id" in low or "msg seq" in low or "被动" in raw or "过期" in raw and "reply" in low:
        return "PASSIVE_EXPIRED", f"被动回复窗口已失效: {raw}", True
    if "forbidden" in low or "无权" in raw or "权限" in raw:
        return "NO_PERMISSION", f"QQ 官方接口拒绝: {raw or code}", True
    if "频" in raw or "frequen" in low or "limit" in low or status_code == 429:
        return "RATE_LIMITED", f"QQ 官方接口限流: {raw or code}", False
    if "openid" in low and ("group" in low or "群" in raw):
        return "NOT_IN_GROUP", f"机器人不在该群或群不可用: {raw}", True
    if "too large" in low or "过大" in raw or "超过" in raw:
        return "FILE_TOO_LARGE", f"文件超过 QQ 官方接口限制: {raw}", True
    return "QQBOT_API_FAILED", f"QQ 官方接口失败 ({code}): {raw or '未知错误'}", status_code >= 400 and status_code < 500


class QQOfficialAdapter(BaseAdapter):
    platform = PLATFORM_QQ
    kind = "official"
    kind_label = "QQ 官方机器人"
    # 官方平台没有"获取群列表"接口：群是被动发现的，列表不代表机器人不在群里
    authoritative_group_list = False

    def __init__(self, app_id: str, app_secret: str, api_base: str = "", public_base: str = "",
                 rate_chat_per_sec: float = 0.33,
                 media_url_maker: Optional[Callable[[str, str, int], str]] = None):
        super().__init__()
        self.app_id = app_id.strip()
        self.app_secret = app_secret.strip()
        self.api_base = (api_base.strip() or "https://api.bot.qq.com").rstrip("/")
        self.public_base = public_base.strip().rstrip("/")
        self._make_media_url = media_url_maker
        self.limiter = ChatRateLimiter(1.0, rate_chat_per_sec, chat_burst=2)
        self.groups: dict[str, ChatInfo] = {}
        self.impl_name = "QQ 官方机器人 (Bot API v2)"
        self._http: Optional[httpx.AsyncClient] = None
        self._task: Optional[asyncio.Task] = None
        self._dispatch_task: Optional[asyncio.Task] = None
        self._events: asyncio.Queue = asyncio.Queue()
        self._stopping = False
        self._token = ""
        self._token_expire = 0.0
        self._seq = 0
        self._session_id = ""
        self.last_heartbeat = 0.0
        # group_openid -> deque of [msg_id, ts, replies_used]  (newest last)
        self._passive: dict[str, deque[list]] = {}

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if not self.app_id or not self.app_secret:
            raise BridgeError("BAD_CONFIG", "QQ 官方机器人缺少 AppID / AppSecret", permanent=True)
        self._stopping = False
        if self._dispatch_task is None or self._dispatch_task.done():
            self._dispatch_task = asyncio.create_task(self._dispatch_loop(), name="qqbot-official-dispatch")
        self._task = asyncio.create_task(self._ws_loop(), name="qqbot-official-ws")

    async def stop(self) -> None:
        self._stopping = True
        for t in (self._task, self._dispatch_task):
            if t:
                t.cancel()
        if self._http:
            await self._http.aclose()
            self._http = None
        self.connected = False

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True,
                                           headers={"User-Agent": "Mozilla/5.0 QQTG-Bridge"})
        return self._http

    # ------------------------------------------------------------ auth / REST
    async def access_token(self, force: bool = False) -> str:
        if not force and self._token and time.time() < self._token_expire - 120:
            return self._token
        try:
            resp = await self.http.post(f"{self.api_base}/app/getAppAccessToken",
                                        json={"appId": self.app_id, "clientSecret": self.app_secret})
            data = resp.json() if resp.status_code == 200 else {}
        except (httpx.HTTPError, ValueError) as exc:
            raise BridgeError("NETWORK", f"获取 QQ 官方 access_token 失败: {type(exc).__name__}") from exc
        token = str(data.get("access_token") or "")
        if not token:
            msg = str(data.get("message") or data.get("msg") or f"HTTP {resp.status_code}")
            raise BridgeError("UNAUTHORIZED", f"AppID/AppSecret 无效或网络异常: {msg}", permanent=True)
        self._token = token
        try:
            ttl = int(str(data.get("expires_in") or "7200"))
        except ValueError:
            ttl = 7200
        self._token_expire = time.time() + ttl
        log.debug("QQ 官方 access_token 已刷新 (TTL %ds)", ttl)
        return token

    async def api(self, method: str, path: str, json_body: Optional[dict[str, Any]] = None,
                  _retry: bool = True) -> Any:
        token = await self.access_token()
        headers = {"Authorization": f"QQBot {token}", "X-Api-AppID": self.app_id}
        try:
            resp = await self.http.request(method, self.api_base + path, json=json_body, headers=headers)
        except httpx.HTTPError as exc:
            raise BridgeError("NETWORK", f"QQ 官方接口网络错误: {type(exc).__name__}") from exc
        if resp.status_code in (401, 403) and _retry:
            await self.access_token(force=True)
            return await self.api(method, path, json_body, _retry=False)
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {}
        if isinstance(data, dict) and resp.status_code == 200 and (data.get("code") in (None, 0, "0")):
            return data
        payload = data if isinstance(data, dict) else {}
        code, message, permanent = classify_official_error(resp.status_code, payload)
        raise BridgeError(code, message, permanent=permanent)

    async def me(self) -> dict[str, Any]:
        return await self.api("GET", "/users/@me")

    # ------------------------------------------------------------ gateway WS
    async def _ws_loop(self) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                token = await self.access_token(force=time.time() > self._token_expire - 300)
                url = await self._gateway_url()
                async with websockets.connect(
                    url,
                    additional_headers={"Authorization": f"QQBot {token}", "X-Api-AppID": self.app_id},
                    ping_interval=20, ping_timeout=20, open_timeout=15, max_size=16 * 1024 * 1024,
                ) as ws:
                    hello = json.loads(await asyncio.wait_for(ws.recv(), 15))
                    if hello.get("op") != 10:
                        raise BridgeError("PROTOCOL", f"网关握手异常 op={hello.get('op')}")
                    interval = float((hello.get("d") or {}).get("heartbeat_interval") or 30000) / 1000.0
                    identify = {"op": 2, "d": {"token": f"QQBot {token}", "intents": INTENT_GROUP_AND_C2C, "shard": [0, 1]}}
                    if self._session_id and self._seq:
                        identify = {"op": 6, "d": {"token": f"QQBot {token}", "session_id": self._session_id, "seq": self._seq}}
                    await ws.send(json.dumps(identify))
                    backoff = 1.0
                    await self._read_ws(ws, interval)
            except asyncio.CancelledError:
                raise
            except BridgeError as exc:
                self.last_error = exc.message
                if exc.code == "UNAUTHORIZED":
                    log.error("QQ 官方机器人连接失败: %s", exc.message)
                    self.connected = False
                    return  # bad credentials: no point retrying until reconfigured
                log.debug("QQ 官方网关连接失败: %s", exc.message)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                log.debug("QQ 官方网关断开: %s", self.last_error)
            if self._stopping:
                break
            if self.connected:
                self.connected = False
                log.warning("QQ 官方机器人连接断开: %s", self.last_error)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def gateway_url(self) -> str:
        return ""  # overridden by api call; kept for clarity

    async def _gateway_url(self) -> str:
        data = await self.api("GET", "/gateway/bot")
        url = str((data or {}).get("url") or "")
        if not url:
            raise BridgeError("PROTOCOL", "网关地址获取失败", permanent=True)
        return url

    async def _read_ws(self, ws: Any, interval: float) -> None:
        while not self._stopping:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(5.0, interval))
            except asyncio.TimeoutError:
                await ws.send(json.dumps({"op": 1, "d": self._seq}))
                continue
            self.last_activity = time.time()
            try:
                data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            op = data.get("op")
            if op == 11:  # heartbeat ACK
                self.last_heartbeat = time.time()
                continue
            if op == 1:  # server-requested heartbeat
                await ws.send(json.dumps({"op": 1, "d": self._seq}))
                continue
            if op == 7:  # server asks to reconnect (try RESUME via identify loop)
                log.info("QQ 官方网关请求重连")
                return
            if op == 9:  # invalid session -> fresh identify
                self._session_id = ""
                self._seq = 0
                return
            if op != 0:
                continue
            self._seq = int(data.get("s") or self._seq)
            self._events.put_nowait((data.get("t") or "", data.get("d") or {}))

    async def _dispatch_loop(self) -> None:
        while True:
            etype, d = await self._events.get()
            try:
                await self._dispatch(etype, d)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("处理 QQ 官方事件失败 (%s)", etype)
            finally:
                self._events.task_done()

    async def _dispatch(self, etype: str, d: dict[str, Any]) -> None:
        if etype in ("READY", "RESUMED"):
            user = d.get("user") or {}
            self.self_id = str(user.get("id") or self.self_id or "")
            self.self_name = str(user.get("username") or self.self_name or "QQ 官方机器人")
            if etype == "READY":
                self._session_id = str(d.get("session_id") or "")
                self._seq = int(d.get("seq") or 0)
            if not self.connected:
                self.connected = True
                self.connected_since = time.time()
                self.last_error = ""
                log.info("QQ 官方机器人已连接: %s (%s)", self.self_name, self.self_id)
                if self.on_event:
                    await self._safe_event("qq_connected", {"platform": PLATFORM_QQ})
            return
        if etype == "GROUP_AT_MESSAGE_CREATE":
            gid = str(d.get("group_openid") or "")
            if not gid:
                return
            info = self._ensure_group(gid)
            if self.on_event:
                await self._safe_event("chat_seen", {"platform": PLATFORM_QQ, "chat": info})
            unified = self.parse_message(d)
            self._remember_passive(gid, unified.message_id)
            if self.on_message and unified is not None:
                await self.on_message(unified)
            return
        if etype == "GROUP_ADD_ROBOT":
            gid = str(d.get("group_openid") or "")
            if gid:
                info = self._ensure_group(gid)
                if self.on_event:
                    await self._safe_event("bot_joined", {"platform": PLATFORM_QQ, "chat_id": gid, "chat": info})
            return
        if etype == "GROUP_DEL_ROBOT":
            gid = str(d.get("group_openid") or "")
            if gid:
                self.groups.pop(gid, None)
                self._passive.pop(gid, None)
                if self.on_event:
                    await self._safe_event("bot_left", {"platform": PLATFORM_QQ, "chat_id": gid, "reason": "机器人已被移出群"})
            return
        if etype == "GROUP_MSG_REJECT":
            gid = str(d.get("group_openid") or "")
            if gid and self.on_event:
                await self._safe_event("bot_limited", {"platform": PLATFORM_QQ, "chat_id": gid, "reason": "群管理员关闭了机器人消息推送"})
            return
        if etype == "GROUP_MSG_RECEIVE":
            gid = str(d.get("group_openid") or "")
            if gid:
                info = self._ensure_group(gid)
                if self.on_event:
                    await self._safe_event("bot_joined", {"platform": PLATFORM_QQ, "chat_id": gid, "chat": info})
            return
        if etype in ("C2C_MESSAGE_CREATE", "FRIEND_ADD", "FRIEND_DEL"):
            log.debug("忽略单聊/好友事件: %s", etype)
            return
        log.debug("忽略 QQ 官方事件: %s", etype)

    async def _safe_event(self, name: str, data: dict[str, Any]) -> None:
        try:
            if self.on_event:
                await self.on_event(name, data)
        except Exception:  # pragma: no cover
            log.exception("QQ 官方事件处理失败")

    # ------------------------------------------------------------ chats
    def _ensure_group(self, gid: str) -> ChatInfo:
        info = self.groups.get(gid)
        if info is None:
            info = ChatInfo(gid, f"QQ群 {gid[-6:].upper()}")
            self.groups[gid] = info
        return info

    async def list_chats(self) -> list[ChatInfo]:
        # 官方平台没有"获取群列表"接口，返回运行期间发现的群
        return list(self.groups.values())

    def seed_chats(self, rows: list[dict[str, Any]]) -> None:
        """由 BridgeApp 用数据库里已发现的群预热（重启后状态不丢）。"""
        import re
        for r in rows or []:
            cid = str((r or {}).get("chat_id") or "")
            if re.fullmatch(r"[A-Za-z0-9_-]{10,64}", cid or "") and cid not in self.groups:
                self.groups[cid] = ChatInfo(cid, str(r.get("title") or "") or f"QQ群 {cid[-6:].upper()}")

    async def check_permissions(self, chat_id: str) -> PermissionReport:
        if not self.connected:
            return PermissionReport(ok=False, present=True, reason="QQ 官方机器人未连接", status="error")
        known = chat_id in self.groups
        return PermissionReport(
            ok=True,
            present=True,
            checks={"send_text": True, "media": bool(self.public_base and self._make_media_url)},
            reason="" if known else "该群暂未与机器人互动过（群聊消息需要 @机器人 才能收到）",
        )

    # ------------------------------------------------------------ passive slots
    def _remember_passive(self, gid: str, msg_id: str) -> None:
        if not msg_id:
            return
        dq = self._passive.setdefault(gid, deque(maxlen=8))
        dq.append([msg_id, time.time(), 0])

    def _take_passive(self, gid: str) -> tuple[str, int]:
        dq = self._passive.get(gid)
        if not dq:
            return "", 0
        now = time.time()
        for slot in reversed(dq):
            if now - slot[1] < PASSIVE_WINDOW and slot[2] < PASSIVE_MAX_REPLIES:
                slot[2] += 1
                return slot[0], slot[2]
        return "", 0

    def _drop_passive(self, gid: str, msg_id: str) -> None:
        dq = self._passive.get(gid)
        if not dq:
            return
        for slot in dq:
            if slot[0] == msg_id:
                slot[2] = PASSIVE_MAX_REPLIES  # window broken; stop using it
                slot[1] = 0.0

    # ------------------------------------------------------------ sending
    async def send(self, msg: OutgoingMessage) -> SendResult:
        if not self.connected:
            return SendResult(ok=False, error_code="DISCONNECTED", error="QQ 官方机器人未连接")
        gid = msg.chat_id
        result = SendResult(ok=True)
        text = msg.text or ""
        fallbacks = [m.fallback_text for m in msg.media if not m.deliverable and m.fallback_text]
        if fallbacks:
            text = (text + "\n\n" if text.strip() else "") + "\n\n".join(fallbacks)
        try:
            if text.strip():
                mid = await self._send_text(gid, text)
                if mid:
                    result.message_ids.append(mid)
            for p in msg.media:
                if not p.deliverable:
                    continue
                mid = await self._send_media(gid, p)
                if mid:
                    result.message_ids.append(mid)
            if not result.message_ids:
                return SendResult(ok=False, error_code="EMPTY", error="空消息", permanent=True)
            return result
        except BridgeError as exc:
            return SendResult(ok=False, message_ids=result.message_ids, error_code=exc.code, error=exc.message,
                              permanent=exc.permanent, retry_after=exc.retry_after)

    async def _send_text(self, gid: str, content: str) -> str:
        await self.limiter.acquire(gid)
        payload: dict[str, Any] = {"msg_type": 0, "content": content[:3000]}
        msg_id, seq = self._take_passive(gid)
        if msg_id:
            payload.update({"msg_id": msg_id, "msg_seq": seq})
        try:
            data = await self.api("POST", f"/v2/groups/{gid}/messages", payload)
        except BridgeError as exc:
            if exc.code == "PASSIVE_EXPIRED" and msg_id:
                self._drop_passive(gid, msg_id)
                payload.pop("msg_id", None)
                payload.pop("msg_seq", None)
                data = await self.api("POST", f"/v2/groups/{gid}/messages", payload)
            else:
                raise
        return str((data or {}).get("id") or "")

    async def _media_url(self, p: Prepared) -> str:
        assert p.path
        if not self.public_base or self._make_media_url is None:
            raise BridgeError("MEDIA_NO_PUBLIC_URL",
                              "官方机器人发送媒体需要公网地址：请在「系统 → 设置」填写「公网媒体地址」（面板必须能被 QQ 服务器访问）",
                              permanent=True)
        name = safe_filename(p.filename, "media")
        return self._make_media_url(str(Path(p.path).resolve()), name, 600)

    async def _send_media(self, gid: str, p: Prepared) -> str:
        assert p.path
        ftype = official_file_type(p.kind)
        size = p.size or Path(p.path).stat().st_size
        limit = OFFICIAL_SIZE_LIMIT.get(ftype, 100 * 1024 * 1024)
        if size > limit:
            raise BridgeError("FILE_TOO_LARGE", f"文件 {size // 1024 // 1024}MB 超过 QQ 官方接口 {limit // 1024 // 1024}MB 限制", permanent=True)
        url = await self._media_url(p)
        await self.limiter.acquire(gid)
        up = await self.api("POST", f"/v2/groups/{gid}/files", {"file_type": ftype, "url": url, "srv_send_msg": False})
        file_info = str((up or {}).get("file_info") or "")
        if not file_info:
            raise BridgeError("QQBOT_API_FAILED", "QQ 富媒体上传未返回 file_info", permanent=True)
        payload: dict[str, Any] = {"msg_type": 7, "media": {"file_info": file_info}}
        msg_id, seq = self._take_passive(gid)
        if msg_id:
            payload.update({"msg_id": msg_id, "msg_seq": seq})
        try:
            data = await self.api("POST", f"/v2/groups/{gid}/messages", payload)
        except BridgeError as exc:
            if exc.code == "PASSIVE_EXPIRED" and msg_id:
                self._drop_passive(gid, msg_id)
                payload.pop("msg_id", None)
                payload.pop("msg_seq", None)
                data = await self.api("POST", f"/v2/groups/{gid}/messages", payload)
            else:
                raise
        return str((data or {}).get("id") or "")

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        return False  # 官方接口不支持撤回群消息

    # ------------------------------------------------------------ download
    async def download(self, media: Media, dest_dir: Path) -> Path:
        name = safe_filename(media.filename, "file")
        dest = dest_dir / f"src_{name}"
        url = media.url or ""
        if not url.startswith("http"):
            raise BridgeError("DOWNLOAD_FAILED", "QQ 官方消息未提供可下载的媒体地址", permanent=True)
        try:
            async with self.http.stream("GET", url, timeout=300.0) as resp:
                if resp.status_code != 200:
                    raise BridgeError("DOWNLOAD_FAILED", f"媒体下载失败 (HTTP {resp.status_code})")
                with open(dest, "wb") as fh:
                    async for chunk in resp.aiter_bytes(1024 * 256):
                        fh.write(chunk)
        except httpx.HTTPError as exc:
            raise BridgeError("NETWORK", f"媒体下载网络错误: {type(exc).__name__}") from exc
        return dest

    # ------------------------------------------------------------ parsing
    def parse_message(self, d: dict[str, Any]) -> Optional[UnifiedMessage]:
        gid = str(d.get("group_openid") or "")
        author = d.get("author") or {}
        uid = str(author.get("member_openid") or author.get("user_openid") or "")
        name = f"QQ用户{uid[-4:].upper()}" if uid else "QQ用户"
        info = self.groups.get(gid)
        text = str(d.get("content") or "").strip()
        media: list[Media] = []
        for att in d.get("attachments") or []:
            url = str(att.get("url") or "")
            ctype = str(att.get("content_type") or "")
            if not url and not ctype:
                continue
            url = url if url.startswith("http") else ("https://" + url.lstrip("/"))
            if "image" in ctype or url.lower().split("?")[0].endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                media.append(Media(kind=MediaKind.PHOTO, url=url, filename=safe_filename(att.get("filename") or "image.jpg")))
            else:
                media.append(Media(kind=MediaKind.DOCUMENT, url=url, filename=safe_filename(att.get("filename") or "file")))
        if not text and not media:
            return None
        msg_id = str(d.get("id") or "")
        reply: Optional[Reply] = None
        return UnifiedMessage(
            platform=PLATFORM_QQ,
            chat_id=gid,
            chat_title=(info.title if info else gid),
            message_id=msg_id,
            sender=Sender(id=uid, name=name, platform=PLATFORM_QQ),
            text=text,
            media=media,
            reply=reply,
            timestamp=_parse_ts(d.get("timestamp")),
            is_command=text.startswith("/"),
            raw={"type": "GROUP_AT_MESSAGE_CREATE", "d": d},
        )

    def status(self) -> dict[str, Any]:
        s = super().status()
        s.update({"kind": self.kind, "kind_label": self.kind_label, "impl": self.impl_name,
                  "app_id": self.app_id, "api_base": self.api_base, "groups": len(self.groups),
                  "last_heartbeat": self.last_heartbeat, "public_media": bool(self.public_base and self._make_media_url)})
        return s


def _parse_ts(value: Any) -> float:
    if not value:
        return time.time()
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            from datetime import datetime
            return datetime.fromisoformat(str(value)).timestamp()
        except ValueError:
            return time.time()
