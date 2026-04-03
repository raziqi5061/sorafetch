"""
ContentCreator Pro Studio - FastAPI Backend v10
FIXED: All platform downloads + RedNote added
Platforms: YouTube, TikTok, Instagram, Facebook, Twitter/X,
           Bilibili, Douyin, Sora, RedNote (Xiaohongshu)
"""

import asyncio
import csv
import io
import json
import os
import re
import shutil
import tempfile
import urllib.parse
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional, List

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Query, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

app = FastAPI(title="ContentCreator Pro Studio", version="10.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

CHUNK_SIZE = 512 * 1024  # 512 KB

# ─── Platform detection ───────────────────────────────────────────────

PLATFORM_PATTERNS = {
    "sora":      [r"sora\.chatgpt\.com", r"sora\.openai\.com"],
    "youtube":   [r"youtube\.com", r"youtu\.be"],
    "tiktok":    [r"tiktok\.com", r"vm\.tiktok\.com", r"vt\.tiktok\.com"],
    "douyin":    [r"douyin\.com", r"iesdouyin\.com"],
    "facebook":  [r"facebook\.com", r"fb\.com", r"fb\.watch"],
    "instagram": [r"instagram\.com", r"instagr\.am"],
    "twitter":   [r"twitter\.com", r"x\.com"],
    "bilibili":  [r"bilibili\.com", r"b23\.tv"],
    "rednote":   [r"xiaohongshu\.com", r"xhslink\.com", r"xhs\.link"],
}

PLATFORM_NAMES = {
    "sora": "Sora", "youtube": "YouTube", "tiktok": "TikTok",
    "douyin": "Douyin", "facebook": "Facebook", "instagram": "Instagram",
    "twitter": "Twitter/X", "bilibili": "Bilibili", "rednote": "RedNote",
    "generic": "Video",
}


def detect_platform(url: str) -> str:
    url_lower = url.lower()
    for platform, patterns in PLATFORM_PATTERNS.items():
        if any(re.search(p, url_lower) for p in patterns):
            return platform
    return "generic"


def format_bytes(size) -> str:
    if not size:
        return "—"
    size = int(size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def format_duration(seconds) -> str:
    if not seconds:
        return "00:00"
    s = int(seconds)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def is_ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def smart_filename(title: str, platform: str, ext: str = "mp4") -> str:
    date = datetime.now().strftime("%Y%m%d")
    clean = re.sub(r'[^\w\s\-]', '', str(title or "video"))
    clean = re.sub(r'\s+', '_', clean.strip()).strip('_')[:40]
    plat = platform.upper()[:8]
    name = f"{plat}_{date}_{clean}"
    if not name.endswith(f".{ext}"):
        name += f".{ext}"
    return name[:100]


def find_file(directory: str) -> Optional[Path]:
    files = sorted(
        [f for f in Path(directory).iterdir() if f.is_file() and not f.name.startswith('.')],
        key=lambda f: f.stat().st_size, reverse=True,
    )
    return files[0] if files else None


# ─── yt-dlp format strings ───────────────────────────────────────────

def best_format(platform: str, quality: str = "best") -> str:
    """Return yt-dlp format string. Always has multiple fallbacks."""
    ffmpeg = is_ffmpeg_available()

    # Without ffmpeg — only pre-merged formats
    if not ffmpeg:
        return "best[ext=mp4]/best[ext=webm]/best"

    if platform == "youtube":
        q = {
            "4k":   "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=2160]+bestaudio/best[height<=2160]/best",
            "hd":   "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
            "sd":   "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]/best",
            "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best",
        }
        return q.get(quality, q["best"])

    if platform in ("tiktok", "douyin"):
        # TikTok: prefer mp4 with no watermark via API hostname
        return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"

    if platform == "rednote":
        return "bestvideo+bestaudio/best[ext=mp4]/best"

    if platform == "bilibili":
        return "bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best"

    # Default for FB, IG, Twitter, generic
    q_map = {
        "4k":   "bestvideo[height<=2160]+bestaudio/best[height<=2160]/best",
        "hd":   "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "sd":   "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
        "best": "bestvideo+bestaudio/best[ext=mp4]/best",
    }
    return q_map.get(quality, q_map["best"])


# ─── Platform-specific yt-dlp options ────────────────────────────────

PLATFORM_EXTRAS = {
    "youtube": {
        "extractor_args": {
            "youtube": {"player_client": ["android", "tv_embedded"], "player_skip": ["webpage"]}
        },
        "http_headers": {
            "User-Agent": "com.google.android.youtube/17.36.4 (Linux; U; Android 12; GB) gzip",
        },
    },
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
    "rednote": {
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            "Referer": "https://www.xiaohongshu.com/",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    },
}


def make_ydl_opts(platform: str, out_path: str, quality: str = "best",
                  subtitles: bool = False, thumbnail: bool = False) -> dict:
    opts = {
        "format": best_format(platform, quality),
        "outtmpl": out_path,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "noplaylist": True,
        "ignoreerrors": False,
    }

    # Only set merge_output_format if ffmpeg available
    if is_ffmpeg_available():
        opts["merge_output_format"] = "mp4"

    if subtitles:
        opts.update({
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitlesformat": "srt",
            "subtitleslangs": ["en", "auto"],
        })
    if thumbnail:
        opts["writethumbnail"] = True

    # Merge platform-specific options
    extras = PLATFORM_EXTRAS.get(platform, {})
    for k, v in extras.items():
        if isinstance(v, dict) and isinstance(opts.get(k), dict):
            opts[k] = {**opts.get(k, {}), **v}
        else:
            opts[k] = v

    return opts


# ─── yt-dlp runner ────────────────────────────────────────────────────

async def run_ydl(url: str, opts: dict) -> Optional[dict]:
    """Run yt-dlp with fallback chain for YouTube bot detection."""

    def _run(o: dict):
        with yt_dlp.YoutubeDL(o) as ydl:
            return ydl.extract_info(url, download=True)

    loop = asyncio.get_event_loop()
    is_yt = bool(re.search(r'youtube\.com|youtu\.be', url, re.I))

    # First attempt
    try:
        return await loop.run_in_executor(None, lambda: _run(opts))
    except yt_dlp.utils.DownloadError as e:
        err = str(e).lower()

        # ffmpeg missing → retry with pre-merged format only
        if "ffmpeg" in err or "merger" in err:
            fb = dict(opts)
            fb["format"] = "best[ext=mp4]/best[ext=webm]/best"
            fb.pop("merge_output_format", None)
            try:
                return await loop.run_in_executor(None, lambda: _run(fb))
            except Exception as e2:
                raise HTTPException(status_code=422, detail={
                    "error": "ffmpeg_missing",
                    "message": f"ffmpeg nahi mila. Dockerfile check karein. Details: {str(e2)[:200]}",
                })

        # YouTube specific fallbacks
        yt_keywords = ["sign in", "bot", "confirm", "not available",
                       "requested format", "format is not available", "precondition"]
        if is_yt and any(k in err for k in yt_keywords):
            fallback_combos = [
                (["android"],                "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"),
                (["tv_embedded"],            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"),
                (["ios"],                    "bestvideo+bestaudio/best[ext=mp4]/best"),
                (["android", "tv_embedded"], "bestvideo+bestaudio/best"),
                (["mweb"],                   "best[ext=mp4]/best"),
                (["android"],                "best"),
                (["tv_embedded"],            "best"),
            ]
            for clients, fmt in fallback_combos:
                try:
                    fb = dict(opts)
                    fb["format"] = fmt
                    fb["extractor_args"] = {
                        "youtube": {"player_client": clients, "player_skip": ["webpage"]}
                    }
                    fb["http_headers"] = {
                        "User-Agent": "com.google.android.youtube/17.36.4 (Linux; U; Android 12; GB) gzip"
                    }
                    return await loop.run_in_executor(None, lambda o=fb: _run(o))
                except Exception:
                    continue
            raise HTTPException(status_code=422, detail={
                "error": "youtube_blocked",
                "message": "YouTube video download nahi ho saki. Age-restricted, private, ya region-locked ho sakti hai.",
            })

        raise HTTPException(status_code=422, detail={
            "error": "download_failed",
            "message": str(e)[:400],
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail={
            "error": "server_error",
            "message": str(e)[:300],
        })


async def info_only(url: str, platform: str) -> dict:
    """Extract metadata without downloading."""
    opts = {
        **make_ydl_opts(platform, "/dev/null"),
        "skip_download": True,
        "quiet": True,
    }
    opts.pop("merge_output_format", None)

    def _run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _run)
    except Exception as e:
        raise HTTPException(status_code=422, detail={
            "error": "info_failed", "message": str(e)[:300]
        })


# ─── Sora extractor ───────────────────────────────────────────────────

FLIFLIK_H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://online.fliflik.com",
    "Referer": "https://online.fliflik.com/sora-video-downloader/",
}
VIDEO_H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Accept-Encoding": "identity",
    "Referer": "https://sora.chatgpt.com/",
}


def _find_url_in_text(text: str) -> Optional[str]:
    try:
        data = json.loads(text)
        for k in ("url", "video_url", "download_url", "link", "source", "videoUrl"):
            v = data.get(k)
            if v and isinstance(v, str) and v.startswith("http"):
                return v
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
        headers={"User-Agent": "Mozilla/5.0"}
    ) as client:
        try:
            await client.get("https://online.fliflik.com/sora-video-downloader/", timeout=10)
        except Exception:
            pass
        try:
            resp = await client.post(
                "https://online.fliflik.com/get-video-link",
                json={"url": url}, headers=FLIFLIK_H, timeout=20,
            )
            if resp.status_code == 200:
                v = _find_url_in_text(resp.text)
                if v:
                    return v
        except Exception:
            pass
    raise HTTPException(status_code=422, detail={
        "error": "sora_failed",
        "message": "Sora video URL extract nahi ho saki."
    })


