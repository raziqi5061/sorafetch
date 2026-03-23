"""
ContentCreator Pro Studio - FastAPI Backend v8
Features:
- Multi-platform download (Sora, TikTok, FB, IG, Twitter, Bilibili, Douyin, YouTube)
- Quality selector (best/hd/sd)
- Video metadata extraction
- Thumbnail download
- Caption/subtitle extraction
- Video clipping (start/end time via ffmpeg)
- Multi-clip export
- Auto-rename with platform + date + title
- CSV/Excel URL import support
"""

import asyncio
import csv
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional, List

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Query, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

app = FastAPI(title="ContentCreator Pro Studio", version="8.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CHUNK_SIZE = 1024 * 512

# ─── Platform Detection ───────────────────────────────────────────────

PLATFORM_PATTERNS = {
    "sora":      [r"sora\.chatgpt\.com", r"sora\.openai\.com"],
    "youtube":   [r"youtube\.com", r"youtu\.be"],
    "tiktok":    [r"tiktok\.com", r"vm\.tiktok\.com", r"vt\.tiktok\.com"],
    "douyin":    [r"douyin\.com", r"iesdouyin\.com"],
    "facebook":  [r"facebook\.com", r"fb\.com", r"fb\.watch"],
    "instagram": [r"instagram\.com", r"instagr\.am"],
    "twitter":   [r"twitter\.com", r"x\.com"],
    "bilibili":  [r"bilibili\.com", r"b23\.tv"],
}

PLATFORM_NAMES = {
    "sora": "Sora", "youtube": "YouTube", "tiktok": "TikTok",
    "douyin": "Douyin", "facebook": "Facebook", "instagram": "Instagram",
    "twitter": "Twitter/X", "bilibili": "Bilibili", "generic": "Video",
}


def detect_platform(url: str) -> str:
    url_lower = url.lower()
    for platform, patterns in PLATFORM_PATTERNS.items():
        if any(re.search(p, url_lower) for p in patterns):
            return platform
    return "generic"


def format_bytes(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def format_duration(seconds: float) -> str:
    if not seconds:
        return "00:00"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def is_ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def smart_filename(title: str, platform: str, ext: str = "mp4") -> str:
    date = datetime.now().strftime("%Y%m%d")
    clean = re.sub(r'[^\w\s\-]', '', str(title or "video"))
    clean = re.sub(r'\s+', '_', clean.strip()).strip('_')[:40]
    plat = platform.upper()[:8]
    return f"{plat}_{date}_{clean}.{ext}"


def safe_filename(name: str, ext: str = "mp4") -> str:
    name = re.sub(r'[^\w\-.]', '_', str(name))
    name = re.sub(r'_+', '_', name).strip('_')
    if not name:
        name = "video"
    if not name.endswith(f".{ext}"):
        name = f"{name}.{ext}"
    return name[:100]


def get_best_format(platform: str, quality: str = "best") -> str:
    ffmpeg = is_ffmpeg_available()
    quality_map = {
        "4k":   "bestvideo[height<=2160]+bestaudio/best[height<=2160]",
        "hd":   "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        "sd":   "bestvideo[height<=480]+bestaudio/best[height<=480]",
        "best": "bestvideo+bestaudio/best",
    }
    if not ffmpeg:
        return "best[ext=mp4]/best[ext=webm]/best"
    fmt = quality_map.get(quality, quality_map["best"])
    if platform in ("tiktok", "douyin"):
        return f"bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    return fmt


def build_ydl_opts(platform: str, out_path: str, quality: str = "best",
                   subtitles: bool = False, thumbnail: bool = False) -> dict:
    opts = {
        "format": get_best_format(platform, quality),
        "outtmpl": out_path,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4" if is_ffmpeg_available() else None,
        "socket_timeout": 30,
        "retries": 3,
        "noplaylist": True,
    }

    if subtitles:
        opts.update({
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitlesformat": "srt",
            "subtitleslangs": ["en", "ur", "hi", "auto"],
        })

    if thumbnail:
        opts["writethumbnail"] = True

    platform_extras = {
        "tiktok": {
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
                "Referer": "https://www.tiktok.com/",
            },
            "extractor_args": {"tiktok": {"api_hostname": "api22-normal-c-useast2a.tiktokv.com"}},
        },
        "youtube": {
            "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        },
        "facebook": {
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            },
        },
        "instagram": {
            "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        },
        "bilibili": {
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.bilibili.com/",
            },
        },
    }

    if platform in platform_extras:
        opts.update(platform_extras[platform])

    return opts


# ─── Sora Extractor ───────────────────────────────────────────────────

FLIFLIK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://online.fliflik.com",
    "Referer": "https://online.fliflik.com/sora-video-downloader/",
}

