"""QQ 官方机器人「扫码授权」协议客户端（q.qq.com 扫码绑定）。

与官方 ``@tencent-connect/qqbot-connector`` SDK 使用同一套协议：

1. ``POST {base}/lite/create_bind_task``  ``{"key": base64(32B 随机数)}``
   → ``{"retcode": 0, "data": {"task_id": "..."}}``
2. 用手机 QQ 扫描 ``https://q.qq.com/qqbot/openclaw/connect.html?task_id=...&source=...&_wv=2``
   的二维码，选择要绑定的机器人并确认；
3. 轮询 ``POST {base}/lite/poll_bind_result`` ``{"task_id": "..."}``
   → ``data.status``：1=等待扫码，2=已完成，3=已过期；
4. 完成后 ``data.bot_encrypt_secret`` 是用第 1 步的 ``key`` 做 AES-256-GCM
   加密的 AppSecret（nonce=前 12 字节，tag=后 16 字节），解密即得明文。

过期后重新创建任务刷新二维码即可。
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import secrets
from typing import Any, Optional

import httpx

from .logsys import get_logger
from .models import BridgeError

log = get_logger("conn")

QR_BASE = os.environ.get("QQTG_QQBOT_QR_BASE") or "https://q.qq.com"
CONNECT_PAGE = "https://q.qq.com/qqbot/openclaw/connect.html"
SOURCE_NAME = "QQTG Bridge"

# poll_bind_result data.status
STATUS_NONE = 0
STATUS_PENDING = 1
STATUS_COMPLETED = 2
STATUS_EXPIRED = 3


def build_connect_url(task_id: str, source: str = SOURCE_NAME) -> str:
    from urllib.parse import quote
    return f"{CONNECT_PAGE}?task_id={quote(task_id)}&source={quote(source)}&_wv=2"


def qr_svg(url: str) -> str:
    """Render a scannable QR code for ``url`` as an inline SVG string."""
    import qrcode
    import qrcode.image.svg as qsvg

    img = qrcode.make(url, image_factory=qsvg.SvgPathImage, border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode("utf-8")
    # strip the XML prolog; the panel injects it into HTML
    return svg[svg.index("<svg"):]


def decrypt_secret(key_b64: str, encrypted_b64: str) -> str:
    """AES-256-GCM decrypt of the binding secret (nonce||ct||tag, base64)."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        key = base64.b64decode(key_b64)
        raw = base64.b64decode(encrypted_b64)
        if len(key) != 32 or len(raw) < 12 + 16:
            raise ValueError("length")
        nonce, tag, ct = raw[:12], raw[-16:], raw[12:-16]
        return AESGCM(key).decrypt(nonce, ct + tag, None).decode("utf-8")
    except (InvalidTag, ValueError, UnicodeDecodeError) as exc:
        raise BridgeError("QR_DECRYPT_FAILED", "扫码结果解密失败（密钥不匹配或数据损坏）", permanent=True) from exc