# ─── Enhancement presets ──────────────────────────────────────────────

ENHANCE_PRESETS = {
    "light":     {"label": "Light",    "vf": "unsharp=5:5:0.8:3:3:0.4,eq=contrast=1.05:saturation=1.1",                                                              "crf": "20", "preset": "fast"},
    "standard":  {"label": "Standard", "vf": "hqdn3d=2:1.5:6:4.5,unsharp=5:5:1.2:3:3:0.6,scale=1920:1080:flags=lanczos,eq=contrast=1.08:saturation=1.15:gamma=0.95","crf": "18", "preset": "medium"},
    "strong":    {"label": "Strong",   "vf": "hqdn3d=3:2.5:8:6,unsharp=7:7:1.5:5:5:0.8,scale=iw*2:ih*2:flags=lanczos,eq=contrast=1.12:saturation=1.2:gamma=0.92",   "crf": "16", "preset": "slow"},
    "cinematic": {"label": "Cinematic","vf": "hqdn3d=2:1.5:5:4,unsharp=5:5:1.0:3:3:0.5,scale=1920:1080:flags=lanczos,eq=contrast=1.1:saturation=0.95:gamma=0.9,vignette=PI/5","crf":"17","preset":"medium"},
}


# ─── Profile fetcher ─────────────────────────────────────────────────

