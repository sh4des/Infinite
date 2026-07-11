# infinite-jukebox — Infinite Jukebox for a local music library.
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
    STATE_DIR=/data \
    CACHE_IN_LIBRARY=1 \
    PORT=8080 \
    ACOUSTID_KEY="" \
    FORCE_TRANSCODE="" \
    ANALYSIS_SR=44100 \
    ANALYSIS_WORKERS="" \
    LOG_LEVEL=info
# ANALYSIS_WORKERS: blank = use all CPU cores. ANALYSIS_SR: analysis sample rate
# (higher = more detail + more CPU). HPSS separation is always on.
# LOG_LEVEL: debug|info|warning|error — controls both app + uvicorn logging.

# Pre-create mount points and prime the numba/librosa cache dir.
RUN mkdir -p /music /data
VOLUME ["/music", "/data"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -fsS http://localhost:${PORT}/healthy || exit 1

# Single uvicorn worker (analysis parallelism comes from subprocess workers, and
# the in-memory locks/stats assume one process). --access-log + our request
# middleware give detailed per-request logging; LOG_LEVEL tunes verbosity.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT} --workers 1 --access-log --log-level $(echo ${LOG_LEVEL:-info} | tr '[:upper:]' '[:lower:]')"]
