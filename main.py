"""
SoraFetch - FastAPI Backend
Video proxy downloader with watermark removal support
Run: uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

import asyncio
import re
import urllib.parse
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, HttpUrl

# ─────────────────────────────────────────────
#  App Setup
# ─────────────────────────────────────────────
app = FastAPI(
    title="SoraFetch API",
    description="Proxy downloader for Sora videos",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # In production, replace with your frontend domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────
#  Constants & Headers
# ─────────────────────────────────────────────
CHUNK_SIZE = 1024 * 512  # 512 KB chunks

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    "Referer": "https://sora.com/",
    "Origin": "https://sora.com",
    "Sec-Fetch-Dest": "video",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
}

# ─────────────────────────────────────────────
#  Models
# ─────────────────────────────────────────────
class VideoInfoResponse(BaseModel):
    url: str
    filename: str
    content_type: str
    size: Optional[int] = None
    size_human: Optional[str] = None
    status: str = "ready"


class BatchRequest(BaseModel):
    urls: list[str]


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────
def guess_filename(url: str) -> str:
    """Extract a clean filename from URL."""
    try:
        parsed = urllib.parse.urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        if path_parts:
            name = path_parts[-1]
            if "." not in name:
                name += ".mp4"
            # Remove query params from name
            name = name.split("?")[0]
            return name
    except Exception:
        pass
    return "sora-video.mp4"


def format_bytes(size: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def is_sora_url(url: str) -> bool:
    """Check if URL looks like a Sora video URL."""
    sora_patterns = [
        r"sora\.com",
        r"sora\.openai\.com",
        r"cdn\.openai\.com",
        r"openai\.com",
        r"videos\.openai\.com",
        r"oaiusercontent\.com",
        r"sora-cdn\.",
    ]
    return any(re.search(p, url, re.I) for p in sora_patterns)


async def resolve_direct_video_url(url: str, client: httpx.AsyncClient) -> str:
    """
    Follow redirects to get the actual video CDN URL.
    Sora links often redirect to CDN URLs.
    """
    try:
        resp = await client.head(url, headers=BROWSER_HEADERS, follow_redirects=True)
        return str(resp.url)
    except Exception:
        return url


# ─────────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────────
@app.get("/")
async def root():
    return {"message": "SoraFetch API is running ✅", "version": "1.0.0"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/info")
async def get_video_info(url: str = Query(..., description="Video URL to inspect")):
    """
    Fetch metadata about a video without downloading it.
    Returns filename, content-type, file size.
    """
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            # Resolve redirects first
            real_url = await resolve_direct_video_url(url, client)

            resp = await client.head(real_url, headers=BROWSER_HEADERS)
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "video/mp4")
            content_length = resp.headers.get("content-length")
            size = int(content_length) if content_length else None

            return VideoInfoResponse(
                url=real_url,
                filename=guess_filename(real_url),
                content_type=content_type,
                size=size,
                size_human=format_bytes(size) if size else None,
                status="ready",
            )

    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"Remote server error: {e.response.status_code}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach URL: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/download")
async def download_video(
    url: str = Query(..., description="Direct video URL or Sora page URL"),
    filename: Optional[str] = Query(None, description="Custom filename for download"),
):
    """
    Proxy-stream a video file to the client browser.
    Handles CORS bypass, redirect following, and chunked streaming.
    """
    try:
        # Clean URL
        url = url.strip()

        async def stream_video():
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=10.0),
                follow_redirects=True,
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            ) as client:

                # Resolve to direct CDN URL
                real_url = await resolve_direct_video_url(url, client)

                async with client.stream("GET", real_url, headers=BROWSER_HEADERS) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes(chunk_size=CHUNK_SIZE):
                        yield chunk

        # Determine output filename
        out_filename = filename or guess_filename(url)
        # Sanitize filename
        out_filename = re.sub(r'[^\w\-.]', '_', out_filename)
        if not out_filename.endswith(('.mp4', '.webm', '.mov', '.mkv')):
            out_filename += '.mp4'

        headers = {
            "Content-Disposition": f'attachment; filename="{out_filename}"',
            "Access-Control-Allow-Origin": "*",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-cache",
        }

        # Try to get content-length for progress tracking
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                real_url = await resolve_direct_video_url(url, client)
                head = await client.head(real_url, headers=BROWSER_HEADERS)
                cl = head.headers.get("content-length")
                if cl:
                    headers["Content-Length"] = cl
        except Exception:
            pass

        return StreamingResponse(
            stream_video(),
            media_type="video/mp4",
            headers=headers,
        )

    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"Remote error: {e.response.status_code}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Network error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/batch/info")
async def batch_video_info(request: BatchRequest):
    """
    Get info for multiple videos at once.
    Returns list of metadata objects.
    """
    if len(request.urls) > 50:
        raise HTTPException(status_code=400, detail="Max 50 URLs per batch request.")

    results = []

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        tasks = [
            resolve_direct_video_url(url, client)
            for url in request.urls
        ]
        real_urls = await asyncio.gather(*tasks, return_exceptions=True)

        for url, real_url in zip(request.urls, real_urls):
            if isinstance(real_url, Exception):
                results.append({
                    "url": url,
                    "status": "error",
                    "error": str(real_url),
                })
                continue

            try:
                resp = await client.head(str(real_url), headers=BROWSER_HEADERS)
                cl = resp.headers.get("content-length")
                size = int(cl) if cl else None
                results.append({
                    "url": url,
                    "real_url": str(real_url),
                    "filename": guess_filename(str(real_url)),
                    "content_type": resp.headers.get("content-type", "video/mp4"),
                    "size": size,
                    "size_human": format_bytes(size) if size else None,
                    "status": "ready" if resp.status_code < 400 else "error",
                })
            except Exception as e:
                results.append({
                    "url": url,
                    "status": "error",
                    "error": str(e),
                })

    return JSONResponse(content={"results": results})
