"""Logging: rotating files + in-database ring buffer, with credential redaction.

Categories (logger names) map to the panel's log tabs::

    qqtg.system  qqtg.conn  qqtg.message  qqtg.media  qqtg.error  qqtg.security
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .security import redact

if TYPE_CHECKING:
    from .db import Database


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.msg = redact(msg)
        record.args = ()
        return True


class DBLogHandler(logging.Handler):
    """Buffers log rows in memory and flushes them to SQLite from the event loop."""

    def __init__(self, max_buffer: int = 2000):
        super().__init__()
        self.buffer: deque[tuple[float, str, str, str, dict[str, Any] | None]] = deque(maxlen=max_buffer)
        self.db: Database | None = None
        self._task: asyncio.Task | None = None
        self.keep_rows = 20000

    def emit(self, record: logging.LogRecord) -> None:
        category = record.name.split(".")[-1] if record.name.startswith("qqtg.") else "system"
        if record.levelno >= logging.ERROR and category not in ("security",):
            category_for_row = "error"
        else:
            category_for_row = category
        details = getattr(record, "details", None)
        self.buffer.append((record.created, record.levelname, category_for_row, record.getMessage(), details))

    def attach(self, db: "Database", loop: asyncio.AbstractEventLoop) -> None:
        self.db = db
        self._task = loop.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        last_trim = 0.0
        while True:
            try:
                await asyncio.sleep(1.0)
                await self.flush_async()
                if time.time() - last_trim > 600:
                    last_trim = time.time()
                    if self.db:
                        await self.db.trim_logs(self.keep_rows)
            except asyncio.CancelledError:
                await self.flush_async()
                raise
            except Exception:  # pragma: no cover - never let logging kill the loop
                pass

    def flush(self) -> None:  # sync hook used by logging.shutdown(); rows are flushed by the loop
        return None

    async def flush_async(self) -> None:
        if not self.db or not self.buffer:
            return
        rows = []
        while self.buffer:
            rows.append(self.buffer.popleft())
        for ts, level, category, message, details in rows:
            try:
                await self.db.add_log(level, category, message, details)
            except Exception:
                pass

    def stop(self) -> None:
        if self._task:
            self._task.cancel()


db_handler = DBLogHandler()


def setup_logging(logs_dir: Path, level: str = "INFO") -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    redact_filter = RedactFilter()

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    stream.addFilter(redact_filter)
    root.addHandler(stream)

    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir / "bridge.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(redact_filter)
    root.addHandler(file_handler)

    db_handler.setLevel(logging.INFO)
    db_handler.addFilter(redact_filter)
    root.addHandler(db_handler)

    # third party noise
    for noisy in ("httpx", "httpcore", "websockets", "uvicorn.access", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(category: str) -> logging.Logger:
    return logging.getLogger(f"qqtg.{category}")
