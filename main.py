"""
SoraFetch - FastAPI Backend v5
Fliflik session-based extraction (visits page first to get cookies, then calls API)
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

app = FastAPI(title="SoraFetch API", version="5.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CHUNK_SIZE = 1024 * 512

VIDEO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Encoding": "identity",
    "Referer": "https://sora.chatgpt.com/",
}


class BatchRequest(BaseModel):
    urls: list[str]


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
    m = re.search(r'/p/(s_[a-f0-9]+)', url, re.I)
    if m:
        return m.group(1)
    m = re.search(r'/videos?/(s_[a-f0-9]+)', url, re.I)
    if m:
        return m.group(1)
    return None


def is_direct_video(url: str) -> bool:
    return any(ext in url.lower() for ext in (".mp4", ".webm", ".mov", ".m4v"))


def extract_video_from_response(text: str) -> Optional[str]:
    """Extract video URL from any response text/JSON."""
    # Try JSON parse first
    try:
        data = json.loads(text)
        # Direct fields
        for key in ("url", "video_url", "download_url", "link", "source",
                    "videoUrl", "downloadUrl", "file_url", "stream_url"):
            val = data.get(key)
            if val and isinstance(val, str) and val.startswith("http"):
                return val
        # Nested
        for key in ("data", "result", "video", "response"):
            sub = data.get(key)
            if isinstance(sub, dict):
                for k2 in ("url", "video_url", "download_url", "link"):
                    val = sub.get(k2)
                    if val and isinstance(val, str) and val.startswith("http"):
                        return val
        # Search entire JSON string
        data_str = json.dumps(data)
        for pattern in [
            r'https://videos\.openai\.com[^"\'\\]+',
            r'https://[^"\'\\]*oaiusercontent[^"\'\\]+',
            r'https://cdn\.openai\.com[^"\'\\]+\.mp4[^"\'\\]*',
            r'https://[^"\'\\]+\.mp4[^"\'\\]*',
        ]:
            m = re.search(pattern, data_str)
            if m:
                return m.group(0).replace('\\u0026', '&').replace('\\/','/')
    except Exception:
        pass

    # Raw text search
    for pattern in [
        r'https://videos\.openai\.com[^"\'<>\s\\]+',
        r'https://[^"\'<>\s\\]*oaiusercontent[^"\'<>\s\\]+',
        r'https://cdn\.openai\.com[^"\'<>\s\\]+\.mp4[^"\'<>\s\\]*',
        r'https://[^"\'<>\s\\]+\.mp4[^"\'<>\s\\]*',
    ]:
        m = re.search(pattern, text)
        if m:
            return m.group(0).replace('\\u0026', '&').replace('\\/', '/')

    return None


async def try_fliflik(sora_url: str) -> Optional[str]:
    """
    Full fliflik session flow:
    1. Visit fliflik page → get cookies + any CSRF token
    2. POST to /get-video-link with session cookies
    """
    base_url = "https://online.fliflik.com"
    page_url = f"{base_url}/sora-video-downloader/"
    api_url  = f"{base_url}/get-video-link"

    # Use a persistent cookie jar so session is maintained
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
    ) as client:

        # Step 1 — visit page to get session cookies
        try:
            page_resp = await client.get(page_url, headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
            })
        except Exception:
            pass  # Even if page fails, try API anyway

        # Extract CSRF token from page if present
        csrf_token = None
        try:
            html = page_resp.text
            for pattern in [
                r'csrf[_-]token["\s:=]+["\']([^"\']+)["\']',
                r'_token["\s:=]+["\']([^"\']+)["\']',
                r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']',
            ]:
                m = re.search(pattern, html, re.I)
                if m:
                    csrf_token = m.group(1)
                    break
        except Exception:
            pass

        # Step 2 — call the API with session cookies
        post_headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": base_url,
            "Referer": page_url,
            "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

        if csrf_token:
            post_headers["X-CSRF-TOKEN"] = csrf_token
            post_headers["X-Csrf-Token"]  = csrf_token

        payload = {"url": sora_url}

        try:
            api_resp = await client.post(api_url, json=payload, headers=post_headers)
            if api_resp.status_code == 200:
                result = extract_video_from_response(api_resp.text)
                if result:
                    return result
        except Exception:
            pass

        # Step 3 — try form-encoded as fallback
        try:
            form_headers = {**post_headers, "Content-Type": "application/x-www-form-urlencoded"}
            api_resp2 = await client.post(api_url, data={"url": sora_url}, headers=form_headers)
            if api_resp2.status_code == 200:
                result = extract_video_from_response(api_resp2.text)
                if result:
                    return result
        except Exception:
            pass

    return None


async def try_page_scrape(url: str) -> Optional[str]:
    """Fallback: scrape the Sora share page directly."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "identity",
            })
            html = resp.text
    except Exception:
        return None

    # og:video
    for pat in [
        r'<meta[^>]+property=["\']og:video:url["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']og:video["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video["\']',
    ]:
        m = re.search(pat, html, re.I)
        if m and ("mp4" in m.group(1) or "video" in m.group(1)):
            return m.group(1)

    # __NEXT_DATA__
    nd = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.S)
    if nd:
        result = extract_video_from_response(nd.group(1))
        if result:
            return result

    # Raw HTML
    for pat in [
        r'https://videos\.openai\.com[^"\'<>\s]+',
        r'https://[^"\'<>\s]*oaiusercontent[^"\'<>\s]+',
        r'https://[^"\'<>\s]+\.mp4[^"\'<>\s]*',
    ]:
        m = re.search(pat, html, re.I)
        if m:
            return m.group(0)

    return None


