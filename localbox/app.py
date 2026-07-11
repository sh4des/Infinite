"""
app.py — localbox backend.

Serves:
  GET  /                         -> library browser UI
  GET  /player                   -> the infinite-jukebox player page
  GET  /api/library?path=REL     -> one directory level (folders + audio files)
  GET  /api/search?q=QUERY       -> recursive search across the library
  GET  /api/analyse/{id}         -> JukeboxTrack JSON (cached; librosa on miss)
  GET  /api/audio/{id}           -> audio bytes (original, or transcoded mp3), Range-aware
  GET  /api/track/{id}           -> lightweight metadata for one track
  GET  /healthy                  -> health check

Everything is local. No Spotify, no YouTube, no database.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles

import math

import analyzer
import library


def _finite(obj):
    """Recursively replace NaN/Inf with 0 so the browser's JSON.parse never chokes."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else 0.0
    if isinstance(obj, list):
        return [_finite(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    return obj

MUSIC_DIR = os.environ.get("MUSIC_DIR", "/music")
CACHE_DIR = os.environ.get("CACHE_DIR", "/data")
ACOUSTID_KEY = os.environ.get("ACOUSTID_KEY", "").strip() or None
# Comma list of extensions to always transcode (in addition to non-browser-safe).
FORCE_TRANSCODE = {
    e.strip().lower() for e in os.environ.get("FORCE_TRANSCODE", "").split(",") if e.strip()
}

ANALYSIS_CACHE = os.path.join(CACHE_DIR, "analysis")
AUDIO_CACHE = os.path.join(CACHE_DIR, "audio")
HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")

os.makedirs(ANALYSIS_CACHE, exist_ok=True)
os.makedirs(AUDIO_CACHE, exist_ok=True)

app = FastAPI(title="localbox")

# Per-id locks so a burst of requests for the same track analyzes/transcodes once.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = _locks[key] = threading.Lock()
        return lk


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/player", response_class=HTMLResponse)
def player():
    return FileResponse(os.path.join(STATIC_DIR, "player.html"))


@app.get("/healthy")
def healthy():
    return {"ok": True, "music_dir": MUSIC_DIR}


# --------------------------------------------------------------------------- #
# Library browsing
# --------------------------------------------------------------------------- #
@app.get("/api/library")
def api_library(path: str = ""):
    try:
        return library.list_dir(MUSIC_DIR, path)
    except FileNotFoundError:
        raise HTTPException(404, "folder not found")
    except ValueError:
        raise HTTPException(400, "invalid path")


@app.get("/api/search")
def api_search(q: str = ""):
    return {"results": library.search(MUSIC_DIR, q)}


@app.get("/api/track/{tid}")
def api_track(tid: str):
    path = library.resolve(MUSIC_DIR, tid)
    if not path:
        raise HTTPException(404, "track not found")
    meta = library.read_tags(path)
    name = os.path.basename(path)
    return {
        "id": tid,
        "name": name,
        "title": meta["title"] or os.path.splitext(name)[0],
        "artist": meta["artist"],
        "album": meta["album"],
        "duration": meta["duration"],
    }


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
@app.get("/api/analyse/{tid}")
def api_analyse(tid: str):
    path = library.resolve(MUSIC_DIR, tid)
    if not path:
        raise HTTPException(404, "track not found")

    cache_file = os.path.join(ANALYSIS_CACHE, f"{tid}.json")
    with _lock_for(f"analyse:{tid}"):
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as fh:
                    return JSONResponse(json.load(fh))
            except Exception:
                pass  # corrupt cache -> re-analyze

        try:
            analysis, duration = analyzer.analyze(path)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"analysis failed: {exc}")

        meta = library.read_tags(path)
        if (not meta["title"] or not meta["artist"]) and ACOUSTID_KEY:
            ident = library.identify(path, ACOUSTID_KEY)
            meta["title"] = meta["title"] or ident.get("title", "")
            meta["artist"] = meta["artist"] or ident.get("artist", "")

        name = os.path.basename(path)
        title = meta["title"] or os.path.splitext(name)[0]
        track = {
            "info": {
                "service": "local",
                "id": tid,
                "name": f"{meta['artist']} - {title}".strip(" -") or name,
                "title": title,
                "artist": meta["artist"],
                "url": "",
                "duration": int(duration * 1000),
            },
            "analysis": analysis,
            "audio_summary": {"duration": duration},
        }
        track = _finite(track)
        tmp = cache_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(track, fh)
        os.replace(tmp, cache_file)
        return JSONResponse(track)


# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #
def _audio_path(tid: str, src: str) -> str:
    """Return a path the browser can decode: original if safe, else a cached mp3."""
    ext = os.path.splitext(src)[1].lower()
    if ext in library.BROWSER_SAFE_EXTS and ext not in FORCE_TRANSCODE:
        return src

    out = os.path.join(AUDIO_CACHE, f"{tid}.mp3")
    with _lock_for(f"transcode:{tid}"):
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out
        tmp = out + ".tmp"
        subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-vn", "-map", "a:0",
             "-codec:a", "libmp3lame", "-q:a", "2", tmp],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.replace(tmp, out)
    return out


@app.get("/api/audio/{tid}")
def api_audio(tid: str, request: Request):
    src = library.resolve(MUSIC_DIR, tid)
    if not src:
        raise HTTPException(404, "track not found")
    try:
        path = _audio_path(tid, src)
    except subprocess.CalledProcessError:
        raise HTTPException(500, "transcode failed")

    file_size = os.path.getsize(path)
    ext = os.path.splitext(path)[1].lower()
    mime = {
        ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
        ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
        ".wav": "audio/wav",
    }.get(ext, "application/octet-stream")

    range_header = request.headers.get("range")
    if range_header and range_header.startswith("bytes="):
        try:
            start_s, end_s = range_header[6:].split("-", 1)
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else file_size - 1
        except ValueError:
            raise HTTPException(416, "invalid range")
        start = max(0, start)
        end = min(end, file_size - 1)
        if start > end:
            raise HTTPException(416, "invalid range")
        length = end - start + 1

        def stream_range():
            with open(path, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        }
        return StreamingResponse(stream_range(), status_code=206,
                                 media_type=mime, headers=headers)

    return FileResponse(path, media_type=mime,
                        headers={"Accept-Ranges": "bytes"})


# Static assets (infinite.js, style.css, images). Mounted last so it doesn't
# shadow the API routes above.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