VIDEO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Accept-Encoding": "identity",
    "Referer": "https://sora.chatgpt.com/",
}


def find_video_url_in_text(text: str) -> Optional[str]:
    try:
        data = json.loads(text)
        for key in ("url", "video_url", "download_url", "link", "source", "videoUrl"):
            val = data.get(key)
            if val and isinstance(val, str) and val.startswith("http"):
                return val
        text = json.dumps(data)
    except Exception:
        pass
    for pat in [
        r'https://videos\.openai\.com[^"\'<>\s\\]+',
        r'https://[^"\'<>\s\\]*oaiusercontent[^"\'<>\s\\]+',
        r'https://[^"\'<>\s\\]+\.mp4[^"\'<>\s\\]*',
    ]:
        m = re.search(pat, text)
        if m:
            return m.group(0).replace('\\u0026', '&').replace('\\/', '/')
    return None


async def resolve_sora(url: str) -> str:
    async with httpx.AsyncClient(follow_redirects=True, timeout=30,
        headers={"User-Agent": "Mozilla/5.0"}) as client:
        try:
            await client.get("https://online.fliflik.com/sora-video-downloader/", timeout=10)
        except Exception:
            pass
        try:
            resp = await client.post("https://online.fliflik.com/get-video-link",
                json={"url": url}, headers=FLIFLIK_HEADERS, timeout=20)
            if resp.status_code == 200:
                video_url = find_video_url_in_text(resp.text)
                if video_url:
                    return video_url
        except Exception:
            pass
    raise HTTPException(status_code=422, detail={"error": "sora_failed", "message": "Sora video extract nahi ho saki."})


# ─── yt-dlp helpers ───────────────────────────────────────────────────

async def run_ydl(url: str, opts: dict) -> dict:
    def _run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _run)
    except yt_dlp.utils.DownloadError as e:
        err = str(e)
        if "ffmpeg" in err.lower() or "merger" in err.lower():
            fallback = {**opts, "format": "best[ext=mp4]/best[ext=webm]/best", "merge_output_format": None}
            try:
                return await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(fallback).extract_info(url, download=True))
            except Exception as e2:
                raise HTTPException(status_code=422, detail={"error": "download_failed", "message": str(e2)[:300]})
        raise HTTPException(status_code=422, detail={"error": "download_failed", "message": err[:300]})


async def extract_info_only(url: str, platform: str) -> dict:
    opts = {**build_ydl_opts(platform, "/dev/null"), "skip_download": True}
    def _run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _run)
    except Exception as e:
        raise HTTPException(status_code=422, detail={"error": "info_failed", "message": str(e)[:300]})


def find_file(tmp_dir: str) -> Optional[Path]:
    files = sorted([f for f in Path(tmp_dir).iterdir() if f.is_file()],
                   key=lambda f: f.stat().st_size, reverse=True)
    return files[0] if files else None


# ─── Routes ──────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "name": "ContentCreator Pro Studio",
        "version": "8.0.0",
        "ffmpeg": is_ffmpeg_available(),
        "platforms": list(PLATFORM_NAMES.values()),
    }