async def resolve_to_video_url(url: str) -> str:
    url = url.strip()

    # Already a direct video
    if is_direct_video(url):
        async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
            resp = await client.head(url, headers=VIDEO_HEADERS)
            return str(resp.url)

    # 1. Fliflik (with session)
    result = await try_fliflik(url)
    if result:
        return result

    # 2. Page scrape
    result = await try_page_scrape(url)
    if result:
        return result

    share_id = extract_share_id(url)
    raise HTTPException(
        status_code=422,
        detail={
            "error": "video_not_found",
            "message": "Video URL extract nahi ho saki.",
            "share_id": share_id,
        }
    )


# ─── Routes ──────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"message": "SoraFetch API v5 running", "version": "5.0.0"}


@app.get("/health")
async def health():
    return {"status": "ok", "version": "5.0.0"}


@app.get("/extract")
async def extract_url(url: str = Query(...)):
    try:
        video_url = await resolve_to_video_url(url)
        return {
            "status": "found",
            "original_url": url,
            "video_url": video_url,
            "filename": guess_filename(video_url, extract_share_id(url)),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/info")
async def get_video_info(url: str = Query(...)):
    try:
        video_url = await resolve_to_video_url(url)
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.head(video_url, headers=VIDEO_HEADERS)
                cl = resp.headers.get("content-length")
                size = int(cl) if cl else None
        except Exception:
            size = None
        return {
            "original_url": url,
            "video_url": video_url,
            "filename": guess_filename(video_url, extract_share_id(url)),
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
        video_url = await resolve_to_video_url(url)

        share_id = extract_share_id(url)
        out_filename = filename or guess_filename(video_url, share_id)
        out_filename = re.sub(r'[^\w\-.]', '_', out_filename)
        if not any(out_filename.endswith(e) for e in ('.mp4', '.webm', '.mov', '.mkv')):
            out_filename += '.mp4'

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
    for url in request.urls:
        try:
            video_url = await resolve_to_video_url(url)
            results.append({
                "url": url, "video_url": video_url,
                "filename": guess_filename(video_url, extract_share_id(url)),
                "status": "ready",
            })
        except Exception as e:
            results.append({"url": url, "status": "error", "error": str(e)})
    return JSONResponse(content={"results": results})