class QQQRSession:
    """One interactive scan-binding session (auto-refreshes the QR on expiry)."""

    TICK = 2.0

    def __init__(self, base: str = ""):
        self.base = (base or QR_BASE).rstrip("/")
        self.state = "idle"  # idle | pending | completed | cancelled | error
        self.url = ""
        self.qr_svg = ""
        self.error = ""
        self.app_id = ""
        self.app_secret = ""
        self.user_openid = ""
        self.scanned_by = ""
        self.started_at = 0.0
        self.refreshes = 0
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await self.cancel()
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self.state = "pending"
        self.error = ""
        self.url = ""
        self.qr_svg = ""
        self.app_id = self.app_secret = self.user_openid = self.scanned_by = ""
        self.started_at = asyncio.get_event_loop().time()
        self._task = asyncio.create_task(self._run(), name="qqbot-qr-bind")
        try:
            # wait until the first QR code is rendered (or the session errored)
            await asyncio.wait_for(self._ready.wait(), timeout=20)
        except asyncio.TimeoutError:
            log.warning("扫码二维码生成超时")

    async def cancel(self) -> None:
        if self._task is not None:
            self._stop.set()
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - best effort
                pass
            self._task = None
        if self.state not in ("completed",):
            self.state = "idle" if self.state == "pending" else self.state

    # ------------------------------------------------------------ loop
    async def _run(self) -> None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=10.0),
                                     headers={"User-Agent": "Mozilla/5.0 QQTG-Bridge"}) as http:
            while not self._stop.is_set():
                key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
                task_id = ""
                try:
                    task_id = await self._create_task(http, key)
                except BridgeError as exc:
                    self.state = "error"
                    self.error = exc.message
                    self._ready.set()
                    log.warning("QQ 官方机器人扫码任务创建失败: %s", exc.message)
                    return
                self.url = build_connect_url(task_id)
                self.qr_svg = qr_svg(self.url)
                self._ready.set()
                expired = False
                while not self._stop.is_set() and not expired:
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=self.TICK)
                        return  # stop requested
                    except asyncio.TimeoutError:
                        pass
                    try:
                        data = await self._poll(http, task_id)
                    except BridgeError as exc:
                        log.debug("扫码轮询失败: %s", exc.message)
                        continue
                    status = int(data.get("status") or 0)
                    if status == STATUS_COMPLETED:
                        self.app_id = str(data.get("bot_appid") or "")
                        self.scanned_by = str(data.get("user_openid") or "")
                        try:
                            self.app_secret = decrypt_secret(key, str(data.get("bot_encrypt_secret") or ""))
                        except BridgeError as exc:
                            self.state = "error"
                            self.error = exc.message
                            return
                        self.state = "completed"
                        log.info("QQ 官方机器人扫码绑定成功 (AppID %s)", self.app_id)
                        return
                    if status == STATUS_EXPIRED:
                        expired = True
                self.refreshes += 1
                log.info("QQ 扫码二维码已过期，自动刷新…")

    async def _create_task(self, http: httpx.AsyncClient, key: str) -> str:
        try:
            resp = await http.post(f"{self.base}/lite/create_bind_task", json={"key": key})
        except httpx.HTTPError as exc:
            raise BridgeError("NETWORK", f"无法连接扫码授权服务: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise BridgeError("QQQR_FAILED", f"创建扫码绑定任务失败 (HTTP {resp.status_code})", permanent=True)
        data = _json(resp)
        if data.get("retcode") != 0 or not (data.get("data") or {}).get("task_id"):
            raise BridgeError("QQQR_FAILED", f"创建扫码绑定任务失败: {data.get('msg') or data.get('message') or retcode_wording(data)}", permanent=True)
        return str(data["data"]["task_id"])

    async def _poll(self, http: httpx.AsyncClient, task_id: str) -> dict[str, Any]:
        try:
            resp = await http.post(f"{self.base}/lite/poll_bind_result", json={"task_id": task_id})
        except httpx.HTTPError as exc:
            raise BridgeError("NETWORK", f"扫码状态轮询失败: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise BridgeError("QQQR_FAILED", f"扫码状态轮询失败 (HTTP {resp.status_code})")
        data = _json(resp)
        if data.get("retcode") != 0:
            raise BridgeError("QQQR_FAILED", f"扫码状态轮询失败: {data.get('msg') or 'retcode != 0'}")
        return data.get("data") or {}

    # ------------------------------------------------------------ output
    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "state": self.state,
            "url": self.url,
            "qr_svg": self.qr_svg if self.state in ("pending", "completed") else "",
            "refreshes": self.refreshes,
        }
        if self.state == "completed":
            out.update({"app_id": self.app_id, "app_secret": self.app_secret, "user_openid": self.scanned_by})
        if self.state == "error":
            out["error"] = self.error
        return out


def retcode_wording(data: dict[str, Any]) -> str:
    return str(data.get("retcode"))


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