@app.get("/health")
async def health():
    return {"status": "ok", "version": "8.0.0", "ffmpeg": is_ffmpeg_available()}


@app.get("/detect")
async def detect_route(url: str = Query(...)):
    p = detect_platform(url)
    return {"platform": p, "platform_name": PLATFORM_NAMES.get(p, "Generic")}


# ── Metadata endpoint ─────────────────────────────────────────────────
@app.get("/metadata")
async def get_metadata(url: str = Query(...)):
    """Extract full metadata: title, description, hashtags, duration, thumbnail, formats."""
    url = url.strip()
    platform = detect_platform(url)

    if platform == "sora":
        video_url = await resolve_sora(url)
        sid = re.search(r'/p/(s_[a-f0-9]+)', url, re.I)
        return {
            "platform": "sora",
            "platform_name": "Sora",
            "title": f"Sora Video {sid.group(1) if sid else ''}",
            "description": "",
            "hashtags": [],
            "duration": None,
            "duration_fmt": None,
            "thumbnail": None,
            "uploader": "Sora / OpenAI",
            "view_count": None,
            "like_count": None,
            "video_url": video_url,
            "available_qualities": ["best"],
        }

    info = await extract_info_only(url, platform)

    # Extract hashtags from title + description + tags
    tags = info.get("tags") or []
    desc = info.get("description") or ""
    hashtags = list(set(
        tags[:20] +
        re.findall(r'#\w+', desc)[:20]
    ))

    # Available quality options
    formats = info.get("formats") or []
    heights = sorted(set(
        f.get("height") for f in formats
        if f.get("height") and f.get("vcodec") != "none"
    ), reverse=True)
    qualities = []
    if any(h >= 2160 for h in heights): qualities.append("4k")
    if any(h >= 1080 for h in heights): qualities.append("hd")
    if any(h >= 480 for h in heights):  qualities.append("sd")
    if not qualities: qualities = ["best"]

    duration = info.get("duration")

    return {
        "platform": platform,
        "platform_name": PLATFORM_NAMES.get(platform, "Generic"),
        "title": info.get("title", ""),
        "description": (desc[:500] + "...") if len(desc) > 500 else desc,
        "hashtags": hashtags[:30],
        "duration": duration,
        "duration_fmt": format_duration(duration) if duration else None,
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader") or info.get("channel") or "",
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "upload_date": info.get("upload_date"),
        "available_qualities": qualities,
        "webpage_url": info.get("webpage_url", url),
    }


# ── Download endpoint ─────────────────────────────────────────────────
@app.get("/download")
async def download_video(
    url: str = Query(...),
    quality: str = Query("best"),
    filename: Optional[str] = Query(None),
    thumbnail: bool = Query(False),
    subtitles: bool = Query(False),
):
    url = url.strip()
    platform = detect_platform(url)

    # Sora
    if platform == "sora":
        video_url = await resolve_sora(url)
        sid = re.search(r'/p/(s_[a-f0-9]+)', url, re.I)
        out_filename = filename or smart_filename(f"sora_{sid.group(1) if sid else 'video'}", "sora")
        resp_headers = {
            "Content-Disposition": f'attachment; filename="{out_filename}"',
            "Access-Control-Allow-Origin": "*",
        }
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
                head = await client.head(video_url, headers=VIDEO_HEADERS)
                cl = head.headers.get("content-length")
                if cl:
                    resp_headers["Content-Length"] = cl
        except Exception:
            pass

        async def sora_stream():
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=20.0, read=600.0, write=60.0, pool=10.0),
                follow_redirects=True,
            ) as client:
                async with client.stream("GET", video_url, headers=VIDEO_HEADERS) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes(chunk_size=CHUNK_SIZE):
                        yield chunk

        return StreamingResponse(sora_stream(), media_type="video/mp4", headers=resp_headers)

    # yt-dlp
    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, "video.%(ext)s")
    opts = build_ydl_opts(platform, tmp_path, quality=quality,
                          subtitles=subtitles, thumbnail=thumbnail)
    try:
        info = await run_ydl(url, opts)
        actual = find_file(tmp_dir)
        if not actual:
            raise HTTPException(status_code=500, detail="File not found after download.")

        title = info.get("title", "video") if info else "video"
        ext = actual.suffix.lstrip('.') or "mp4"
        out_filename = filename or smart_filename(title, platform, ext)
        file_size = actual.stat().st_size

        resp_headers = {
            "Content-Disposition": f'attachment; filename="{out_filename}"',
            "Content-Length": str(file_size),
            "Access-Control-Allow-Origin": "*",
            "X-Video-Title": urllib.parse.quote(title[:100]),
            "X-Platform": platform,
        }

        async def file_stream():
            try:
                with open(actual, "rb") as f:
                    while True:
                        chunk = f.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        yield chunk
            finally:
                try:
                    actual.unlink(missing_ok=True)
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                except Exception:
                    pass

        media_type = f"video/{ext}" if ext in ("mp4", "webm", "mov") else "video/mp4"
        return StreamingResponse(file_stream(), media_type=media_type, headers=resp_headers)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:300])


