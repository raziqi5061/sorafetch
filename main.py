"""
SoraFetch - FastAPI Backend v3
Sora share link API extraction + direct video streaming
"""

import json
import re
import urllib.parse
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

app = FastAPI(title="SoraFetch API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CHUNK_SIZE = 1024 * 512

# Browser headers — mimic real Chrome as closely as possible
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    "Referer": "https://sora.chatgpt.com/",
    "Origin": "https://sora.chatgpt.com",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

VIDEO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Encoding": "identity",
    "Referer": "https://sora.chatgpt.com/",
}


class BatchRequest(BaseModel):
    urls: list[str]


# ─── Helpers ───────────────────────────────────────────────────────────

def format_bytes(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def guess_filename(url: str, share_id: str = None) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        name = parts[-1].split("?")[0] if parts else None
        if name and "." in name:
            return name
    except Exception:
        pass
    return f"sora-{share_id or 'video'}.mp4"


def extract_share_id(url: str) -> Optional[str]:
    """Extract share ID from Sora URL like /p/s_abc123"""
    match = re.search(r'/p/(s_[a-f0-9]+)', url, re.I)
    if match:
        return match.group(1)
    match = re.search(r'/videos?/(s_[a-f0-9]+)', url, re.I)
    if match:
        return match.group(1)
    return None


def is_direct_video(url: str) -> bool:
    url_lower = url.lower()
    return any(ext in url_lower for ext in (".mp4", ".webm", ".mov", ".m4v"))


# ─── Core: Sora API extraction ─────────────────────────────────────────

async def try_sora_api(share_id: str, client: httpx.AsyncClient) -> Optional[str]:
    """
    Try Sora's internal API endpoints to get the direct video URL.
    These are the endpoints that tools like fliflik use.
    """

    # Endpoint patterns Sora uses internally for share links
    api_endpoints = [
        f"https://sora.chatgpt.com/backend-api/video_generations/{share_id}",
        f"https://sora.chatgpt.com/api/video/{share_id}",
        f"https://sora.chatgpt.com/api/share/{share_id}",
        f"https://sora.chatgpt.com/api/videos/{share_id}",
        f"https://sora.chatgpt.com/backend-api/videos/{share_id}",
        f"https://sora.chatgpt.com/public-api/video/{share_id}",
        f"https://sora.chatgpt.com/v1/video/{share_id}",
    ]

    for endpoint in api_endpoints:
        try:
            resp = await client.get(endpoint, headers=API_HEADERS, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                # Look for video URL in common JSON field names
                video_url = (
                    data.get("video_url") or
                    data.get("url") or
                    data.get("source_url") or
                    data.get("asset_url") or
                    data.get("download_url") or
                    data.get("file_url") or
                    (data.get("video", {}) or {}).get("url") or
                    (data.get("result", {}) or {}).get("url") or
                    (data.get("data", {}) or {}).get("url")
                )
                if video_url and ("http" in video_url):
                    return video_url
                # Search recursively in JSON
                data_str = json.dumps(data)
                mp4_match = re.search(r'https://[^"\'\\]+\.mp4[^"\'\\]*', data_str)
                if mp4_match:
                    return mp4_match.group(0)
        except Exception:
            continue

    return None


async def try_page_scrape(url: str, client: httpx.AsyncClient) -> Optional[str]:
    """
    Fetch the share page HTML and extract video URL using multiple strategies.
    """
    try:
        resp = await client.get(url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=20)
        html = resp.text
    except Exception:
        return None

    # Strategy 1: og:video meta tag
    for pattern in [
        r'<meta[^>]+property=["\']og:video:url["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']og:video["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video["\']',
    ]:
        m = re.search(pattern, html, re.I)
        if m and ("mp4" in m.group(1) or "video" in m.group(1)):
            return m.group(1)

    # Strategy 2: twitter player stream
    m = re.search(r'<meta[^>]+name=["\']twitter:player:stream["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    if m:
        return m.group(1)

    # Strategy 3: <video src> or <source src>
    m = re.search(r'<(?:video|source)[^>]+src=["\']([^"\']+\.mp4[^"\']*)["\']', html, re.I)
    if m:
        return m.group(1)

    # Strategy 4: __NEXT_DATA__ JSON deep search
    m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.S)
    if m:
        try:
            data_str = json.dumps(json.loads(m.group(1)))
            # Look for CDN video URLs
            for cdn_pattern in [
                r'https://[^"\\]+oaiusercontent[^"\\]+\.mp4[^"\\]*',
                r'https://cdn\.openai\.com[^"\\]+\.mp4[^"\\]*',
                r'https://videos\.openai\.com[^"\\]+\.mp4[^"\\]*',
                r'https://[^"\\]+\.cloudfront\.net[^"\\]+\.mp4[^"\\]*',
                r'https://[^"\\]+\.mp4[^"\\]*',
            ]:
                cdn = re.search(cdn_pattern, data_str)
                if cdn:
                    return cdn.group(0).replace('\\u0026', '&')
        except Exception:
            pass

    # Strategy 5: Any CDN .mp4 URL in raw HTML
    for cdn_pattern in [
        r'https://[^"\'<>\s]+oaiusercontent[^"\'<>\s]+\.mp4[^"\'<>\s]*',
        r'https://cdn\.openai\.com[^"\'<>\s]+\.mp4[^"\'<>\s]*',
        r'https://videos\.openai\.com[^"\'<>\s]+\.mp4[^"\'<>\s]*',
        r'https://[^"\'<>\s]+\.mp4[^"\'<>\s]*',
    ]:
        m = re.search(cdn_pattern, html, re.I)
        if m:
            return m.group(0)

    return None


async def try_oembed(url: str, client: httpx.AsyncClient) -> Optional[str]:
    """Try oEmbed endpoint which some platforms expose for share links."""
    try:
        oembed_url = f"https://sora.chatgpt.com/oembed?url={urllib.parse.quote(url)}&format=json"
        resp = await client.get(oembed_url, headers=API_HEADERS, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            data_str = json.dumps(data)
            m = re.search(r'https://[^"\'\\]+\.mp4[^"\'\\]*', data_str)
            if m:
                return m.group(0)
    except Exception:
        pass
    return None


async def resolve_to_video_url(url: str, client: httpx.AsyncClient) -> str:
    """
    Master resolver: try all strategies in order, return first working video URL.
    """
    url = url.strip()

    # Already a direct .mp4? Just return it.
    if is_direct_video(url):
        try:
            resp = await client.head(url, headers=VIDEO_HEADERS, follow_redirects=True, timeout=10)
            return str(resp.url)
        except Exception:
            return url

    share_id = extract_share_id(url)

    # 1. Try Sora's internal API (fastest, most reliable)
    if share_id:
        api_url = await try_sora_api(share_id, client)
        if api_url:
            return api_url

    # 2. Try oEmbed
    oembed_url = await try_oembed(url, client)
    if oembed_url:
        return oembed_url

    # 3. Try page scraping
    page_url = await try_page_scrape(url, client)
    if page_url:
        return page_url

    # 4. All failed
    raise HTTPException(
        status_code=422,
        detail={
            "error": "video_not_found",
            "message": "Is share link se video URL nahi mili. Video private ho sakti hai ya Sora ne API update kar di ho. Dev Tools Network tab se .mp4 URL directly copy karein.",
            "share_id": share_id,
            "tried": ["sora_api", "oembed", "page_scrape"],
        }
    )


# ─── Routes ────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"message": "SoraFetch API v3 running", "version": "3.0.0"}


@app.get("/health")
async def health():
    return {"status": "ok", "version": "3.0.0"}


@app.get("/extract")
async def extract_url(url: str = Query(..., description="Sora share URL")):
    """Extract direct video URL from a Sora share link."""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            video_url = await resolve_to_video_url(url, client)
            share_id = extract_share_id(url)
            return {
                "status": "found",
                "original_url": url,
                "video_url": video_url,
                "filename": guess_filename(video_url, share_id),
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/info")
async def get_video_info(url: str = Query(...)):
    """Get metadata without downloading."""
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            video_url = await resolve_to_video_url(url, client)
            share_id = extract_share_id(url)
            try:
                resp = await client.head(video_url, headers=VIDEO_HEADERS, timeout=10)
                cl = resp.headers.get("content-length")
                size = int(cl) if cl else None
            except Exception:
                size = None
            return {
                "original_url": url,
                "video_url": video_url,
                "filename": guess_filename(video_url, share_id),
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
    """Resolve and stream video to browser."""
    try:
        url = url.strip()
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            video_url = await resolve_to_video_url(url, client)

        share_id = extract_share_id(url)
        out_filename = filename or guess_filename(video_url, share_id)
        out_filename = re.sub(r'[^\w\-.]', '_', out_filename)
        if not any(out_filename.endswith(e) for e in ('.mp4', '.webm', '.mov', '.mkv')):
            out_filename += '.mp4'

        resp_headers = {
            "Content-Disposition": f'attachment; filename="{out_filename}"',
            "Access-Control-Allow-Origin": "*",
        }

        # Get content-length for progress bar
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
                head = await client.head(video_url, headers=VIDEO_HEADERS)
                cl = head.headers.get("content-length")
                if cl:
                    resp_headers["Content-Length"] = cl
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

        return StreamingResponse(stream(), media_type="video/mp4", headers=resp_headers)

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
                share_id = extract_share_id(url)
                results.append({
                    "url": url, "video_url": video_url,
                    "filename": guess_filename(video_url, share_id),
                    "status": "ready",
                })
            except Exception as e:
                results.append({"url": url, "status": "error", "error": str(e)})
    return JSONResponse(content={"results": results})