PROFILE_URL_MAP = {
    "youtube":   lambda u: f"https://www.youtube.com/@{u}/videos",
    "tiktok":    lambda u: f"https://www.tiktok.com/@{u}",
    "instagram": lambda u: f"https://www.instagram.com/{u}/",
    "twitter":   lambda u: f"https://twitter.com/{u}",
    "facebook":  lambda u: f"https://www.facebook.com/{u}/videos",
    "bilibili":  lambda u: f"https://space.bilibili.com/{u}/video" if u.isdigit() else f"https://www.bilibili.com/@{u}",
    "rednote":   lambda u: f"https://www.xiaohongshu.com/user/profile/{u}",
}

PROFILE_YDL_EXTRAS = {
    **PLATFORM_EXTRAS,
    "rednote": {
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
            "Referer": "https://www.xiaohongshu.com/",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    },
}


def parse_identifier(raw: str, platform: str) -> str:
    """Extract username/ID from URL or bare input."""
    raw = raw.strip().lstrip('@')
    patterns = {
        "youtube":   r'youtube\.com/@?([^/?&\s]+)',
        "tiktok":    r'tiktok\.com/@([^/?&\s]+)',
        "instagram": r'instagram\.com/([^/?&\s]+)/?',
        "twitter":   r'(?:twitter|x)\.com/([^/?&\s]+)',
        "facebook":  r'facebook\.com/([^/?&\s]+)',
        "bilibili":  r'space\.bilibili\.com/(\d+)',
        "rednote":   r'xiaohongshu\.com/user/profile/([^/?&\s]+)',
    }
    pat = patterns.get(platform)
    if pat:
        m = re.search(pat, raw, re.I)
        if m:
            return m.group(1)
    # Strip trailing slashes and query strings
    return raw.split('/')[0].split('?')[0]


def make_video_dict(entry: dict, platform: str, identifier: str) -> Optional[dict]:
    """Convert yt-dlp flat entry to our dict."""
    if not entry:
        return None
    vid_url = (entry.get("webpage_url") or entry.get("url") or "")
    if not vid_url.startswith("http"):
        vid_id = entry.get("id", "")
        if vid_id:
            fallback = {
                "youtube":   f"https://www.youtube.com/watch?v={vid_id}",
                "tiktok":    f"https://www.tiktok.com/@{identifier}/video/{vid_id}",
                "instagram": f"https://www.instagram.com/p/{vid_id}/",
                "twitter":   f"https://twitter.com/i/web/status/{vid_id}",
                "bilibili":  f"https://www.bilibili.com/video/{vid_id}",
                "facebook":  f"https://www.facebook.com/video/{vid_id}",
                "rednote":   f"https://www.xiaohongshu.com/explore/{vid_id}",
            }
            vid_url = fallback.get(platform, "")
    if not vid_url:
        return None
    title = (entry.get("title") or "Video")[:100]
    if title.lower() in ("[private]", "[deleted]", "private video", "deleted video"):
        return None
    thumbs = entry.get("thumbnails") or []
    thumb = entry.get("thumbnail") or (thumbs[-1].get("url") if thumbs else None)
    return {
        "url": vid_url,
        "title": title,
        "duration": entry.get("duration"),
        "duration_fmt": format_duration(entry.get("duration") or 0),
        "thumbnail": thumb,
        "view_count": entry.get("view_count"),
        "like_count": entry.get("like_count"),
        "upload_date": entry.get("upload_date", ""),
        "id": entry.get("id", ""),
        "platform": platform,
    }


