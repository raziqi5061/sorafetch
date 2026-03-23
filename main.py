"""
MultiVidFetch - FastAPI Backend v7
Multi-platform: Sora, TikTok, Facebook, Instagram, Twitter/X, Douyin, Bilibili
ffmpeg-safe format selection + nixpacks ffmpeg support
"""

import asyncio
import json
import os
import re
import tempfile
import urllib.parse
from pathlib import Path
from typing import Optional

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

app = FastAPI(title="MultiVidFetch API", version="7.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CHUNK_SIZE = 1024 * 512

# ─── Platform detection ───────────────────────────────────────────────

PLATFORM_PATTERNS = {
    "sora":      [r"sora\.chatgpt\.com", r"sora\.openai\.com"],
    "tiktok":    [r"tiktok\.com", r"vm\.tiktok\.com", r"vt\.tiktok\.com"],
    "douyin":    [r"douyin\.com", r"iesdouyin\.com"],
    "facebook":  [r"facebook\.com", r"fb\.com", r"fb\.watch"],
    "instagram": [r"instagram\.com", r"instagr\.am"],
    "twitter":   [r"twitter\.com", r"x\.com"],
    "bilibili":  [r"bilibili\.com", r"b23\.tv"],
}

PLATFORM_NAMES = {
    "sora": "Sora", "tiktok": "TikTok", "douyin": "Douyin",
    "facebook": "Facebook", "instagram": "Instagram",
    "twitter": "Twitter/X", "bilibili": "Bilibili",
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


def safe_filename(name: str, ext: str = "mp4") -> str:
    name = re.sub(r'[^\w\-.]', '_', str(name))
    name = re.sub(r'_+', '_', name).strip('_')
    if not name:
        name = "video"
    if not name.endswith(f".{ext}"):
        name = f"{name}.{ext}"
    return name[:100]


def is_ffmpeg_available() -> bool:
    """Check if ffmpeg is installed on the system."""
    import shutil
    return shutil.which("ffmpeg") is not None


def get_best_format(platform: str) -> str:
    """
    Return yt-dlp format string.
    If ffmpeg available: prefer best quality with merge.
    If not: prefer pre-merged single-file formats only.
    """
    if is_ffmpeg_available():
        # Full quality with merge
        if platform == "tiktok":
            return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        if platform == "bilibili":
            return "bestvideo+bestaudio/best"
        return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    else:
        # No ffmpeg — only pre-merged formats, no merging needed
        return "best[ext=mp4]/best[ext=webm]/best"


# ─── yt-dlp common options ────────────────────────────────────────────

def build_ydl_opts(platform: str, out_path: str) -> dict:
    fmt = get_best_format(platform)

    base = {
        "format": fmt,
        "outtmpl": out_path,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "socket_timeout": 30,
        "retries": 3,
        "noplaylist": True,
    }

    # Only add ffmpeg options if available
    if not is_ffmpeg_available():
        base["prefer_ffmpeg"] = False
        base["merge_output_format"] = None

    platform_extras = {
        "tiktok": {
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
                "Referer": "https://www.tiktok.com/",
            },
            "extractor_args": {
                "tiktok": {"api_hostname": "api22-normal-c-useast2a.tiktokv.com"}
            },
        },
        "douyin": {
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
                "Referer": "https://www.douyin.com/",
            },
        },
        "facebook": {
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
        },
        "instagram": {
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            },
        },
        "twitter": {
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            },
        },
        "bilibili": {
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.bilibili.com/",
            },
        },
    }

    if platform in platform_extras:
        base.update(platform_extras[platform])

    return base


# ─── Sora extractor (fliflik) ─────────────────────────────────────────

FLIFLIK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://online.fliflik.com",
    "Referer": "https://online.fliflik.com/sora-video-downloader/",
}

VIDEO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Encoding": "identity",
    "Referer": "https://sora.chatgpt.com/",
}


def extract_share_id(url: str) -> Optional[str]:
    m = re.search(r'/p/(s_[a-f0-9]+)', url, re.I)
    return m.group(1) if m else None


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
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=30,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    ) as client:
        try:
            await client.get("https://online.fliflik.com/sora-video-downloader/", timeout=10)
        except Exception:
            pass
        try:
            resp = await client.post(
                "https://online.fliflik.com/get-video-link",
                json={"url": url},
                headers=FLIFLIK_HEADERS,
                timeout=20,
            )
            if resp.status_code == 200:
                video_url = find_video_url_in_text(resp.text)
                if video_url:
                    return video_url
        except Exception:
            pass

    raise HTTPException(status_code=422, detail={
        "error": "sora_extract_failed",
        "message": "Sora video URL extract nahi ho saki.",
        "platform": "sora",
    })