# ── Clip endpoint ─────────────────────────────────────────────────────
@app.get("/clip")
async def clip_video(
    url: str = Query(...),
    start: str = Query(..., description="Start time: HH:MM:SS or seconds"),
    end: str = Query(..., description="End time: HH:MM:SS or seconds"),
    quality: str = Query("hd"),
    filename: Optional[str] = Query(None),
):
    """Download video and clip to start-end range using ffmpeg."""
    if not is_ffmpeg_available():
        raise HTTPException(status_code=503, detail={
            "error": "ffmpeg_not_available",
            "message": "Clipping ke liye ffmpeg zaroori hai. nixpacks.toml check karein.",
        })

    url = url.strip()
    platform = detect_platform(url)

    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, "source.%(ext)s")

    try:
        # Step 1: Download full video
        if platform == "sora":
            video_url = await resolve_sora(url)
            src_path = os.path.join(tmp_dir, "source.mp4")
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=20.0, read=600.0, write=60.0, pool=10.0),
                follow_redirects=True,
            ) as client:
                async with client.stream("GET", video_url, headers=VIDEO_HEADERS) as resp:
                    resp.raise_for_status()
                    with open(src_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=CHUNK_SIZE):
                            f.write(chunk)
            title = "sora_video"
        else:
            opts = build_ydl_opts(platform, tmp_path, quality=quality)
            info = await run_ydl(url, opts)
            src_file = find_file(tmp_dir)
            if not src_file:
                raise HTTPException(status_code=500, detail="Source video download failed.")
            src_path = str(src_file)
            title = info.get("title", "video") if info else "video"

        # Step 2: Clip with ffmpeg
        clip_path = os.path.join(tmp_dir, "clip.mp4")
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-to", str(end),
            "-i", src_path,
            "-c:v", "libx264",
            "-c:a", "aac",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            clip_path,
        ]

        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise HTTPException(status_code=500, detail={
                "error": "clip_failed",
                "message": stderr.decode()[-500:] if stderr else "ffmpeg error",
            })

        clip_file = Path(clip_path)
        if not clip_file.exists() or clip_file.stat().st_size == 0:
            raise HTTPException(status_code=500, detail="Clip file empty or not created.")

        out_filename = filename or smart_filename(f"{title}_clip_{start}-{end}".replace(":", "-"), platform)
        file_size = clip_file.stat().st_size

        async def clip_stream():
            try:
                with open(clip_file, "rb") as f:
                    while True:
                        chunk = f.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        yield chunk
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        return StreamingResponse(
            clip_stream(),
            media_type="video/mp4",
            headers={
                "Content-Disposition": f'attachment; filename="{out_filename}"',
                "Content-Length": str(file_size),
                "Access-Control-Allow-Origin": "*",
            },
        )

    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e)[:300])


