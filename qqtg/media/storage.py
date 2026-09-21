"""Temporary media storage with quota / TTL protection.

The bridge is *not* a file server: media only touches the disk while it is
being converted or uploaded, and is deleted right after.  The sweeper handles
anything left behind by a crash.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time
import uuid
from pathlib import Path

from ..logsys import get_logger

log = get_logger("media")


class TempStorage:
    def __init__(self, root: Path, quota_mb: int = 2048, ttl_min: int = 30):
        self.root = root
        self.quota_mb = quota_mb
        self.ttl_min = ttl_min
        self.paused = False  # set when disk is almost full
        self._task: asyncio.Task | None = None

    def configure(self, quota_mb: int, ttl_min: int) -> None:
        self.quota_mb = quota_mb
        self.ttl_min = ttl_min

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.startup_cleanup()
        self._task = asyncio.get_event_loop().create_task(self._sweeper())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def new_job_dir(self) -> Path:
        d = self.root / f"job-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def release(self, job_dir: Path) -> None:
        shutil.rmtree(job_dir, ignore_errors=True)

    def usage_bytes(self) -> int:
        total = 0
        for dirpath, _, files in os.walk(self.root):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
        return total

    def disk_free_bytes(self) -> int:
        try:
            st = os.statvfs(self.root)
            return st.f_bavail * st.f_frsize
        except OSError:
            return 0

    def status(self) -> dict:
        used = self.usage_bytes()
        quota = self.quota_mb * 1024 * 1024
        return {
            "used_bytes": used,
            "quota_bytes": quota,
            "percent": round(used * 100 / quota, 1) if quota else 0,
            "disk_free_bytes": self.disk_free_bytes(),
            "paused": self.paused,
        }

    def startup_cleanup(self) -> None:
        removed = 0
        for child in self.root.iterdir() if self.root.exists() else []:
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink()
                removed += 1
            except OSError:
                pass
        if removed:
            log.info("启动清理: 删除 %d 个残留临时文件/目录", removed)

    def sweep(self) -> None:
        now = time.time()
        ttl = self.ttl_min * 60
        for child in list(self.root.iterdir()) if self.root.exists() else []:
            try:
                age = now - child.stat().st_mtime
                if age > ttl:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink()
            except OSError:
                pass
        used = self.usage_bytes()
        quota = self.quota_mb * 1024 * 1024
        free = self.disk_free_bytes()
        if quota and used > quota * 0.8:
            # aggressive cleanup: delete oldest first until under 60%
            entries = sorted(self.root.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0)
            for child in entries:
                if used <= quota * 0.6:
                    break
                try:
                    size = sum(f.stat().st_size for f in child.rglob("*") if f.is_file()) if child.is_dir() else child.stat().st_size
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink()
                    used -= size
                except OSError:
                    pass
            log.warning("临时目录使用超过 80%%，已清理到 %.1f MB", used / 1024 / 1024)
        was_paused = self.paused
        self.paused = bool((quota and used > quota * 0.95) or (free and free < 200 * 1024 * 1024))
        if self.paused and not was_paused:
            log.error("磁盘空间不足（临时目录 >95%% 或剩余 <200MB），媒体任务已暂停")
        elif was_paused and not self.paused:
            log.info("磁盘空间恢复，媒体任务继续")

    async def _sweeper(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                await asyncio.get_event_loop().run_in_executor(None, self.sweep)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover
                log.debug("sweeper error: %s", exc)
