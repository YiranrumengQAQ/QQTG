"""Runtime settings (stored in the ``settings`` table, edited from the panel)."""
from __future__ import annotations

import os
from typing import Any

from .db import Database


def _cpu_count() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except AttributeError:  # pragma: no cover
        return max(1, os.cpu_count() or 1)


def _mem_total_mb() -> int:
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 2048


def recommended_media_workers() -> int:
    cpus = _cpu_count()
    mem = _mem_total_mb()
    if cpus <= 2 or mem < 2048:
        return 1
    if cpus <= 4 or mem < 4096:
        return 2
    return min(4, cpus // 2)


DEFAULTS: dict[str, Any] = {
    # presentation
    "display_mode": "standard",  # simple | standard | full
    "timezone": "Asia/Shanghai",
    # workers / limits
    "media_workers": recommended_media_workers(),
    "send_workers": 4,
    "media_timeout_sec": 180,
    "tmp_quota_mb": 2048,
    "tmp_ttl_min": 30,
    # size policy (MB)
    "tg_upload_limit_mb": 50,  # Bot API hard limit for standard servers
    "tg_download_limit_mb": 20,  # Bot API hard limit for standard servers
    "qq_media_limit_mb": 100,
    "direct_send_limit_mb": 10,  # <= : send directly, between: try, above qq/tg limit: fallback text
    # rate limiting
    "tg_rate_per_chat_per_min": 20,
    "tg_rate_global_per_sec": 25,
    "qq_rate_per_chat_per_sec": 1.5,
    # retries
    "retry_delays_sec": [1, 5, 30],
    # audio
    "qq_voice_format": "wav",  # wav is accepted by every OneBot implementation
    # behaviour
    "bridge_other_bots": False,  # TG messages from other bots
    "event_sync_default": False,
    "telegram_api_base": "https://api.telegram.org",
    # retention
    "log_retention_rows": 20000,
    "message_retention_days": 30,
    "media_cache_days": 30,
    # panel
    "panel_title": "QQ ↔ Telegram Bridge",
    "session_hours": 72,
}

SETTING_SCHEMA: dict[str, dict[str, Any]] = {
    "display_mode": {"type": "choice", "choices": ["simple", "standard", "full"], "label": "默认显示模式"},
    "timezone": {"type": "str", "label": "时区"},
    "media_workers": {"type": "int", "min": 1, "max": 8, "label": "媒体处理并发 (FFmpeg)"},
    "send_workers": {"type": "int", "min": 1, "max": 16, "label": "发送并发"},
    "media_timeout_sec": {"type": "int", "min": 10, "max": 3600, "label": "单个媒体处理超时 (秒)"},
    "tmp_quota_mb": {"type": "int", "min": 64, "max": 1024 * 1024, "label": "临时目录配额 (MB)"},
    "tmp_ttl_min": {"type": "int", "min": 1, "max": 1440, "label": "临时文件保留 (分钟)"},
    "tg_upload_limit_mb": {"type": "int", "min": 1, "max": 4000, "label": "Telegram 上传上限 (MB)"},
    "tg_download_limit_mb": {"type": "int", "min": 1, "max": 4000, "label": "Telegram 下载上限 (MB)"},
    "qq_media_limit_mb": {"type": "int", "min": 1, "max": 4000, "label": "QQ 媒体上限 (MB)"},
    "direct_send_limit_mb": {"type": "int", "min": 1, "max": 4000, "label": "直接发送阈值 (MB)"},
    "tg_rate_per_chat_per_min": {"type": "int", "min": 1, "max": 60, "label": "Telegram 每群每分钟"},
    "tg_rate_global_per_sec": {"type": "int", "min": 1, "max": 30, "label": "Telegram 全局每秒"},
    "qq_rate_per_chat_per_sec": {"type": "float", "min": 0.1, "max": 20, "label": "QQ 每群每秒"},
    "qq_voice_format": {"type": "choice", "choices": ["wav", "mp3", "ogg"], "label": "发往 QQ 的语音格式"},
    "bridge_other_bots": {"type": "bool", "label": "转发 Telegram 其他机器人的消息"},
    "event_sync_default": {"type": "bool", "label": "新桥默认同步群事件"},
    "telegram_api_base": {"type": "str", "label": "Telegram Bot API 地址"},
    "log_retention_rows": {"type": "int", "min": 1000, "max": 1000000, "label": "日志保留条数"},
    "message_retention_days": {"type": "int", "min": 1, "max": 3650, "label": "消息记录保留 (天)"},
    "media_cache_days": {"type": "int", "min": 1, "max": 3650, "label": "file_id 缓存保留 (天)"},
    "panel_title": {"type": "str", "label": "面板标题"},
    "session_hours": {"type": "int", "min": 1, "max": 24 * 30, "label": "登录有效期 (小时)"},
}


class Settings:
    """Cached view over the settings table."""

    def __init__(self, db: Database):
        self.db = db
        self._cache: dict[str, Any] = dict(DEFAULTS)

    async def load(self) -> None:
        stored = await self.db.all_settings()
        self._cache = dict(DEFAULTS)
        for k, v in stored.items():
            self._cache[k] = v

    def get(self, key: str, default: Any = None) -> Any:
        return self._cache.get(key, DEFAULTS.get(key, default))

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def public(self) -> dict[str, Any]:
        return {k: self._cache.get(k, v) for k, v in DEFAULTS.items()}

    async def set(self, key: str, value: Any) -> Any:
        value = validate_setting(key, value)
        await self.db.set_setting(key, value)
        self._cache[key] = value
        return value

    async def set_internal(self, key: str, value: Any) -> None:
        """Internal keys not exposed in the schema (setup token hash etc.)."""
        await self.db.set_setting(key, value)
        self._cache[key] = value


def validate_setting(key: str, value: Any) -> Any:
    spec = SETTING_SCHEMA.get(key)
    if spec is None:
        raise ValueError(f"未知设置项: {key}")
    t = spec["type"]
    if t == "bool":
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)
    if t == "int":
        try:
            iv = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{spec['label']} 必须是整数") from None
        if iv < spec.get("min", -10**12) or iv > spec.get("max", 10**12):
            raise ValueError(f"{spec['label']} 超出范围 {spec.get('min')}~{spec.get('max')}")
        return iv
    if t == "float":
        try:
            fv = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{spec['label']} 必须是数字") from None
        if fv < spec.get("min", -1e12) or fv > spec.get("max", 1e12):
            raise ValueError(f"{spec['label']} 超出范围")
        return fv
    if t == "choice":
        if value not in spec["choices"]:
            raise ValueError(f"{spec['label']} 只能是 {'/'.join(spec['choices'])}")
        return value
    if t == "str":
        s = str(value).strip()
        if len(s) > 500:
            raise ValueError(f"{spec['label']} 过长")
        return s
    return value
