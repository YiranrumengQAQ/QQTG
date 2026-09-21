"""REST API used by the single-page admin panel."""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ..core.app import BridgeApp
from ..logsys import get_logger
from ..models import PLATFORM_QQ, PLATFORM_TG, BridgeError
from ..security import hash_password, verify_password
from ..settings import SETTING_SCHEMA
from .auth import authenticate, check_csrf, client_ip, create_session, current_session, destroy_session, require_role

log = get_logger("system")


def build_router(app: BridgeApp) -> APIRouter:
    router = APIRouter(prefix="/api")

    # ------------------------------------------------------------ deps
    async def session_dep(request: Request) -> dict[str, Any]:
        sess = await current_session(app, request)
        if not sess:
            raise HTTPException(401, "未登录")
        check_csrf(request, sess)
        return sess

    async def admin_dep(sess: dict[str, Any] = Depends(session_dep)) -> dict[str, Any]:
        require_role(sess, "admin")
        return sess

    async def owner_dep(sess: dict[str, Any] = Depends(session_dep)) -> dict[str, Any]:
        require_role(sess, "owner")
        return sess

    async def body(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def wrap_error(exc: Exception) -> HTTPException:
        if isinstance(exc, HTTPException):
            return exc
        if isinstance(exc, (ValueError, BridgeError)):
            return HTTPException(400, str(getattr(exc, "message", exc)))
        log.exception("API error")
        return HTTPException(500, f"内部错误: {type(exc).__name__}")

    # --------------------------------------------------------- session
    @router.get("/session")
    async def get_session(request: Request) -> dict[str, Any]:
        needs_setup = await app.needs_setup()
        sess = await current_session(app, request)
        return {
            "needs_setup": needs_setup,
            "authenticated": bool(sess),
            "user": {"id": sess["id"], "username": sess["username"], "role": sess["role"]} if sess else None,
            "csrf": sess["csrf"] if sess else None,
            "title": app.settings.get("panel_title"),
            "version": app.system_info()["version"],
        }

    @router.post("/setup")
    async def setup(request: Request, response: Response) -> dict[str, Any]:
        if not await app.needs_setup():
            raise HTTPException(400, "已完成初始化")
        data = await body(request)
        if not await app.verify_setup_token(str(data.get("setup_token", ""))):
            log.warning("初始化令牌错误，来自 %s", client_ip(request))
            raise HTTPException(403, "初始化令牌错误。请查看安装脚本输出，或在服务器执行: qqtg setup-token")
        try:
            uid = await app.create_user(str(data.get("username", "")), str(data.get("password", "")), "owner")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        await app.settings.set_internal("setup_token_hash", "")
        user = await app.db.fetchone("SELECT * FROM users WHERE id=?", (uid,))
        assert user
        csrf = await create_session(app, user, request, response)
        log.info("初始化完成，Owner 账号 %s 已创建", user["username"])
        return {"ok": True, "csrf": csrf, "user": {"id": uid, "username": user["username"], "role": "owner"}}

    @router.post("/login")
    async def login(request: Request, response: Response) -> dict[str, Any]:
        data = await body(request)
        user = await authenticate(app, str(data.get("username", "")), str(data.get("password", "")), client_ip(request))
        csrf = await create_session(app, user, request, response)
        return {"ok": True, "csrf": csrf, "user": {"id": user["id"], "username": user["username"], "role": user["role"]}}

    @router.post("/logout")
    async def logout(request: Request, response: Response) -> dict[str, Any]:
        await destroy_session(app, request, response)
        return {"ok": True}

    @router.post("/me/password")
    async def change_password(request: Request, sess: dict[str, Any] = Depends(session_dep)) -> dict[str, Any]:
        data = await body(request)
        row = await app.db.fetchone("SELECT * FROM users WHERE id=?", (sess["id"],))
        if not row or not verify_password(str(data.get("old_password", "")), row["password_hash"]):
            raise HTTPException(400, "原密码错误")
        new = str(data.get("new_password", ""))
        if len(new) < 8:
            raise HTTPException(400, "新密码至少 8 位")
        await app.db.update("users", {"password_hash": hash_password(new)}, "id=?", (sess["id"],))
        return {"ok": True}

    # -------------------------------------------------------- overview
    @router.get("/overview")
    async def overview(sess: dict[str, Any] = Depends(session_dep)) -> dict[str, Any]:
        health = await app.health()
        stats = await app.stats_overview()
        bridges = await app.list_bridges()
        pending = await app.db.fetchall("SELECT * FROM chats WHERE status='pending' ORDER BY updated_at DESC")
        return {"health": health, "stats": stats, "bridges": bridges, "pending_chats": pending,
                "recent_errors": app.engine.recent_errors[:10] if app.engine else [], "system": app.system_info()}

    @router.get("/health")
    async def health(sess: dict[str, Any] = Depends(session_dep)) -> dict[str, Any]:
        return await app.health()

    @router.post("/diagnose")
    async def diagnose(sess: dict[str, Any] = Depends(admin_dep)) -> dict[str, Any]:
        return {"checks": await app.diagnose()}

    @router.get("/system")
    async def system(sess: dict[str, Any] = Depends(session_dep)) -> dict[str, Any]:
        return app.system_info()

    # ----------------------------------------------------- connections
    @router.get("/connections")
    async def connections(sess: dict[str, Any] = Depends(session_dep)) -> dict[str, Any]:
        return {"qq": await app.public_connection(PLATFORM_QQ), "telegram": await app.public_connection(PLATFORM_TG)}

    @router.post("/connections/telegram/test")
    async def tg_test(request: Request, sess: dict[str, Any] = Depends(owner_dep)) -> dict[str, Any]:
        data = await body(request)
        token = str(data.get("token", "")).strip()
        if not token:
            row = await app.get_connection(PLATFORM_TG)
            token = (row or {}).get("config", {}).get("token", "")
        if not token:
            raise HTTPException(400, "请输入 Bot Token")
        return await app.test_telegram_token(token, str(data.get("api_base", "")).strip())

    @router.put("/connections/telegram")
    async def tg_save(request: Request, sess: dict[str, Any] = Depends(owner_dep)) -> dict[str, Any]:
        data = await body(request)
        token = str(data.get("token", "")).strip()
        api_base = str(data.get("api_base", "")).strip()
        existing = await app.get_connection(PLATFORM_TG)
        if not token and existing:
            token = existing["config"].get("token", "")
        if not token or ":" not in token:
            raise HTTPException(400, "Bot Token 格式不正确")
        test = await app.test_telegram_token(token, api_base)
        if not test.get("ok"):
            raise HTTPException(400, f"Token 验证失败: {test.get('error')}")
        try:
            await app.save_connection(PLATFORM_TG, {"token": token, "api_base": api_base}, name=test.get("username") or "")
            await app.start_adapter(PLATFORM_TG)
        except Exception as exc:
            raise wrap_error(exc)
        log.info("Telegram Bot 已配置: @%s", test.get("username"))
        return {"ok": True, "bot": test}

    @router.put("/connections/qq")
    async def qq_save(request: Request, sess: dict[str, Any] = Depends(owner_dep)) -> dict[str, Any]:
        data = await body(request)
        mode = str(data.get("mode", "forward"))
        ws_url = str(data.get("ws_url", "")).strip()
        access_token = str(data.get("access_token", "")).strip()
        existing = await app.get_connection(PLATFORM_QQ)
        if not access_token and existing and data.get("keep_token", True):
            access_token = existing["config"].get("access_token", "")
        if mode == "forward" and not (ws_url.startswith("ws://") or ws_url.startswith("wss://")):
            raise HTTPException(400, "正向 WebSocket 地址需以 ws:// 或 wss:// 开头")
        try:
            await app.save_connection(PLATFORM_QQ, {"mode": mode, "ws_url": ws_url, "access_token": access_token}, name="OneBot v11")
            await app.start_adapter(PLATFORM_QQ)
        except Exception as exc:
            raise wrap_error(exc)
        log.info("QQ (OneBot) 连接已配置: mode=%s", mode)
        return {"ok": True}

    @router.post("/connections/{platform}/restart")
    async def conn_restart(platform: str, sess: dict[str, Any] = Depends(admin_dep)) -> dict[str, Any]:
        if platform not in (PLATFORM_QQ, PLATFORM_TG):
            raise HTTPException(404)
        await app.start_adapter(platform)
        return {"ok": True, "status": await app.public_connection(platform)}

    @router.delete("/connections/{platform}")
    async def conn_delete(platform: str, sess: dict[str, Any] = Depends(owner_dep)) -> dict[str, Any]:
        if platform not in (PLATFORM_QQ, PLATFORM_TG):
            raise HTTPException(404)
        await app.delete_connection(platform)
        return {"ok": True}

    # ------------------------------------------------------------ chats
    @router.get("/chats")
    async def chats(platform: Optional[str] = None, sess: dict[str, Any] = Depends(session_dep)) -> dict[str, Any]:
        return {"chats": await app.list_chats(platform)}

    @router.post("/chats/refresh")
    async def chats_refresh(sess: dict[str, Any] = Depends(admin_dep)) -> dict[str, Any]:
        return {"ok": True, "counts": await app.refresh_chats()}

    @router.post("/chats/manual")
    async def chats_manual(request: Request, sess: dict[str, Any] = Depends(owner_dep)) -> dict[str, Any]:
        data = await body(request)
        platform = str(data.get("platform", ""))
        if platform not in (PLATFORM_QQ, PLATFORM_TG):
            raise HTTPException(400, "无效平台")
        try:
            row = await app.add_chat_manual(platform, str(data.get("chat_id", "")), str(data.get("title", "")))
        except Exception as exc:
            raise wrap_error(exc)
        return {"ok": True, "chat": row}

    @router.post("/chats/{chat_id}/status")
    async def chat_status(chat_id: int, request: Request, sess: dict[str, Any] = Depends(owner_dep)) -> dict[str, Any]:
        data = await body(request)
        try:
            await app.set_chat_status(chat_id, str(data.get("status", "")), str(data.get("reason", "")))
        except Exception as exc:
            raise wrap_error(exc)
        return {"ok": True}

    @router.post("/chats/{chat_id}/check")
    async def chat_check(chat_id: int, sess: dict[str, Any] = Depends(admin_dep)) -> dict[str, Any]:
        row = await app.db.fetchone("SELECT * FROM chats WHERE id=?", (chat_id,))
        if not row:
            raise HTTPException(404, "群不存在")
        rep = await app.check_chat(row)
        return {"ok": rep.ok, "present": rep.present, "muted": rep.muted, "checks": rep.checks, "reason": rep.reason, "status": rep.status}

    @router.delete("/chats/{chat_id}")
    async def chat_delete(chat_id: int, sess: dict[str, Any] = Depends(owner_dep)) -> dict[str, Any]:
        await app.db.execute("DELETE FROM chats WHERE id=?", (chat_id,))
        if app.engine:
            await app.engine.reload_routes()
        return {"ok": True}

    # ---------------------------------------------------------- bridges
    @router.get("/bridges")
    async def bridges(sess: dict[str, Any] = Depends(session_dep)) -> dict[str, Any]:
        return {"bridges": await app.list_bridges()}

    @router.post("/bridges")
    async def bridge_create(request: Request, sess: dict[str, Any] = Depends(owner_dep)) -> dict[str, Any]:
        data = await body(request)
        try:
            bid = await app.create_bridge(str(data.get("name", "")), int(data.get("qq_chat_id", 0)), int(data.get("tg_chat_id", 0)),
                                          str(data.get("direction", "both")), data.get("options") or {}, bool(data.get("enabled", False)))
        except Exception as exc:
            raise wrap_error(exc)
        return {"ok": True, "id": bid, "bridge": await app.get_bridge(bid)}

    @router.get("/bridges/{bridge_id}")
    async def bridge_get(bridge_id: int, sess: dict[str, Any] = Depends(session_dep)) -> dict[str, Any]:
        b = await app.get_bridge(bridge_id)
        if not b:
            raise HTTPException(404, "桥接不存在")
        return {"bridge": b}

    @router.patch("/bridges/{bridge_id}")
    async def bridge_update(bridge_id: int, request: Request, sess: dict[str, Any] = Depends(admin_dep)) -> dict[str, Any]:
        data = await body(request)
        if any(k in data for k in ("name", "direction", "options")):
            require_role(sess, "owner")
        try:
            await app.update_bridge(bridge_id, name=data.get("name"), direction=data.get("direction"), enabled=data.get("enabled"),
                                    options=data.get("options"))
        except Exception as exc:
            raise wrap_error(exc)
        log.info("桥接 #%d 已更新 (%s)", bridge_id, ", ".join(k for k in data))
        return {"ok": True, "bridge": await app.get_bridge(bridge_id)}

    @router.delete("/bridges/{bridge_id}")
    async def bridge_delete(bridge_id: int, sess: dict[str, Any] = Depends(owner_dep)) -> dict[str, Any]:
        await app.delete_bridge(bridge_id)
        log.info("桥接 #%d 已删除", bridge_id)
        return {"ok": True}

    @router.post("/bridges/{bridge_id}/verify")
    async def bridge_verify(bridge_id: int, sess: dict[str, Any] = Depends(admin_dep)) -> dict[str, Any]:
        b = await app.get_bridge(bridge_id)
        if not b:
            raise HTTPException(404, "桥接不存在")
        return await app.verify_bridge(b)

    @router.post("/bridges/{bridge_id}/test")
    async def bridge_test(bridge_id: int, sess: dict[str, Any] = Depends(admin_dep)) -> dict[str, Any]:
        b = await app.get_bridge(bridge_id)
        if not b or not app.engine:
            raise HTTPException(404, "桥接不存在")
        return {"results": await app.engine.send_test(b)}

    # --------------------------------------------------------- messages
    @router.get("/messages")
    async def messages(status: Optional[str] = None, bridge_id: Optional[int] = None, limit: int = 50, before: Optional[int] = None,
                       sess: dict[str, Any] = Depends(session_dep)) -> dict[str, Any]:
        limit = max(1, min(limit, 200))
        where, params = ["1=1"], []
        if status:
            if status == "failed":
                where.append("status IN ('failed','dead')")
            else:
                where.append("status=?")
                params.append(status)
        if bridge_id:
            where.append("bridge_id=?")
            params.append(bridge_id)
        if before:
            where.append("id<?")
            params.append(before)
        rows = await app.db.fetchall(f"SELECT * FROM messages WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", (*params, limit))
        for r in rows:
            try:
                r["steps"] = json.loads(r.get("steps") or "[]")
            except ValueError:
                r["steps"] = []
        return {"messages": rows}

    @router.post("/messages/{row_id}/retry")
    async def message_retry(row_id: int, sess: dict[str, Any] = Depends(admin_dep)) -> dict[str, Any]:
        if not app.engine or not await app.engine.retry_message(row_id):
            raise HTTPException(400, "该消息无法重试")
        return {"ok": True}

    # ------------------------------------------------------------- logs
    @router.get("/logs")
    async def logs(category: Optional[str] = None, level: Optional[str] = None, limit: int = 200, before: Optional[int] = None,
                   q: Optional[str] = None, sess: dict[str, Any] = Depends(session_dep)) -> dict[str, Any]:
        limit = max(1, min(limit, 1000))
        where, params = ["1=1"], []
        if category and category != "all":
            where.append("category=?")
            params.append(category)
        if level:
            levels = {"WARNING": ["WARNING", "ERROR", "CRITICAL"], "ERROR": ["ERROR", "CRITICAL"]}.get(level.upper())
            if levels:
                where.append(f"level IN ({','.join('?' for _ in levels)})")
                params.extend(levels)
        if before:
            where.append("id<?")
            params.append(before)
        if q:
            where.append("message LIKE ?")
            params.append(f"%{q}%")
        rows = await app.db.fetchall(f"SELECT * FROM logs WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", (*params, limit))
        return {"logs": rows}

    # --------------------------------------------------------- settings
    @router.get("/settings")
    async def get_settings(sess: dict[str, Any] = Depends(session_dep)) -> dict[str, Any]:
        return {"settings": app.settings.public(), "schema": SETTING_SCHEMA}

    @router.put("/settings")
    async def put_settings(request: Request, sess: dict[str, Any] = Depends(owner_dep)) -> dict[str, Any]:
        data = await body(request)
        changed = []
        for key, value in data.items():
            if key not in SETTING_SCHEMA:
                continue
            try:
                await app.settings.set(key, value)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            changed.append(key)
        if app.processor:
            app.processor.reconfigure()
        app.storage.configure(int(app.settings.get("tmp_quota_mb")), int(app.settings.get("tmp_ttl_min")))
        if app.engine:
            for platform in (PLATFORM_QQ, PLATFORM_TG):
                a = app.engine.adapter(platform)
                if a and hasattr(a, "limiter"):
                    if platform == PLATFORM_TG:
                        a.limiter.reconfigure(float(app.settings.get("tg_rate_global_per_sec")), float(app.settings.get("tg_rate_per_chat_per_min")) / 60.0, 5)
                        a.bridge_other_bots = bool(app.settings.get("bridge_other_bots"))
                    else:
                        a.limiter.reconfigure(10, float(app.settings.get("qq_rate_per_chat_per_sec")), 3)
        log.info("设置已更新: %s", ", ".join(changed))
        return {"ok": True, "settings": app.settings.public()}

    # ------------------------------------------------------------ users
    @router.get("/users")
    async def users(sess: dict[str, Any] = Depends(owner_dep)) -> dict[str, Any]:
        return {"users": await app.db.fetchall("SELECT id, username, role, created_at, last_login_at FROM users ORDER BY id")}

    @router.post("/users")
    async def user_create(request: Request, sess: dict[str, Any] = Depends(owner_dep)) -> dict[str, Any]:
        data = await body(request)
        try:
            uid = await app.create_user(str(data.get("username", "")), str(data.get("password", "")), str(data.get("role", "viewer")))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                raise HTTPException(400, "用户名已存在")
            raise wrap_error(exc)
        log.info("用户 %s 创建了账号 %s (%s)", sess["username"], data.get("username"), data.get("role"))
        return {"ok": True, "id": uid}

    @router.post("/users/{user_id}/password")
    async def user_password(user_id: int, request: Request, sess: dict[str, Any] = Depends(owner_dep)) -> dict[str, Any]:
        data = await body(request)
        pw = str(data.get("password", ""))
        if len(pw) < 8:
            raise HTTPException(400, "密码至少 8 位")
        await app.db.update("users", {"password_hash": hash_password(pw)}, "id=?", (user_id,))
        await app.db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        return {"ok": True}

    @router.delete("/users/{user_id}")
    async def user_delete(user_id: int, sess: dict[str, Any] = Depends(owner_dep)) -> dict[str, Any]:
        if user_id == sess["id"]:
            raise HTTPException(400, "不能删除自己")
        target = await app.db.fetchone("SELECT role FROM users WHERE id=?", (user_id,))
        if target and target["role"] == "owner":
            owners = await app.db.fetchone("SELECT COUNT(*) AS n FROM users WHERE role='owner'")
            if owners and int(owners["n"]) <= 1:
                raise HTTPException(400, "至少保留一个 Owner")
        await app.db.execute("DELETE FROM users WHERE id=?", (user_id,))
        return {"ok": True}

    # ----------------------------------------------------------- backup
    @router.get("/backup")
    async def backup(with_secrets: int = 0, sess: dict[str, Any] = Depends(owner_dep)) -> Response:
        data = await app.export_backup(bool(with_secrets))
        content = json.dumps(data, ensure_ascii=False, indent=2)
        name = f"qqtg-backup-{time.strftime('%Y%m%d-%H%M%S')}.json"
        return Response(content=content, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @router.post("/restore")
    async def restore(request: Request, sess: dict[str, Any] = Depends(owner_dep)) -> dict[str, Any]:
        data = await body(request)
        try:
            counts = await app.import_backup(data)
        except Exception as exc:
            raise wrap_error(exc)
        log.info("已从备份恢复: %s", counts)
        return {"ok": True, "counts": counts}

    return router