async def fetch_profile_videos(platform: str, identifier: str, max_videos: int) -> List[dict]:
    identifier = identifier.strip().lstrip('@')
    builder = PROFILE_URL_MAP.get(platform)
    profile_url = builder(identifier) if builder else f"https://www.{platform}.com/@{identifier}"

    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "playlistend": max_videos,
        "ignoreerrors": True,
    }
    extras = PROFILE_YDL_EXTRAS.get(platform, {})
    opts = {**base_opts, **extras}

    def _extract(url: str) -> List[dict]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return []
        entries = info.get("entries") or []
        flat = []
        for e in entries:
            if not e:
                continue
            if e.get("_type") == "playlist" and e.get("entries"):
                flat.extend(e["entries"])
            else:
                flat.append(e)
        result = []
        for entry in flat[:max_videos]:
            v = make_video_dict(entry, platform, identifier)
            if v:
                result.append(v)
        return result

    loop = asyncio.get_event_loop()

    # Try primary URL
    try:
        videos = await loop.run_in_executor(None, lambda: _extract(profile_url))
        if videos:
            return videos
    except Exception:
        pass

    # YouTube fallbacks
    if platform == "youtube":
        for fb_url in [
            f"https://www.youtube.com/c/{identifier}/videos",
            f"https://www.youtube.com/user/{identifier}/videos",
            f"https://www.youtube.com/@{identifier}",
        ]:
            if fb_url == profile_url:
                continue
            try:
                videos = await loop.run_in_executor(None, lambda u=fb_url: _extract(u))
                if videos:
                    return videos
            except Exception:
                continue

    # TikTok desktop fallback
    if platform == "tiktok":
        desktop_opts = {
            **base_opts,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
            },
        }
        try:
            videos = await loop.run_in_executor(
                None,
                lambda: _extract.__func__(None, f"https://www.tiktok.com/@{identifier}")
                if False else yt_dlp.YoutubeDL(desktop_opts).extract_info(
                    f"https://www.tiktok.com/@{identifier}", download=False
                )
            )
        except Exception:
            pass

    raise HTTPException(status_code=422, detail={
        "error": "no_videos_found",
        "message": f"@{identifier} se videos nahi mile. Account public hai? Username sahi hai? Platform: {PLATFORM_NAMES.get(platform)}",
        "platform": platform,
        "profile_url": profile_url,
    })


# ═══════════════════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    return {
        "name": "ContentCreator Pro Studio",
        "version": "10.0.0",
        "ffmpeg": is_ffmpeg_available(),
        "platforms": list(PLATFORM_NAMES.values()),
    }


@app.get("/health")
async def health():
    return {"status": "ok", "version": "10.0.0", "ffmpeg": is_ffmpeg_available()}


@app.get("/detect")
async def detect_route(url: str = Query(...)):
    p = detect_platform(url)
    return {"platform": p, "platform_name": PLATFORM_NAMES.get(p, "Generic")}


# ── Download single video ─────────────────────────────────────────────

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

    # Sora — stream directly from CDN
    if platform == "sora":
        video_url = await resolve_sora(url)
        sid = re.search(r'/p/(s_[a-f0-9]+)', url, re.I)
        out_fn = filename or smart_filename(f"sora_{sid.group(1) if sid else 'video'}", "sora")
        h = {"Content-Disposition": f'attachment; filename="{out_fn}"', "Access-Control-Allow-Origin": "*"}
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True) as c:
                head = await c.head(video_url, headers=VIDEO_H)
                if head.headers.get("content-length"):
                    h["Content-Length"] = head.headers["content-length"]
        except Exception:
            pass

        async def _sora():
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=20, read=600, write=60, pool=10),
                follow_redirects=True
            ) as c:
                async with c.stream("GET", video_url, headers=VIDEO_H) as r:
                    r.raise_for_status()
                    async for chunk in r.aiter_bytes(CHUNK_SIZE):
                        yield chunk

        return StreamingResponse(_sora(), media_type="video/mp4", headers=h)

    # All other platforms — yt-dlp
    tmp = tempfile.mkdtemp()
    try:
        out_tmpl = os.path.join(tmp, "video.%(ext)s")
        opts = make_ydl_opts(platform, out_tmpl, quality=quality,
                             subtitles=subtitles, thumbnail=thumbnail)
        info = await run_ydl(url, opts)

        actual = find_file(tmp)
        if not actual:
            raise HTTPException(status_code=500, detail="File not found after download.")

        title = (info or {}).get("title", "video")
        ext = actual.suffix.lstrip('.') or "mp4"
        out_fn = filename or smart_filename(title, platform, ext)
        fsize = actual.stat().st_size

        async def _stream():
            try:
                with open(actual, "rb") as f:
                    while True:
                        chunk = f.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        yield chunk
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        return StreamingResponse(_stream(), media_type="video/mp4", headers={
            "Content-Disposition": f'attachment; filename="{out_fn}"',
            "Content-Length": str(fsize),
            "Access-Control-Allow-Origin": "*",
            "X-Video-Title": urllib.parse.quote(title[:100]),
            "X-Platform": platform,
        })

    except HTTPException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e)[:300])


