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
                        "disc": meta["disc"],
                        "track": meta["track"],
                        "ext": ext,
                    })
    folders.sort(key=lambda f: f["name"].lower())
    # Album order: by disc then track number, then filename. Tracks without a
    # number sort last (large sentinel) so numbered tracks lead in album order.
    files.sort(key=lambda f: (f["disc"] or 1,
                              f["track"] if f["track"] else 10_000,
                              f["name"].lower()))
    parent = os.path.dirname(rel.rstrip("/")) if rel else None
    return {"path": rel, "parent": parent, "folders": folders, "files": files}


@lru_cache(maxsize=4096)
def _index(music_dir: str) -> tuple:
    """Full recursive index: id -> absolute path. Cached; call clear_index() to refresh.

    Hidden folders (notably the .infinite-jukebox cache) are skipped so cached
    transcodes never masquerade as library tracks."""
    mapping = {}
    for dirpath, dirs, filenames in os.walk(music_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
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


def _lead_int(s: str):
    """Parse the leading integer from tags like '3', '3/12'. None if absent."""
    if not s:
        return None
    num = ""
    for ch in str(s).strip():
        if ch.isdigit():
            num += ch
        else:
            break
    return int(num) if num else None


def read_tags(path: str) -> dict:
    """Best-effort title/artist/album/duration/track/disc from embedded tags."""
    title = artist = album = ""
    duration = 0.0
    track = disc = None
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
            track = _lead_int(first("tracknumber", "track"))
            disc = _lead_int(first("discnumber", "disc"))
    except Exception:
        pass
    return {"title": title, "artist": artist, "album": album,
            "duration": duration, "track": track, "disc": disc}


def read_rating(path: str):
    """Best-effort star rating (0-5) from tags, or None if not rated.

    Handles the common encodings:
      - ID3 POPM (Popularimeter), 0-255, Windows-Media/iTunes 5-star mapping
      - Vorbis (FLAC/OGG): RATING 0-100, or FMPS_RATING 0.0-1.0
      - MP4/M4A: 'rate' atom 0-100
    """
    try:
        from mutagen import File as MutagenFile

        raw = MutagenFile(path)
        if raw is None:
            return None
        tags = raw.tags
        if not tags:
            return None

        # ID3 POPM (key looks like "POPM:user@host" or "POPM:")
        for key in getattr(tags, "keys", lambda: [])():
            if str(key).upper().startswith("POPM"):
                popm = tags[key]
                val = getattr(popm, "rating", None)
                if val is not None:
                    return _popm_to_stars(int(val))

        # Vorbis comments (FLAC / OGG) — tags behave like a dict of lists
        def _vorbis(name):
            try:
                v = tags.get(name) or tags.get(name.lower()) or tags.get(name.upper())
                if v:
                    return str(v[0]) if isinstance(v, list) else str(v)
            except Exception:
                return None
            return None

        fmps = _vorbis("FMPS_RATING")
        if fmps is not None:
            try:
                return int(round(float(fmps) * 5))
            except ValueError:
                pass
        rating = _vorbis("RATING")
        if rating is not None:
            try:
                r = float(rating)
                return int(round(r / 20.0)) if r > 5 else int(round(r))
            except ValueError:
                pass

        # MP4 / M4A rating atom (0-100)
        try:
            if "rate" in tags:
                r = tags["rate"]
                r = r[0] if isinstance(r, list) else r
                return int(round(float(r) / 20.0))
        except Exception:
            pass
    except Exception:
        return None
    return None


def _popm_to_stars(v: int) -> int:
    """Map an ID3 POPM 0-255 byte to 0-5 stars (WMP/iTunes convention)."""
    if v <= 0:
        return 0
    if v <= 31:
        return 1
    if v <= 95:
        return 2
    if v <= 159:
        return 3
    if v <= 223:
        return 4
    return 5


def list_scope(music_dir: str, scope: str, path: str = "") -> list[dict]:
    """Enumerate tracks for a shuffle scope: 'folder' (recursive under path),
    'all' (whole library), or 'stars' (rating == 5). Returns light dicts."""
    if scope == "folder":
        root = _safe_join(music_dir, path)
        paths = []
        for dirpath, dirs, filenames in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                if os.path.splitext(fn)[1].lower() in AUDIO_EXTS:
                    paths.append(os.path.join(dirpath, fn))
    else:
        paths = [p for _tid, p in _index(music_dir)]

    out = []
    want_stars = scope == "stars"
    for p in paths:
        if want_stars and read_rating(p) != 5:
            continue
        name = os.path.basename(p)
        meta = read_tags(p) if (scope != "all" or want_stars) else None
        out.append({
            "id": track_id(music_dir, p),
            "title": (meta["title"] if meta and meta["title"] else os.path.splitext(name)[0]),
            "artist": meta["artist"] if meta else "",
            "path": os.path.relpath(p, music_dir),
        })
    return out


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
