# infinite-jukebox — Infinite Jukebox for your local music library

A self-contained container that turns **any file in your music library** into an
infinite, never-repeating remix — the [Infinite Jukebox](https://labs.echonest.com/Uploader/index.html)
effect, applied to your own files, **fully offline**. No Spotify, no YouTube, no
database.

Point it at `/mnt/user/music`, open the web UI, browse or search your library,
click a track. The app analyses the song locally with **librosa** (beats, bars,
sections, and per-beat pitch/timbre), builds a graph of "beats that sound alike",
and plays through it forever, seamlessly jumping between similar beats.

This lives alongside the original EternalJukebox in the parent folder; it's a
purpose-built local-library variant, not a fork of the Java app.

---

## What it does / doesn't do

- ✅ Browse and search your whole library (reads ID3 / Vorbis / MP4 tags).
- ✅ Infinite-remix playback of **any** local file, generated on-device.
- ✅ **Natural jumps** — splices only happen between beats at the same position
  in the bar (downbeat→downbeat) with similar energy, and every jump is an
  equal-power crossfade rather than a hard cut. A jump cooldown keeps it musical
  instead of frantic.
- ✅ **Autoplay** — a track starts as soon as it loads (with a one-tap fallback
  if your browser blocks autoplay).
- ✅ **Shuffle** three ways, each as a randomized queue with a **Next ⏭** button:
  *this folder* (recursive), *everywhere*, and *5-star* (from your rating tags).
- ✅ **Auto-advance timer** — optionally jump to the next shuffled track every
  1/3/5/10/30 minutes.
- ✅ **Lifetime stats** (`/stats`) — total time played, full-song-equivalent
  loops, and your most-selected / most-looped tracks. Persisted next to the cache.
- ✅ **Mobile Safari / iOS** — responsive portrait layout, tap-to-start for
  autoplay, and an automatic mp3 transcode fallback for formats iOS Web Audio
  can't decode (ogg/opus/flac).
- ✅ Handles mp3, m4a/aac, ogg/opus, wav directly; transcodes flac/wma/alac/etc.
  to mp3 on the fly (cached).
- ✅ Analysis and transcodes are cached **inside your library** at
  `<music>/.infinite-jukebox` — the fingerprints travel with the music, and each track
  is only analysed once (a few seconds of CPU the first time).
- ➕ *Optional* acoustic fingerprinting (AcoustID/Chromaprint) to fill in
  title/artist for **untagged** files. See below.
- ❌ It does **not** need or use Spotify/YouTube. Fingerprinting only identifies
  *what* a track is — the remix beat-analysis is always generated locally.

---

## Volumes & ports

| Container path | Purpose | Unraid suggestion |
|---|---|---|
| `/music` | Your music library — **read-write** (only `.infinite-jukebox` is written) | `/mnt/user/music` |
| `/data`  | **Playback stats (always)** + fallback cache when `CACHE_IN_LIBRARY=0` | `/mnt/user/appdata/infinite-jukebox` |
| `:8239`  | Web UI (host port; container listens on 8080) | `8239` |

> **Why read-write?** By default the analysis/fingerprint cache is stored at
> `<music>/.infinite-jukebox` so it travels with your library. The app only ever writes
> to that one hidden folder (which the browser hides from you). If you'd rather
> keep your music mount read-only, set `CACHE_IN_LIBRARY=0` and it uses `/data`.

Environment variables (all optional):

| Var | Default | Meaning |
|---|---|---|
| `CACHE_IN_LIBRARY` | `1` | `1` = cache in `<music>/.infinite-jukebox` (needs rw mount). `0` = cache in `/data` (mount `/music` read-only). |
| `STATE_DIR` | `/data` | Where lifetime playback stats are stored. Always the persistent `/data` mount, independent of the cache — so stats survive redeploys. |
| `ANALYSIS_SR` | `44100` | Analysis sample rate. Higher = more pitch/timbre detail + more CPU. |
| `ANALYSIS_WORKERS` | *(all cores)* | Number of parallel analysis worker processes. Blank = `os.cpu_count()`. |
| `ACOUSTID_KEY` | *(empty)* | Enable AcoustID fingerprint fallback. Free key: https://acoustid.org/new-application |
| `FORCE_TRANSCODE` | *(empty)* | Comma list of extensions to always transcode to mp3, e.g. `.flac,.wav` |
| `LOG_LEVEL` | `info` | `debug`/`info`/`warning`/`error` — controls app + uvicorn logging verbosity. |
| `PORT` | `8080` | Container-internal port (the deploy script publishes host `8239` → this). |

## Data persistence

Lifetime playback stats are stored at `/data/stats.json` — the `/data` mount is
**always** bind-mounted to persistent appdata (`/mnt/user/appdata/infinite-jukebox`),
so stats survive container recreation, image rebuilds, redeploys, and even
toggling `CACHE_IN_LIBRARY`. On first start after upgrading, any stats found in
the old cache location are migrated automatically, so nothing is lost. (The only
thing that would orphan stats is changing the host `/data` path itself.)

## Logging & troubleshooting

- **Container logs** are detailed: every HTTP request is logged with status and
  timing, and each analysis/transcode logs start, duration, and beat/segment
  counts (or the exact failure). View them from the Unraid Docker page (the
  container's **Logs** action) or:
  ```bash
  ssh root@10.0.23.105 docker logs -f infinite-jukebox
  ```
  Set `LOG_LEVEL=debug` for even more. Analysis failures are logged per track,
  in isolation — one bad file can't take the server down.
- **WebUI button on the Docker page**: the container is labelled so Unraid shows
  a clickable **WebUI** icon straight from the Docker tab (no template needed).
- **Browser errors**: every client-side error is written to the browser
  **developer tools** console with an `[infinite-jukebox]` prefix, and *critical*
  errors (analysis failed, audio couldn't decode, shuffle failed, uncaught JS)
  also appear as a dismissible red banner on the page itself.

## Analysis quality & CPU

The analyzer runs **HPSS** (harmonic/percussive source separation) on every
track: beats are tracked from the *percussive* signal (crisp, not fooled by
sustained chords) and pitch/chroma is read from the *harmonic* signal (cleaner
tonal content). It analyses at 44.1 kHz by default. This is deliberately
CPU-heavy for better remixes; each track is analysed once and cached.

Analysis happens **on demand** — a track is analysed the first time you open it
(if not already cached), then reused instantly forever after. Nothing analyses
your library in the background.

Analysis is **multicore** and **crash-isolated** — each analysis runs as its own
short-lived subprocess (`analyzer.py` as a CLI), capped at `ANALYSIS_WORKERS`
concurrent (all cores by default). Because every analysis is its own process, a
bad file or a native crash in librosa can only fail that one track; it can never
poison a shared worker pool and stop analysis for everything else. If you'd rather warm a folder ahead
of time, the **🎛 Analyze this folder** button on the home page analyses every
not-yet-cached track under the folder you're viewing (recursive) across all
cores, with live progress; or `curl -XPOST 'http://TOWER:8239/api/analyse-folder?path=Artist/Album'`.

Tuning: lower `ANALYSIS_SR` (e.g. `22050`) or cap `ANALYSIS_WORKERS` if you want
infinite-jukebox to leave headroom for other Unraid workloads.

## Shuffle & 5-star ratings

The library page has three shuffle buttons. Each opens the player with a
randomized queue you advance with **Next ⏭**:

- **Shuffle this folder** — every track under the folder you're viewing (recursive).
- **Shuffle everywhere** — your entire library.
- **Shuffle 5-star** — only tracks your tags rate 5 stars.

5-star detection is best-effort across the common rating encodings: ID3 `POPM`
(the Windows Media / iTunes 0–255 → 5-star mapping), Vorbis `RATING` (0–100) and
`FMPS_RATING` (0.0–1.0), and the MP4 `rate` atom. If a format stores ratings
somewhere exotic, those tracks just won't be picked up.

---

## Install on Unraid

You are building a **custom image**, so pick one of these two paths.

### Option A — Compose Manager plugin (simplest, builds on the box)

1. Install **Compose Manager** from Community Applications (if not already).
2. Copy this whole `infinite-jukebox/` folder to the server, e.g.
   `/mnt/user/appdata/infinite-jukebox-src/`.
3. In Compose Manager: **Add New Stack → infinite-jukebox**, then paste/point it at the
   included `docker-compose.yml` (edit the `/mnt/user/music` path if yours
   differs). Compose Manager will `build` the image and start it.
4. Open `http://<tower-ip>:8239/`.

### Option B — Build & push an image, then use the Unraid template

1. Build and push to a registry you control (GHCR shown):
   ```bash
   cd infinite-jukebox
   docker build -t ghcr.io/YOURUSER/infinite-jukebox:latest .
   docker push ghcr.io/YOURUSER/infinite-jukebox:latest
   ```
2. Copy `infinite-jukebox.xml` to the server at
   `/boot/config/plugins/dockerMan/templates-user/my-infinite-jukebox.xml`.
3. Edit its `<Repository>` line to `ghcr.io/YOURUSER/infinite-jukebox:latest`.
4. Unraid → **Docker → Add Container → Template: infinite-jukebox**. Confirm the
   `/music` (read-write) and `/data` paths and the port, then **Apply**.
5. Open the WebUI from the Docker tab.

> The template defaults to mapping `/mnt/user/music` → `/music` read-write (for
> the `.infinite-jukebox` cache) and `/mnt/user/appdata/infinite-jukebox` → `/data` (fallback).

---

## Run anywhere with docker compose

```bash
cd infinite-jukebox
# edit docker-compose.yml if your library isn't at /mnt/user/music
docker compose up -d --build
# → http://localhost:8239
```

---

## How it works (short version)

1. **`library.py`** scans `/music`, gives each file a stable id (`local-<hash>`
   of its path), and reads tags with mutagen.
2. **`analyzer.py`** runs librosa to produce a Spotify-audio-analysis-shaped
   object: `beats`, `bars`, `tatums`, `sections`, and `segments` carrying a
   12-bin chroma vector (`pitches`) and 12 MFCC coefficients (`timbre`). Those
   two vectors are what make beats comparable.
3. **`app.py`** (FastAPI) serves the browser UI, the analysis JSON (cached to
   `<music>/.infinite-jukebox/analysis`), the audio (original, or a cached mp3 transcode
   with HTTP range support), and the `/api/shuffle` queues.
4. **`static/infinite.js`** builds a per-beat feature vector, finds "edges"
   between similar beats — gated so a jump lands on the **same beat position in
   the bar** with **similar loudness**, then ranked by a short forward-window
   similarity — and schedules beat-accurate playback through the Web Audio API
   with an **equal-power crossfade** at each splice and a jump cooldown, so it
   loops forever and the jumps sound musical. `static/player.html` draws the
   circular beat/edge visualiser and drives autoplay + the shuffle queue.

---

## Notes & tuning

- **First play is slow-ish** (a few seconds of analysis); replays are instant
  from cache. To pre-warm your library you can hit `/api/analyse/<id>` for each
  track, but on-demand is usually fine.
- **flac/wav** decode fine in Chromium browsers but can be spotty in Safari; set
  `FORCE_TRANSCODE=.flac,.wav` if you hit playback issues.
- **CPU**: analysis is single-threaded per request and the container runs one
  worker (the per-track locks assume one process). That's intentional and
  plenty for a home server.
- **"Jump chance"** slider in the player controls how adventurous the remix is;
  higher = more frequent jumps.
- Untagged files show up under their filename. Add an `ACOUSTID_KEY` to have the
  bundled `fpcalc` fingerprint them and fill in title/artist.
