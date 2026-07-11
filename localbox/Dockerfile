# localbox — Infinite Jukebox for a local music library.
#
# Debian-slim base (glibc) so librosa/numpy/scipy install from prebuilt wheels
# without a compiler, and ffmpeg + libsndfile are available for decoding/transcode.
FROM python:3.11-slim

# System deps:
#   ffmpeg      - decode odd formats + transcode to mp3 for the browser
#   libsndfile1 - soundfile backend for librosa
#   libchromaprint-tools - provides `fpcalc` for the optional AcoustID path
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libsndfile1 \
        libchromaprint-tools \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY analyzer.py library.py app.py ./
COPY static ./static

ENV MUSIC_DIR=/music \
    CACHE_DIR=/data \
    PORT=8080 \
    ACOUSTID_KEY="" \
    FORCE_TRANSCODE=""

# Pre-create mount points and prime the numba/librosa cache dir.
RUN mkdir -p /music /data
VOLUME ["/music", "/data"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -fsS http://localhost:${PORT}/healthy || exit 1

# Single worker: analysis is CPU-bound and cached, and the per-id locks assume
# one process. Bump with a reverse proxy if you really need concurrency.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT} --workers 1"]
