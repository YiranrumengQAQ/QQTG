"""Media processor: turn a source ``Media`` into something the target platform
can natively display (photo / animation / video / audio / voice / document).

Principles (see design doc):
  * never convert when the target already accepts the format,
  * media failure never drops the text – we return a ``fallback_text`` instead,
  * the disk is only a scratch pad; the caller releases the job directory,
  * Telegram ``file_id`` are cached by content hash so re-sends skip upload.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ..db import Database
from ..logsys import get_logger
from ..models import PLATFORM_QQ, PLATFORM_TG, BridgeError, Media, MediaKind
from ..settings import Settings
from .detect import Sniff, safe_filename, sniff_file
from .ffmpeg import FFmpeg
from .storage import TempStorage

log = get_logger("media")

Downloader = Callable[[Media, Path], Awaitable[Path]]

TG_PHOTO_MAX_BYTES = 10 * 1024 * 1024
TG_PHOTO_MAX_SIDE_SUM = 10000
TG_PHOTO_MAX_RATIO = 20


@dataclass
class Prepared:
    kind: MediaKind
    original: Media
    path: Optional[str] = None
    file_ref: Optional[str] = None
    mime: Optional[str] = None
    filename: Optional[str] = None
    size: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[float] = None
    thumbnail_path: Optional[str] = None
    hash_keys: list[str] = field(default_factory=list)
    fallback_text: Optional[str] = None
    steps: list[str] = field(default_factory=list)

    @property
    def deliverable(self) -> bool:
        return self.fallback_text is None and (self.path is not None or self.file_ref is not None)


def _mb(n: Optional[int]) -> str:
    return f"{(n or 0) / 1024 / 1024:.1f} MB"


class MediaProcessor:
    def __init__(self, db: Database, settings: Settings, ffmpeg: FFmpeg, storage: TempStorage):
        self.db = db
        self.settings = settings
        self.ffmpeg = ffmpeg
        self.storage = storage
        self._sem = asyncio.Semaphore(max(1, int(settings.get("media_workers", 1))))

    def reconfigure(self) -> None:
        self._sem = asyncio.Semaphore(max(1, int(self.settings.get("media_workers", 1))))
        self.ffmpeg.timeout = int(self.settings.get("media_timeout_sec", 180))

    # -- cache -----------------------------------------------------------------
    async def cache_lookup(self, keys: list[str], platform: str, kind: str) -> Optional[str]:
        for key in keys:
            row = await self.db.fetchone(
                "SELECT file_ref FROM media_cache WHERE hash=? AND platform=? AND kind=?", (key, platform, kind)
            )
            if row:
                await self.db.execute(
                    "UPDATE media_cache SET last_used_at=? WHERE hash=? AND platform=? AND kind=?",
                    (time.time(), key, platform, kind),
                )
                return row["file_ref"]
        return None

    async def cache_store(self, keys: list[str], platform: str, kind: str, file_ref: str, size: Optional[int]) -> None:
        now = time.time()
        for key in keys:
            await self.db.execute(
                "INSERT INTO media_cache(hash, platform, kind, file_ref, size, created_at, last_used_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(hash, platform, kind) DO UPDATE SET file_ref=excluded.file_ref, last_used_at=excluded.last_used_at",
                (key, platform, kind, file_ref, size, now, now),
            )

    async def cache_invalidate(self, file_ref: str) -> None:
        await self.db.execute("DELETE FROM media_cache WHERE file_ref=?", (file_ref,))

    async def cache_prune(self) -> None:
        days = int(self.settings.get("media_cache_days", 30))
        await self.db.execute("DELETE FROM media_cache WHERE last_used_at < ?", (time.time() - days * 86400,))

    # -- entry point -------------------------------------------------------------
    async def prepare(self, media: Media, target_platform: str, downloader: Downloader, job_dir: Path) -> Prepared:
        """Resolve + convert ``media`` for ``target_platform``.  Never raises for
        media problems: failures are reported through ``fallback_text``."""
        if self.storage.paused:
            return self._fallback(media, "服务器磁盘空间不足，媒体已暂停转发")

        prehash = media.extra.get("prehash")
        keys: list[str] = [f"pre:{prehash}"] if prehash else []
        wanted_kind = self._target_kind_guess(media, target_platform)

        # Fast path: Telegram file_id reuse without downloading anything.
        if target_platform == PLATFORM_TG and keys:
            ref = await self.cache_lookup(keys, PLATFORM_TG, wanted_kind.value)
            if ref:
                p = Prepared(kind=wanted_kind, original=media, file_ref=ref, filename=media.filename,
                             width=media.width, height=media.height, duration=media.duration, hash_keys=keys)
                p.steps.append("file_id 缓存命中")
                return p

        # Download.
        try:
            path = await downloader(media, job_dir)
        except BridgeError as exc:
            return self._fallback(media, exc.message)
        except Exception as exc:  # network etc.
            log.warning("媒体下载失败: %s", exc)
            return self._fallback(media, f"媒体下载失败: {type(exc).__name__}")

        size = os.path.getsize(path)
        sniff = sniff_file(path, media.filename)
        digest = await asyncio.get_event_loop().run_in_executor(None, _sha256_file, path)
        keys.append(f"sha256:{digest}")
        media.path = str(path)
        media.size = size
        if not media.mime or media.mime == "application/octet-stream":
            media.mime = sniff.mime

        # Second cache chance with the content hash.
        if target_platform == PLATFORM_TG:
            ref = await self.cache_lookup(keys, PLATFORM_TG, wanted_kind.value)
            if ref:
                p = Prepared(kind=wanted_kind, original=media, file_ref=ref, filename=media.filename, size=size,
                             width=media.width, height=media.height, duration=media.duration, hash_keys=keys)
                p.steps.append("file_id 缓存命中 (内容哈希)")
                return p

        # Size policy against the target platform.
        limit_mb = int(self.settings.get("tg_upload_limit_mb" if target_platform == PLATFORM_TG else "qq_media_limit_mb", 50))
        if size > limit_mb * 1024 * 1024:
            return self._fallback(
                media,
                f"该文件无法通过当前 Bot 接口转发\n类型：{sniff.ext.upper()}\n大小：{_mb(size)}\n目标平台上限：{limit_mb} MB",
            )

        try:
            async with self._sem:
                if target_platform == PLATFORM_TG:
                    prepared = await self._prepare_for_telegram(media, str(path), sniff, job_dir)
                else:
                    prepared = await self._prepare_for_qq(media, str(path), sniff, job_dir)
        except BridgeError as exc:
            return self._fallback(media, exc.message, extra_steps=[exc.code])
        except Exception as exc:
            log.exception("媒体处理异常")
            return self._fallback(media, f"媒体处理异常: {type(exc).__name__}")

        prepared.hash_keys = keys
        if prepared.path and prepared.size is None:
            prepared.size = os.path.getsize(prepared.path)
        if prepared.filename is None:
            prepared.filename = safe_filename(media.filename, f"{prepared.kind.value}.{sniff.ext}")
        # Converted output might exceed limits again (rare).
        if prepared.path and os.path.getsize(prepared.path) > limit_mb * 1024 * 1024:
            return self._fallback(media, f"转换后的媒体仍超过目标平台上限 {limit_mb} MB")
        return prepared

    # -- helpers -----------------------------------------------------------------
    def _fallback(self, media: Media, reason: str, extra_steps: Optional[list[str]] = None) -> Prepared:
        name = media.filename or ""
        head = f"[{media.describe()}]"
        if media.emoji:
            head = f"[贴纸 {media.emoji}]"
        lines = [head]
        if name:
            lines.append(f"文件名：{safe_filename(name)}")
        if media.size:
            lines.append(f"大小：{_mb(media.size)}")
        lines.append(reason)
        p = Prepared(kind=media.kind, original=media, fallback_text="\n".join(lines))
        p.steps.extend(extra_steps or [])
        p.steps.append(f"fallback: {reason}")
        return p

    def _target_kind_guess(self, media: Media, target: str) -> MediaKind:
        if target == PLATFORM_TG:
            if media.kind == MediaKind.STICKER:
                return MediaKind.PHOTO
            return media.kind
        # QQ
        if media.kind in (MediaKind.STICKER, MediaKind.ANIMATION):
            return MediaKind.PHOTO
        if media.kind == MediaKind.AUDIO:
            return MediaKind.DOCUMENT
        return media.kind

    async def _probe_into(self, prepared: Prepared, path: str) -> None:
        info = await self.ffmpeg.probe(path)
        prepared.width = prepared.width or info.width
        prepared.height = prepared.height or info.height
        prepared.duration = prepared.duration or info.duration

    # -- QQ -> Telegram ----------------------------------------------------------------
    async def _prepare_for_telegram(self, media: Media, path: str, sniff: Sniff, job_dir: Path) -> Prepared:
        size = os.path.getsize(path)
        kind = media.kind
        p = Prepared(kind=kind, original=media, path=path, mime=sniff.mime, size=size,
                     width=media.width, height=media.height, duration=media.duration)

        # --- images / stickers ---
        if kind in (MediaKind.PHOTO, MediaKind.STICKER):
            if sniff.category == "animation":
                p.kind = MediaKind.ANIMATION
                p.filename = safe_filename(media.filename, "animation.gif")
                if sniff.ext != "gif" and self.ffmpeg.available():
                    # apng / animated webp -> mp4 so Telegram shows it as an animation
                    out = str(job_dir / "anim.mp4")
                    try:
                        await self.ffmpeg.to_mp4(path, out, silent=True)
                        p.path, p.mime, p.filename = out, "video/mp4", "animation.mp4"
                        p.steps.append(f"{sniff.ext} → mp4")
                    except BridgeError:
                        p.kind = MediaKind.DOCUMENT
                await self._probe_into(p, p.path or path)
                return p
            if sniff.category == "image":
                p.kind = MediaKind.PHOTO
                if sniff.ext in ("jpg", "png", "webp") and size <= TG_PHOTO_MAX_BYTES:
                    await self._probe_into(p, path)
                    if self._photo_dims_ok(p.width, p.height):
                        return p
                # convert / shrink to jpeg
                if self.ffmpeg.available():
                    out = str(job_dir / "photo.jpg")
                    await self.ffmpeg.to_jpeg(path, out)
                    p.path, p.mime, p.filename = out, "image/jpeg", "photo.jpg"
                    p.size = os.path.getsize(out)
                    p.width = p.height = None
                    await self._probe_into(p, out)
                    p.steps.append(f"{sniff.ext} → jpg")
                    if p.size <= TG_PHOTO_MAX_BYTES and self._photo_dims_ok(p.width, p.height):
                        return p
                p.kind = MediaKind.DOCUMENT
                p.filename = safe_filename(media.filename, f"image.{sniff.ext}")
                p.steps.append("图片超出 Telegram 照片限制，改为文件发送")
                return p
            # not an image at all -> document
            p.kind = MediaKind.DOCUMENT
            return p

        # --- animation (explicit) ---
        if kind == MediaKind.ANIMATION:
            if sniff.ext == "gif" or (sniff.category == "video" and sniff.ext == "mp4"):
                await self._probe_into(p, path)
                return p
            if self.ffmpeg.available():
                out = str(job_dir / "anim.mp4")
                await self.ffmpeg.to_mp4(path, out, silent=True)
                p.path, p.mime, p.filename = out, "video/mp4", "animation.mp4"
                p.steps.append(f"{sniff.ext} → mp4")
                await self._probe_into(p, out)
                return p
            p.kind = MediaKind.DOCUMENT
            return p

        # --- video ---
        if kind == MediaKind.VIDEO:
            p.kind = MediaKind.VIDEO
            info = await self.ffmpeg.probe(path)
            p.width, p.height, p.duration = p.width or info.width, p.height or info.height, p.duration or info.duration
            if sniff.ext != "mp4" and self.ffmpeg.available():
                out = str(job_dir / "video.mp4")
                try:
                    if info.vcodec in ("h264",) and info.acodec in (None, "aac", "mp3"):
                        await self.ffmpeg.remux_mp4(path, out)
                        p.steps.append(f"{sniff.ext} → mp4 (remux)")
                    else:
                        await self.ffmpeg.to_mp4(path, out)
                        p.steps.append(f"{sniff.ext} → mp4 (transcode)")
                    p.path, p.mime, p.filename = out, "video/mp4", safe_filename(Path(media.filename or "video").stem + ".mp4")
                    p.size = os.path.getsize(out)
                except BridgeError as exc:
                    p.steps.append(f"转码失败，按文件发送: {exc.code}")
                    p.kind = MediaKind.DOCUMENT
                    return p
            if self.ffmpeg.available():
                thumb = str(job_dir / "thumb.jpg")
                try:
                    await self.ffmpeg.thumbnail(p.path or path, thumb, at=min(1.0, (p.duration or 2) / 2))
                    p.thumbnail_path = thumb
                except BridgeError:
                    pass
            return p

        # --- voice ---
        if kind == MediaKind.VOICE:
            p.kind = MediaKind.VOICE
            if sniff.ext == "ogg" and b"OpusHead" in _head(path):
                await self._probe_into(p, path)
                return p
            if self.ffmpeg.available():
                out = str(job_dir / "voice.ogg")
                try:
                    await self.ffmpeg.to_ogg_opus(path, out)
                    p.path, p.mime, p.filename = out, "audio/ogg", "voice.ogg"
                    p.size = os.path.getsize(out)
                    p.steps.append(f"{sniff.ext} → ogg/opus")
                    await self._probe_into(p, out)
                    return p
                except BridgeError as exc:
                    p.steps.append(f"语音转换失败: {exc.code}")
            # fallback: deliver as audio/document
            p.kind = MediaKind.AUDIO if sniff.category == "audio" and sniff.ext in ("mp3", "m4a", "ogg", "flac", "wav") else MediaKind.DOCUMENT
            p.filename = safe_filename(media.filename, f"voice.{sniff.ext}")
            return p

        # --- audio ---
        if kind == MediaKind.AUDIO:
            if sniff.ext in ("mp3", "m4a", "flac", "ogg", "wav"):
                p.kind = MediaKind.AUDIO
                await self._probe_into(p, path)
                return p
            if sniff.ext in ("amr", "silk") and self.ffmpeg.available() and sniff.ext != "silk":
                out = str(job_dir / "audio.ogg")
                try:
                    await self.ffmpeg.to_ogg_opus(path, out)
                    p.kind = MediaKind.VOICE
                    p.path, p.mime, p.filename = out, "audio/ogg", "voice.ogg"
                    p.steps.append("amr → ogg/opus")
                    return p
                except BridgeError:
                    pass
            p.kind = MediaKind.DOCUMENT
            return p

        # --- document / anything else ---
        p.kind = MediaKind.DOCUMENT
        p.filename = safe_filename(media.filename, f"file.{sniff.ext}")
        return p

    @staticmethod
    def _photo_dims_ok(w: Optional[int], h: Optional[int]) -> bool:
        if not w or not h:
            return True  # unknown: let Telegram decide
        if w + h > TG_PHOTO_MAX_SIDE_SUM:
            return False
        ratio = max(w, h) / max(1, min(w, h))
        return ratio <= TG_PHOTO_MAX_RATIO

    # -- Telegram -> QQ ----------------------------------------------------------------
    async def _prepare_for_qq(self, media: Media, path: str, sniff: Sniff, job_dir: Path) -> Prepared:
        size = os.path.getsize(path)
        kind = media.kind
        p = Prepared(kind=kind, original=media, path=path, mime=sniff.mime, size=size,
                     width=media.width, height=media.height, duration=media.duration)

        if kind == MediaKind.PHOTO:
            if sniff.category in ("image", "animation"):
                return p
            p.kind = MediaKind.DOCUMENT
            return p

        if kind == MediaKind.STICKER:
            p.kind = MediaKind.PHOTO
            if media.is_animated:
                # .tgs (lottie) cannot be rendered without a lottie engine: use the
                # static preview Telegram provides, otherwise a text fallback.
                raise BridgeError("STICKER_TGS", "动画贴纸 (TGS) 无法转换", permanent=True)
            if media.is_video or sniff.category == "video":
                if not self.ffmpeg.available():
                    raise BridgeError("FFMPEG_MISSING", "视频贴纸需要 FFmpeg 转换", permanent=True)
                out = str(job_dir / "sticker.gif")
                await self.ffmpeg.to_gif(path, out, max_width=320, fps=15, max_seconds=6)
                p.path, p.mime, p.filename = out, "image/gif", "sticker.gif"
                p.steps.append("webm → gif")
                return p
            if sniff.ext == "webp" and self.ffmpeg.available():
                out = str(job_dir / "sticker.png")
                try:
                    await self.ffmpeg.to_png(path, out)
                    p.path, p.mime, p.filename = out, "image/png", "sticker.png"
                    p.steps.append("webp → png")
                except BridgeError:
                    pass  # most OneBot implementations accept webp anyway
            return p

        if kind == MediaKind.ANIMATION:
            p.kind = MediaKind.PHOTO
            if sniff.ext == "gif":
                return p
            if not self.ffmpeg.available():
                p.kind = MediaKind.VIDEO
                p.steps.append("无 FFmpeg，动画按视频发送")
                return p
            info = await self.ffmpeg.probe(path)
            duration = info.duration or media.duration or 0
            if duration and duration > 20:
                p.kind = MediaKind.VIDEO
                p.steps.append("动画过长，按视频发送")
                return p
            out = str(job_dir / "animation.gif")
            try:
                await self.ffmpeg.to_gif(path, out, max_width=400, fps=12, max_seconds=20)
                if os.path.getsize(out) > 8 * 1024 * 1024:
                    raise BridgeError("GIF_TOO_BIG", "gif too big")
                p.path, p.mime, p.filename = out, "image/gif", "animation.gif"
                p.steps.append("mp4 → gif")
            except BridgeError:
                p.kind = MediaKind.VIDEO
                p.path = path
                p.steps.append("GIF 转换失败或过大，按视频发送")
            return p

        if kind == MediaKind.VIDEO:
            if sniff.ext in ("mp4", "mov", "3gp"):
                return p
            if self.ffmpeg.available():
                out = str(job_dir / "video.mp4")
                info = await self.ffmpeg.probe(path)
                try:
                    if info.vcodec == "h264" and info.acodec in (None, "aac", "mp3"):
                        await self.ffmpeg.remux_mp4(path, out)
                        p.steps.append(f"{sniff.ext} → mp4 (remux)")
                    else:
                        await self.ffmpeg.to_mp4(path, out)
                        p.steps.append(f"{sniff.ext} → mp4 (transcode)")
                    p.path, p.mime, p.filename = out, "video/mp4", safe_filename(Path(media.filename or "video").stem + ".mp4")
                    return p
                except BridgeError as exc:
                    p.steps.append(f"转码失败，按文件发送: {exc.code}")
            p.kind = MediaKind.DOCUMENT
            return p

        if kind == MediaKind.VOICE:
            fmt = str(self.settings.get("qq_voice_format", "wav"))
            if sniff.ext == fmt:
                return p
            if not self.ffmpeg.available():
                p.steps.append("无 FFmpeg，直接发送原始语音")
                return p
            out = str(job_dir / f"voice.{fmt}")
            try:
                if fmt == "wav":
                    await self.ffmpeg.to_wav(path, out)
                elif fmt == "mp3":
                    await self.ffmpeg.to_mp3(path, out)
                else:
                    await self.ffmpeg.to_ogg_opus(path, out)
                p.path, p.filename = out, f"voice.{fmt}"
                p.mime = {"wav": "audio/wav", "mp3": "audio/mpeg", "ogg": "audio/ogg"}[fmt]
                p.steps.append(f"{sniff.ext} → {fmt}")
            except BridgeError as exc:
                p.steps.append(f"语音转换失败，发送原始文件: {exc.code}")
            return p

        if kind == MediaKind.AUDIO:
            p.kind = MediaKind.DOCUMENT
            p.filename = safe_filename(media.filename, f"audio.{sniff.ext}")
            return p

        p.kind = MediaKind.DOCUMENT
        p.filename = safe_filename(media.filename, f"file.{sniff.ext}")
        return p


def _sha256_file(path: os.PathLike[str] | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _head(path: str, n: int = 128) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read(n)
    except OSError:
        return b""