# ── Metadata ──────────────────────────────────────────────────────────

@app.get("/metadata")
async def get_metadata(url: str = Query(...)):
    url = url.strip()
    platform = detect_platform(url)
    if platform == "sora":
        video_url = await resolve_sora(url)
        sid = re.search(r'/p/(s_[a-f0-9]+)', url, re.I)
        return {"platform": "sora", "platform_name": "Sora",
                "title": f"Sora Video {sid.group(1) if sid else ''}",
                "description": "", "hashtags": [], "duration": None,
                "thumbnail": None, "uploader": "OpenAI/Sora",
                "video_url": video_url, "available_qualities": ["best"]}
    inf = await info_only(url, platform)
    desc = inf.get("description") or ""
    tags = inf.get("tags") or []
    hashtags = list(set(tags[:20] + re.findall(r'#\w+', desc)[:20]))
    fmts = inf.get("formats") or []
    heights = sorted({f.get("height") for f in fmts
                      if f.get("height") and f.get("vcodec") != "none"}, reverse=True)
    qualities = []
    if any(h >= 2160 for h in heights): qualities.append("4k")
    if any(h >= 1080 for h in heights): qualities.append("hd")
    if any(h >= 480 for h in heights):  qualities.append("sd")
    if not qualities: qualities = ["best"]
    return {
        "platform": platform,
        "platform_name": PLATFORM_NAMES.get(platform, "Generic"),
        "title": inf.get("title", ""),
        "description": desc[:500] + ("..." if len(desc) > 500 else ""),
        "hashtags": hashtags[:30],
        "duration": inf.get("duration"),
        "duration_fmt": format_duration(inf.get("duration") or 0),
        "thumbnail": inf.get("thumbnail"),
        "uploader": inf.get("uploader") or inf.get("channel") or "",
        "view_count": inf.get("view_count"),
        "like_count": inf.get("like_count"),
        "upload_date": inf.get("upload_date"),
        "available_qualities": qualities,
    }


# ── Profile fetch ─────────────────────────────────────────────────────

@app.get("/profile")
async def profile_fetch(
    url: str = Query(...),
    max_videos: int = Query(50, ge=1, le=200),
):
    url = url.strip()
    platform = detect_platform(url)
    if platform in ("sora", "generic"):
        raise HTTPException(status_code=422, detail={
            "error": "unsupported",
            "message": "Platform URL paste karein: youtube.com/@channel, tiktok.com/@user, instagram.com/user, xiaohongshu.com/user/profile/ID, etc.",
        })
    identifier = parse_identifier(url, platform)
    if not identifier:
        raise HTTPException(status_code=400, detail="Valid username ya profile URL chahiye.")
    videos = await fetch_profile_videos(platform, identifier, max_videos)
    builder = PROFILE_URL_MAP.get(platform)
    return JSONResponse(content={
        "platform": platform,
        "platform_name": PLATFORM_NAMES.get(platform),
        "identifier": identifier,
        "profile_url": builder(identifier) if builder else url,
        "total_found": len(videos),
        "videos": videos,
    })


# ── Clip ──────────────────────────────────────────────────────────────

@app.get("/clip")
async def clip_video(
    url: str = Query(...), start: str = Query(...), end: str = Query(...),
    quality: str = Query("hd"), filename: Optional[str] = Query(None),
):
    if not is_ffmpeg_available():
        raise HTTPException(status_code=503, detail={"error": "ffmpeg_not_available"})
    url = url.strip()
    platform = detect_platform(url)
    tmp = tempfile.mkdtemp()
    try:
        if platform == "sora":
            video_url = await resolve_sora(url)
            src = os.path.join(tmp, "source.mp4")
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=20, read=600, write=60, pool=10),
                follow_redirects=True
            ) as c:
                async with c.stream("GET", video_url, headers=VIDEO_H) as r:
                    r.raise_for_status()
                    with open(src, "wb") as f:
                        async for chunk in r.aiter_bytes(CHUNK_SIZE): f.write(chunk)
            title = "sora_video"
        else:
            opts = make_ydl_opts(platform, os.path.join(tmp, "source.%(ext)s"), quality=quality)
            info = await run_ydl(url, opts)
            sf = find_file(tmp)
            if not sf:
                raise HTTPException(status_code=500, detail="Source download failed.")
            src = str(sf)
            title = (info or {}).get("title", "video")

        clip_path = os.path.join(tmp, "clip.mp4")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-ss", str(start), "-to", str(end), "-i", src,
            "-c:v", "libx264", "-c:a", "aac",
            "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", clip_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise HTTPException(status_code=500, detail={
                "error": "clip_failed",
                "message": (stderr.decode(errors="ignore") or "ffmpeg error")[-300:]
            })
        cf = Path(clip_path)
        if not cf.exists() or cf.stat().st_size == 0:
            raise HTTPException(status_code=500, detail="Clip file empty.")
        out_fn = filename or smart_filename(f"{title}_clip_{start}-{end}".replace(":", "-"), platform)

        async def _clip():
            try:
                with open(cf, "rb") as f:
                    while True:
                        chunk = f.read(CHUNK_SIZE)
                        if not chunk: break
                        yield chunk
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        return StreamingResponse(_clip(), media_type="video/mp4", headers={
            "Content-Disposition": f'attachment; filename="{out_fn}"',
            "Content-Length": str(cf.stat().st_size),
            "Access-Control-Allow-Origin": "*",
        })
    except HTTPException:
        shutil.rmtree(tmp, ignore_errors=True); raise
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e)[:300])