# ─── yt-dlp download ──────────────────────────────────────────────────

async def download_with_ytdlp(url: str, platform: str, out_path: str) -> dict:
    opts = build_ydl_opts(platform, out_path)

    def _run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)

    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _run)
    except yt_dlp.utils.DownloadError as e:
        err_msg = str(e)

        # ffmpeg missing error — retry with no-merge format
        if "ffmpeg" in err_msg.lower() or "merger" in err_msg.lower():
            fallback_opts = {**opts, "format": "best[ext=mp4]/best[ext=webm]/best"}
            if "merge_output_format" in fallback_opts:
                fallback_opts["merge_output_format"] = None

            def _retry():
                with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                    return ydl.extract_info(url, download=True)

            try:
                return await loop.run_in_executor(None, _retry)
            except Exception as e2:
                raise HTTPException(status_code=422, detail={
                    "error": "download_failed",
                    "message": str(e2)[:300],
                    "platform": platform,
                })

        raise HTTPException(status_code=422, detail={
            "error": "download_failed",
            "message": err_msg[:300],
            "platform": platform,
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


def find_downloaded_file(tmp_dir: str) -> Optional[Path]:
    """Find the actual downloaded file — yt-dlp writes real ext."""
    candidates = sorted(
        [f for f in Path(tmp_dir).iterdir() if f.is_file()],
        key=lambda f: f.stat().st_size,
        reverse=True,
    )
    return candidates[0] if candidates else None


# ─── Routes ──────────────────────────────────────────────────────────

@app.get("/")
async def root():
    ffmpeg = is_ffmpeg_available()
    return {
        "message": "MultiVidFetch API v7",
        "version": "7.0.0",
        "ffmpeg_available": ffmpeg,
        "supported": list(PLATFORM_NAMES.values()),
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "7.0.0",
        "ffmpeg": is_ffmpeg_available(),
    }


@app.get("/detect")
async def detect(url: str = Query(...)):
    platform = detect_platform(url)
    return {
        "platform": platform,
        "platform_name": PLATFORM_NAMES.get(platform, "Generic"),
        "ffmpeg_available": is_ffmpeg_available(),
    }


@app.get("/download")
async def download_video(
    url: str = Query(...),
    filename: Optional[str] = Query(None),
):
    url = url.strip()
    platform = detect_platform(url)

    # ── Sora ─────────────────────────────────────────────────────────
    if platform == "sora":
        video_url = await resolve_sora(url)
        share_id = extract_share_id(url)
        out_filename = filename or f"sora-{share_id or 'video'}.mp4"
        out_filename = safe_filename(out_filename)

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

    # ── yt-dlp platforms ──────────────────────────────────────────────
    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, "video.%(ext)s")

    try:
        info = await download_with_ytdlp(url, platform, tmp_path)

        actual_file = find_downloaded_file(tmp_dir)
        if not actual_file:
            raise HTTPException(status_code=500, detail="Downloaded file not found.")

        title = info.get("title", "video") if info else "video"
        ext = actual_file.suffix.lstrip('.') or "mp4"
        out_filename = filename or safe_filename(title, ext)
        file_size = actual_file.stat().st_size

        resp_headers = {
            "Content-Disposition": f'attachment; filename="{out_filename}"',
            "Content-Length": str(file_size),
            "Access-Control-Allow-Origin": "*",
        }

        async def file_stream():
            try:
                with open(actual_file, "rb") as f:
                    while True:
                        chunk = f.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        yield chunk
            finally:
                try:
                    actual_file.unlink(missing_ok=True)
                    Path(tmp_dir).rmdir()
                except Exception:
                    pass

        media_type = "video/mp4" if ext == "mp4" else "video/webm"
        return StreamingResponse(file_stream(), media_type=media_type, headers=resp_headers)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:300])


class BatchRequest(BaseModel):
    urls: list[str]


@app.post("/batch/info")
async def batch_info(request: BatchRequest):
    if len(request.urls) > 50:
        raise HTTPException(status_code=400, detail="Max 50 URLs allowed.")
    results = []
    for url in request.urls:
        platform = detect_platform(url.strip())
        results.append({
            "url": url,
            "platform": platform,
            "platform_name": PLATFORM_NAMES.get(platform, "Generic"),
            "status": "queued",
        })
    return JSONResponse(content={"results": results, "total": len(results)})