# ── Multi-clip endpoint ───────────────────────────────────────────────
class ClipSegment(BaseModel):
    start: str
    end: str
    label: Optional[str] = None


class MultiClipRequest(BaseModel):
    url: str
    segments: List[ClipSegment]
    quality: str = "hd"


@app.post("/multi-clip")
async def multi_clip(request: MultiClipRequest):
    """Download once, export multiple clips."""
    if not is_ffmpeg_available():
        raise HTTPException(status_code=503, detail="ffmpeg not available")
    if len(request.segments) > 20:
        raise HTTPException(status_code=400, detail="Max 20 clips per request.")

    url = request.url.strip()
    platform = detect_platform(url)
    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, "source.%(ext)s")

    try:
        # Download once
        if platform == "sora":
            video_url = await resolve_sora(url)
            src_path = os.path.join(tmp_dir, "source.mp4")
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=20.0, read=600.0, write=60.0, pool=10.0),
                follow_redirects=True,
            ) as client:
                async with client.stream("GET", video_url, headers=VIDEO_HEADERS) as resp:
                    with open(src_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=CHUNK_SIZE):
                            f.write(chunk)
            title = "sora_clip"
        else:
            opts = build_ydl_opts(platform, tmp_path, quality=request.quality)
            info = await run_ydl(url, opts)
            src_file = find_file(tmp_dir)
            if not src_file:
                raise HTTPException(status_code=500, detail="Source download failed.")
            src_path = str(src_file)
            title = info.get("title", "video") if info else "video"

        # Export each clip
        results = []
        for i, seg in enumerate(request.segments):
            clip_name = seg.label or f"clip_{i+1}"
            clip_path = os.path.join(tmp_dir, f"{clip_name}.mp4")
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(seg.start),
                "-to", str(seg.end),
                "-i", src_path,
                "-c:v", "libx264", "-c:a", "aac",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart",
                clip_path,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, stderr = await proc.communicate()

            if proc.returncode == 0 and Path(clip_path).exists():
                results.append({
                    "label": clip_name,
                    "start": seg.start,
                    "end": seg.end,
                    "status": "ready",
                    "size": Path(clip_path).stat().st_size,
                    "size_human": format_bytes(Path(clip_path).stat().st_size),
                    "download_path": clip_path,
                })
            else:
                results.append({
                    "label": clip_name,
                    "start": seg.start,
                    "end": seg.end,
                    "status": "failed",
                    "error": stderr.decode()[-200:] if stderr else "unknown",
                })

        return JSONResponse(content={
            "title": title,
            "platform": platform,
            "total_clips": len(results),
            "ready": sum(1 for r in results if r["status"] == "ready"),
            "clips": results,
            "tmp_dir": tmp_dir,
        })

    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e)[:300])


@app.get("/clip-file")
async def get_clip_file(path: str = Query(...)):
    """Stream a specific clip file that was prepared by multi-clip."""
    clip_file = Path(path)
    if not clip_file.exists() or not clip_file.is_file():
        raise HTTPException(status_code=404, detail="Clip file not found.")
    file_size = clip_file.stat().st_size
    out_filename = clip_file.name

    async def stream():
        try:
            with open(clip_file, "rb") as f:
                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                clip_file.unlink(missing_ok=True)
            except Exception:
                pass

    return StreamingResponse(stream(), media_type="video/mp4", headers={
        "Content-Disposition": f'attachment; filename="{out_filename}"',
        "Content-Length": str(file_size),
        "Access-Control-Allow-Origin": "*",
    })


