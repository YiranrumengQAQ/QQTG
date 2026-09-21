"""Bridge core: normalise → deduplicate → loop-protect → route → queue →
media → send → map.

Loop protection has three layers:
  1. adapters drop messages sent by the bot itself (``self_id``),
  2. incoming ids that exist as a *target* in ``message_mapping`` are dropped
     (that is a copy we produced),
  3. every (platform, chat, message_id, bridge) is unique in ``messages`` so
     duplicated deliveries from a flaky connection are ignored.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from ..adapters.base import BaseAdapter, ChatInfo, OutgoingMessage
from ..db import Database
from ..logsys import get_logger
from ..media.processor import MediaProcessor, Prepared
from ..media.storage import TempStorage
from ..models import PLATFORM_TG, SendResult, UnifiedMessage, platform_label
from ..settings import Settings
from .formatter import render, render_edit

log = get_logger("message")
slog = get_logger("system")

DEFAULT_BRIDGE_OPTIONS: dict[str, Any] = {
    "media": {"text": True, "photo": True, "animation": True, "video": True, "audio": True, "voice": True,
              "document": True, "sticker": True, "forward": True},
    "reply_sync": True,
    "recall_sync": False,
    "edit_sync": False,
    "event_sync": False,
    "display_mode": None,
}


def merge_options(options: dict[str, Any] | None) -> dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULT_BRIDGE_OPTIONS))
    for k, v in (options or {}).items():
        if k == "media" and isinstance(v, dict):
            merged["media"].update({kk: bool(vv) for kk, vv in v.items()})
        else:
            merged[k] = v
    return merged


@dataclass(order=True)
class Job:
    priority: int
    seq: int
    row_id: int = field(compare=False)
    message: UnifiedMessage = field(compare=False)
    bridge: dict[str, Any] = field(compare=False)
    target_platform: str = field(compare=False)
    target_chat_id: str = field(compare=False)
    attempts: int = field(default=0, compare=False)
    steps: list[dict[str, Any]] = field(default_factory=list, compare=False)
    started: float = field(default_factory=time.time, compare=False)

    def step(self, name: str, ok: bool = True, detail: str = "") -> None:
        self.steps.append({"t": round(time.time() - self.started, 3), "name": name, "ok": ok, "detail": detail})


class BridgeEngine:
    def __init__(self, db: Database, settings: Settings, processor: MediaProcessor, storage: TempStorage):
        self.db = db
        self.settings = settings
        self.processor = processor
        self.storage = storage
        self.adapters: dict[str, BaseAdapter] = {}
        self.queue: asyncio.PriorityQueue[Job] = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self._workers: list[asyncio.Task] = []
        self._routes: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._chat_cache: dict[tuple[str, str], tuple[str, float]] = {}
        self._chat_status: dict[int, str] = {}
        self._pending_retries: set[asyncio.Task] = set()
        self.started_at = time.time()
        self.counters = {"received": 0, "sent": 0, "failed": 0, "dropped": 0, "duplicates": 0, "loops": 0}
        self.recent_errors: list[dict[str, Any]] = []
        self._running = False

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await self.reload_routes()
        self._running = True
        n = max(1, int(self.settings.get("send_workers", 4)))
        self._workers = [asyncio.create_task(self._worker(i), name=f"bridge-worker-{i}") for i in range(n)]
        slog.info("Bridge Core 已启动: %d 个发送 worker, %d 条路由", n, sum(len(v) for v in self._routes.values()) // 1)

    async def stop(self) -> None:
        self._running = False
        for t in self._workers:
            t.cancel()
        for t in list(self._pending_retries):
            t.cancel()
        self._workers = []

    def register_adapter(self, adapter: BaseAdapter) -> None:
        adapter.on_message = self.handle_incoming
        adapter.on_event = self.handle_event
        self.adapters[adapter.platform] = adapter

    def unregister_adapter(self, platform: str) -> None:
        self.adapters.pop(platform, None)

    def adapter(self, platform: str) -> Optional[BaseAdapter]:
        return self.adapters.get(platform)

    # -------------------------------------------------------------- routing
    BRIDGE_SELECT = (
        """SELECT b.*, ca.chat_id AS a_chat, ca.platform AS a_platform, ca.title AS a_title, ca.status AS a_status,
                  cb.chat_id AS b_chat, cb.platform AS b_platform, cb.title AS b_title, cb.status AS b_status
           FROM bridges b JOIN chats ca ON ca.id=b.a_chat_id JOIN chats cb ON cb.id=b.b_chat_id"""
    )

    async def reload_routes(self) -> None:
        rows = await self.db.fetchall(self.BRIDGE_SELECT)
        routes: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for r in rows:
            r["options_parsed"] = merge_options(json.loads(r.get("options") or "{}"))
            if not r["enabled"]:
                continue
            if r["a_status"] in ("rejected", "disabled", "left") or r["b_status"] in ("rejected", "disabled", "left"):
                continue
            if r["direction"] in ("both", "a_to_b"):
                routes.setdefault((r["a_platform"], str(r["a_chat"])), []).append(r)
            if r["direction"] in ("both", "b_to_a"):
                routes.setdefault((r["b_platform"], str(r["b_chat"])), []).append(r)
        self._routes = routes
        chat_rows = await self.db.fetchall("SELECT id, status FROM chats")
        self._chat_status = {c["id"]: c["status"] for c in chat_rows}

    def routes_for(self, platform: str, chat_id: str) -> list[dict[str, Any]]:
        return self._routes.get((platform, str(chat_id)), [])

    @staticmethod
    def target_side(bridge: dict[str, Any], source_platform: str, source_chat: str) -> Optional[tuple[str, str, str]]:
        """Resolve (direction, target_platform, target_chat) for a message
        coming from ``source_platform/source_chat`` in ``bridge``."""
        if bridge.get("a_platform") == source_platform and str(bridge.get("a_chat")) == str(source_chat):
            return "a_to_b", str(bridge.get("b_platform")), str(bridge.get("b_chat"))
        return "b_to_a", str(bridge.get("a_platform")), str(bridge.get("a_chat"))

    def is_bridged(self, platform: str, chat_id: str) -> bool:
        """True when the chat takes part in any active bridge (either direction)."""
        if (platform, str(chat_id)) in self._routes:
            return True
        for routes in self._routes.values():
            for r in routes:
                if (r["a_platform"] == platform and str(r["a_chat"]) == str(chat_id)) or \
                   (r["b_platform"] == platform and str(r["b_chat"]) == str(chat_id)):
                    return True
        return False

    # ------------------------------------------------------------- incoming
    async def handle_incoming(self, msg: UnifiedMessage) -> None:
        self.counters["received"] += 1
        adapter = self.adapters.get(msg.platform)
        # layer 1: never bridge ourselves
        if adapter and msg.sender.id and msg.sender.id == adapter.self_id:
            self.counters["loops"] += 1
            return
        if msg.platform == PLATFORM_TG and msg.sender.is_bot and not self.settings.get("bridge_other_bots", False):
            return
        await self._remember_name(msg)

        if msg.is_command and _is_bridge_command(msg.text):
            await self._handle_bridge_command(msg)
            return

        routes = self.routes_for(msg.platform, msg.chat_id)
        if not routes:
            return

        # layer 2: is this a copy we produced?
        hit = await self.db.fetchone(
            "SELECT 1 FROM message_mapping WHERE target_platform=? AND target_chat_id=? AND target_message_id=? LIMIT 1",
            (msg.platform, msg.chat_id, msg.message_id),
        )
        if hit:
            self.counters["loops"] += 1
            log.debug("忽略自身桥接消息 %s/%s", msg.platform, msg.message_id)
            return

        # Adapters may expose optional enrichment hooks (reply / forward fetch).
        if adapter is not None:
            enrich = getattr(adapter, "enrich_reply", None)
            if enrich and msg.reply:
                try:
                    await enrich(msg)
                except Exception:
                    pass
            fetch_fwd = getattr(adapter, "fetch_forward_text", None)
            if fetch_fwd and isinstance(msg.raw, dict) and msg.raw.get("_forward_id"):
                try:
                    await fetch_fwd(msg)
                except Exception:
                    pass

        for bridge in routes:
            await self._enqueue(msg, bridge)

    async def _enqueue(self, msg: UnifiedMessage, bridge: dict[str, Any]) -> None:
        direction, target_platform, target_chat = self.target_side(bridge, msg.platform, msg.chat_id)
        opts = bridge["options_parsed"]

        # filters
        if msg.event and not opts.get("event_sync"):
            return
        if msg.forward and not opts["media"].get("forward", True):
            self.counters["dropped"] += 1
            return
        allowed_media = [m for m in msg.media if opts["media"].get(m.kind.value, True)]
        filtered_out = len(msg.media) - len(allowed_media)
        if not opts["media"].get("text", True) and not allowed_media:
            self.counters["dropped"] += 1
            return
        if filtered_out and not allowed_media and not msg.text:
            self.counters["dropped"] += 1
            return

        # layer 3: dedupe
        now = time.time()
        try:
            row_id = await self.db.insert("messages", {
                "bridge_id": bridge["id"], "direction": direction, "source_platform": msg.platform,
                "source_chat_id": msg.chat_id, "source_message_id": msg.message_id, "source_user_id": msg.sender.id,
                "source_user_name": msg.sender.name, "kind": msg.kind, "summary": msg.summary(), "status": "pending",
                "created_at": now, "updated_at": now,
            })
        except Exception as exc:  # sqlite3.IntegrityError -> duplicate
            if "UNIQUE" in str(exc).upper():
                self.counters["duplicates"] += 1
                log.debug("重复消息忽略 %s/%s", msg.platform, msg.message_id)
                return
            raise

        job_msg = msg if not filtered_out else _with_media(msg, allowed_media)
        job = Job(priority=job_msg.priority, seq=next(self._seq), row_id=row_id, message=job_msg, bridge=bridge,
                  target_platform=target_platform, target_chat_id=target_chat)
        job.step("received", detail=f"{platform_label(msg.platform)} {msg.chat_id}/{msg.message_id}")
        job.step("route", detail=f"bridge #{bridge['id']} {bridge['name']}")
        if filtered_out:
            job.step("filter", detail=f"过滤 {filtered_out} 个媒体")
        await self.queue.put(job)

    # --------------------------------------------------------------- worker
    async def _worker(self, idx: int) -> None:
        while True:
            job = await self.queue.get()
            try:
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("worker %d 处理消息异常", idx)
                await self._finish(job, SendResult(ok=False, error_code="INTERNAL", error=f"{type(exc).__name__}: {exc}"))
            finally:
                self.queue.task_done()

    async def _process(self, job: Job) -> None:
        msg = job.message
        bridge = job.bridge
        opts = bridge["options_parsed"]
        target = self.adapters.get(job.target_platform)
        source = self.adapters.get(msg.platform)
        job.attempts += 1
        await self.db.update("messages", {"status": "processing", "attempts": job.attempts, "updated_at": time.time()}, "id=?", (job.row_id,))

        if target is None or not target.connected:
            await self._finish(job, SendResult(ok=False, error_code="TARGET_OFFLINE", error=f"{platform_label(job.target_platform)} 未连接"))
            return

        mode = opts.get("display_mode") or self.settings.get("display_mode", "standard")
        tz = self.settings.get("timezone", "Asia/Shanghai")
        rendered = render(msg, mode, tz, include_reply_preview=True)

        # reply mapping
        reply_to: Optional[str] = None
        if msg.reply and opts.get("reply_sync", True):
            reply_to = await self._resolve_reply(msg, job.target_platform, job.target_chat_id)
            if reply_to:
                rendered = render(msg, mode, tz, include_reply_preview=False)
                job.step("reply", detail=f"→ {reply_to}")

        # media
        prepared: list[Prepared] = []
        job_dir = None
        if msg.media and source is not None:
            job_dir = self.storage.new_job_dir()
            for media in msg.media:
                t0 = time.time()
                p = await self.processor.prepare(media, job.target_platform, source.download, job_dir)
                prepared.append(p)
                detail = ", ".join(p.steps) if p.steps else (f"{p.kind.value}" + (f" {(p.size or 0) // 1024} KB" if p.size else ""))
                job.step("media", ok=p.deliverable, detail=f"{media.kind.value}→{p.kind.value} {detail} ({time.time() - t0:.1f}s)")

        out = OutgoingMessage(chat_id=job.target_chat_id, text=rendered.text, html=rendered.html, media=prepared,
                              reply_to_message_id=reply_to)
        await self.db.update("messages", {"status": "sending", "updated_at": time.time()}, "id=?", (job.row_id,))
        try:
            result = await target.send(out)
        finally:
            if job_dir is not None:
                self.storage.release(job_dir)

        if result.file_refs:
            for key, ref in result.file_refs.items():
                kind, _, hkey = key.partition("|")
                await self.processor.cache_store([hkey], job.target_platform, kind, ref, None)
        if not result.ok and result.error_code == "FILE_ID_INVALID":
            for p in prepared:
                if p.file_ref:
                    await self.processor.cache_invalidate(p.file_ref)
        job.step("send", ok=result.ok, detail=(",".join(result.message_ids) if result.ok else f"{result.error_code}: {result.error}"))
        await self._finish(job, result)

    async def _finish(self, job: Job, result: SendResult) -> None:
        msg = job.message
        now = time.time()
        duration_ms = int((now - job.started) * 1000)
        if result.ok or result.message_ids:
            for mid in result.message_ids:
                await self.db.insert("message_mapping", {
                    "bridge_id": job.bridge["id"], "source_platform": msg.platform, "source_chat_id": msg.chat_id,
                    "source_message_id": msg.message_id, "target_platform": job.target_platform,
                    "target_chat_id": job.target_chat_id, "target_message_id": mid, "created_at": now,
                })
        if result.ok:
            self.counters["sent"] += 1
            await self.db.update("messages", {"status": "sent", "steps": json.dumps(job.steps, ensure_ascii=False),
                                              "duration_ms": duration_ms, "updated_at": now, "error": None, "error_code": None},
                                 "id=?", (job.row_id,))
            await self._bump_stats(job, ok=True)
            log.info("#%d %s %s→%s 成功 (%.2fs) %s", job.row_id, job.bridge["name"], platform_label(msg.platform),
                     platform_label(job.target_platform), duration_ms / 1000, msg.summary(40))
            return

        delays = list(self.settings.get("retry_delays_sec", [1, 5, 30]))
        can_retry = not result.permanent and job.attempts <= len(delays) and self._running
        if can_retry:
            delay = float(result.retry_after or delays[job.attempts - 1])
            await self.db.update("messages", {"status": "failed", "error_code": result.error_code, "error": result.error,
                                              "steps": json.dumps(job.steps, ensure_ascii=False), "updated_at": now}, "id=?", (job.row_id,))
            log.warning("#%d 发送失败 (%s)，%.0fs 后第 %d 次重试", job.row_id, result.error, delay, job.attempts + 1)
            t = asyncio.create_task(self._retry_later(job, delay))
            self._pending_retries.add(t)
            t.add_done_callback(self._pending_retries.discard)
            return

        self.counters["failed"] += 1
        await self.db.update("messages", {"status": "dead", "error_code": result.error_code, "error": result.error,
                                          "steps": json.dumps(job.steps, ensure_ascii=False), "duration_ms": duration_ms,
                                          "updated_at": now}, "id=?", (job.row_id,))
        await self._bump_stats(job, ok=False)
        self._remember_error(job, result)
        log.error("#%d %s 最终失败: %s (%s)", job.row_id, job.bridge["name"], result.error, result.error_code)
        if result.error_code in ("CHAT_FORBIDDEN", "CHAT_NOT_FOUND", "NO_PERMISSION", "MUTED", "NOT_IN_GROUP"):
            await self._mark_chat_status(job.target_platform, job.target_chat_id,
                                         "left" if result.error_code in ("CHAT_NOT_FOUND", "NOT_IN_GROUP") else "limited", result.error)

    async def _retry_later(self, job: Job, delay: float) -> None:
        await asyncio.sleep(delay)
        job.step("retry", detail=f"第 {job.attempts + 1} 次")
        await self.queue.put(job)

    async def retry_message(self, row_id: int) -> bool:
        """Re-queue a dead message from the panel (text only re-render; media is re-fetched)."""
        row = await self.db.fetchone("SELECT * FROM messages WHERE id=?", (row_id,))
        if not row or row["status"] not in ("dead", "failed"):
            return False
        # we no longer have the UnifiedMessage; rebuild a minimal text version
        bridge = await self._bridge_row(row["bridge_id"])
        if not bridge:
            return False
        from ..models import Sender  # local import to avoid cycle noise
        msg = UnifiedMessage(platform=row["source_platform"], chat_id=row["source_chat_id"], message_id=row["source_message_id"],
                             sender=Sender(id=row["source_user_id"] or "", name=row["source_user_name"] or "", platform=row["source_platform"]),
                             text=row["summary"], timestamp=row["created_at"])
        direction, target_platform, target_chat = self.target_side(bridge, msg.platform, msg.chat_id)
        job = Job(priority=0, seq=next(self._seq), row_id=row_id, message=msg, bridge=bridge, target_platform=target_platform,
                  target_chat_id=target_chat, attempts=0)
        job.step("manual_retry")
        await self.db.update("messages", {"status": "pending", "updated_at": time.time()}, "id=?", (row_id,))
        await self.queue.put(job)
        return True

    async def _bridge_row(self, bridge_id: int | None) -> Optional[dict[str, Any]]:
        if bridge_id is None:
            return None
        for routes in self._routes.values():
            for r in routes:
                if r["id"] == bridge_id:
                    return r
        row = await self.db.fetchone(self.BRIDGE_SELECT + " WHERE b.id=?", (bridge_id,))
        if row:
            row["options_parsed"] = merge_options(json.loads(row.get("options") or "{}"))
        return row

    # --------------------------------------------------------------- helpers
    async def _resolve_reply(self, msg: UnifiedMessage, target_platform: str, target_chat: str) -> Optional[str]:
        assert msg.reply
        rid = msg.reply.message_id
        row = await self.db.fetchone(
            "SELECT target_message_id FROM message_mapping WHERE source_platform=? AND source_chat_id=? AND source_message_id=? "
            "AND target_platform=? AND target_chat_id=? ORDER BY id LIMIT 1",
            (msg.platform, msg.chat_id, rid, target_platform, target_chat),
        )
        if row:
            return str(row["target_message_id"])
        row = await self.db.fetchone(
            "SELECT source_message_id FROM message_mapping WHERE target_platform=? AND target_chat_id=? AND target_message_id=? "
            "AND source_platform=? AND source_chat_id=? ORDER BY id LIMIT 1",
            (msg.platform, msg.chat_id, rid, target_platform, target_chat),
        )
        return str(row["source_message_id"]) if row else None

    async def _bump_stats(self, job: Job, ok: bool) -> None:
        day = datetime.now().strftime("%Y-%m-%d")
        direction = self.target_side(job.bridge, job.message.platform, job.message.chat_id)[0]
        col = "sent" if ok else "failed"
        await self.db.execute(
            f"INSERT INTO stats_daily(day, bridge_id, direction, kind, {col}) VALUES (?,?,?,?,1) "
            f"ON CONFLICT(day, bridge_id, direction, kind) DO UPDATE SET {col}={col}+1",
            (day, job.bridge["id"], direction, job.message.kind),
        )

    def _remember_error(self, job: Job, result: SendResult) -> None:
        self.recent_errors.insert(0, {"id": job.row_id, "bridge": job.bridge["name"], "code": result.error_code,
                                      "error": result.error, "ts": time.time(), "summary": job.message.summary(60)})
        del self.recent_errors[50:]

    async def _remember_name(self, msg: UnifiedMessage) -> None:
        if not msg.sender.id:
            return
        await self.db.execute(
            "INSERT INTO user_names(platform, user_id, name, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(platform, user_id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at",
            (msg.platform, msg.sender.id, msg.sender.name, time.time()),
        )

    async def _mark_chat_status(self, platform: str, chat_id: str, status: str, reason: str) -> None:
        await self.db.update("chats", {"status": status, "status_reason": reason, "updated_at": time.time()},
                             "platform=? AND chat_id=? AND status NOT IN ('rejected','disabled')", (platform, str(chat_id)))
        await self.reload_routes()

    # ---------------------------------------------------------------- events
    async def handle_event(self, name: str, data: dict[str, Any]) -> None:
        platform = data.get("platform", "")
        if name == "chat_seen":
            await self.upsert_chat(platform, data["chat"])
        elif name == "chats_sync":
            # Emitted by an adapter on (re)connect to refresh its group list.
            adapter = self.adapters.get(platform)
            if adapter:
                infos = await adapter.list_chats()
                for info in infos:
                    await self.upsert_chat(platform, info, force=True)
                if getattr(adapter, "authoritative_group_list", True):
                    await self._restore_left_chats(platform, {c.chat_id for c in infos})
        elif name == "bot_left":
            await self._mark_chat_status(platform, data["chat_id"], "left", data.get("reason", ""))
            slog.warning("%s 机器人离开群 %s", platform_label(platform), data["chat_id"])
        elif name == "bot_limited":
            await self._mark_chat_status(platform, data["chat_id"], "limited", data.get("reason", ""))
        elif name == "bot_joined":
            info = data.get("chat")
            if info:
                await self.upsert_chat(platform, info, force=True)
            row = await self.db.fetchone("SELECT status FROM chats WHERE platform=? AND chat_id=?", (platform, str(data["chat_id"])))
            if row and row["status"] in ("left", "limited", "error"):
                await self.db.update("chats", {"status": "authorized" if await self._has_bridge(platform, data["chat_id"]) else "discovered",
                                               "status_reason": "", "updated_at": time.time()}, "platform=? AND chat_id=?", (platform, str(data["chat_id"])))
                await self.reload_routes()
            slog.info("%s 机器人加入群 %s（默认不转发，请在面板中配置桥接）", platform_label(platform), data["chat_id"])
        elif name == "recall":
            await self._handle_recall(platform, data)
        elif name == "edit":
            await self._handle_edit(data["message"])
        elif name == "member_event":
            msg = UnifiedMessage(platform=platform, chat_id=str(data["chat_id"]), message_id=f"evt-{int(time.time() * 1000)}",
                                 sender=_system_sender(platform), text=data["text"], event=data.get("event"))
            for bridge in self.routes_for(platform, msg.chat_id):
                if bridge["options_parsed"].get("event_sync"):
                    await self._enqueue(msg, bridge)

    async def _has_bridge(self, platform: str, chat_id: str) -> bool:
        row = await self.db.fetchone(
            """SELECT 1 FROM bridges b
               JOIN chats ca ON ca.id=b.a_chat_id JOIN chats cb ON cb.id=b.b_chat_id
               WHERE (ca.platform=? AND ca.chat_id=?) OR (cb.platform=? AND cb.chat_id=?) LIMIT 1""",
            (platform, str(chat_id), platform, str(chat_id)))
        return bool(row)

    async def _restore_left_chats(self, platform: str, present: set[str]) -> None:
        rows = await self.db.fetchall("SELECT id, chat_id, status FROM chats WHERE platform=?", (platform,))
        changed = False
        for r in rows:
            if r["chat_id"] in present and r["status"] == "left":
                await self.db.update("chats", {"status": "authorized" if await self._has_bridge(platform, r["chat_id"]) else "discovered",
                                               "status_reason": "", "updated_at": time.time()}, "id=?", (r["id"],))
                changed = True
            elif r["chat_id"] not in present and r["status"] not in ("left", "rejected", "disabled"):
                await self.db.update("chats", {"status": "left", "status_reason": "机器人不在该群", "updated_at": time.time()}, "id=?", (r["id"],))
                changed = True
        if changed:
            await self.reload_routes()

    async def upsert_chat(self, platform: str, info: ChatInfo, force: bool = False) -> None:
        key = (platform, info.chat_id)
        cached = self._chat_cache.get(key)
        now = time.time()
        if not force and cached and cached[0] == info.title and now - cached[1] < 300:
            return
        self._chat_cache[key] = (info.title, now)
        row = await self.db.fetchone("SELECT id, title, member_count FROM chats WHERE platform=? AND chat_id=?", (platform, info.chat_id))
        if row:
            values: dict[str, Any] = {"last_seen_at": now, "updated_at": now}
            if info.title and info.title != row["title"]:
                values["title"] = info.title
            if info.member_count is not None:
                values["member_count"] = info.member_count
            await self.db.update("chats", values, "id=?", (row["id"],))
        else:
            await self.db.insert("chats", {"platform": platform, "chat_id": info.chat_id, "title": info.title or info.chat_id,
                                           "chat_type": info.chat_type, "member_count": info.member_count, "status": "discovered",
                                           "last_seen_at": now, "created_at": now, "updated_at": now})
            slog.info("发现新群: %s · %s (%s)", platform_label(platform), info.title, info.chat_id)

    async def _handle_recall(self, platform: str, data: dict[str, Any]) -> None:
        chat_id, mid = str(data["chat_id"]), str(data["message_id"])
        rows = await self.db.fetchall(
            "SELECT bridge_id, target_platform, target_chat_id, target_message_id FROM message_mapping "
            "WHERE source_platform=? AND source_chat_id=? AND source_message_id=?", (platform, chat_id, mid))
        for r in rows:
            bridge = await self._bridge_row(r["bridge_id"])
            if not bridge or not bridge["options_parsed"].get("recall_sync"):
                continue
            target = self.adapters.get(r["target_platform"])
            if target and await target.delete_message(r["target_chat_id"], r["target_message_id"]):
                log.info("撤回同步: %s %s → %s %s", platform_label(platform), mid, platform_label(r["target_platform"]), r["target_message_id"])

    async def _handle_edit(self, msg: UnifiedMessage) -> None:
        for bridge in self.routes_for(msg.platform, msg.chat_id):
            opts = bridge["options_parsed"]
            if not opts.get("edit_sync"):
                continue
            _direction, target_platform, target_chat = self.target_side(bridge, msg.platform, msg.chat_id)
            target = self.adapters.get(target_platform)
            if not target:
                continue
            row = await self.db.fetchone(
                "SELECT target_message_id FROM message_mapping WHERE source_platform=? AND source_chat_id=? AND source_message_id=? "
                "AND target_platform=? AND target_chat_id=? ORDER BY id LIMIT 1",
                (msg.platform, msg.chat_id, msg.message_id, target_platform, target_chat))
            mode = opts.get("display_mode") or self.settings.get("display_mode", "standard")
            rendered = render_edit(msg, mode, self.settings.get("timezone", "Asia/Shanghai"))
            await target.send(OutgoingMessage(chat_id=target_chat, text=rendered.text, html=rendered.html,
                                              reply_to_message_id=str(row["target_message_id"]) if row else None))

    # -------------------------------------------------------------- commands
    async def _handle_bridge_command(self, msg: UnifiedMessage) -> None:
        adapter = self.adapters.get(msg.platform)
        if not adapter:
            return
        row = await self.db.fetchone("SELECT * FROM chats WHERE platform=? AND chat_id=?", (msg.platform, msg.chat_id))
        label = platform_label(msg.platform)
        title = msg.chat_title or (row["title"] if row else msg.chat_id)
        bridged = self.is_bridged(msg.platform, msg.chat_id)
        if bridged:
            text = f"群组已识别\n\n{label} 群：{title}\nID：{msg.chat_id}\n状态：✓ 已桥接"
        else:
            if row and row["status"] in ("discovered", "left", "error"):
                await self.db.update("chats", {"status": "pending", "status_reason": f"由 {msg.sender.name} 申请", "updated_at": time.time()}, "id=?", (row["id"],))
            elif not row:
                await self.upsert_chat(msg.platform, ChatInfo(msg.chat_id, title), force=True)
                await self.db.update("chats", {"status": "pending", "status_reason": f"由 {msg.sender.name} 申请", "updated_at": time.time()},
                                     "platform=? AND chat_id=?", (msg.platform, msg.chat_id))
            text = (f"群组已识别\n\n{label} 群：{title}\nID：{msg.chat_id}\n状态：待授权\n\n"
                    f"已提交桥接申请，请管理员在 Web 面板「群组」页面中授权并创建桥接。")
        slog.info("%s 群 %s (%s) 发送了 /bridge", label, title, msg.chat_id)
        try:
            await adapter.send(OutgoingMessage(chat_id=msg.chat_id, text=text, reply_to_message_id=msg.message_id))
        except Exception as exc:
            log.debug("reply to /bridge failed: %s", exc)

    # ----------------------------------------------------------------- tests
    async def send_test(self, bridge: dict[str, Any]) -> dict[str, Any]:
        """Send a probe into each side the direction allows; keyed by data flow."""
        results: dict[str, Any] = {}
        stamp = datetime.now().strftime("%H:%M:%S")
        for direction, from_side, to_side in (("a_to_b", "a", "b"), ("b_to_a", "b", "a")):
            if bridge["direction"] not in ("both", direction):
                continue
            adapter = self.adapters.get(str(bridge[f"{to_side}_platform"]))
            if adapter and adapter.connected:
                from_chat = bridge.get(f"{from_side}_title") or bridge.get(f"{from_side}_chat")
                to_chat = bridge.get(f"{to_side}_title") or bridge.get(f"{to_side}_chat")
                r = await adapter.send(OutgoingMessage(
                    chat_id=str(bridge[f"{to_side}_chat"]),
                    text=f"✅ Rain Bridge 测试消息 {stamp}\n桥接：{bridge['name']}\n方向：{from_chat} → {to_chat}"))
                results[direction] = {"ok": r.ok, "error": r.error}
            else:
                results[direction] = {"ok": False, "error": f"{platform_label(str(bridge[f'{to_side}_platform']))} 未连接"}
        return results

    def queue_status(self) -> dict[str, Any]:
        return {"depth": self.queue.qsize(), "workers": len([w for w in self._workers if not w.done()]),
                "retries_waiting": len(self._pending_retries), "counters": dict(self.counters)}


def _is_bridge_command(text: str) -> bool:
    first = (text or "").split()[0] if text else ""
    return first.split("@")[0].lower() in ("/bridge", "/qqtg")


def _with_media(msg: UnifiedMessage, media: list) -> UnifiedMessage:
    from dataclasses import replace
    return replace(msg, media=media)


def _system_sender(platform: str):
    from ..models import Sender
    return Sender(id="system", name="系统", platform=platform)
