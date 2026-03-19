"""
SoraFetch - FastAPI Backend (v2 - Share Link Support)
Automatically extracts video URL from Sora share links
Run: uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

import asyncio
import json
import re
import urllib.parse
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

app = FastAPI(title="SoraFetch API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CHUNK_SIZE = 1024 * 512

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
}

VIDEO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    "Referer": "https://sora.chatgpt.com/",
    "Origin": "https://sora.chatgpt.com",
}


class BatchRequest(BaseModel):
    urls: list[str]


def format_bytes(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def guess_filename(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        name = parts[-1].split("?")[0] if parts else "sora-video"
        if "." not in name:
            name += ".mp4"
        return name
    except Exception:
        return "sora-video.mp4"


def is_sora_share_link(url: str) -> bool:
    patterns = [
        r"sora\.chatgpt\.com/p/",
        r"sora\.openai\.com/p/",
        r"sora\.com/p/",
        r"sora\.chatgpt\.com/video/",
        r"sora\.com/video/",
        r"sora\.com/videos/",
    ]
    return any(re.search(p, url, re.I) for p in patterns)


def is_direct_video(url: str) -> bool:
    video_exts = (".mp4", ".webm", ".mov", ".mkv", ".m4v")
    video_hosts = ("cdn.openai.com", "videos.openai.com", "oaiusercontent.com",
                   "storage.googleapis.com", "cloudfront.net", "akamaized.net")
    url_lower = url.lower()
    has_ext = any(ext in url_lower for ext in video_exts)
    has_host = any(host in url_lower for host in video_hosts)
    return has_ext or has_host


async def extract_video_url_from_page(url: str, client: httpx.AsyncClient) -> str:
    try:
        resp = await client.get(url, headers=BROWSER_HEADERS, follow_redirects=True)
        html = resp.text
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Page fetch failed: {str(e)}")

    # Strategy 1: og:video
    og_video = re.search(
        r'<meta[^>]+property=["\']og:video["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    if not og_video:
        og_video = re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video["\']', html, re.I)
    if og_video:
        v = og_video.group(1)
        if v and ("mp4" in v or "video" in v):
            return v

    # Strategy 2: twitter stream
    tw = re.search(
        r'<meta[^>]+name=["\']twitter:player:stream["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    if tw:
        return tw.group(1)

    # Strategy 3: <video> or <source> tag
    vs = re.search(
        r'<(?:video|source)[^>]+src=["\']([^"\']+\.(?:mp4|webm|mov)[^"\']*)["\']', html, re.I)
    if vs:
        return vs.group(1)

    # Strategy 4: __NEXT_DATA__ JSON
    nd = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.S)
    if nd:
        try:
            data_str = json.dumps(json.loads(nd.group(1)))
            cdn = re.search(r'https://[^"\'\\]+\.(?:mp4|webm|mov)[^"\'\\]*', data_str)
            if cdn:
                return cdn.group(0)
        except Exception:
            pass

    # Strategy 5: Any CDN video URL
    cdn_url = re.search(
        r'https://(?:cdn\.openai\.com|videos\.openai\.com|oaiusercontent\.com|'
        r'[a-z0-9-]+\.cloudfront\.net|[a-z0-9-]+\.akamaized\.net)'
        r'[^"\'<>\s]+\.(?:mp4|webm|mov)', html, re.I)
    if cdn_url:
        return cdn_url.group(0)

    # Strategy 6: Any .mp4 in page
    any_mp4 = re.search(r'https://[^"\'<>\s]+\.mp4[^"\'<>\s]*', html)
    if any_mp4:
        return any_mp4.group(0)

    raise HTTPException(
        status_code=422,
        detail="Video URL extract nahi ho saki. Sora ne is video ko protect kiya hua hai. Dev Tools Network tab se direct .mp4 URL use karein."
    )


async def resolve_to_video_url(url: str, client: httpx.AsyncClient) -> str:
    url = url.strip()
    if is_direct_video(url):
        resp = await client.head(url, headers=VIDEO_HEADERS, follow_redirects=True)
        return str(resp.url)
    if is_sora_share_link(url):
        return await extract_video_url_from_page(url, client)
    try:
        resp = await client.head(url, headers=VIDEO_HEADERS, follow_redirects=True)
        final = str(resp.url)
        if is_direct_video(final):
            return final
    except Exception:
        pass
    return await extract_video_url_from_page(url, client)


@app.get("/")
async def root():
    return {"message": "SoraFetch API v2 is running", "version": "2.0.0"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/extract")
async def extract_url(url: str = Query(...)):
    """Just extract the video URL without downloading."""
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            video_url = await resolve_to_video_url(url, client)
            return {"original_url": url, "video_url": video_url, "status": "found"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/info")
async def get_video_info(url: str = Query(...)):
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            video_url = await resolve_to_video_url(url, client)
            resp = await client.head(video_url, headers=VIDEO_HEADERS)
            cl = resp.headers.get("content-length")
            size = int(cl) if cl else None
            return {
                "original_url": url,
                "video_url": video_url,
                "filename": guess_filename(video_url),
                "content_type": resp.headers.get("content-type", "video/mp4"),
                "size": size,
                "size_human": format_bytes(size) if size else None,
                "status": "ready",
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/download")
async def download_video(url: str = Query(...), filename: Optional[str] = Query(None)):
    try:
        url = url.strip()

        # Resolve video URL once for headers
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            video_url = await resolve_to_video_url(url, client)

        out_filename = filename or guess_filename(video_url)
        out_filename = re.sub(r'[^\w\-.]', '_', out_filename)
        if not out_filename.endswith(('.mp4', '.webm', '.mov', '.mkv')):
            out_filename += '.mp4'

        headers = {
            "Content-Disposition": f'attachment; filename="{out_filename}"',
            "Access-Control-Allow-Origin": "*",
        }

        # Get content-length for progress
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                head = await client.head(video_url, headers=VIDEO_HEADERS)
                cl = head.headers.get("content-length")
                if cl:
                    headers["Content-Length"] = cl
        except Exception:
            pass

        async def stream():
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=20.0, read=600.0, write=60.0, pool=10.0),
                follow_redirects=True,
            ) as client:
                async with client.stream("GET", video_url, headers=VIDEO_HEADERS) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes(chunk_size=CHUNK_SIZE):
                        yield chunk

        return StreamingResponse(stream(), media_type="video/mp4", headers=headers)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/batch/info")
async def batch_info(request: BatchRequest):
    if len(request.urls) > 50:
        raise HTTPException(status_code=400, detail="Max 50 URLs allowed.")
    results = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for url in request.urls:
            try:
                video_url = await resolve_to_video_url(url, client)
                resp = await client.head(video_url, headers=VIDEO_HEADERS)
                cl = resp.headers.get("content-length")
                size = int(cl) if cl else None
                results.append({
                    "url": url, "video_url": video_url,
                    "filename": guess_filename(video_url),
                    "size_human": format_bytes(size) if size else None,
                    "status": "ready",
                })
            except Exception as e:
                results.append({"url": url, "status": "error", "error": str(e)})
    return JSONResponse(content={"results": results})
