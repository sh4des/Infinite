"""
app.py — infinite-jukebox backend.

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
  POST /api/stats/play           -> record a track selection
  POST /api/stats/time           -> accumulate listening time (heartbeat/beacon)
  GET  /api/stats                -> lifetime playback stats
  GET  /healthy                  -> health check

Everything is local. No Spotify, no YouTube, no database.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import subprocess
import sys
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles

import library
# analyzer.py is invoked as an isolated subprocess (see _run_analyzer), not
# imported here — so a crash in librosa/numba can never take down the server.

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("infinite-jukebox")


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
# library (a hidden .infinite-jukebox folder) so the fingerprints/analysis travel with
# the music. Set CACHE_IN_LIBRARY=0 to use CACHE_DIR (default /data) instead.
CACHE_IN_LIBRARY = os.environ.get("CACHE_IN_LIBRARY", "1").lower() in ("1", "true", "yes")


def _resolve_cache_dir() -> str:
    if CACHE_IN_LIBRARY:
        candidate = os.path.join(MUSIC_DIR, ".infinite-jukebox")
        try:
            os.makedirs(candidate, exist_ok=True)
            # confirm it is actually writable (mount may still be read-only)
            probe = os.path.join(candidate, ".write_test")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
            return candidate
        except OSError:
            log.warning("%s not writable; falling back to /data. Mount your music "
                        "share read-write to keep the cache with the library.", candidate)
    return os.environ.get("CACHE_DIR", "/data")


CACHE_DIR = _resolve_cache_dir()
ANALYSIS_CACHE = os.path.join(CACHE_DIR, "analysis")
AUDIO_CACHE = os.path.join(CACHE_DIR, "audio")
HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")

os.makedirs(ANALYSIS_CACHE, exist_ok=True)
os.makedirs(AUDIO_CACHE, exist_ok=True)
log.info("cache dir: %s", CACHE_DIR)
log.info("music dir: %s", MUSIC_DIR)

app = FastAPI(title="infinite-jukebox")


@app.middleware("http")
async def _log_requests(request: Request, call_next):
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled error: %s %s", request.method, request.url.path)
        raise
    dt = (time.perf_counter() - t0) * 1000
    lvl = logging.WARNING if response.status_code >= 400 else logging.INFO
    log.log(lvl, "%s %s -> %d (%.0f ms)", request.method,
            request.url.path, response.status_code, dt)
    return response

# Multicore analysis: each analysis runs as an isolated child process
# (analyzer.py as a CLI). Real processes -> real parallelism past the GIL, and —
# crucially — a crash in one analysis (a corrupt file, a native segfault in
# librosa/numba) can only fail that one track. It can never poison a shared pool
# and take down analysis for everything else. A semaphore caps concurrency so we
# use at most ANALYSIS_WORKERS cores at once.
_workers_env = os.environ.get("ANALYSIS_WORKERS", "").strip()
ANALYSIS_WORKERS = int(_workers_env) if _workers_env.isdigit() and int(_workers_env) > 0 \
    else (os.cpu_count() or 2)
_analysis_sem = asyncio.Semaphore(ANALYSIS_WORKERS)
_ANALYZER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analyzer.py")
log.info("analysis workers: %d", ANALYSIS_WORKERS)


async def _run_analyzer(path: str):
    """Analyse `path` in an isolated subprocess; return (analysis, duration)."""
    async with _analysis_sem:
        log.info("analysing: %s", path)
        t0 = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            sys.executable, _ANALYZER, path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        dt = time.perf_counter() - t0
        if proc.returncode != 0 or not out:
            msg = (err.decode(errors="replace").strip() or "analyzer exited abnormally")
            last = msg.splitlines()[-1][:300] if msg else "analyzer failed"
            log.error("analysis FAILED (%.1fs, rc=%s): %s — %s", dt, proc.returncode, path, last)
            raise RuntimeError(last)
        data = json.loads(out)
        a = data["analysis"]
        log.info("analysed in %.1fs: %s (%d beats, %d segments, %.0fs audio)",
                 dt, os.path.basename(path), len(a.get("beats", [])),
                 len(a.get("segments", [])), data["audio_summary"]["duration"])
        return a, data["audio_summary"]["duration"]

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

# -------- lifetime playback stats (persisted, survives redeploys) ----------- #
# Stats live in a DEDICATED persistent dir, NOT in the cache dir. The cache dir
# moves depending on CACHE_IN_LIBRARY (library/.infinite-jukebox vs /data), and
# the library cache can be cleaned — so keeping stats there risks losing them.
# STATE_DIR defaults to /data, which the deploy script / template ALWAYS bind-
# mount to persistent appdata, so stats survive `docker rm`/rebuild/redeploy and
# even toggling CACHE_IN_LIBRARY.
STATE_DIR = os.environ.get("STATE_DIR", "/data")
try:
    os.makedirs(STATE_DIR, exist_ok=True)
except OSError:
    STATE_DIR = CACHE_DIR  # last-resort fallback if /data isn't writable
STATS_FILE = os.path.join(STATE_DIR, "stats.json")
_LEGACY_STATS = os.path.join(CACHE_DIR, "stats.json")  # pre-change location
_stats_lock = threading.Lock()


def _load_stats() -> dict:
    # Prefer the persistent location; migrate legacy stats from the cache dir if
    # present and we don't have a persistent copy yet (so nothing is lost).
    for src in (STATS_FILE, _LEGACY_STATS):
        try:
            with open(src, "r", encoding="utf-8") as fh:
                s = json.load(fh)
                s.setdefault("total_seconds", 0.0)
                s.setdefault("tracks", {})
                if src != STATS_FILE:
                    log.info("migrating playback stats from %s -> %s", src, STATS_FILE)
                return s
        except Exception:
            continue
    return {"total_seconds": 0.0, "tracks": {}}


_stats = _load_stats()


def _save_stats():
    try:
        tmp = STATS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_stats, fh)
        os.replace(tmp, STATS_FILE)
    except OSError as exc:
        log.warning("could not persist stats to %s: %s", STATS_FILE, exc)


# Persist immediately at startup so a migrated/fresh stats file exists in the
# durable location even before the first playback event.
log.info("stats file: %s (%d tracks tracked)", STATS_FILE, len(_stats.get("tracks", {})))
_save_stats()


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/player", response_class=HTMLResponse)
def player():
    return FileResponse(os.path.join(STATIC_DIR, "player.html"))


@app.get("/stats", response_class=HTMLResponse)
def stats_page():
    return FileResponse(os.path.join(STATIC_DIR, "stats.html"))


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

        analysis, duration = await _run_analyzer(path)
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
    # Concurrency is bounded by the global _analysis_sem inside _run_analyzer, so
    # folder-analysis and on-demand plays share the same core budget.
    _batch.update(running=True, total=len(items), done=0)

    async def one(tid: str, path: str):
        try:
            await _ensure_analysis(tid, path)
        except Exception as exc:  # noqa: BLE001
            log.warning("folder analyse failed for %s: %s", tid, exc)
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
# Lifetime stats
# --------------------------------------------------------------------------- #
def _track_stat(tid: str) -> dict:
    t = _stats["tracks"].get(tid)
    if t is None:
        t = _stats["tracks"][tid] = {"title": "", "artist": "", "duration": 0.0,
                                     "plays": 0, "seconds": 0.0}
    return t


@app.post("/api/stats/play")
async def api_stats_play(request: Request):
    """Record that a track was selected/opened for playback."""
    body = await request.json()
    tid = body.get("id")
    if not tid:
        raise HTTPException(400, "missing id")
    with _stats_lock:
        t = _track_stat(tid)
        t["plays"] += 1
        t["title"] = body.get("title") or t["title"]
        t["artist"] = body.get("artist") or t["artist"]
        if body.get("duration"):
            t["duration"] = float(body["duration"])
        _save_stats()
    return {"ok": True}


@app.post("/api/stats/time")
async def api_stats_time(request: Request):
    """Accumulate elapsed listening time for a track (heartbeat / beacon)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "bad body")
    tid = body.get("id")
    secs = float(body.get("seconds") or 0)
    if not tid or secs <= 0:
        return {"ok": True}
    secs = min(secs, 3600)  # guard against absurd deltas from a slept tab
    with _stats_lock:
        t = _track_stat(tid)
        t["seconds"] += secs
        if body.get("duration") and not t["duration"]:
            t["duration"] = float(body["duration"])
        _stats["total_seconds"] += secs
        _save_stats()
    return {"ok": True}


