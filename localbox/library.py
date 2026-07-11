"""
library.py — scan the mounted music library, expose a browsable tree, and turn
each file into a stable id with metadata.

Track identity comes from, in order of preference:
  1. The file's own tags (ID3 / Vorbis / MP4) via mutagen.
  2. Optional AcoustID/Chromaprint fingerprint -> MusicBrainz, IF an ACOUSTID_KEY
     is configured AND the `fpcalc` binary is present. This is the real acoustic
     fingerprinting path, used to fill in title/artist for untagged files.
  3. The filename.

Note: fingerprinting only identifies *what* a track is. The infinite-remix beat
analysis always comes from analyzer.py — no online service provides that for
arbitrary local files.
"""

from __future__ import annotations

import hashlib
import os
from functools import lru_cache

AUDIO_EXTS = {
    ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus",
    ".wav", ".wma", ".alac", ".aiff", ".aif",
}

# Formats a browser's Web Audio decodeAudioData handles directly. Anything else
# gets transcoded to mp3 on demand by the backend.
BROWSER_SAFE_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav"}


def track_id(music_dir: str, abs_path: str) -> str:
    """Stable id derived from the path relative to the library root."""
    rel = os.path.relpath(abs_path, music_dir)
    h = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:20]
    return f"local-{h}"


def _safe_join(root: str, rel: str) -> str:
    """Join and confirm the result stays within `root` (no path traversal)."""
    rel = (rel or "").lstrip("/")
    target = os.path.realpath(os.path.join(root, rel))
    root_real = os.path.realpath(root)
    if target != root_real and not target.startswith(root_real + os.sep):
        raise ValueError("path escapes library root")
    return target


def list_dir(music_dir: str, rel: str = "") -> dict:
    """List one directory level: subfolders + audio files (with ids/metadata)."""
    target = _safe_join(music_dir, rel)
    if not os.path.isdir(target):
        raise FileNotFoundError(rel)

    folders, files = [], []
    with os.scandir(target) as it:
        for entry in it:
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                folders.append({
                    "name": entry.name,
                    "path": os.path.relpath(entry.path, music_dir),
                })
            elif entry.is_file():
                ext = os.path.splitext(entry.name)[1].lower()
                if ext in AUDIO_EXTS:
                    meta = read_tags(entry.path)
                    files.append({
                        "id": track_id(music_dir, entry.path),
                        "name": entry.name,
                        "path": os.path.relpath(entry.path, music_dir),
                        "title": meta["title"] or os.path.splitext(entry.name)[0],
                        "artist": meta["artist"],
                        "album": meta["album"],
                        "ext": ext,
                    })
    folders.sort(key=lambda f: f["name"].lower())
    files.sort(key=lambda f: (f["artist"].lower(), f["title"].lower()))
    parent = os.path.dirname(rel.rstrip("/")) if rel else None
    return {"path": rel, "parent": parent, "folders": folders, "files": files}


@lru_cache(maxsize=4096)
def _index(music_dir: str) -> tuple:
    """Full recursive index: id -> absolute path. Cached; call clear_index() to refresh."""
    mapping = {}
    for dirpath, _dirs, filenames in os.walk(music_dir):
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in AUDIO_EXTS:
                p = os.path.join(dirpath, fn)
                mapping[track_id(music_dir, p)] = p
    return tuple(mapping.items())


def clear_index():
    _index.cache_clear()


def resolve(music_dir: str, tid: str) -> str | None:
    """Map a track id back to its absolute file path (refreshing index if missing)."""
    mapping = dict(_index(music_dir))
    if tid in mapping and os.path.exists(mapping[tid]):
        return mapping[tid]
    clear_index()
    mapping = dict(_index(music_dir))
    return mapping.get(tid)


def search(music_dir: str, query: str, limit: int = 100) -> list[dict]:
    q = query.strip().lower()
    if not q:
        return []
    results = []
    for tid, path in _index(music_dir):
        meta = read_tags(path)
        hay = " ".join([
            meta["title"] or os.path.basename(path),
            meta["artist"], meta["album"], os.path.basename(path),
        ]).lower()
        if q in hay:
            results.append({
                "id": tid,
                "path": os.path.relpath(path, music_dir),
                "title": meta["title"] or os.path.splitext(os.path.basename(path))[0],
                "artist": meta["artist"],
                "album": meta["album"],
                "ext": os.path.splitext(path)[1].lower(),
            })
            if len(results) >= limit:
                break
    results.sort(key=lambda f: (f["artist"].lower(), f["title"].lower()))
    return results


def read_tags(path: str) -> dict:
    """Best-effort title/artist/album/duration from embedded tags."""
    title = artist = album = ""
    duration = 0.0
    try:
        from mutagen import File as MutagenFile

        mf = MutagenFile(path, easy=True)
        if mf is not None:
            if getattr(mf, "info", None) is not None:
                duration = float(getattr(mf.info, "length", 0.0) or 0.0)
            tags = mf.tags or {}

            def first(*keys):
                for k in keys:
                    v = tags.get(k)
                    if v:
                        return str(v[0]) if isinstance(v, list) else str(v)
                return ""

            title = first("title")
            artist = first("artist", "albumartist")
            album = first("album")
    except Exception:
        pass
    return {"title": title, "artist": artist, "album": album, "duration": duration}


def identify(path: str, acoustid_key: str | None) -> dict:
    """Optional AcoustID fingerprint lookup to fill missing tags. Silent no-op
    if disabled, unavailable, or nothing matches."""
    if not acoustid_key:
        return {}
    try:
        import acoustid  # pyacoustid; requires the `fpcalc` binary on PATH

        for score, rid, title, artist in acoustid.match(acoustid_key, path):
            return {"title": title or "", "artist": artist or "",
                    "musicbrainz_recording_id": rid, "acoustid_score": score}
    except Exception:
        return {}
    return {}
