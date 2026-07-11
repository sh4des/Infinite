"""
app.py — localbox backend.

Serves:
  GET  /                         -> library browser UI
  GET  /player                   -> the infinite-jukebox player page
  GET  /api/library?path=REL     -> one directory level (folders + audio files)
  GET  /api/search?q=QUERY       -> recursive search across the library
  GET  /api/analyse/{id}         -> JukeboxTrack JSON (cached; librosa on a worker core on miss)
  GET  /api/audio/{id}           -> audio bytes (original, or transcoded mp3), Range-aware
  GET  /api/track/{id}           -> lightweight metadata for one track
  POST /api/analyse-folder?path= -> analyse all uncached tracks in a folder across cores
  GET  /api/analyse-status       -> progress of the current folder analysis
  GET  /healthy                  -> health check

Everything is local. No Spotify, no YouTube, no database.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import subprocess
import threading
from concurrent.futures import ProcessPoolExecutor

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles

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
ACOUSTID_KEY = os.environ.get("ACOUSTID_KEY", "").strip() or None
# Comma list of extensions to always transcode (in addition to non-browser-safe).
FORCE_TRANSCODE = {
    e.strip().lower() for e in os.environ.get("FORCE_TRANSCODE", "").split(",") if e.strip()
}

# Where the analysis + transcode cache lives. By default it goes *inside* the
# library (a hidden .localbox folder) so the fingerprints/analysis travel with
# the music. Set CACHE_IN_LIBRARY=0 to use CACHE_DIR (default /data) instead.
CACHE_IN_LIBRARY = os.environ.get("CACHE_IN_LIBRARY", "1").lower() in ("1", "true", "yes")


def _resolve_cache_dir() -> str:
    if CACHE_IN_LIBRARY:
        candidate = os.path.join(MUSIC_DIR, ".localbox")
        try:
            os.makedirs(candidate, exist_ok=True)
            # confirm it is actually writable (mount may still be read-only)
            probe = os.path.join(candidate, ".write_test")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
            return candidate
        except OSError:
            print(f"[localbox] {candidate} not writable; falling back to /data. "
                  f"Mount your music share read-write to keep the cache with the library.")
    return os.environ.get("CACHE_DIR", "/data")


CACHE_DIR = _resolve_cache_dir()
ANALYSIS_CACHE = os.path.join(CACHE_DIR, "analysis")
AUDIO_CACHE = os.path.join(CACHE_DIR, "audio")
HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")

os.makedirs(ANALYSIS_CACHE, exist_ok=True)
os.makedirs(AUDIO_CACHE, exist_ok=True)
print(f"[localbox] cache dir: {CACHE_DIR}")

app = FastAPI(title="localbox")

# Multicore analysis: a pool of worker processes so several tracks (or one big
# pre-warm run) can analyse on separate CPU cores at once. Each worker is a real
# OS process, sidestepping the GIL.
_workers_env = os.environ.get("ANALYSIS_WORKERS", "").strip()
ANALYSIS_WORKERS = int(_workers_env) if _workers_env.isdigit() and int(_workers_env) > 0 \
    else (os.cpu_count() or 2)
_pool = ProcessPoolExecutor(max_workers=ANALYSIS_WORKERS)
print(f"[localbox] analysis workers: {ANALYSIS_WORKERS}")

# Per-id threading locks (used for on-the-fly transcode dedupe).
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = _locks[key] = threading.Lock()
        return lk


# Per-id async locks so concurrent requests for the same track analyse once.
_alocks: dict[str, asyncio.Lock] = {}
_alocks_guard = threading.Lock()


def _alock_for(key: str) -> asyncio.Lock:
    with _alocks_guard:
        lk = _alocks.get(key)
        if lk is None:
            lk = _alocks[key] = asyncio.Lock()
        return lk


# Live progress of a folder (batch) analysis run.
_batch = {"running": False, "path": None, "total": 0, "done": 0}


@app.on_event("shutdown")
def _shutdown_pool():
    _pool.shutdown(wait=False, cancel_futures=True)


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


@app.get("/api/shuffle")
def api_shuffle(scope: str = "all", path: str = ""):
    """Return a shuffled queue of tracks for a scope: folder | all | stars."""
    if scope not in ("folder", "all", "stars"):
        raise HTTPException(400, "scope must be folder, all or stars")
    try:
        tracks = library.list_scope(MUSIC_DIR, scope, path)
    except (FileNotFoundError, ValueError):
        raise HTTPException(400, "invalid path")
    random.shuffle(tracks)
    return {"scope": scope, "path": path, "count": len(tracks), "tracks": tracks}


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
# Analysis (multicore)
# --------------------------------------------------------------------------- #
def _cache_file(tid: str) -> str:
    return os.path.join(ANALYSIS_CACHE, f"{tid}.json")


def _read_cache(tid: str):
    cf = _cache_file(tid)
    if os.path.exists(cf):
        try:
            with open(cf, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None  # corrupt -> re-analyze
    return None


def _build_track(tid: str, path: str, analysis: dict, duration: float) -> dict:
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
    return _finite(track)


async def _ensure_analysis(tid: str, path: str) -> dict:
    """Return the JukeboxTrack for a track, analysing on a worker core if needed."""
    cached = _read_cache(tid)
    if cached is not None:
        return cached

    async with _alock_for(tid):
        cached = _read_cache(tid)  # re-check after acquiring the lock
        if cached is not None:
            return cached

        loop = asyncio.get_running_loop()
        analysis, duration = await loop.run_in_executor(_pool, analyzer.analyze, path)
        track = _build_track(tid, path, analysis, duration)

        tmp = _cache_file(tid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(track, fh)
        os.replace(tmp, _cache_file(tid))
        return track


@app.get("/api/analyse/{tid}")
async def api_analyse(tid: str):
    path = library.resolve(MUSIC_DIR, tid)
    if not path:
        raise HTTPException(404, "track not found")
    try:
        track = await _ensure_analysis(tid, path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"analysis failed: {exc}")
    return JSONResponse(track)


async def _batch_run(items: list[tuple[str, str]]):
    _batch.update(running=True, total=len(items), done=0)
    sem = asyncio.Semaphore(ANALYSIS_WORKERS)

    async def one(tid: str, path: str):
        async with sem:
            try:
                await _ensure_analysis(tid, path)
            except Exception as exc:  # noqa: BLE001
                print(f"[localbox] folder analyse failed for {tid}: {exc}")
            finally:
                _batch["done"] += 1

    try:
        await asyncio.gather(*(one(t, p) for t, p in items))
    finally:
        _batch["running"] = False


@app.post("/api/analyse-folder")
async def api_analyse_folder(path: str = ""):
    """Analyse every not-yet-cached track under `path` (recursive) across cores.

    Returns immediately; poll /api/analyse-status for progress."""
    if _batch["running"]:
        return {"status": "already running", **_batch}
    try:
        tracks = library.list_scope(MUSIC_DIR, "folder", path)
    except (FileNotFoundError, ValueError):
        raise HTTPException(400, "invalid path")

    items = [
        (t["id"], os.path.join(MUSIC_DIR, t["path"]))
        for t in tracks
        if not os.path.exists(_cache_file(t["id"]))
    ]
    _batch.update(path=path, total=len(items), done=0)
    if items:
        asyncio.create_task(_batch_run(items))
    return {"status": "started", "path": path,
            "queued": len(items), "already_cached": len(tracks) - len(items),
            "workers": ANALYSIS_WORKERS}


@app.get("/api/analyse-status")
def api_analyse_status():
    return _batch


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
