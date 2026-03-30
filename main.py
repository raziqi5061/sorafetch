"""
ContentCreator Pro Studio - FastAPI Backend v9
All-platform profile bulk downloader + ZIP fix + YouTube fix
Platforms: YouTube, TikTok, Instagram, Facebook, Twitter/X, Bilibili, Douyin, Sora
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

app = FastAPI(title="ContentCreator Pro Studio", version="9.0.0")

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

# Profile URL patterns for each platform
PROFILE_PATTERNS = {
    "youtube":   r"(?:youtube\.com/@|youtube\.com/(?:user|c|channel)/)([^/?&\s]+)",
    "tiktok":    r"tiktok\.com/@([^/?&\s]+)",
    "instagram": r"instagram\.com/([^/?&\s]+)/?$",
    "twitter":   r"(?:twitter\.com|x\.com)/([^/?&\s]+)",
    "bilibili":  r"space\.bilibili\.com/(\d+)|bilibili\.com/([^/?&\s]+)",
    "facebook":  r"facebook\.com/([^/?&\s]+)",
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
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def is_ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def smart_filename(title: str, platform: str, ext: str = "mp4") -> str:
    date = datetime.now().strftime("%Y%m%d")
    clean = re.sub(r'[^\w\s\-]', '', str(title or "video"))
    clean = re.sub(r'\s+', '_', clean.strip()).strip('_')[:40]
    return f"{platform.upper()[:8]}_{date}_{clean}.{ext}"


def safe_filename(name: str, ext: str = "mp4") -> str:
    name = re.sub(r'[^\w\-.]', '_', str(name))
    name = re.sub(r'_+', '_', name).strip('_')
    if not name:
        name = "video"
    if not name.endswith(f".{ext}"):
        name = f"{name}.{ext}"
    return name[:100]


def find_file(tmp_dir: str) -> Optional[Path]:
    files = sorted(
        [f for f in Path(tmp_dir).iterdir() if f.is_file()],
        key=lambda f: f.stat().st_size, reverse=True,
    )
    return files[0] if files else None


def find_files_by_prefix(directory: str, prefix: str) -> List[Path]:
    return sorted(
        [f for f in Path(directory).iterdir() if f.is_file() and f.name.startswith(prefix)],
        key=lambda f: f.stat().st_mtime, reverse=True,
    )


# ─── Format selection ─────────────────────────────────────────────────

def get_best_format(platform: str, quality: str = "best") -> str:
    if not is_ffmpeg_available():
        return "best[ext=mp4]/best[ext=webm]/best"

    if platform in ("tiktok", "douyin"):
        return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"

    if platform == "youtube":
        q = {
            "4k":   "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=2160]+bestaudio/best",
            "hd":   "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best",
            "sd":   "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best",
            "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        }
        return q.get(quality, q["best"])

    q_map = {
        "4k":   "bestvideo[height<=2160]+bestaudio/best",
        "hd":   "bestvideo[height<=1080]+bestaudio/best",
        "sd":   "bestvideo[height<=480]+bestaudio/best",
        "best": "bestvideo+bestaudio/best",
    }
    return q_map.get(quality, q_map["best"])


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
        opts.update({"writesubtitles": True, "writeautomaticsub": True,
                     "subtitlesformat": "srt", "subtitleslangs": ["en", "auto"]})
    if thumbnail:
        opts["writethumbnail"] = True

    extras = {
        "tiktok": {
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
                "Referer": "https://www.tiktok.com/",
            },
            "extractor_args": {"tiktok": {"api_hostname": "api22-normal-c-useast2a.tiktokv.com"}},
        },
        "youtube": {
            "extractor_args": {
                "youtube": {"player_client": ["android", "tv_embedded"], "player_skip": ["webpage"]}
            },
            "http_headers": {"User-Agent": "com.google.android.youtube/17.36.4 (Linux; U; Android 12; GB) gzip"},
        },
        "facebook":  {"http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}},
        "instagram": {"http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}},
        "twitter":   {"http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}},
        "bilibili":  {"http_headers": {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}},
        "douyin":    {"http_headers": {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15", "Referer": "https://www.douyin.com/"}},
    }
    if platform in extras:
        opts.update(extras[platform])
    return opts


# ─── yt-dlp runner with YouTube fallback chain ────────────────────────

async def run_ydl(url: str, opts: dict) -> dict:
    def _run(o):
        with yt_dlp.YoutubeDL(o) as ydl:
            return ydl.extract_info(url, download=True)

    loop = asyncio.get_event_loop()
    is_yt = "youtube.com" in url or "youtu.be" in url

    try:
        return await loop.run_in_executor(None, lambda: _run(opts))
    except yt_dlp.utils.DownloadError as e:
        err = str(e)

        # ffmpeg missing
        if "ffmpeg" in err.lower() or "merger" in err.lower():
            fb = {**opts, "format": "best[ext=mp4]/best[ext=webm]/best", "merge_output_format": None}
            try:
                return await loop.run_in_executor(None, lambda: _run(fb))
            except Exception as e2:
                raise HTTPException(status_code=422, detail={"error": "ffmpeg_error", "message": str(e2)[:200]})

        # YouTube bot / format errors
        yt_err = any(k in err.lower() for k in ["sign in", "bot", "confirm", "not available", "requested format", "format"])
        if is_yt and yt_err:
            combos = [
                (["android"],               "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"),
                (["tv_embedded"],           "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"),
                (["ios"],                   "bestvideo+bestaudio/best"),
                (["android", "tv_embedded"],"bestvideo+bestaudio/best"),
                (["mweb"],                  "best[ext=mp4]/best"),
                (["android"],               "best"),
                (["tv_embedded"],           "best"),
            ]
            for clients, fmt in combos:
                try:
                    yo = {
                        **opts,
                        "format": fmt,
                        "extractor_args": {"youtube": {"player_client": clients, "player_skip": ["webpage"]}},
                        "http_headers": {"User-Agent": "com.google.android.youtube/17.36.4 (Linux; U; Android 12; GB) gzip"},
                    }
                    return await loop.run_in_executor(None, lambda o=yo: _run(o))
                except Exception:
                    continue
            raise HTTPException(status_code=422, detail={
                "error": "youtube_failed",
                "message": "YouTube video download nahi ho saki. Age-restricted, private, ya region-locked ho sakti hai.",
            })

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


# ─── Sora extractor ───────────────────────────────────────────────────

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


# ─── Enhancement presets ──────────────────────────────────────────────

ENHANCE_PRESETS = {
    "light":     {"label": "Light Boost",    "vf": "unsharp=5:5:0.8:3:3:0.4,eq=contrast=1.05:saturation=1.1",                                                                     "crf": "20", "preset": "fast"},
    "standard":  {"label": "Standard",       "vf": "hqdn3d=2:1.5:6:4.5,unsharp=5:5:1.2:3:3:0.6,scale=1920:1080:flags=lanczos,eq=contrast=1.08:saturation=1.15:gamma=0.95",       "crf": "18", "preset": "medium"},
    "strong":    {"label": "Strong",         "vf": "hqdn3d=3:2.5:8:6,unsharp=7:7:1.5:5:5:0.8,scale=iw*2:ih*2:flags=lanczos,eq=contrast=1.12:saturation=1.2:gamma=0.92",          "crf": "16", "preset": "slow"},
    "cinematic": {"label": "Cinematic",      "vf": "hqdn3d=2:1.5:5:4,unsharp=5:5:1.0:3:3:0.5,scale=1920:1080:flags=lanczos,eq=contrast=1.1:saturation=0.95:gamma=0.9,vignette=PI/5","crf":"17","preset":"medium"},
}


# ─── All-platform profile fetcher ────────────────────────────────────

def parse_profile_input(raw: str, platform: str) -> str:
    """Extract username/channel-id from any input format."""
    raw = raw.strip()
    pat = PROFILE_PATTERNS.get(platform)
    if pat:
        m = re.search(pat, raw, re.I)
        if m:
            return next(g for g in m.groups() if g)
    # Strip @ and return as-is
    return raw.lstrip('@').split('/')[0].split('?')[0]


def build_profile_url(platform: str, identifier: str) -> str:
    """Build the correct playlist/channel URL for each platform."""
    ident = identifier.strip().lstrip('@')
    urls = {
        "youtube":   [
            f"https://www.youtube.com/@{ident}/videos",
            f"https://www.youtube.com/c/{ident}/videos",
            f"https://www.youtube.com/user/{ident}/videos",
        ],
        "tiktok":    [f"https://www.tiktok.com/@{ident}"],
        "instagram": [f"https://www.instagram.com/{ident}/"],
        "twitter":   [f"https://twitter.com/{ident}", f"https://x.com/{ident}"],
        "bilibili":  [f"https://space.bilibili.com/{ident}/video"] if ident.isdigit()
                     else [f"https://www.bilibili.com/@{ident}"],
        "facebook":  [f"https://www.facebook.com/{ident}/videos"],
    }
    result = urls.get(platform, [f"https://www.{platform}.com/@{ident}"])
    return result[0]  # Primary URL


PROFILE_YDL_OPTS = {
    "youtube": {
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "tv_embedded"],
                "player_skip": ["webpage"],
            }
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
            "tiktok": {"api_hostname": "api22-normal-c-useast2a.tiktokv.com"},
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
    "facebook": {
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


def make_video_entry(entry: dict, platform: str, identifier: str) -> Optional[dict]:
    """Convert a yt-dlp flat entry to our video dict."""
    if not entry:
        return None

    # Try all possible URL fields
    vid_url = (
        entry.get("webpage_url") or
        entry.get("url") or
        entry.get("original_url") or
        ""
    )

    # Build URL from ID if not found
    if not vid_url or not vid_url.startswith("http"):
        vid_id = entry.get("id", "")
        if vid_id:
            url_map = {
                "youtube":   f"https://www.youtube.com/watch?v={vid_id}",
                "tiktok":    f"https://www.tiktok.com/@{identifier}/video/{vid_id}",
                "instagram": f"https://www.instagram.com/p/{vid_id}/",
                "twitter":   f"https://twitter.com/i/web/status/{vid_id}",
                "bilibili":  f"https://www.bilibili.com/video/{vid_id}",
                "facebook":  f"https://www.facebook.com/video/{vid_id}",
            }
            vid_url = url_map.get(platform, "")

    if not vid_url:
        return None

    title = (entry.get("title") or entry.get("description") or vid_url.split("/")[-1] or "Video")[:100]
    # Skip "Private video", "Deleted video" etc
    if title.lower() in ("private video", "deleted video", "[private]", "[deleted]"):
        return None

    return {
        "url": vid_url,
        "title": title,
        "duration": entry.get("duration"),
        "duration_fmt": format_duration(entry.get("duration") or 0),
        "thumbnail": entry.get("thumbnail") or entry.get("thumbnails", [{}])[-1].get("url") if entry.get("thumbnails") else None,
        "view_count": entry.get("view_count"),
        "like_count": entry.get("like_count"),
        "upload_date": entry.get("upload_date", ""),
        "id": entry.get("id", ""),
        "platform": platform,
    }


async def fetch_profile_videos(platform: str, identifier: str, max_videos: int = 50) -> List[dict]:
    """
    Fetch all public videos from a user profile using yt-dlp.
    Uses extract_flat=True for speed — only gets URLs/metadata, no download.
    """
    identifier = identifier.strip().lstrip('@')
    profile_url = build_profile_url(platform, identifier)

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",   # Better than True — works for nested playlists
        "playlistend": max_videos,
        "ignoreerrors": True,             # Skip private/deleted videos
        "age_limit": None,
        **PROFILE_YDL_OPTS.get(platform, {}),
    }

    def _extract(url: str) -> list:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return []

            entries = info.get("entries") or []

            # Handle nested playlists (YouTube sometimes returns playlist of playlists)
            flat = []
            for e in entries:
                if not e:
                    continue
                if e.get("_type") == "playlist" and e.get("entries"):
                    flat.extend(e["entries"])
                else:
                    flat.append(e)

            videos = []
            for entry in flat[:max_videos]:
                v = make_video_entry(entry, platform, identifier)
                if v:
                    videos.append(v)
            return videos

    loop = asyncio.get_event_loop()

    # Try primary URL
    try:
        videos = await loop.run_in_executor(None, lambda: _extract(profile_url))
        if videos:
            return videos
    except Exception as e:
        last_err = str(e)

    # Fallback URLs for YouTube
    if platform == "youtube":
        fallback_urls = [
            f"https://www.youtube.com/c/{identifier}/videos",
            f"https://www.youtube.com/user/{identifier}/videos",
            f"https://www.youtube.com/@{identifier}",
        ]
        for fb_url in fallback_urls:
            if fb_url == profile_url:
                continue
            try:
                videos = await loop.run_in_executor(None, lambda u=fb_url: _extract(u))
                if videos:
                    return videos
            except Exception:
                continue

    # Fallback for TikTok — try without extractor_args
    if platform == "tiktok":
        simple_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
            "playlistend": max_videos,
            "ignoreerrors": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            },
        }
        try:
            videos = await loop.run_in_executor(
                None, lambda: yt_dlp.YoutubeDL(simple_opts).extract_info(
                    f"https://www.tiktok.com/@{identifier}", download=False
                )
            )
            if videos and videos.get("entries"):
                result = []
                for e in (videos.get("entries") or [])[:max_videos]:
                    v = make_video_entry(e, platform, identifier)
                    if v:
                        result.append(v)
                if result:
                    return result
        except Exception:
            pass

    raise HTTPException(status_code=422, detail={
        "error": "profile_fetch_failed",
        "message": f"{PLATFORM_NAMES.get(platform, platform)} @{identifier} se videos nahi mile. Check karein: 1) Account public hai? 2) Username sahi hai? 3) Videos hain?",
        "platform": platform,
        "identifier": identifier,
        "profile_url": profile_url,
    })


# ─── Routes ──────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"name": "ContentCreator Pro Studio", "version": "9.0.0", "ffmpeg": is_ffmpeg_available()}


@app.get("/health")
async def health():
    return {"status": "ok", "version": "9.0.0", "ffmpeg": is_ffmpeg_available()}


@app.get("/detect")
async def detect_route(url: str = Query(...)):
    p = detect_platform(url)
    return {"platform": p, "platform_name": PLATFORM_NAMES.get(p, "Generic")}


# ── Profile fetch (ALL platforms) ────────────────────────────────────
@app.get("/profile")
async def profile_fetch(
    url: str = Query(..., description="Profile URL or @username — any platform"),
    max_videos: int = Query(50, ge=1, le=200),
):
    """
    Universal profile video fetcher.
    Accepts full profile URL or bare @username (with platform= param).
    """
    url = url.strip()
    platform = detect_platform(url)

    if platform == "sora":
        raise HTTPException(status_code=422, detail="Sora profile fetch not supported.")

    # If platform unknown — treat as YouTube (most common) or raise
    if platform == "generic":
        # Maybe it's a bare username? Not much we can do without platform context
        raise HTTPException(status_code=422, detail={
            "error": "unknown_platform",
            "message": "Platform URL paste karein — e.g. youtube.com/@channel, tiktok.com/@user, instagram.com/user",
        })

    identifier = parse_profile_input(url, platform)
    if not identifier:
        raise HTTPException(status_code=400, detail="Valid username ya profile URL chahiye.")

    videos = await fetch_profile_videos(platform, identifier, max_videos)

    if not videos:
        raise HTTPException(status_code=404, detail={
            "error": "no_videos",
            "message": f"{PLATFORM_NAMES.get(platform)} @{identifier} ke koi public videos nahi mile. Account private ho sakta hai ya videos nahi hain.",
        })

    return JSONResponse(content={
        "platform": platform,
        "platform_name": PLATFORM_NAMES.get(platform, "Generic"),
        "identifier": identifier,
        "profile_url": build_profile_url(platform, identifier),
        "total_found": len(videos),
        "videos": videos,
    })


# ── Metadata ──────────────────────────────────────────────────────────
@app.get("/metadata")
async def get_metadata(url: str = Query(...)):
    url = url.strip()
    platform = detect_platform(url)
    if platform == "sora":
        video_url = await resolve_sora(url)
        sid = re.search(r'/p/(s_[a-f0-9]+)', url, re.I)
        return {"platform": "sora", "platform_name": "Sora", "title": f"Sora Video {sid.group(1) if sid else ''}",
                "description": "", "hashtags": [], "duration": None, "duration_fmt": None,
                "thumbnail": None, "uploader": "Sora/OpenAI", "video_url": video_url, "available_qualities": ["best"]}
    info = await extract_info_only(url, platform)
    desc = info.get("description") or ""
    tags = info.get("tags") or []
    hashtags = list(set(tags[:20] + re.findall(r'#\w+', desc)[:20]))
    formats = info.get("formats") or []
    heights = sorted(set(f.get("height") for f in formats if f.get("height") and f.get("vcodec") != "none"), reverse=True)
    qualities = []
    if any(h >= 2160 for h in heights): qualities.append("4k")
    if any(h >= 1080 for h in heights): qualities.append("hd")
    if any(h >= 480 for h in heights):  qualities.append("sd")
    if not qualities: qualities = ["best"]
    return {
        "platform": platform, "platform_name": PLATFORM_NAMES.get(platform, "Generic"),
        "title": info.get("title", ""), "description": desc[:500] + ("..." if len(desc) > 500 else ""),
        "hashtags": hashtags[:30], "duration": info.get("duration"),
        "duration_fmt": format_duration(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader") or info.get("channel") or "",
        "view_count": info.get("view_count"), "like_count": info.get("like_count"),
        "upload_date": info.get("upload_date"), "available_qualities": qualities,
    }


# ── Download ──────────────────────────────────────────────────────────
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

    if platform == "sora":
        video_url = await resolve_sora(url)
        sid = re.search(r'/p/(s_[a-f0-9]+)', url, re.I)
        out_filename = filename or smart_filename(f"sora_{sid.group(1) if sid else 'video'}", "sora")
        resp_headers = {"Content-Disposition": f'attachment; filename="{out_filename}"', "Access-Control-Allow-Origin": "*"}
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
                head = await client.head(video_url, headers=VIDEO_HEADERS)
                cl = head.headers.get("content-length")
                if cl: resp_headers["Content-Length"] = cl
        except Exception:
            pass
        async def sora_stream():
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=20.0, read=600.0, write=60.0, pool=10.0), follow_redirects=True) as client:
                async with client.stream("GET", video_url, headers=VIDEO_HEADERS) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes(chunk_size=CHUNK_SIZE):
                        yield chunk
        return StreamingResponse(sora_stream(), media_type="video/mp4", headers=resp_headers)

    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, "video.%(ext)s")
    opts = build_ydl_opts(platform, tmp_path, quality=quality, subtitles=subtitles, thumbnail=thumbnail)
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
                        if not chunk: break
                        yield chunk
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        return StreamingResponse(file_stream(), media_type="video/mp4", headers=resp_headers)
    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e)[:300])


# ── Clip ──────────────────────────────────────────────────────────────
@app.get("/clip")
async def clip_video(
    url: str = Query(...),
    start: str = Query(...),
    end: str = Query(...),
    quality: str = Query("hd"),
    filename: Optional[str] = Query(None),
):
    if not is_ffmpeg_available():
        raise HTTPException(status_code=503, detail={"error": "ffmpeg_not_available", "message": "ffmpeg zaroori hai."})
    url = url.strip()
    platform = detect_platform(url)
    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, "source.%(ext)s")
    try:
        if platform == "sora":
            video_url = await resolve_sora(url)
            src_path = os.path.join(tmp_dir, "source.mp4")
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=20.0, read=600.0, write=60.0, pool=10.0), follow_redirects=True) as client:
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
                raise HTTPException(status_code=500, detail="Source download failed.")
            src_path = str(src_file)
            title = info.get("title", "video") if info else "video"

        clip_path = os.path.join(tmp_dir, "clip.mp4")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-ss", str(start), "-to", str(end), "-i", src_path,
            "-c:v", "libx264", "-c:a", "aac", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", clip_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise HTTPException(status_code=500, detail={"error": "clip_failed", "message": stderr.decode()[-300:] if stderr else "ffmpeg error"})

        clip_file = Path(clip_path)
        if not clip_file.exists() or clip_file.stat().st_size == 0:
            raise HTTPException(status_code=500, detail="Clip file empty.")
        out_filename = filename or smart_filename(f"{title}_clip_{start}-{end}".replace(":", "-"), platform)
        async def clip_stream():
            try:
                with open(clip_file, "rb") as f:
                    while True:
                        chunk = f.read(CHUNK_SIZE)
                        if not chunk: break
                        yield chunk
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        return StreamingResponse(clip_stream(), media_type="video/mp4", headers={
            "Content-Disposition": f'attachment; filename="{out_filename}"',
            "Content-Length": str(clip_file.stat().st_size),
            "Access-Control-Allow-Origin": "*",
        })
    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e)[:300])


# ── Multi-clip ────────────────────────────────────────────────────────
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
    if not is_ffmpeg_available():
        raise HTTPException(status_code=503, detail="ffmpeg not available")
    if len(request.segments) > 20:
        raise HTTPException(status_code=400, detail="Max 20 clips.")
    url = request.url.strip()
    platform = detect_platform(url)
    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, "source.%(ext)s")
    try:
        if platform == "sora":
            video_url = await resolve_sora(url)
            src_path = os.path.join(tmp_dir, "source.mp4")
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=20.0, read=600.0, write=60.0, pool=10.0), follow_redirects=True) as client:
                async with client.stream("GET", video_url, headers=VIDEO_HEADERS) as resp:
                    with open(src_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=CHUNK_SIZE): f.write(chunk)
            title = "sora_clip"
        else:
            opts = build_ydl_opts(platform, tmp_path, quality=request.quality)
            info = await run_ydl(url, opts)
            src_file = find_file(tmp_dir)
            if not src_file:
                raise HTTPException(status_code=500, detail="Source download failed.")
            src_path = str(src_file)
            title = info.get("title", "video") if info else "video"

        results = []
        for i, seg in enumerate(request.segments):
            clip_name = seg.label or f"clip_{i+1}"
            clip_path = os.path.join(tmp_dir, f"{clip_name}.mp4")
            cmd = ["ffmpeg", "-y", "-ss", str(seg.start), "-to", str(seg.end), "-i", src_path,
                   "-c:v", "libx264", "-c:a", "aac", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", clip_path]
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, stderr = await proc.communicate()
            if proc.returncode == 0 and Path(clip_path).exists():
                results.append({"label": clip_name, "start": seg.start, "end": seg.end, "status": "ready",
                                 "size_human": format_bytes(Path(clip_path).stat().st_size), "download_path": clip_path})
            else:
                results.append({"label": clip_name, "start": seg.start, "end": seg.end, "status": "failed",
                                 "error": stderr.decode()[-200:] if stderr else "unknown"})
        return JSONResponse(content={"title": title, "platform": platform, "clips": results, "tmp_dir": tmp_dir,
                                     "ready": sum(1 for r in results if r["status"] == "ready")})
    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e)[:300])


@app.get("/clip-file")
async def get_clip_file(path: str = Query(...)):
    clip_file = Path(path)
    if not clip_file.exists():
        raise HTTPException(status_code=404, detail="Clip file not found.")
    async def stream():
        try:
            with open(clip_file, "rb") as f:
                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk: break
                    yield chunk
        finally:
            clip_file.unlink(missing_ok=True)
    return StreamingResponse(stream(), media_type="video/mp4", headers={
        "Content-Disposition": f'attachment; filename="{clip_file.name}"',
        "Content-Length": str(clip_file.stat().st_size),
        "Access-Control-Allow-Origin": "*",
    })


# ── Enhance ───────────────────────────────────────────────────────────
@app.get("/enhance/presets")
async def get_enhance_presets():
    return {"presets": [{"id": k, "label": v["label"]} for k, v in ENHANCE_PRESETS.items()],
            "ffmpeg_available": is_ffmpeg_available()}


@app.get("/enhance")
async def enhance_video(
    url: str = Query(...),
    preset: str = Query("standard"),
    filename: Optional[str] = Query(None),
):
    if not is_ffmpeg_available():
        raise HTTPException(status_code=503, detail={"error": "ffmpeg_not_available", "message": "ffmpeg zaroori hai enhancement ke liye. nixpacks.toml check karein."})
    if preset not in ENHANCE_PRESETS:
        raise HTTPException(status_code=400, detail=f"Invalid preset. Use: {', '.join(ENHANCE_PRESETS)}")
    url = url.strip()
    config = ENHANCE_PRESETS[preset]
    tmp_dir = tempfile.mkdtemp()
    try:
        video_url = await resolve_sora(url)
        src_path = os.path.join(tmp_dir, "original.mp4")
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=20.0, read=600.0, write=60.0, pool=10.0), follow_redirects=True) as client:
            async with client.stream("GET", video_url, headers=VIDEO_HEADERS) as resp:
                resp.raise_for_status()
                with open(src_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=CHUNK_SIZE): f.write(chunk)
        if not Path(src_path).exists() or Path(src_path).stat().st_size == 0:
            raise HTTPException(status_code=500, detail="Original video download failed.")
        enhanced_path = os.path.join(tmp_dir, "enhanced.mp4")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", src_path,
            "-vf", config["vf"], "-c:v", "libx264", "-crf", config["crf"],
            "-preset", config["preset"], "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", "-pix_fmt", "yuv420p", enhanced_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise HTTPException(status_code=500, detail={"error": "enhance_failed", "message": stderr.decode()[-400:] if stderr else "ffmpeg error"})
        enhanced_file = Path(enhanced_path)
        if not enhanced_file.exists() or enhanced_file.stat().st_size == 0:
            raise HTTPException(status_code=500, detail="Enhanced file not created.")
        sid = re.search(r'/p/(s_[a-f0-9]+)', url, re.I)
        base = sid.group(1) if sid else "sora_video"
        out_filename = filename or f"SORA_{datetime.now().strftime('%Y%m%d')}_{base}_{preset}_enhanced.mp4"
        out_filename = re.sub(r'[^\w\-.]', '_', out_filename)[:120]
        file_size = enhanced_file.stat().st_size
        orig_size = Path(src_path).stat().st_size
        async def enhanced_stream():
            try:
                with open(enhanced_file, "rb") as f:
                    while True:
                        chunk = f.read(CHUNK_SIZE)
                        if not chunk: break
                        yield chunk
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        return StreamingResponse(enhanced_stream(), media_type="video/mp4", headers={
            "Content-Disposition": f'attachment; filename="{out_filename}"',
            "Content-Length": str(file_size),
            "Access-Control-Allow-Origin": "*",
            "X-Original-Size": str(orig_size),
            "X-Enhanced-Size": str(file_size),
            "X-Preset": preset,
        })
    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e)[:300])


# ── Thumbnail ─────────────────────────────────────────────────────────
@app.get("/thumbnail")
async def get_thumbnail(url: str = Query(...)):
    url = url.strip()
    platform = detect_platform(url)
    if platform == "sora":
        raise HTTPException(status_code=422, detail="Sora thumbnails not supported.")
    info = await extract_info_only(url, platform)
    thumb_url = info.get("thumbnail")
    if not thumb_url:
        raise HTTPException(status_code=404, detail="Thumbnail not found.")
    async def thumb_stream():
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            async with client.stream("GET", thumb_url) as resp:
                async for chunk in resp.aiter_bytes(chunk_size=65536): yield chunk
    title = info.get("title", "thumbnail")
    fname = smart_filename(title, platform, "jpg")
    return StreamingResponse(thumb_stream(), media_type="image/jpeg", headers={
        "Content-Disposition": f'attachment; filename="{fname}"', "Access-Control-Allow-Origin": "*"})


# ── Subtitles ─────────────────────────────────────────────────────────
@app.get("/subtitles")
async def get_subtitles(url: str = Query(...), lang: str = Query("en")):
    url = url.strip()
    platform = detect_platform(url)
    if platform == "sora":
        raise HTTPException(status_code=422, detail="Sora subtitles not supported.")
    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, "sub.%(ext)s")
    opts = {"skip_download": True, "writesubtitles": True, "writeautomaticsub": True,
            "subtitlesformat": "srt", "subtitleslangs": [lang, "en"], "outtmpl": tmp_path, "quiet": True}
    loop = asyncio.get_event_loop()
    try:
        info = await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(opts).extract_info(url, download=True))
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=f"Subtitle extraction failed: {str(e)[:200]}")
    sub_files = list(Path(tmp_dir).glob("*.srt")) + list(Path(tmp_dir).glob("*.vtt"))
    if not sub_files:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=404, detail="No subtitles found.")
    sub_content = sub_files[0].read_text(encoding="utf-8", errors="ignore")
    title = info.get("title", "subtitle") if info else "subtitle"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return StreamingResponse(iter([sub_content.encode("utf-8")]), media_type="text/plain", headers={
        "Content-Disposition": f'attachment; filename="{smart_filename(title, platform, "srt")}"',
        "Access-Control-Allow-Origin": "*"})


# ── Import URLs ────────────────────────────────────────────────────────
@app.post("/import-urls")
async def import_urls(file: UploadFile = File(...)):
    content = await file.read()
    text = content.decode("utf-8", errors="ignore")
    urls = []
    try:
        reader = csv.reader(io.StringIO(text))
        for row in reader:
            for cell in row:
                cell = cell.strip().strip('"').strip("'")
                if cell.startswith("http"):
                    urls.append(cell)
    except Exception:
        pass
    if not urls:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("http"):
                urls.append(line)
    if not urls:
        raise HTTPException(status_code=422, detail="Koi valid URL nahi mili file mein.")
    result = []
    for url in urls[:50]:
        p = detect_platform(url)
        result.append({"url": url, "platform": p, "platform_name": PLATFORM_NAMES.get(p, "Generic")})
    return JSONResponse(content={"urls": result, "total": len(result)})


# ── ZIP Download (FIXED) ──────────────────────────────────────────────
class ZipRequest(BaseModel):
    urls: List[str]
    username: str = "videos"
    platform: str = "mixed"


async def download_one_for_zip(url: str, idx: int, vid_dir: str) -> dict:
    """Download a single video into vid_dir for ZIP packaging."""
    platform = detect_platform(url)
    pfx = str(idx + 1).zfill(3)

    if platform == "sora":
        try:
            vurl = await resolve_sora(url)
            out_path = os.path.join(vid_dir, f"{pfx}_sora_video.mp4")
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=20.0, read=300.0, write=60.0, pool=10.0),
                follow_redirects=True,
            ) as client:
                async with client.stream("GET", vurl, headers=VIDEO_HEADERS) as resp:
                    resp.raise_for_status()
                    with open(out_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=CHUNK_SIZE):
                            f.write(chunk)
            return {"status": "ok", "file": out_path}
        except Exception as e:
            return {"status": "failed", "error": str(e)[:80]}

    # yt-dlp — use safe title in filename
    out_tmpl = os.path.join(vid_dir, f"{pfx}_%(title).50s.%(ext)s")
    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": get_best_format(platform, "hd"),
        "outtmpl": out_tmpl,
        "merge_output_format": "mp4" if is_ffmpeg_available() else None,
        "socket_timeout": 30,
        "retries": 2,
        "noplaylist": True,
        **PROFILE_YDL_OPTS.get(platform, {}),
    }
    if platform == "youtube":
        opts["extractor_args"] = {"youtube": {"player_client": ["android", "tv_embedded"], "player_skip": ["webpage"]}}
        opts["http_headers"] = {"User-Agent": "com.google.android.youtube/17.36.4 (Linux; U; Android 12; GB) gzip"}

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, lambda o=opts: yt_dlp.YoutubeDL(o).extract_info(url, download=True))
        # Find the file we just downloaded
        matches = sorted(
            [f for f in Path(vid_dir).iterdir() if f.is_file() and f.name.startswith(pfx)],
            key=lambda f: f.stat().st_mtime, reverse=True,
        )
        if matches:
            return {"status": "ok", "file": str(matches[0])}
        return {"status": "failed", "error": "file not found after download"}
    except Exception as e:
        return {"status": "failed", "error": str(e)[:80]}


@app.post("/download-zip")
async def download_zip(request: ZipRequest):
    """
    Download multiple videos and return as a single ZIP file.
    Fixed version: builds ZIP correctly in memory.
    """
    urls = [u.strip() for u in request.urls if u.strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="Koi URL nahi di.")
    if len(urls) > 50:
        raise HTTPException(status_code=400, detail="Max 50 videos per ZIP.")

    safe_user = re.sub(r'[^\w\-]', '_', request.username or "videos")[:30]
    plat_label = re.sub(r'[^\w]', '', request.platform or "mixed").upper()[:10]
    date_str = datetime.now().strftime("%Y%m%d_%H%M")
    zip_filename = f"{plat_label}_{safe_user}_{date_str}_{len(urls)}videos.zip"

    tmp_dir = tempfile.mkdtemp()
    vid_dir = os.path.join(tmp_dir, "videos")
    os.makedirs(vid_dir, exist_ok=True)

    try:
        # Download in batches of 3
        results = []
        for i in range(0, len(urls), 3):
            batch = urls[i:i+3]
            tasks = [download_one_for_zip(url, i+j, vid_dir) for j, url in enumerate(batch)]
            batch_res = await asyncio.gather(*tasks)
            results.extend(batch_res)

        ok_files = [r["file"] for r in results if r.get("status") == "ok" and r.get("file")]
        failed_count = len(urls) - len(ok_files)

        if not ok_files:
            raise HTTPException(status_code=422, detail={
                "error": "all_failed",
                "message": f"Koi bhi video download nahi ho saki. Errors: " +
                           "; ".join(r.get("error","?") for r in results[:3]),
            })

        # Build ZIP in memory
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for fpath_str in ok_files:
                fpath = Path(fpath_str)
                if fpath.exists() and fpath.stat().st_size > 0:
                    arc_name = re.sub(r'[^\w\-. ]', '_', fpath.name)[:80]
                    zf.write(str(fpath), arc_name)

        zip_bytes = zip_buf.getvalue()
        zip_buf.close()

        if len(zip_bytes) < 100:
            raise HTTPException(status_code=500, detail="ZIP file empty — videos download nahi huyi.")

        async def stream_zip():
            MB = 512 * 1024
            for offset in range(0, len(zip_bytes), MB):
                yield zip_bytes[offset:offset + MB]

        return StreamingResponse(
            stream_zip(),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{zip_filename}"',
                "Content-Length": str(len(zip_bytes)),
                "Access-Control-Allow-Origin": "*",
                "X-Total": str(len(urls)),
                "X-Success": str(len(ok_files)),
                "X-Failed": str(failed_count),
            },
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


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
        results.append({"url": url, "platform": p, "platform_name": PLATFORM_NAMES.get(p, "Generic"), "status": "queued"})
    return JSONResponse(content={"results": results, "total": len(results)})
