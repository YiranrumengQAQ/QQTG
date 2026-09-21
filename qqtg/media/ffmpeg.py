"""Thin async wrapper around ffmpeg / ffprobe with hard timeouts.

Every conversion is run through ``run_ffmpeg`` which:
  * kills the process on timeout (a broken video must never wedge a worker),
  * limits threads so a single job cannot eat the whole VPS,
  * never logs command lines containing credentials (only local paths are used).
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..logsys import get_logger
from ..models import BridgeError

log = get_logger("media")


@dataclass
class ProbeInfo:
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[float] = None
    vcodec: Optional[str] = None
    acodec: Optional[str] = None
    has_audio: bool = False
    has_video: bool = False
    format_name: str = ""
    nb_frames: Optional[int] = None


class FFmpeg:
    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe", timeout: int = 180, threads: int = 2):
        self.ffmpeg_bin = shutil.which(ffmpeg) or ffmpeg
        self.ffprobe_bin = shutil.which(ffprobe) or ffprobe
        self.timeout = timeout
        self.threads = threads
        self._encoders: Optional[set[str]] = None

    # -- availability ---------------------------------------------------------
    def available(self) -> bool:
        return shutil.which(self.ffmpeg_bin) is not None or Path(self.ffmpeg_bin).is_file()

    def probe_available(self) -> bool:
        return shutil.which(self.ffprobe_bin) is not None or Path(self.ffprobe_bin).is_file()

    async def version(self) -> str:
        if not self.available():
            return ""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.ffmpeg_bin, "-version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            first = out.decode(errors="replace").splitlines()[0] if out else ""
            m = re.search(r"ffmpeg version (\S+)", first)
            return m.group(1) if m else first
        except Exception:
            return ""

    async def encoders(self) -> set[str]:
        if self._encoders is not None:
            return self._encoders
        found: set[str] = set()
        if self.available():
            try:
                proc = await asyncio.create_subprocess_exec(
                    self.ffmpeg_bin, "-hide_banner", "-encoders",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
                for line in out.decode(errors="replace").splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and re.match(r"^[VAS][F.][S.][X.][B.][D.]$", parts[0]):
                        found.add(parts[1])
            except Exception:
                pass
        self._encoders = found
        return found

    async def has_encoder(self, *names: str) -> bool:
        enc = await self.encoders()
        return any(n in enc for n in names)

    # -- probing ---------------------------------------------------------------
    async def probe(self, path: str) -> ProbeInfo:
        info = ProbeInfo()
        if self.probe_available():
            try:
                proc = await asyncio.create_subprocess_exec(
                    self.ffprobe_bin, "-v", "error", "-print_format", "json",
                    "-show_format", "-show_streams", path,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
                data = json.loads(out.decode(errors="replace") or "{}")
                fmt = data.get("format", {})
                info.format_name = fmt.get("format_name", "")
                try:
                    info.duration = float(fmt.get("duration")) if fmt.get("duration") else None
                except (TypeError, ValueError):
                    info.duration = None
                for s in data.get("streams", []):
                    if s.get("codec_type") == "video" and not info.has_video:
                        # attached pictures (album art) are not real video
                        if s.get("disposition", {}).get("attached_pic"):
                            continue
                        info.has_video = True
                        info.vcodec = s.get("codec_name")
                        info.width = s.get("width")
                        info.height = s.get("height")
                        if s.get("nb_frames"):
                            try:
                                info.nb_frames = int(s["nb_frames"])
                            except ValueError:
                                pass
                        if info.duration is None and s.get("duration"):
                            try:
                                info.duration = float(s["duration"])
                            except ValueError:
                                pass
                    elif s.get("codec_type") == "audio" and not info.has_audio:
                        info.has_audio = True
                        info.acodec = s.get("codec_name")
                return info
            except Exception as exc:
                log.debug("ffprobe failed, falling back to ffmpeg -i: %s", exc)
        # Fallback: parse `ffmpeg -i` output
        if not self.available():
            return info
        try:
            proc = await asyncio.create_subprocess_exec(
                self.ffmpeg_bin, "-hide_banner", "-i", path,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(proc.communicate(), timeout=30)
            text = err.decode(errors="replace")
            m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
            if m:
                info.duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            vm = re.search(r"Video: (\w+).*?(\d{2,5})x(\d{2,5})", text)
            if vm:
                info.has_video = True
                info.vcodec = vm.group(1)
                info.width, info.height = int(vm.group(2)), int(vm.group(3))
            am = re.search(r"Audio: (\w+)", text)
            if am:
                info.has_audio = True
                info.acodec = am.group(1)
            im = re.search(r"Input #0, (\S+),", text)
            if im:
                info.format_name = im.group(1)
        except Exception as exc:
            log.debug("ffmpeg -i probe failed: %s", exc)
        return info

    # -- running ---------------------------------------------------------------
    async def run(self, args: list[str], timeout: Optional[int] = None) -> None:
        if not self.available():
            raise BridgeError("FFMPEG_MISSING", "服务器未安装 FFmpeg，无法转换媒体", permanent=True)
        cmd = [self.ffmpeg_bin, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-threads", str(self.threads), *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout or self.timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise BridgeError("MEDIA_TIMEOUT", f"媒体转换超时（>{timeout or self.timeout}s）", permanent=True)
        if proc.returncode != 0:
            msg = err.decode(errors="replace").strip().splitlines()
            tail = msg[-1] if msg else f"exit code {proc.returncode}"
            raise BridgeError("MEDIA_CONVERT_FAILED", f"媒体转换失败: {tail[:200]}", permanent=True)

    # -- conversions -----------------------------------------------------------
    async def to_ogg_opus(self, src: str, dst: str) -> None:
        codec = "libopus" if await self.has_encoder("libopus") else "opus"
        await self.run(["-i", src, "-vn", "-c:a", codec, "-b:a", "48k", "-ar", "48000", "-ac", "1", "-application", "voip", dst])

    async def to_wav(self, src: str, dst: str) -> None:
        await self.run(["-i", src, "-vn", "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", dst])

    async def to_mp3(self, src: str, dst: str) -> None:
        if not await self.has_encoder("libmp3lame"):
            raise BridgeError("FFMPEG_CODEC", "FFmpeg 缺少 libmp3lame 编码器", permanent=True)
        await self.run(["-i", src, "-vn", "-c:a", "libmp3lame", "-b:a", "64k", "-ac", "1", dst])

    async def to_mp4(self, src: str, dst: str, max_width: int = 1280, silent: bool = False) -> None:
        if not await self.has_encoder("libx264"):
            raise BridgeError("FFMPEG_CODEC", "FFmpeg 缺少 libx264 编码器", permanent=True)
        vf = f"scale='min({max_width},iw)':-2:flags=lanczos,format=yuv420p"
        args = ["-i", src, "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-movflags", "+faststart"]
        if silent:
            args += ["-an"]
        else:
            args += ["-c:a", "aac", "-b:a", "128k"]
        await self.run(args + [dst])

    async def remux_mp4(self, src: str, dst: str) -> None:
        await self.run(["-i", src, "-c", "copy", "-movflags", "+faststart", dst])

    async def to_gif(self, src: str, dst: str, max_width: int = 400, fps: int = 15, max_seconds: float = 15) -> None:
        # Two-pass palette for decent quality; alpha is flattened onto white for webm stickers.
        filters = (
            f"fps={fps},scale='min({max_width},iw)':-1:flags=lanczos,"
            "split[s0][s1];[s0]palettegen=max_colors=192:stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=3"
        )
        await self.run(["-t", str(max_seconds), "-i", src, "-vf", filters, "-loop", "0", dst])

    async def to_png(self, src: str, dst: str) -> None:
        await self.run(["-i", src, "-frames:v", "1", dst])

    async def to_jpeg(self, src: str, dst: str, max_side: int = 4096) -> None:
        vf = f"scale='if(gt(iw,ih),min({max_side},iw),-2)':'if(gt(iw,ih),-2,min({max_side},ih))'"
        await self.run(["-i", src, "-frames:v", "1", "-vf", vf, "-q:v", "4", dst])

    async def thumbnail(self, src: str, dst: str, at: float = 1.0) -> None:
        await self.run(["-ss", str(at), "-i", src, "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", dst], timeout=60)
