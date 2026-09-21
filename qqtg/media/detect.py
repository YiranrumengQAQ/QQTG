"""Content sniffing by magic bytes (no external ``file`` dependency)."""
from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Sniff:
    mime: str
    ext: str
    category: str  # image | animation | video | audio | other

    @property
    def is_image(self) -> bool:
        return self.category == "image"


_IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "bmp", "heic"}
_AUDIO_MIME = {
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "amr": "audio/amr",
    "silk": "audio/silk",
    "wma": "audio/x-ms-wma",
}
_VIDEO_MIME = {
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "mkv": "video/x-matroska",
    "webm": "video/webm",
    "avi": "video/x-msvideo",
    "flv": "video/x-flv",
    "3gp": "video/3gpp",
    "ts": "video/mp2t",
}


def _from_ext(ext: str) -> Sniff:
    ext = ext.lower().lstrip(".")
    if ext in _IMAGE_EXT:
        mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
        return Sniff(mime, "jpg" if ext == "jpeg" else ext, "image")
    if ext == "gif":
        return Sniff("image/gif", "gif", "animation")
    if ext in _AUDIO_MIME:
        return Sniff(_AUDIO_MIME[ext], ext, "audio")
    if ext in _VIDEO_MIME:
        return Sniff(_VIDEO_MIME[ext], ext, "video")
    if ext == "tgs":
        return Sniff("application/x-tgsticker", "tgs", "other")
    guess, _ = mimetypes.guess_type("x." + ext) if ext else (None, None)
    return Sniff(guess or "application/octet-stream", ext or "bin", "other")


def sniff_bytes(head: bytes, filename: str | None = None) -> Sniff:
    h = head
    if h.startswith(b"\xff\xd8\xff"):
        return Sniff("image/jpeg", "jpg", "image")
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        # APNG detection: look for acTL chunk in the first bytes
        if b"acTL" in h[:256]:
            return Sniff("image/apng", "png", "animation")
        return Sniff("image/png", "png", "image")
    if h.startswith(b"GIF87a") or h.startswith(b"GIF89a"):
        return Sniff("image/gif", "gif", "animation")
    if h.startswith(b"RIFF") and h[8:12] == b"WEBP":
        # Animated WebP has VP8X with animation flag
        if h[12:16] == b"VP8X" and len(h) > 20 and (h[20] & 0x02):
            return Sniff("image/webp", "webp", "animation")
        return Sniff("image/webp", "webp", "image")
    if h.startswith(b"BM"):
        return Sniff("image/bmp", "bmp", "image")
    if h[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypheim"):
        return Sniff("image/heic", "heic", "image")
    if h[4:8] == b"ftyp":
        brand = h[8:12]
        if brand in (b"M4A ", b"M4B "):
            return Sniff("audio/mp4", "m4a", "audio")
        if brand == b"qt  ":
            return Sniff("video/quicktime", "mov", "video")
        if brand in (b"3gp4", b"3gp5", b"3gp6", b"3gpp"):
            return Sniff("video/3gpp", "3gp", "video")
        return Sniff("video/mp4", "mp4", "video")
    if h.startswith(b"\x1a\x45\xdf\xa3"):
        if b"webm" in h[:64]:
            return Sniff("video/webm", "webm", "video")
        return Sniff("video/x-matroska", "mkv", "video")
    if h.startswith(b"RIFF") and h[8:12] == b"AVI ":
        return Sniff("video/x-msvideo", "avi", "video")
    if h.startswith(b"FLV\x01"):
        return Sniff("video/x-flv", "flv", "video")
    if h.startswith(b"ID3") or (len(h) > 1 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0 and (h[1] & 0x06) != 0):
        return Sniff("audio/mpeg", "mp3", "audio")
    if h.startswith(b"OggS"):
        if b"OpusHead" in h[:64]:
            return Sniff("audio/ogg", "ogg", "audio")
        return Sniff("audio/ogg", "ogg", "audio")
    if h.startswith(b"fLaC"):
        return Sniff("audio/flac", "flac", "audio")
    if h.startswith(b"RIFF") and h[8:12] == b"WAVE":
        return Sniff("audio/wav", "wav", "audio")
    if h.startswith(b"#!AMR"):
        return Sniff("audio/amr", "amr", "audio")
    if h.startswith(b"#!SILK") or h.startswith(b"\x02#!SILK"):
        return Sniff("audio/silk", "silk", "audio")
    if h.startswith(b"\x30\x26\xb2\x75\x8e\x66\xcf\x11"):
        return Sniff("audio/x-ms-wma", "wma", "audio")
    if h.startswith(b"\x1f\x8b"):
        if filename and filename.lower().endswith(".tgs"):
            return Sniff("application/x-tgsticker", "tgs", "other")
        return Sniff("application/gzip", "gz", "other")
    if h.startswith(b"%PDF"):
        return Sniff("application/pdf", "pdf", "other")
    if h.startswith(b"PK\x03\x04"):
        if filename:
            ext = Path(filename).suffix.lower().lstrip(".")
            if ext in ("apk", "docx", "xlsx", "pptx", "jar", "epub"):
                return _from_ext(ext)
        return Sniff("application/zip", "zip", "other")
    if h.startswith(b"Rar!\x1a\x07"):
        return Sniff("application/vnd.rar", "rar", "other")
    if h.startswith(b"7z\xbc\xaf\x27\x1c"):
        return Sniff("application/x-7z-compressed", "7z", "other")
    if filename:
        return _from_ext(Path(filename).suffix)
    return Sniff("application/octet-stream", "bin", "other")


def sniff_file(path: str | os.PathLike[str], filename: str | None = None) -> Sniff:
    p = Path(path)
    try:
        with open(p, "rb") as fh:
            head = fh.read(512)
    except OSError:
        head = b""
    return sniff_bytes(head, filename or p.name)


def safe_filename(name: str | None, default: str = "file") -> str:
    if not name:
        return default
    name = os.path.basename(name).strip().replace("\x00", "")
    name = "".join(c for c in name if c not in '<>:"/\\|?*')
    return name[:120] or default
