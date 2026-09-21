"""SQLite storage with a tiny forward-only migration system.

The schema is intentionally modelled as::

    Connection (bot) -> Chat -> Bridge -> Message -> MessageMapping

so that one bot can serve many bridges and the data model never has to be
rewritten when one-to-many / many-to-one routing is added later.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

SCHEMA_VERSION = 1

MIGRATIONS: dict[int, list[str]] = {
    1: [
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('owner','admin','viewer')),
            created_at REAL NOT NULL,
            last_login_at REAL
        )""",
        """CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            csrf TEXT NOT NULL,
            ip TEXT,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL CHECK(platform IN ('qq','telegram')),
            name TEXT NOT NULL DEFAULT '',
            config_enc TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            self_id TEXT,
            self_name TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            connection_id INTEGER REFERENCES connections(id) ON DELETE SET NULL,
            platform TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            chat_type TEXT NOT NULL DEFAULT 'group',
            member_count INTEGER,
            status TEXT NOT NULL DEFAULT 'discovered',
            status_reason TEXT NOT NULL DEFAULT '',
            permissions TEXT NOT NULL DEFAULT '{}',
            last_seen_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(platform, chat_id)
        )""",
        """CREATE TABLE IF NOT EXISTS bridges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            direction TEXT NOT NULL DEFAULT 'both' CHECK(direction IN ('both','qq_to_tg','tg_to_qq')),
            qq_chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
            tg_chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
            options TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_bridges_qq ON bridges(qq_chat_id)",
        "CREATE INDEX IF NOT EXISTS idx_bridges_tg ON bridges(tg_chat_id)",
        """CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bridge_id INTEGER REFERENCES bridges(id) ON DELETE SET NULL,
            direction TEXT NOT NULL,
            source_platform TEXT NOT NULL,
            source_chat_id TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            source_user_id TEXT,
            source_user_name TEXT,
            kind TEXT NOT NULL DEFAULT 'text',
            summary TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            error_code TEXT,
            error TEXT,
            steps TEXT NOT NULL DEFAULT '[]',
            duration_ms INTEGER,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(source_platform, source_chat_id, source_message_id, bridge_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status)",
        "CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at)",
        """CREATE TABLE IF NOT EXISTS message_mapping (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bridge_id INTEGER,
            source_platform TEXT NOT NULL,
            source_chat_id TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            target_platform TEXT NOT NULL,
            target_chat_id TEXT NOT NULL,
            target_message_id TEXT NOT NULL,
            created_at REAL NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_map_source ON message_mapping(source_platform, source_chat_id, source_message_id)",
        "CREATE INDEX IF NOT EXISTS idx_map_target ON message_mapping(target_platform, target_chat_id, target_message_id)",
        """CREATE TABLE IF NOT EXISTS media_cache (
            hash TEXT NOT NULL,
            platform TEXT NOT NULL,
            kind TEXT NOT NULL,
            file_ref TEXT NOT NULL,
            size INTEGER,
            created_at REAL NOT NULL,
            last_used_at REAL NOT NULL,
            PRIMARY KEY (hash, platform, kind)
        )""",
        """CREATE TABLE IF NOT EXISTS user_names (
            platform TEXT NOT NULL,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (platform, user_id)
        )""",
        """CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            level TEXT NOT NULL,
            category TEXT NOT NULL,
            message TEXT NOT NULL,
            details TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts)",
        """CREATE TABLE IF NOT EXISTS stats_daily (
            day TEXT NOT NULL,
            bridge_id INTEGER NOT NULL,
            direction TEXT NOT NULL,
            kind TEXT NOT NULL,
            sent INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, bridge_id, direction, kind)
        )""",
    ],
}


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self.migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database not opened")
        return self._conn

    # -- migrations -------------------------------------------------------
    async def current_version(self) -> int:
        cur = await self.conn.execute("PRAGMA user_version")
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def migrate(self) -> None:
        version = await self.current_version()
        for target in sorted(MIGRATIONS):
            if target <= version:
                continue
            async with self._lock:
                await self.conn.execute("BEGIN")
                try:
                    for statement in MIGRATIONS[target]:
                        await self.conn.execute(statement)
                    await self.conn.execute(f"PRAGMA user_version={target}")
                    await self.conn.execute("COMMIT")
                except Exception:
                    await self.conn.execute("ROLLBACK")
                    raise

    # -- helpers ------------------------------------------------------------
    async def execute(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Cursor:
        return await self.conn.execute(sql, tuple(params))

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        cur = await self.conn.execute(sql, tuple(params))
        row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        cur = await self.conn.execute(sql, tuple(params))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def insert(self, table: str, values: dict[str, Any]) -> int:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cur = await self.conn.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values())
        )
        return int(cur.lastrowid or 0)

    async def update(self, table: str, values: dict[str, Any], where: str, params: Iterable[Any]) -> int:
        sets = ", ".join(f"{k}=?" for k in values)
        cur = await self.conn.execute(
            f"UPDATE {table} SET {sets} WHERE {where}", tuple(values.values()) + tuple(params)
        )
        return cur.rowcount

    # -- settings -----------------------------------------------------------
    async def get_setting(self, key: str, default: Any = None) -> Any:
        row = await self.fetchone("SELECT value FROM settings WHERE key=?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return row["value"]

    async def set_setting(self, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), time.time()),
        )

    async def all_settings(self) -> dict[str, Any]:
        rows = await self.fetchall("SELECT key, value FROM settings")
        out: dict[str, Any] = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except (TypeError, ValueError):
                out[r["key"]] = r["value"]
        return out

    # -- logs ---------------------------------------------------------------
    async def add_log(self, level: str, category: str, message: str, details: dict | None = None) -> None:
        await self.execute(
            "INSERT INTO logs(ts, level, category, message, details) VALUES (?,?,?,?,?)",
            (time.time(), level, category, message, json.dumps(details, ensure_ascii=False) if details else None),
        )

    async def trim_logs(self, keep_rows: int) -> None:
        await self.execute(
            "DELETE FROM logs WHERE id < (SELECT COALESCE(MAX(id),0) FROM logs) - ?",
            (keep_rows,),
        )

    async def ping(self) -> bool:
        try:
            await self.execute("SELECT 1")
            return True
        except Exception:
            return False


def sync_backup(src: Path, dst: Path) -> None:
    """Consistent online backup of the SQLite file (used before upgrades)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(src))
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
