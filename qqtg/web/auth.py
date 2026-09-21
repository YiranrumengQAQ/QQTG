"""Session authentication, CSRF and login throttling for the web panel."""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import HTTPException, Request, Response

from ..core.app import BridgeApp
from ..logsys import get_logger
from ..security import new_token, sha256_hex, verify_password

log = get_logger("security")

COOKIE_NAME = "qqtg_session"
ROLE_LEVEL = {"viewer": 0, "admin": 1, "owner": 2}
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60


class LoginThrottle:
    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}

    def check(self, ip: str) -> None:
        until = self._locked.get(ip)
        if until and until > time.time():
            raise HTTPException(429, f"登录失败次数过多，请 {int((until - time.time()) / 60) + 1} 分钟后再试")

    def fail(self, ip: str) -> None:
        now = time.time()
        hist = [t for t in self._failures.get(ip, []) if now - t < LOCK_SECONDS]
        hist.append(now)
        self._failures[ip] = hist
        if len(hist) >= MAX_FAILURES:
            self._locked[ip] = now + LOCK_SECONDS
            self._failures[ip] = []
            log.warning("IP %s 登录失败 %d 次，已临时锁定", ip, MAX_FAILURES)

    def success(self, ip: str) -> None:
        self._failures.pop(ip, None)
        self._locked.pop(ip, None)


throttle = LoginThrottle()


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real
    return request.client.host if request.client else "unknown"


def is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto.split(",")[0].strip() == "https"


async def create_session(app: BridgeApp, user: dict[str, Any], request: Request, response: Response) -> str:
    token = new_token(32)
    csrf = new_token(16)
    hours = int(app.settings.get("session_hours", 72))
    now = time.time()
    await app.db.insert("sessions", {"token_hash": sha256_hex(token), "user_id": user["id"], "csrf": csrf,
                                     "ip": client_ip(request), "created_at": now, "expires_at": now + hours * 3600})
    response.set_cookie(COOKIE_NAME, token, max_age=hours * 3600, httponly=True, samesite="lax", secure=is_https(request), path="/")
    return csrf


async def destroy_session(app: BridgeApp, request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        await app.db.execute("DELETE FROM sessions WHERE token_hash=?", (sha256_hex(token),))
    response.delete_cookie(COOKIE_NAME, path="/")


async def current_session(app: BridgeApp, request: Request) -> Optional[dict[str, Any]]:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    row = await app.db.fetchone(
        "SELECT s.csrf, s.expires_at, u.id, u.username, u.role FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
        (sha256_hex(token),))
    if not row or row["expires_at"] < time.time():
        return None
    return row


async def authenticate(app: BridgeApp, username: str, password: str, ip: str) -> dict[str, Any]:
    throttle.check(ip)
    row = await app.db.fetchone("SELECT * FROM users WHERE username=?", (username.strip(),))
    if not row or not verify_password(password, row["password_hash"]):
        throttle.fail(ip)
        log.warning("登录失败: 用户 %s 来自 %s", username[:32], ip)
        raise HTTPException(401, "用户名或密码错误")
    throttle.success(ip)
    await app.db.update("users", {"last_login_at": time.time()}, "id=?", (row["id"],))
    log.info("用户 %s 登录成功 (%s)", row["username"], ip)
    return row


def require_role(session: dict[str, Any], minimum: str) -> None:
    if ROLE_LEVEL.get(session.get("role", "viewer"), 0) < ROLE_LEVEL[minimum]:
        raise HTTPException(403, "权限不足")


def check_csrf(request: Request, session: dict[str, Any]) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    header = request.headers.get("x-csrf-token", "")
    if not header or header != session["csrf"]:
        raise HTTPException(403, "CSRF 校验失败，请刷新页面")