# ── Multi-clip ────────────────────────────────────────────────────────

class ClipSeg(BaseModel):
    start: str; end: str; label: Optional[str] = None

class MultiClipReq(BaseModel):
    url: str; segments: List[ClipSeg]; quality: str = "hd"

@app.post("/multi-clip")
async def multi_clip(req: MultiClipReq):
    if not is_ffmpeg_available():
        raise HTTPException(status_code=503, detail="ffmpeg not available")
    if len(req.segments) > 20:
        raise HTTPException(status_code=400, detail="Max 20 clips.")
    url = req.url.strip()
    platform = detect_platform(url)
    tmp = tempfile.mkdtemp()
    try:
        if platform == "sora":
            video_url = await resolve_sora(url)
            src = os.path.join(tmp, "source.mp4")
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=20, read=600, write=60, pool=10),
                follow_redirects=True
            ) as c:
                async with c.stream("GET", video_url, headers=VIDEO_H) as r:
                    with open(src, "wb") as f:
                        async for chunk in r.aiter_bytes(CHUNK_SIZE): f.write(chunk)
            title = "sora_clip"
        else:
            opts = make_ydl_opts(platform, os.path.join(tmp, "source.%(ext)s"), quality=req.quality)
            info = await run_ydl(url, opts)
            sf = find_file(tmp)
            if not sf: raise HTTPException(status_code=500, detail="Source download failed.")
            src = str(sf)
            title = (info or {}).get("title", "video")
        results = []
        for i, seg in enumerate(req.segments):
            name = seg.label or f"clip_{i+1}"
            cp = os.path.join(tmp, f"{name}.mp4")
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-ss", seg.start, "-to", seg.end, "-i", src,
                "-c:v", "libx264", "-c:a", "aac", "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart", cp,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0 and Path(cp).exists():
                results.append({"label": name, "start": seg.start, "end": seg.end,
                                 "status": "ready", "size_human": format_bytes(Path(cp).stat().st_size),
                                 "download_path": cp})
            else:
                results.append({"label": name, "start": seg.start, "end": seg.end,
                                 "status": "failed", "error": (stderr.decode(errors="ignore") or "")[-100:]})
        return JSONResponse(content={"title": title, "platform": platform, "clips": results, "tmp_dir": tmp,
                                     "ready": sum(1 for r in results if r["status"] == "ready")})
    except HTTPException:
        shutil.rmtree(tmp, ignore_errors=True); raise
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e)[:300])


@app.get("/clip-file")
async def get_clip_file(path: str = Query(...)):
    cf = Path(path)
    if not cf.exists(): raise HTTPException(status_code=404, detail="File not found.")
    async def _s():
        try:
            with open(cf, "rb") as f:
                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk: break
                    yield chunk
        finally:
            cf.unlink(missing_ok=True)
    return StreamingResponse(_s(), media_type="video/mp4", headers={
        "Content-Disposition": f'attachment; filename="{cf.name}"',
        "Content-Length": str(cf.stat().st_size),
        "Access-Control-Allow-Origin": "*",
    })


# ── Enhance ───────────────────────────────────────────────────────────

@app.get("/enhance/presets")
async def get_enhance_presets():
    return {"presets": [{"id": k, "label": v["label"]} for k, v in ENHANCE_PRESETS.items()],
            "ffmpeg_available": is_ffmpeg_available()}