@app.get("/api/stats")
def api_stats():
    with _stats_lock:
        tracks = []
        total_equiv = 0.0
        for tid, t in _stats["tracks"].items():
            dur = t.get("duration") or 0
            equiv = (t["seconds"] / dur) if dur > 0 else 0.0
            total_equiv += equiv
            tracks.append({
                "id": tid, "title": t.get("title") or tid,
                "artist": t.get("artist", ""), "plays": t.get("plays", 0),
                "seconds": round(t.get("seconds", 0.0), 1),
                "equiv": round(equiv, 2),
            })
        by_plays = sorted(tracks, key=lambda x: (-x["plays"], -x["seconds"]))[:25]
        by_time = sorted(tracks, key=lambda x: -x["seconds"])[:25]
        return {
            "total_seconds": round(_stats["total_seconds"], 1),
            "total_equivalent_plays": round(total_equiv, 1),
            "distinct_tracks": len(tracks),
            "top_by_plays": by_plays,
            "top_by_time": by_time,
        }


# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #
def _audio_path(tid: str, src: str, force: bool = False) -> str:
    """Return a path the browser can decode: original if safe, else a cached mp3.

    `force=True` always returns the mp3 transcode — used as an iOS/Safari
    fallback, since Web Audio there can't decode ogg/opus/flac."""
    ext = os.path.splitext(src)[1].lower()
    if not force and ext in library.BROWSER_SAFE_EXTS and ext not in FORCE_TRANSCODE:
        return src

    out = os.path.join(AUDIO_CACHE, f"{tid}.mp3")
    with _lock_for(f"transcode:{tid}"):
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out
        log.info("transcoding to mp3: %s", os.path.basename(src))
        t0 = time.perf_counter()
        tmp = out + ".tmp"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", src, "-vn", "-map", "a:0",
                 "-codec:a", "libmp3lame", "-q:a", "2", tmp],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            log.error("transcode FAILED: %s", src)
            raise
        os.replace(tmp, out)
        log.info("transcoded in %.1fs: %s", time.perf_counter() - t0, os.path.basename(src))
    return out


@app.get("/api/audio/{tid}")
def api_audio(tid: str, request: Request, transcode: int = 0):
    src = library.resolve(MUSIC_DIR, tid)
    if not src:
        raise HTTPException(404, "track not found")
    try:
        path = _audio_path(tid, src, force=bool(transcode))
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
