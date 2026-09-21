"""Application container: wires database, settings, media layer, adapters and
the bridge engine together, and exposes management operations used by both
the web panel and the CLI."""
from __future__ import annotations

import asyncio
import json
import os
import platform as py_platform
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from .. import __version__
from ..adapters.base import BaseAdapter, PermissionReport
from ..adapters.onebot import OneBotAdapter
from ..adapters.qqbot import QQOfficialAdapter
from ..adapters.telegram import TelegramAdapter
from ..config import Config
from ..db import Database
from ..logsys import db_handler, get_logger
from ..media.ffmpeg import FFmpeg
from ..media.processor import MediaProcessor
from ..media.storage import TempStorage
from ..models import PLATFORM_LABEL, PLATFORM_QQ, PLATFORM_TG, BridgeError
from ..qqbotqr import QQQRSession
from ..security import SecretBox, hash_password, mask_secret, new_token, sha256_hex, sign_media_token
from ..settings import Settings
from .engine import BridgeEngine, merge_options

log = get_logger("system")
clog = get_logger("conn")

CHAT_STATUS_LABEL = {
    "discovered": "已发现", "pending": "待授权", "authorized": "已授权", "rejected": "已拒绝", "disabled": "已禁用",
    "left": "机器人离群", "limited": "权限不足", "error": "连接异常",
}


class BridgeApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.settings = Settings(self.db)
        self.secrets = SecretBox(cfg.secret_key) if cfg.secret_key else None
        self.ffmpeg = FFmpeg(cfg.ffmpeg, cfg.ffprobe)
        self.storage = TempStorage(cfg.tmp_dir)
        self.processor: MediaProcessor | None = None
        self.engine: BridgeEngine | None = None
        self._adapter_lock = asyncio.Lock()
        self._maintenance: asyncio.Task | None = None
        self.qq_qr = QQQRSession()
        self.started_at = 0.0

    # ------------------------------------------------------------ lifecycle
    async def start(self, with_adapters: bool = True) -> None:
        self.cfg.ensure_dirs()
        await self.db.open()
        await self.settings.load()
        db_handler.keep_rows = int(self.settings.get("log_retention_rows", 20000))
        db_handler.attach(self.db, asyncio.get_event_loop())
        self.storage.configure(int(self.settings.get("tmp_quota_mb", 2048)), int(self.settings.get("tmp_ttl_min", 30)))
        self.storage.start()
        self.ffmpeg.timeout = int(self.settings.get("media_timeout_sec", 180))
        self.processor = MediaProcessor(self.db, self.settings, self.ffmpeg, self.storage)
        self.engine = BridgeEngine(self.db, self.settings, self.processor, self.storage)
        await self.engine.start()
        self.started_at = time.time()
        log.info("QQTG Bridge v%s 启动 (home=%s)", __version__, self.cfg.home)
        if with_adapters:
            await self.start_adapters()
        self._maintenance = asyncio.create_task(self._maintenance_loop())

    async def stop(self) -> None:
        if self._maintenance:
            self._maintenance.cancel()
        await self.qq_qr.cancel()
        if self.engine:
            for adapter in list(self.engine.adapters.values()):
                try:
                    await adapter.stop()
                except Exception:
                    pass
            await self.engine.stop()
        self.storage.stop()
        db_handler.stop()
        await db_handler.flush_async()
        await self.db.close()

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(3600)
                days = int(self.settings.get("message_retention_days", 30))
                cutoff = time.time() - days * 86400
                await self.db.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
                await self.db.execute("DELETE FROM message_mapping WHERE created_at < ?", (cutoff,))
                await self.db.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
                if self.processor:
                    await self.processor.cache_prune()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover
                log.debug("maintenance error: %s", exc)

    # ---------------------------------------------------------- connections
    def _require_secrets(self) -> SecretBox:
        if not self.secrets:
            raise BridgeError("NO_SECRET", "QQTG_SECRET_KEY 未配置，无法保存凭据", permanent=True)
        return self.secrets

    async def get_connection(self, platform: str) -> Optional[dict[str, Any]]:
        row = await self.db.fetchone("SELECT * FROM connections WHERE platform=? ORDER BY id LIMIT 1", (platform,))
        if not row:
            return None
        try:
            row["config"] = json.loads(self._require_secrets().decrypt(row["config_enc"]))
        except (BridgeError, ValueError) as exc:
            log.error("无法解密 %s 连接配置: %s", platform, exc)
            row["config"] = {}
        return row

    async def save_connection(self, platform: str, config: dict[str, Any], name: str = "") -> int:
        enc = self._require_secrets().encrypt(json.dumps(config, ensure_ascii=False))
        now = time.time()
        row = await self.db.fetchone("SELECT id FROM connections WHERE platform=? ORDER BY id LIMIT 1", (platform,))
        if row:
            await self.db.update("connections", {"config_enc": enc, "name": name, "updated_at": now, "enabled": 1}, "id=?", (row["id"],))
            return int(row["id"])
        return await self.db.insert("connections", {"platform": platform, "name": name, "config_enc": enc, "enabled": 1,
                                                    "created_at": now, "updated_at": now})

    async def delete_connection(self, platform: str) -> None:
        await self.stop_adapter(platform)
        await self.db.execute("DELETE FROM connections WHERE platform=?", (platform,))

    async def public_connection(self, platform: str) -> dict[str, Any]:
        row = await self.get_connection(platform)
        adapter = self.engine.adapter(platform) if self.engine else None
        out: dict[str, Any] = {"configured": bool(row), "status": adapter.status() if adapter else {"connected": False}}
        if row:
            cfg = row["config"]
            if platform == PLATFORM_TG:
                out["config"] = {"token_masked": mask_secret(cfg.get("token", "")), "api_base": cfg.get("api_base", "")}
            elif cfg.get("kind") == "official":
                out["config"] = {"kind": "official", "app_id": cfg.get("app_id", ""),
                                 "app_secret_masked": mask_secret(cfg.get("app_secret", "")),
                                 "api_base": cfg.get("api_base", "")}
            else:
                out["config"] = {"kind": "onebot", "mode": cfg.get("mode", "forward"), "ws_url": cfg.get("ws_url", ""),
                                 "access_token_masked": mask_secret(cfg.get("access_token", ""))}
            out["self_id"] = row.get("self_id")
            out["self_name"] = row.get("self_name")
        return out

    async def start_adapters(self) -> None:
        for platform in (PLATFORM_QQ, PLATFORM_TG):
            try:
                await self.start_adapter(platform)
            except Exception as exc:
                clog.error("%s 连接启动失败: %s", PLATFORM_LABEL[platform], exc)

    def _build_adapter(self, platform: str, cfg: dict[str, Any]) -> BaseAdapter:
        if platform == PLATFORM_TG:
            a = TelegramAdapter(cfg.get("token", ""), cfg.get("api_base") or self.settings.get("telegram_api_base"),
                                download_limit_mb=int(self.settings.get("tg_download_limit_mb", 20)),
                                rate_global_per_sec=float(self.settings.get("tg_rate_global_per_sec", 25)),
                                rate_chat_per_min=float(self.settings.get("tg_rate_per_chat_per_min", 20)))
            a.bridge_other_bots = bool(self.settings.get("bridge_other_bots", False))
            return a
        if cfg.get("kind") == "official":
            return QQOfficialAdapter(cfg.get("app_id", ""), cfg.get("app_secret", ""), api_base=cfg.get("api_base", ""),
                                     public_base=self.effective_public_base(),
                                     rate_chat_per_sec=float(self.settings.get("qqbot_rate_per_chat_per_sec", 0.33)),
                                     media_url_maker=self.make_media_url)
        return OneBotAdapter(cfg.get("mode", "forward"), cfg.get("ws_url", "ws://127.0.0.1:3001"), cfg.get("access_token", ""),
                             rate_chat_per_sec=float(self.settings.get("qq_rate_per_chat_per_sec", 1.5)))

    # ------------------------------------------------------ official QQ bot
    def effective_public_base(self) -> str:
        """Public base URL used when handing media to the official QQ servers."""
        return str(self.settings.get("public_media_base") or self.cfg.public_url or "").strip().rstrip("/")

    def make_media_url(self, absolute_path: str, name: str, ttl_sec: int) -> str:
        """Signed, expiring URL the QQ servers can fetch for rich media upload."""
        from urllib.parse import quote
        base = self.effective_public_base()
        if not base:
            raise BridgeError("MEDIA_NO_PUBLIC_URL", "未配置公网地址，官方机器人无法发送媒体", permanent=True)
        try:
            rel = str(Path(absolute_path).resolve().relative_to(self.cfg.tmp_dir.resolve()))
        except ValueError as exc:
            raise BridgeError("MEDIA_NO_PUBLIC_URL", "媒体文件不在临时目录内", permanent=True) from exc
        expires = int(time.time()) + max(60, ttl_sec)
        sig = sign_media_token(self._require_secret_key(), rel, expires, name)
        return f"{base}/qqbot/media?p={quote(rel)}&e={expires}&n={quote(name)}&s={sig}"

    def _require_secret_key(self) -> str:
        if not self.cfg.secret_key:
            raise BridgeError("NO_SECRET", "QQTG_SECRET_KEY 未配置", permanent=True)
        return self.cfg.secret_key

    async def test_qqbot_credentials(self, app_id: str, app_secret: str, api_base: str = "") -> dict[str, Any]:
        adapter = QQOfficialAdapter(app_id, app_secret, api_base=api_base)
        try:
            me = await adapter.me()
            return {"ok": True, "id": me.get("id"), "username": me.get("username"), "avatar": me.get("avatar", "")}
        except BridgeError as exc:
            return {"ok": False, "error": exc.message}
        finally:
            if adapter._http:
                await adapter._http.aclose()

    async def save_qqbot_official(self, app_id: str, app_secret: str, api_base: str = "") -> dict[str, Any]:
        test = await self.test_qqbot_credentials(app_id, app_secret, api_base)
        if not test.get("ok"):
            raise BridgeError("UNAUTHORIZED", str(test.get("error") or "AppID/AppSecret 验证失败"), permanent=True)
        await self.save_connection(PLATFORM_QQ, {"kind": "official", "app_id": app_id, "app_secret": app_secret,
                                                 "api_base": api_base}, name=str(test.get("username") or "QQ 官方机器人"))
        await self.start_adapter(PLATFORM_QQ)
        return test

    async def start_adapter(self, platform: str) -> Optional[BaseAdapter]:
        assert self.engine
        async with self._adapter_lock:
            await self._stop_adapter_unlocked(platform)
            row = await self.get_connection(platform)
            if not row or not row["enabled"] or not row["config"]:
                return None
            adapter = self._build_adapter(platform, row["config"])
            if platform == PLATFORM_QQ and hasattr(adapter, "seed_chats"):
                known = await self.db.fetchall(
                    "SELECT chat_id, title FROM chats WHERE platform=? AND status NOT IN ('rejected','disabled')", (PLATFORM_QQ,))
                adapter.seed_chats(known)
            self.engine.register_adapter(adapter)
            try:
                await adapter.start()
            except BridgeError as exc:
                adapter.last_error = exc.message
                clog.error("%s 连接失败: %s", PLATFORM_LABEL[platform], exc.message)
                if exc.code == "UNAUTHORIZED":
                    return adapter
                # keep the adapter registered; it will retry in its own loop where applicable
                if platform == PLATFORM_TG:
                    asyncio.create_task(self._retry_start(platform, adapter))
                return adapter
            if adapter.self_id:
                await self.db.update("connections", {"self_id": adapter.self_id, "self_name": adapter.self_name}, "id=?", (row["id"],))
            return adapter

    async def _retry_start(self, platform: str, adapter: BaseAdapter) -> None:
        delay = 5.0
        while self.engine and self.engine.adapter(platform) is adapter and not adapter.connected:
            await asyncio.sleep(delay)
            try:
                await adapter.start()
                clog.info("%s 已连接（重试成功）", PLATFORM_LABEL[platform])
                row = await self.get_connection(platform)
                if row and adapter.self_id:
                    await self.db.update("connections", {"self_id": adapter.self_id, "self_name": adapter.self_name}, "id=?", (row["id"],))
                return
            except BridgeError as exc:
                adapter.last_error = exc.message
                if exc.code == "UNAUTHORIZED":
                    return
                delay = min(delay * 2, 60)

    async def _stop_adapter_unlocked(self, platform: str) -> None:
        assert self.engine
        old = self.engine.adapter(platform)
        if old:
            self.engine.unregister_adapter(platform)
            try:
                await old.stop()
            except Exception:
                pass

    async def stop_adapter(self, platform: str) -> None:
        async with self._adapter_lock:
            await self._stop_adapter_unlocked(platform)

    async def test_telegram_token(self, token: str, api_base: str = "") -> dict[str, Any]:
        adapter = TelegramAdapter(token, api_base or self.settings.get("telegram_api_base"))
        try:
            me = await adapter.api("getMe", timeout=20)
            return {"ok": True, "id": me.get("id"), "username": me.get("username"), "name": me.get("first_name")}
        except BridgeError as exc:
            return {"ok": False, "error": exc.message}
        finally:
            if adapter._client:
                await adapter._client.aclose()

    # ----------------------------------------------------------------- chats
    async def list_chats(self, platform: str | None = None) -> list[dict[str, Any]]:
        if platform:
            rows = await self.db.fetchall("SELECT * FROM chats WHERE platform=? ORDER BY status='pending' DESC, title", (platform,))
        else:
            rows = await self.db.fetchall("SELECT * FROM chats ORDER BY platform, status='pending' DESC, title")
        bridges = await self.db.fetchall("SELECT id, name, qq_chat_id, tg_chat_id FROM bridges")
        by_chat: dict[int, list[dict[str, Any]]] = {}
        for b in bridges:
            by_chat.setdefault(b["qq_chat_id"], []).append({"id": b["id"], "name": b["name"]})
            by_chat.setdefault(b["tg_chat_id"], []).append({"id": b["id"], "name": b["name"]})
        for r in rows:
            r["status_label"] = CHAT_STATUS_LABEL.get(r["status"], r["status"])
            r["permissions"] = json.loads(r.get("permissions") or "{}")
            r["bridges"] = by_chat.get(r["id"], [])
            r["platform_label"] = PLATFORM_LABEL.get(r["platform"], r["platform"])
        return rows

    async def refresh_chats(self) -> dict[str, int]:
        assert self.engine
        counts = {}
        for platform, adapter in self.engine.adapters.items():
            if not adapter.connected:
                continue
            infos = await adapter.list_chats()
            for info in infos:
                await self.engine.upsert_chat(platform, info, force=True)
            counts[platform] = len(infos)
        return counts

    async def set_chat_status(self, chat_id: int, status: str, reason: str = "") -> None:
        if status not in CHAT_STATUS_LABEL:
            raise ValueError("无效状态")
        await self.db.update("chats", {"status": status, "status_reason": reason, "updated_at": time.time()}, "id=?", (chat_id,))
        if self.engine:
            await self.engine.reload_routes()

    async def check_chat(self, chat_row: dict[str, Any]) -> PermissionReport:
        assert self.engine
        adapter = self.engine.adapter(chat_row["platform"])
        if not adapter:
            report = PermissionReport(ok=False, present=False, reason=f"{PLATFORM_LABEL[chat_row['platform']]} 未配置", status="error")
        else:
            report = await adapter.check_permissions(str(chat_row["chat_id"]))
            if chat_row["platform"] == PLATFORM_TG and report.present:
                info = await adapter.get_chat(str(chat_row["chat_id"])) if hasattr(adapter, "get_chat") else None
                if info:
                    await self.db.update("chats", {"title": info.title, "member_count": info.member_count}, "id=?", (chat_row["id"],))
        values: dict[str, Any] = {"permissions": json.dumps({"checks": report.checks, "checked_at": time.time(), "reason": report.reason}),
                                  "updated_at": time.time()}
        if chat_row["status"] not in ("rejected", "disabled", "pending", "discovered") or report.status == "left":
            if report.status == "left":
                values.update({"status": "left", "status_reason": report.reason})
            elif not report.ok and chat_row["status"] not in ("pending", "discovered"):
                values.update({"status": "limited", "status_reason": report.reason})
            elif report.ok and chat_row["status"] in ("limited", "error", "left"):
                values.update({"status": "authorized", "status_reason": ""})
        await self.db.update("chats", values, "id=?", (chat_row["id"],))
        if "status" in values and self.engine:
            await self.engine.reload_routes()
        return report

    async def add_chat_manual(self, platform: str, chat_id: str, title: str = "") -> dict[str, Any]:
        assert self.engine
        chat_id = chat_id.strip()
        if not chat_id.lstrip("-").isdigit():
            raise ValueError("群 ID 必须是数字")
        adapter = self.engine.adapter(platform)
        if platform == PLATFORM_TG and adapter and hasattr(adapter, "get_chat"):
            info = await adapter.get_chat(chat_id)
            if info:
                title = info.title
        from ..adapters.base import ChatInfo
        await self.engine.upsert_chat(platform, ChatInfo(chat_id, title or chat_id), force=True)
        row = await self.db.fetchone("SELECT * FROM chats WHERE platform=? AND chat_id=?", (platform, chat_id))
        assert row
        return row

    # --------------------------------------------------------------- bridges
    async def list_bridges(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """SELECT b.*, q.chat_id AS qq_chat, q.title AS qq_title, q.status AS qq_status, q.member_count AS qq_members,
                      t.chat_id AS tg_chat, t.title AS tg_title, t.status AS tg_status, t.member_count AS tg_members
               FROM bridges b JOIN chats q ON q.id=b.qq_chat_id JOIN chats t ON t.id=b.tg_chat_id ORDER BY b.id""")
        today = time.strftime("%Y-%m-%d")
        stats = await self.db.fetchall("SELECT bridge_id, direction, SUM(sent) AS sent, SUM(failed) AS failed, "
                                       "SUM(CASE WHEN kind!='text' THEN sent ELSE 0 END) AS media FROM stats_daily WHERE day=? GROUP BY bridge_id, direction", (today,))
        by_bridge: dict[int, dict[str, Any]] = {}
        for s in stats:
            d = by_bridge.setdefault(s["bridge_id"], {"sent": 0, "failed": 0, "media": 0, "qq_to_tg": 0, "tg_to_qq": 0})
            d["sent"] += s["sent"] or 0
            d["failed"] += s["failed"] or 0
            d["media"] += s["media"] or 0
            d[s["direction"]] += s["sent"] or 0
        for r in rows:
            r["options"] = merge_options(json.loads(r.get("options") or "{}"))
            r["today"] = by_bridge.get(r["id"], {"sent": 0, "failed": 0, "media": 0, "qq_to_tg": 0, "tg_to_qq": 0})
            r["warnings"] = self._bridge_warnings(r)
        return rows

    def _bridge_warnings(self, r: dict[str, Any]) -> list[str]:
        w: list[str] = []
        for side, label in (("qq", "QQ"), ("tg", "TG")):
            st = r.get(f"{side}_status")
            if st in ("left", "limited", "error", "rejected", "disabled"):
                w.append(f"{label} {CHAT_STATUS_LABEL.get(st, st)}")
        if self.engine:
            for platform, label in ((PLATFORM_QQ, "QQ"), (PLATFORM_TG, "TG")):
                a = self.engine.adapter(platform)
                if not a or not a.connected:
                    w.append(f"{label} 未连接")
        if r.get("today", {}).get("failed"):
            w.append(f"今日 {r['today']['failed']} 条失败")
        return w

    async def get_bridge(self, bridge_id: int) -> Optional[dict[str, Any]]:
        for b in await self.list_bridges():
            if b["id"] == bridge_id:
                return b
        return None

    async def create_bridge(self, name: str, qq_chat_row_id: int, tg_chat_row_id: int, direction: str = "both",
                            options: dict[str, Any] | None = None, enabled: bool = False) -> int:
        if direction not in ("both", "qq_to_tg", "tg_to_qq"):
            raise ValueError("无效方向")
        qq = await self.db.fetchone("SELECT * FROM chats WHERE id=? AND platform=?", (qq_chat_row_id, PLATFORM_QQ))
        tg = await self.db.fetchone("SELECT * FROM chats WHERE id=? AND platform=?", (tg_chat_row_id, PLATFORM_TG))
        if not qq or not tg:
            raise ValueError("请选择有效的 QQ 群和 Telegram 群")
        dup = await self.db.fetchone("SELECT id FROM bridges WHERE qq_chat_id=? AND tg_chat_id=?", (qq_chat_row_id, tg_chat_row_id))
        if dup:
            raise ValueError("这两个群之间已经存在桥接")
        opts = merge_options(options)
        if "event_sync" not in (options or {}):
            opts["event_sync"] = bool(self.settings.get("event_sync_default", False))
        now = time.time()
        bridge_id = await self.db.insert("bridges", {"name": name.strip() or f"{qq['title']} ↔ {tg['title']}", "enabled": 1 if enabled else 0,
                                                     "direction": direction, "qq_chat_id": qq_chat_row_id, "tg_chat_id": tg_chat_row_id,
                                                     "options": json.dumps(opts, ensure_ascii=False), "created_at": now, "updated_at": now})
        # creating a bridge from the panel implies authorization of both chats
        for row in (qq, tg):
            if row["status"] in ("discovered", "pending"):
                await self.db.update("chats", {"status": "authorized", "status_reason": "", "updated_at": now}, "id=?", (row["id"],))
        if self.engine:
            await self.engine.reload_routes()
        log.info("创建桥接 #%d: %s (%s ↔ %s)", bridge_id, name, qq["title"], tg["title"])
        return bridge_id

    async def update_bridge(self, bridge_id: int, **fields: Any) -> None:
        values: dict[str, Any] = {"updated_at": time.time()}
        if "name" in fields and fields["name"] is not None:
            values["name"] = str(fields["name"]).strip()[:100]
        if "direction" in fields and fields["direction"] is not None:
            if fields["direction"] not in ("both", "qq_to_tg", "tg_to_qq"):
                raise ValueError("无效方向")
            values["direction"] = fields["direction"]
        if "enabled" in fields and fields["enabled"] is not None:
            values["enabled"] = 1 if fields["enabled"] else 0
        if "options" in fields and fields["options"] is not None:
            current = await self.db.fetchone("SELECT options FROM bridges WHERE id=?", (bridge_id,))
            merged = merge_options(json.loads((current or {}).get("options") or "{}"))
            incoming = fields["options"]
            for k, v in incoming.items():
                if k == "media" and isinstance(v, dict):
                    merged["media"].update({kk: bool(vv) for kk, vv in v.items()})
                elif k == "display_mode":
                    merged[k] = v if v in ("simple", "standard", "full") else None
                elif k in ("reply_sync", "recall_sync", "edit_sync", "event_sync"):
                    merged[k] = bool(v)
            values["options"] = json.dumps(merged, ensure_ascii=False)
        await self.db.update("bridges", values, "id=?", (bridge_id,))
        if self.engine:
            await self.engine.reload_routes()

    async def delete_bridge(self, bridge_id: int) -> None:
        await self.db.execute("DELETE FROM bridges WHERE id=?", (bridge_id,))
        if self.engine:
            await self.engine.reload_routes()

    async def verify_bridge(self, bridge: dict[str, Any]) -> dict[str, Any]:
        """Permission check on both sides (used before enabling a bridge)."""
        qq_row = await self.db.fetchone("SELECT * FROM chats WHERE id=?", (bridge["qq_chat_id"],))
        tg_row = await self.db.fetchone("SELECT * FROM chats WHERE id=?", (bridge["tg_chat_id"],))
        result: dict[str, Any] = {}
        for label, row in (("qq", qq_row), ("tg", tg_row)):
            if not row:
                result[label] = {"ok": False, "reason": "群不存在"}
                continue
            rep = await self.check_chat(row)
            result[label] = {"ok": rep.ok, "present": rep.present, "muted": rep.muted, "checks": rep.checks, "reason": rep.reason}
        result["ok"] = bool(result["qq"].get("ok") and result["tg"].get("ok"))
        return result

    # ---------------------------------------------------------------- health
    async def health(self) -> dict[str, Any]:
        assert self.engine
        qq = self.engine.adapter(PLATFORM_QQ)
        tg = self.engine.adapter(PLATFORM_TG)
        storage = self.storage.status()
        ffmpeg_ok = self.ffmpeg.available()
        components = {
            "qq": {"ok": bool(qq and qq.connected), "label": "QQ Adapter", "detail": (qq.last_error if qq and not qq.connected else "") or ("未配置" if not qq else "")},
            "telegram": {"ok": bool(tg and tg.connected), "label": "Telegram API", "detail": (tg.last_error if tg and not tg.connected else "") or ("未配置" if not tg else "")},
            "database": {"ok": await self.db.ping(), "label": "Database", "detail": str(self.cfg.db_path)},
            "media_worker": {"ok": ffmpeg_ok and not storage["paused"], "label": "Media Worker", "detail": "FFmpeg 缺失" if not ffmpeg_ok else ("磁盘不足已暂停" if storage["paused"] else "")},
            "queue": {"ok": self.engine.queue.qsize() < 500, "label": "Queue", "detail": f"{self.engine.queue.qsize()} 待处理"},
            "storage": {"ok": storage["percent"] < 80, "label": "Storage", "detail": f"{storage['used_bytes'] // 1024 // 1024} MB / {storage['quota_bytes'] // 1024 // 1024} MB"},
            "ffmpeg": {"ok": ffmpeg_ok, "label": "FFmpeg", "detail": await self.ffmpeg.version() if ffmpeg_ok else "未安装"},
        }
        return {
            "version": __version__,
            "uptime": time.time() - self.started_at if self.started_at else 0,
            "components": components,
            "queue": self.engine.queue_status(),
            "storage": storage,
            "adapters": {"qq": qq.status() if qq else None, "telegram": tg.status() if tg else None},
            "system": {"python": py_platform.python_version(), "os": py_platform.platform(), "cpus": os.cpu_count()},
        }

    async def diagnose(self) -> list[dict[str, Any]]:
        assert self.engine
        checks: list[dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str = "", warn: bool = False) -> None:
            checks.append({"name": name, "status": "PASS" if ok else ("WARN" if warn else "FAIL"), "detail": detail})

        qq = self.engine.adapter(PLATFORM_QQ)
        tg = self.engine.adapter(PLATFORM_TG)
        add("QQ Connection", bool(qq and qq.connected), (qq.last_error if qq else "未配置") if not (qq and qq.connected) else f"[{getattr(qq, 'kind_label', 'QQ')}] {qq.self_name} ({qq.self_id})")
        add("TG Connection", bool(tg and tg.connected), (tg.last_error if tg else "未配置") if not (tg and tg.connected) else f"@{tg.self_name}")
        add("Database", await self.db.ping(), str(self.cfg.db_path))
        try:
            probe = self.cfg.tmp_dir / ".probe"
            probe.write_text("ok")
            probe.unlink()
            add("Temp dir writable", True, str(self.cfg.tmp_dir))
        except OSError as exc:
            add("Temp dir writable", False, str(exc))
        free = self.storage.disk_free_bytes()
        add("Disk free", free > 500 * 1024 * 1024, f"{free // 1024 // 1024} MB", warn=free > 200 * 1024 * 1024)
        ff = self.ffmpeg.available()
        add("FFmpeg", ff, await self.ffmpeg.version() if ff else "未安装：语音/贴纸/动画转换不可用")
        if ff:
            add("FFprobe", self.ffmpeg.probe_available(), "" if self.ffmpeg.probe_available() else "缺少 ffprobe，使用 ffmpeg 回退探测", warn=True)
            add("Codec libopus (TG voice)", await self.ffmpeg.has_encoder("libopus", "opus"), "", warn=True)
            add("Codec libx264 (video)", await self.ffmpeg.has_encoder("libx264"), "", warn=True)
            add("Codec libmp3lame (mp3)", await self.ffmpeg.has_encoder("libmp3lame"), "", warn=True)
        qs = self.engine.queue_status()
        add("Queue", qs["workers"] > 0, f"{qs['workers']} workers, depth {qs['depth']}")
        bridges = await self.list_bridges()
        add("Bridges", True, f"{len([b for b in bridges if b['enabled']])} 启用 / {len(bridges)} 总计", warn=True)
        for b in bridges:
            if not b["enabled"]:
                continue
            v = await self.verify_bridge(b)
            add(f"Bridge #{b['id']} {b['name']}", v["ok"], "; ".join(f"{k}: {v[k].get('reason') or 'ok'}" for k in ("qq", "tg")))
        return checks

    async def stats_overview(self) -> dict[str, Any]:
        today = time.strftime("%Y-%m-%d")
        rows = await self.db.fetchall("SELECT direction, kind, SUM(sent) AS sent, SUM(failed) AS failed FROM stats_daily WHERE day=? GROUP BY direction, kind", (today,))
        out = {"today": {"sent": 0, "failed": 0, "qq_to_tg": 0, "tg_to_qq": 0, "kinds": {}}}
        for r in rows:
            out["today"]["sent"] += r["sent"] or 0
            out["today"]["failed"] += r["failed"] or 0
            out["today"][r["direction"]] += r["sent"] or 0
            out["today"]["kinds"][r["kind"]] = out["today"]["kinds"].get(r["kind"], 0) + (r["sent"] or 0)
        week = await self.db.fetchall("SELECT day, SUM(sent) AS sent, SUM(failed) AS failed FROM stats_daily WHERE day >= date('now','-6 days') GROUP BY day ORDER BY day")
        out["week"] = week
        issues = await self.db.fetchall("SELECT error_code, COUNT(*) AS n FROM messages WHERE status='dead' AND created_at > ? GROUP BY error_code ORDER BY n DESC LIMIT 10", (time.time() - 86400,))
        out["issues"] = issues
        out["counters"] = self.engine.counters if self.engine else {}
        return out

    # ---------------------------------------------------------- setup/users
    async def needs_setup(self) -> bool:
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM users")
        return not row or int(row["n"]) == 0

    async def generate_setup_token(self) -> str:
        token = new_token(24)
        await self.settings.set_internal("setup_token_hash", sha256_hex(token))
        return token

    async def verify_setup_token(self, token: str) -> bool:
        stored = self.settings.get("setup_token_hash")
        return bool(stored) and sha256_hex(token.strip()) == stored

    async def create_user(self, username: str, password: str, role: str) -> int:
        username = username.strip()
        if not (3 <= len(username) <= 32) or not all(c.isalnum() or c in "_-." for c in username):
            raise ValueError("用户名需为 3-32 位字母数字")
        if len(password) < 8:
            raise ValueError("密码至少 8 位")
        if role not in ("owner", "admin", "viewer"):
            raise ValueError("无效角色")
        return await self.db.insert("users", {"username": username, "password_hash": hash_password(password), "role": role, "created_at": time.time()})

    # ------------------------------------------------------------- backup
    async def export_backup(self, with_secrets: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {"format": "qqtg-backup", "version": 1, "app_version": __version__, "created_at": time.time()}
        data["settings"] = {k: v for k, v in (await self.db.all_settings()).items() if k != "setup_token_hash"}
        data["chats"] = await self.db.fetchall("SELECT * FROM chats")
        data["bridges"] = await self.db.fetchall("SELECT * FROM bridges")
        data["users"] = await self.db.fetchall("SELECT id, username, password_hash, role, created_at FROM users")
        conns = await self.db.fetchall("SELECT id, platform, name, enabled, config_enc FROM connections")
        if with_secrets:
            data["connections"] = conns  # still encrypted with the secret key
        else:
            data["connections"] = [{k: v for k, v in c.items() if k != "config_enc"} for c in conns]
        return data

    async def import_backup(self, data: dict[str, Any]) -> dict[str, int]:
        if data.get("format") != "qqtg-backup":
            raise ValueError("不是有效的 QQTG 备份文件")
        counts = {"settings": 0, "chats": 0, "bridges": 0, "users": 0, "connections": 0}
        for k, v in (data.get("settings") or {}).items():
            await self.db.set_setting(k, v)
            counts["settings"] += 1
        id_map: dict[int, int] = {}
        now = time.time()
        for c in data.get("chats") or []:
            row = await self.db.fetchone("SELECT id FROM chats WHERE platform=? AND chat_id=?", (c["platform"], str(c["chat_id"])))
            if row:
                id_map[c["id"]] = row["id"]
                await self.db.update("chats", {"title": c.get("title", ""), "status": c.get("status", "discovered"), "updated_at": now}, "id=?", (row["id"],))
            else:
                new_id = await self.db.insert("chats", {"platform": c["platform"], "chat_id": str(c["chat_id"]), "title": c.get("title", ""),
                                                        "chat_type": c.get("chat_type", "group"), "member_count": c.get("member_count"),
                                                        "status": c.get("status", "discovered"), "status_reason": c.get("status_reason", ""),
                                                        "permissions": c.get("permissions", "{}"), "created_at": now, "updated_at": now})
                id_map[c["id"]] = new_id
            counts["chats"] += 1
        for b in data.get("bridges") or []:
            qq_id, tg_id = id_map.get(b["qq_chat_id"]), id_map.get(b["tg_chat_id"])
            if not qq_id or not tg_id:
                continue
            dup = await self.db.fetchone("SELECT id FROM bridges WHERE qq_chat_id=? AND tg_chat_id=?", (qq_id, tg_id))
            if dup:
                await self.db.update("bridges", {"name": b["name"], "enabled": b.get("enabled", 0), "direction": b.get("direction", "both"),
                                                 "options": b.get("options", "{}"), "updated_at": now}, "id=?", (dup["id"],))
            else:
                await self.db.insert("bridges", {"name": b["name"], "enabled": b.get("enabled", 0), "direction": b.get("direction", "both"),
                                                 "qq_chat_id": qq_id, "tg_chat_id": tg_id, "options": b.get("options", "{}"),
                                                 "created_at": now, "updated_at": now})
            counts["bridges"] += 1
        for u in data.get("users") or []:
            existing = await self.db.fetchone("SELECT id FROM users WHERE username=?", (u["username"],))
            if existing:
                continue
            await self.db.insert("users", {"username": u["username"], "password_hash": u["password_hash"], "role": u.get("role", "viewer"), "created_at": now})
            counts["users"] += 1
        for c in data.get("connections") or []:
            if not c.get("config_enc"):
                continue
            try:
                self._require_secrets().decrypt(c["config_enc"])
            except (BridgeError, ValueError):
                log.warning("备份中的 %s 凭据无法用当前密钥解密，已跳过（请重新输入）", c["platform"])
                continue
            existing = await self.db.fetchone("SELECT id FROM connections WHERE platform=?", (c["platform"],))
            if existing:
                await self.db.update("connections", {"config_enc": c["config_enc"], "name": c.get("name", ""), "enabled": c.get("enabled", 1), "updated_at": now}, "id=?", (existing["id"],))
            else:
                await self.db.insert("connections", {"platform": c["platform"], "name": c.get("name", ""), "config_enc": c["config_enc"],
                                                     "enabled": c.get("enabled", 1), "created_at": now, "updated_at": now})
            counts["connections"] += 1
        await self.settings.load()
        if self.engine:
            await self.engine.reload_routes()
        return counts

    def system_info(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "home": str(self.cfg.home),
            "bind": f"{self.cfg.bind}:{self.cfg.port}",
            "public_url": self.cfg.public_url,
            "ffmpeg": shutil.which(self.cfg.ffmpeg) or "",
            "python": py_platform.python_version(),
        }
