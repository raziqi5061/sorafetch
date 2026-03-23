"""
MultiVidFetch - FastAPI Backend v6
Multi-platform video downloader:
  Sora, TikTok, Facebook, Instagram, Twitter/X, Douyin, Bilibili
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
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from pydantic import BaseModel

app = FastAPI(title="MultiVidFetch API", version="6.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CHUNK_SIZE = 1024 * 512  # 512 KB

# ─── Platform Detection ───────────────────────────────────────────────

PLATFORM_PATTERNS = {
    "sora":      [r"sora\.chatgpt\.com", r"sora\.openai\.com", r"sora\.com/p/"],
    "tiktok":    [r"tiktok\.com", r"vm\.tiktok\.com", r"vt\.tiktok\.com"],
    "douyin":    [r"douyin\.com", r"iesdouyin\.com"],
    "facebook":  [r"facebook\.com", r"fb\.com", r"fb\.watch"],
    "instagram": [r"instagram\.com", r"instagr\.am"],
    "twitter":   [r"twitter\.com", r"x\.com", r"t\.co"],
    "bilibili":  [r"bilibili\.com", r"b23\.tv"],
}

PLATFORM_NAMES = {
    "sora": "Sora",
    "tiktok": "TikTok",
    "douyin": "Douyin",
    "facebook": "Facebook",
    "instagram": "Instagram",
    "twitter": "Twitter/X",
    "bilibili": "Bilibili",
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
    name = re.sub(r'[^\w\-.]', '_', name)
    name = re.sub(r'_+', '_', name).strip('_')
    if not name:
        name = "video"
    if not name.endswith(f".{ext}"):
        name = f"{name}.{ext}"
    return name[:100]


# ─── Sora Extractor (fliflik method) ─────────────────────────────────

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
        r'https://cdn\.openai\.com[^"\'<>\s\\]+\.mp4[^"\'<>\s\\]*',
        r'https://[^"\'<>\s\\]+\.mp4[^"\'<>\s\\]*',
    ]:
        m = re.search(pat, text)
        if m:
            return m.group(0).replace('\\u0026', '&').replace('\\/', '/')
    return None


async def resolve_sora(url: str) -> str:
    async with httpx.AsyncClient(follow_redirects=True, timeout=30,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}) as client:

        # Visit fliflik page for cookies
        try:
            await client.get("https://online.fliflik.com/sora-video-downloader/", timeout=10)
        except Exception:
            pass

        # POST to fliflik API
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
        "message": "Sora video URL extract nahi ho saki. Video private ho sakti hai.",
        "platform": "sora",
    })


# ─── yt-dlp Extractor (TikTok, FB, IG, Twitter, Douyin, Bilibili) ────

YDL_COMMON_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": False,
    "socket_timeout": 30,
}

PLATFORM_YDL_OPTS = {
    "tiktok": {
        **YDL_COMMON_OPTS,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            "Referer": "https://www.tiktok.com/",
        },
        # Remove TikTok watermark by preferring no-watermark sources
        "extractor_args": {"tiktok": {"api_hostname": "api22-normal-c-useast2a.tiktokv.com"}},
    },
    "douyin": {
        **YDL_COMMON_OPTS,
        "format": "bestvideo[ext=mp4]+bestaudio/best",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
            "Referer": "https://www.douyin.com/",
        },
    },
    "facebook": {
        **YDL_COMMON_OPTS,
        "format": "bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        },
    },
    "instagram": {
        **YDL_COMMON_OPTS,
        "format": "bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        },
    },
    "twitter": {
        **YDL_COMMON_OPTS,
        "format": "bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best",
    },
    "bilibili": {
        **YDL_COMMON_OPTS,
        "format": "bestvideo[ext=mp4]+bestaudio/best",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.bilibili.com/",
        },
    },
    "generic": {
        **YDL_COMMON_OPTS,
        "format": "bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best",
    },
}


def get_ydl_opts(platform: str) -> dict:
    return PLATFORM_YDL_OPTS.get(platform, PLATFORM_YDL_OPTS["generic"])


async def extract_info_ytdlp(url: str, platform: str) -> dict:
    """Extract video info using yt-dlp (no download)."""
    opts = {**get_ydl_opts(platform), "skip_download": True}

    def _extract():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    loop = asyncio.get_event_loop()
    try:
        info = await loop.run_in_executor(None, _extract)
        return info
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail={
            "error": "extract_failed",
            "message": str(e)[:200],
            "platform": platform,
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


async def download_ytdlp_to_file(url: str, platform: str, out_path: str) -> dict:
    """Download video using yt-dlp to a temp file, return info."""
    opts = {
        **get_ydl_opts(platform),
        "outtmpl": out_path,
        "merge_output_format": "mp4",
    }

    def _download():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return info

    loop = asyncio.get_event_loop()
    try:
        info = await loop.run_in_executor(None, _download)
        return info
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail={
            "error": "download_failed",
            "message": str(e)[:300],
            "platform": platform,
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ─── Routes ──────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "message": "MultiVidFetch API v6 running",
        "version": "6.0.0",
        "supported_platforms": list(PLATFORM_NAMES.values()),
    }


@app.get("/health")
async def health():
    return {"status": "ok", "version": "6.0.0"}


@app.get("/detect")
async def detect(url: str = Query(...)):
    """Detect platform from URL."""
    platform = detect_platform(url)
    return {
        "url": url,
        "platform": platform,
        "platform_name": PLATFORM_NAMES.get(platform, "Generic"),
    }


@app.get("/info")
async def get_info(url: str = Query(...)):
    """Get video metadata without downloading."""
    url = url.strip()
    platform = detect_platform(url)

    if platform == "sora":
        video_url = await resolve_sora(url)
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.head(video_url, headers=VIDEO_HEADERS)
                cl = resp.headers.get("content-length")
                size = int(cl) if cl else None
        except Exception:
            size = None
        share_id = extract_share_id(url)
        return {
            "platform": "sora",
            "platform_name": "Sora",
            "title": f"Sora Video {share_id or ''}",
            "url": url,
            "video_url": video_url,
            "size": size,
            "size_human": format_bytes(size) if size else None,
            "filename": f"sora-{share_id or 'video'}.mp4",
        }

    # yt-dlp platforms
    info = await extract_info_ytdlp(url, platform)
    title = info.get("title", "video")
    duration = info.get("duration")
    uploader = info.get("uploader") or info.get("channel") or ""
    filesize = info.get("filesize") or info.get("filesize_approx")

    return {
        "platform": platform,
        "platform_name": PLATFORM_NAMES.get(platform, "Generic"),
        "title": title,
        "uploader": uploader,
        "duration": duration,
        "url": url,
        "size": filesize,
        "size_human": format_bytes(filesize) if filesize else None,
        "filename": safe_filename(title),
        "thumbnail": info.get("thumbnail"),
    }


@app.get("/download")
async def download_video(
    url: str = Query(...),
    filename: Optional[str] = Query(None),
    quality: str = Query("best", description="best | hd | sd"),
):
    """Download and stream video to browser."""
    url = url.strip()
    platform = detect_platform(url)

    # ── Sora: stream directly from CDN ───────────────────────────────
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

    # ── yt-dlp platforms: download to temp, stream to client ─────────
    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, "video.%(ext)s")

    try:
        info = await download_ytdlp_to_file(url, platform, tmp_path)

        # Find the downloaded file
        title = info.get("title", "video")
        ext = info.get("ext", "mp4")
        out_filename = filename or safe_filename(title, ext)

        # yt-dlp writes actual ext into filename
        actual_file = None
        for f in Path(tmp_dir).iterdir():
            if f.is_file():
                actual_file = f
                break

        if not actual_file or not actual_file.exists():
            raise HTTPException(status_code=500, detail="Downloaded file not found on server.")

        file_size = actual_file.stat().st_size
        resp_headers = {
            "Content-Disposition": f'attachment; filename="{out_filename}"',
            "Content-Length": str(file_size),
            "Access-Control-Allow-Origin": "*",
        }

        # Stream file to client then delete
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
                    actual_file.unlink()
                    Path(tmp_dir).rmdir()
                except Exception:
                    pass

        return StreamingResponse(file_stream(), media_type="video/mp4", headers=resp_headers)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:300])


class BatchRequest(BaseModel):
    urls: list[str]


@app.post("/batch/info")
async def batch_info(request: BatchRequest):
    """Get info for multiple URLs."""
    if len(request.urls) > 50:
        raise HTTPException(status_code=400, detail="Max 50 URLs allowed.")

    results = []
    for url in request.urls:
        try:
            platform = detect_platform(url.strip())
            results.append({
                "url": url,
                "platform": platform,
                "platform_name": PLATFORM_NAMES.get(platform, "Generic"),
                "status": "queued",
            })
        except Exception as e:
            results.append({"url": url, "status": "error", "error": str(e)})

    return JSONResponse(content={"results": results, "total": len(results)})