@app.get("/enhance")
async def enhance_video(url: str = Query(...), preset: str = Query("standard"),
                         filename: Optional[str] = Query(None)):
    if not is_ffmpeg_available():
        raise HTTPException(status_code=503, detail={
            "error": "ffmpeg_not_available",
            "message": "ffmpeg zaroori hai. nixpacks.toml ya Dockerfile check karein."
        })
    if preset not in ENHANCE_PRESETS:
        raise HTTPException(status_code=400, detail=f"Invalid preset. Use: {', '.join(ENHANCE_PRESETS)}")
    url = url.strip()
    cfg = ENHANCE_PRESETS[preset]
    tmp = tempfile.mkdtemp()
    try:
        video_url = await resolve_sora(url)
        src = os.path.join(tmp, "original.mp4")
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=20, read=600, write=60, pool=10),
            follow_redirects=True
        ) as c:
            async with c.stream("GET", video_url, headers=VIDEO_H) as r:
                r.raise_for_status()
                with open(src, "wb") as f:
                    async for chunk in r.aiter_bytes(CHUNK_SIZE): f.write(chunk)
        if not Path(src).exists() or Path(src).stat().st_size == 0:
            raise HTTPException(status_code=500, detail="Source download failed.")
        ep = os.path.join(tmp, "enhanced.mp4")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", src, "-vf", cfg["vf"],
            "-c:v", "libx264", "-crf", cfg["crf"], "-preset", cfg["preset"],
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-pix_fmt", "yuv420p", ep,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise HTTPException(status_code=500, detail={
                "error": "enhance_failed",
                "message": (stderr.decode(errors="ignore") or "ffmpeg error")[-400:]
            })
        ef = Path(ep)
        if not ef.exists() or ef.stat().st_size == 0:
            raise HTTPException(status_code=500, detail="Enhanced file empty.")
        sid = re.search(r'/p/(s_[a-f0-9]+)', url, re.I)
        out_fn = filename or f"SORA_{datetime.now().strftime('%Y%m%d')}_{sid.group(1) if sid else 'video'}_{preset}_enhanced.mp4"
        out_fn = re.sub(r'[^\w\-.]', '_', out_fn)[:120]
        async def _es():
            try:
                with open(ef, "rb") as f:
                    while True:
                        chunk = f.read(CHUNK_SIZE)
                        if not chunk: break
                        yield chunk
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        return StreamingResponse(_es(), media_type="video/mp4", headers={
            "Content-Disposition": f'attachment; filename="{out_fn}"',
            "Content-Length": str(ef.stat().st_size),
            "Access-Control-Allow-Origin": "*",
            "X-Original-Size": str(Path(src).stat().st_size),
            "X-Preset": preset,
        })
    except HTTPException:
        shutil.rmtree(tmp, ignore_errors=True); raise
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e)[:300])


# ── Thumbnail ─────────────────────────────────────────────────────────

@app.get("/thumbnail")
async def get_thumbnail(url: str = Query(...)):
    url = url.strip()
    platform = detect_platform(url)
    if platform == "sora":
        raise HTTPException(status_code=422, detail="Sora thumbnails not supported.")
    inf = await info_only(url, platform)
    thumb = inf.get("thumbnail")
    if not thumb:
        raise HTTPException(status_code=404, detail="Thumbnail not found.")
    async def _t():
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as c:
            async with c.stream("GET", thumb) as r:
                async for chunk in r.aiter_bytes(65536): yield chunk
    return StreamingResponse(_t(), media_type="image/jpeg", headers={
        "Content-Disposition": f'attachment; filename="{smart_filename(inf.get("title","thumb"), platform, "jpg")}"',
        "Access-Control-Allow-Origin": "*",
    })


# ── Subtitles ─────────────────────────────────────────────────────────

@app.get("/subtitles")
async def get_subtitles(url: str = Query(...), lang: str = Query("en")):
    url = url.strip()
    platform = detect_platform(url)
    if platform == "sora":
        raise HTTPException(status_code=422, detail="Sora subtitles not supported.")
    tmp = tempfile.mkdtemp()
    opts = {"skip_download": True, "writesubtitles": True, "writeautomaticsub": True,
            "subtitlesformat": "srt", "subtitleslangs": [lang, "en"],
            "outtmpl": os.path.join(tmp, "sub.%(ext)s"), "quiet": True}
    loop = asyncio.get_event_loop()
    try:
        info = await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(opts).extract_info(url, download=True))
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(status_code=422, detail=f"Subtitles nahi mile: {str(e)[:200]}")
    subs = list(Path(tmp).glob("*.srt")) + list(Path(tmp).glob("*.vtt"))
    if not subs:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(status_code=404, detail="No subtitles found.")
    content = subs[0].read_text(encoding="utf-8", errors="ignore")
    shutil.rmtree(tmp, ignore_errors=True)
    return StreamingResponse(iter([content.encode()]), media_type="text/plain", headers={
        "Content-Disposition": f'attachment; filename="captions_{lang}.srt"',
        "Access-Control-Allow-Origin": "*",
    })


# ── Import URLs ────────────────────────────────────────────────────────