# ── Thumbnail endpoint ────────────────────────────────────────────────
@app.get("/thumbnail")
async def get_thumbnail(url: str = Query(...)):
    """Get thumbnail URL and optionally proxy it."""
    url = url.strip()
    platform = detect_platform(url)
    if platform == "sora":
        raise HTTPException(status_code=422, detail="Sora thumbnails not supported.")
    info = await extract_info_only(url, platform)
    thumb_url = info.get("thumbnail")
    if not thumb_url:
        raise HTTPException(status_code=404, detail="Thumbnail not found.")

    # Proxy the thumbnail
    async def thumb_stream():
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            async with client.stream("GET", thumb_url) as resp:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    yield chunk

    title = info.get("title", "thumbnail")
    fname = smart_filename(title, platform, "jpg")
    return StreamingResponse(thumb_stream(), media_type="image/jpeg", headers={
        "Content-Disposition": f'attachment; filename="{fname}"',
        "Access-Control-Allow-Origin": "*",
    })


# ── Subtitles endpoint ────────────────────────────────────────────────
@app.get("/subtitles")
async def get_subtitles(url: str = Query(...), lang: str = Query("en")):
    """Extract subtitles/captions as text."""
    url = url.strip()
    platform = detect_platform(url)
    if platform == "sora":
        raise HTTPException(status_code=422, detail="Sora subtitles not supported.")

    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, "sub.%(ext)s")
    opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitlesformat": "srt",
        "subtitleslangs": [lang, "en"],
        "outtmpl": tmp_path,
        "quiet": True,
        "no_warnings": True,
    }

    def _run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)

    loop = asyncio.get_event_loop()
    try:
        info = await loop.run_in_executor(None, _run)
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=f"Subtitle extraction failed: {str(e)[:200]}")

    # Find subtitle file
    sub_files = list(Path(tmp_dir).glob("*.srt")) + list(Path(tmp_dir).glob("*.vtt"))
    if not sub_files:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=404, detail="No subtitles found for this video.")

    sub_content = sub_files[0].read_text(encoding="utf-8", errors="ignore")
    title = info.get("title", "subtitle") if info else "subtitle"
    fname = smart_filename(title, platform, "srt")
    shutil.rmtree(tmp_dir, ignore_errors=True)

    return StreamingResponse(
        iter([sub_content.encode("utf-8")]),
        media_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Access-Control-Allow-Origin": "*",
        },
    )


# ── CSV/Excel URL import ──────────────────────────────────────────────
@app.post("/import-urls")
async def import_urls(file: UploadFile = File(...)):
    """Parse CSV or plain text file and return list of URLs."""
    content = await file.read()
    text = content.decode("utf-8", errors="ignore")

    urls = []
    # Try CSV first
    try:
        reader = csv.reader(io.StringIO(text))
        for row in reader:
            for cell in row:
                cell = cell.strip().strip('"').strip("'")
                if cell.startswith("http"):
                    urls.append(cell)
    except Exception:
        pass

    # Fallback: line by line
    if not urls:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("http"):
                urls.append(line)

    if not urls:
        raise HTTPException(status_code=422, detail="Koi valid URL nahi mili file mein.")

    # Detect platforms
    result = []
    for url in urls[:50]:
        p = detect_platform(url)
        result.append({
            "url": url,
            "platform": p,
            "platform_name": PLATFORM_NAMES.get(p, "Generic"),
        })

    return JSONResponse(content={"urls": result, "total": len(result)})


# ── Batch info ────────────────────────────────────────────────────────
class BatchRequest(BaseModel):
    urls: List[str]


@app.post("/batch/info")
async def batch_info(request: BatchRequest):
    if len(request.urls) > 50:
        raise HTTPException(status_code=400, detail="Max 50 URLs.")
    results = []
    for url in request.urls:
        p = detect_platform(url.strip())
        results.append({
            "url": url,
            "platform": p,
            "platform_name": PLATFORM_NAMES.get(p, "Generic"),
            "status": "queued",
        })
    return JSONResponse(content={"results": results, "total": len(results)})
