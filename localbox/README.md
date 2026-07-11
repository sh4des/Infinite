# localbox — Infinite Jukebox for your local music library

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
- ✅ Handles mp3, m4a/aac, ogg/opus, wav directly; transcodes flac/wma/alac/etc.
  to mp3 on the fly (cached).
- ✅ Analysis and transcodes are cached under `/data` — each track is only
  analysed once (a few seconds of CPU the first time).
- ➕ *Optional* acoustic fingerprinting (AcoustID/Chromaprint) to fill in
  title/artist for **untagged** files. See below.
- ❌ It does **not** need or use Spotify/YouTube. Fingerprinting only identifies
  *what* a track is — the remix beat-analysis is always generated locally.

---

## Volumes & ports

| Container path | Purpose | Unraid suggestion |
|---|---|---|
| `/music` | Your music library, **read-only** | `/mnt/user/music` |
| `/data`  | Analysis + transcode cache (persist!) | `/mnt/user/appdata/localbox` |
| `:8080`  | Web UI | host port of your choice |

Environment variables (all optional):

| Var | Default | Meaning |
|---|---|---|
| `ACOUSTID_KEY` | *(empty)* | Enable AcoustID fingerprint fallback. Free key: https://acoustid.org/new-application |
| `FORCE_TRANSCODE` | *(empty)* | Comma list of extensions to always transcode to mp3, e.g. `.flac,.wav` |
| `PORT` | `8080` | Internal port |

---

## Install on Unraid

You are building a **custom image**, so pick one of these two paths.

### Option A — Compose Manager plugin (simplest, builds on the box)

1. Install **Compose Manager** from Community Applications (if not already).
2. Copy this whole `localbox/` folder to the server, e.g.
   `/mnt/user/appdata/localbox-src/`.
3. In Compose Manager: **Add New Stack → localbox**, then paste/point it at the
   included `docker-compose.yml` (edit the `/mnt/user/music` path if yours
   differs). Compose Manager will `build` the image and start it.
4. Open `http://<tower-ip>:8080/`.

### Option B — Build & push an image, then use the Unraid template

1. Build and push to a registry you control (GHCR shown):
   ```bash
   cd localbox
   docker build -t ghcr.io/YOURUSER/localbox:latest .
   docker push ghcr.io/YOURUSER/localbox:latest
   ```
2. Copy `localbox.xml` to the server at
   `/boot/config/plugins/dockerMan/templates-user/my-localbox.xml`.
3. Edit its `<Repository>` line to `ghcr.io/YOURUSER/localbox:latest`.
4. Unraid → **Docker → Add Container → Template: localbox**. Confirm the
   `/music` (read-only) and `/data` paths and the port, then **Apply**.
5. Open the WebUI from the Docker tab.

> The template defaults to mapping `/mnt/user/music` → `/music` read-only and
> `/mnt/user/appdata/localbox` → `/data`.

---

## Run anywhere with docker compose

```bash
cd localbox
# edit docker-compose.yml if your library isn't at /mnt/user/music
docker compose up -d --build
# → http://localhost:8080
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
   `/data/analysis`), and the audio (original, or a cached mp3 transcode with
   HTTP range support).
4. **`static/infinite.js`** builds a per-beat feature vector, finds "edges"
   between similar beats (adaptive threshold, like the original), and schedules
   beat-accurate playback through the Web Audio API, jumping along edges so it
   never ends. `static/player.html` draws the circular beat/edge visualiser.

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