@app.post("/import-urls")
async def import_urls(file: UploadFile = File(...)):
    text = (await file.read()).decode("utf-8", errors="ignore")
    urls = []
    try:
        for row in csv.reader(io.StringIO(text)):
            for cell in row:
                c = cell.strip().strip('"\'')
                if c.startswith("http"): urls.append(c)
    except Exception:
        pass
    if not urls:
        urls = [l.strip() for l in text.splitlines() if l.strip().startswith("http")]
    if not urls:
        raise HTTPException(status_code=422, detail="Koi URL nahi mili.")
    result = [{"url": u, "platform": detect_platform(u),
               "platform_name": PLATFORM_NAMES.get(detect_platform(u), "Generic")}
              for u in urls[:50]]
    return JSONResponse(content={"urls": result, "total": len(result)})


# ── ZIP download ──────────────────────────────────────────────────────

class ZipReq(BaseModel):
    urls: List[str]
    username: str = "videos"
    platform: str = "mixed"


async def _download_for_zip(url: str, idx: int, vid_dir: str) -> dict:
    platform = detect_platform(url)
    pfx = str(idx + 1).zfill(3)

    if platform == "sora":
        try:
            vurl = await resolve_sora(url)
            out = os.path.join(vid_dir, f"{pfx}_sora.mp4")
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=20, read=300, write=60, pool=10),
                follow_redirects=True
            ) as c:
                async with c.stream("GET", vurl, headers=VIDEO_H) as r:
                    r.raise_for_status()
                    with open(out, "wb") as f:
                        async for chunk in r.aiter_bytes(CHUNK_SIZE): f.write(chunk)
            return {"status": "ok", "file": out}
        except Exception as e:
            return {"status": "failed", "error": str(e)[:80]}

    out_tmpl = os.path.join(vid_dir, f"{pfx}_%(title).50s.%(ext)s")
    opts = make_ydl_opts(platform, out_tmpl, quality="hd")

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(opts).extract_info(url, download=True))
        matches = sorted(
            [f for f in Path(vid_dir).iterdir() if f.is_file() and f.name.startswith(pfx)],
            key=lambda f: f.stat().st_mtime, reverse=True,
        )
        if matches:
            return {"status": "ok", "file": str(matches[0])}
        return {"status": "failed", "error": "file not found"}
    except Exception as e:
        return {"status": "failed", "error": str(e)[:80]}


@app.post("/download-zip")
async def download_zip(req: ZipReq):
    urls = [u.strip() for u in req.urls if u.strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="Koi URL nahi.")
    if len(urls) > 50:
        raise HTTPException(status_code=400, detail="Max 50 per ZIP.")

    safe_user = re.sub(r'[^\w\-]', '_', req.username or "videos")[:30]
    plat_lbl  = re.sub(r'[^\w]', '', req.platform or "mixed").upper()[:10]
    date_s    = datetime.now().strftime("%Y%m%d_%H%M")
    zip_fn    = f"{plat_lbl}_{safe_user}_{date_s}_{len(urls)}videos.zip"

    tmp = tempfile.mkdtemp()
    vid_dir = os.path.join(tmp, "videos")
    os.makedirs(vid_dir, exist_ok=True)

    try:
        results = []
        for i in range(0, len(urls), 3):
            batch = urls[i:i+3]
            res = await asyncio.gather(*[_download_for_zip(u, i+j, vid_dir) for j, u in enumerate(batch)])
            results.extend(res)

        ok = [r["file"] for r in results if r.get("status") == "ok" and r.get("file")]
        failed = len(urls) - len(ok)

        if not ok:
            errs = "; ".join(r.get("error", "?") for r in results[:3])
            raise HTTPException(status_code=422, detail={
                "error": "all_failed",
                "message": f"Koi video download nahi ho saki. Errors: {errs}",
            })

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for fp in ok:
                p = Path(fp)
                if p.exists() and p.stat().st_size > 0:
                    arc = re.sub(r'[^\w\-. ]', '_', p.name)[:80]
                    zf.write(str(p), arc)

        zip_bytes = buf.getvalue()
        buf.close()

        if len(zip_bytes) < 200:
            raise HTTPException(status_code=500, detail="ZIP file empty — koi video download nahi hui.")

        async def _zip():
            MB = 512 * 1024
            for off in range(0, len(zip_bytes), MB):
                yield zip_bytes[off:off+MB]

        return StreamingResponse(_zip(), media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{zip_fn}"',
            "Content-Length": str(len(zip_bytes)),
            "Access-Control-Allow-Origin": "*",
            "X-Total": str(len(urls)),
            "X-Success": str(len(ok)),
            "X-Failed": str(failed),
        })
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── Batch info ────────────────────────────────────────────────────────

class BatchReq(BaseModel):
    urls: List[str]

@app.post("/batch/info")
async def batch_info(req: BatchReq):
    if len(req.urls) > 50:
        raise HTTPException(status_code=400, detail="Max 50 URLs.")
    results = [{"url": u, "platform": detect_platform(u.strip()),
                "platform_name": PLATFORM_NAMES.get(detect_platform(u.strip()), "Generic"),
                "status": "queued"} for u in req.urls]
    return JSONResponse(content={"results": results, "total": len(results)})
