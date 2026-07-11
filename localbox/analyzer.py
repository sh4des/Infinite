"""
analyzer.py — generate Spotify-shaped audio analysis for a local file using librosa.

The EternalJukebox / Infinite-Jukebox front end is driven entirely by a
"JukeboxTrack" object of the shape:

    {
      "info":    { service, id, name, title, artist, url, duration },
      "analysis": {
          "sections": [ {start, duration, confidence, loudness, tempo,
                         tempo_confidence, key, key_confidence, mode,
                         mode_confidence, time_signature,
                         time_signature_confidence} ],
          "bars":     [ {start, duration, confidence} ],
          "beats":    [ {start, duration, confidence} ],
          "tatums":   [ {start, duration, confidence} ],
          "segments": [ {start, duration, confidence, loudness_start,
                         loudness_max_time, loudness_max,
                         pitches[12], timbre[12]} ]
      },
      "audio_summary": { duration }
    }

Spotify normally produces this from its own hosted analysis. Here we regenerate
the same structure locally with librosa so the infinite-remix effect works on
*any* file on disk, with no external service.

The two fields that actually drive the infinite jump graph are the per-segment
`pitches` (12-bin chroma, 0..1) and `timbre` (12 MFCC-ish coefficients). We take
care to make those meaningful; everything else is best-effort but well-formed.
"""

from __future__ import annotations

import os

import numpy as np
import librosa


# Krumhansl-Schmuckler key profiles, used for a best-effort key/mode estimate.
_MAJOR_PROFILE = np.array(
    [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
)
_MINOR_PROFILE = np.array(
    [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
)


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    """Pearson correlation that returns 0.0 (never NaN) for degenerate input."""
    a = a - a.mean()
    b = b - b.mean()
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom <= 1e-12:
        return 0.0
    return float(np.dot(a, b) / denom)


def _estimate_key_mode(chroma_mean: np.ndarray):
    """Return (key 0-11, mode 0/1, confidence 0..1) from a mean chroma vector."""
    if chroma_mean.sum() <= 0 or np.std(chroma_mean) == 0:
        return -1, 0, 0.0
    v = chroma_mean / (np.linalg.norm(chroma_mean) + 1e-9)
    best = (-1e9, -1, 0)
    scores = []
    for key in range(12):
        maj = _corr(v, np.roll(_MAJOR_PROFILE, key))
        minr = _corr(v, np.roll(_MINOR_PROFILE, key))
        scores.append(maj)
        scores.append(minr)
        if maj > best[0]:
            best = (maj, key, 1)
        if minr > best[0]:
            best = (minr, key, 0)
    scores = np.array(scores)
    # confidence: how far the winner sits above the mean of all candidates
    conf = (best[0] - scores.mean()) / (scores.std() + 1e-9) / 3.0
    conf = float(np.clip(conf, 0.0, 1.0)) if np.isfinite(conf) else 0.0
    return best[1], best[2], conf


def _rms_to_db(rms: float) -> float:
    return float(20.0 * np.log10(max(rms, 1e-6)))


# Default analysis sample rate. Higher = more timbral/pitch detail and more CPU.
_sr_env = os.environ.get("ANALYSIS_SR", "").strip()
DEFAULT_SR = int(_sr_env) if _sr_env.isdigit() and int(_sr_env) > 0 else 44100


def analyze(path: str, sr: int | None = None) -> dict:
    """Analyze `path` and return (analysis dict, duration).

    Uses HPSS (harmonic/percussive source separation) so beats are tracked from
    the percussive component (crisp, less confused by sustained notes) while
    pitch/chroma is read from the harmonic component (cleaner tonal content).
    This costs an extra STFT pass — deliberately, for quality.
    """
    sr = sr or DEFAULT_SR
    y, sr = librosa.load(path, sr=sr, mono=True)
    duration = float(librosa.get_duration(y=y, sr=sr))
    if duration <= 0 or y.size == 0:
        raise ValueError("empty or unreadable audio")

    hop = 512
    # --- harmonic/percussive separation -----------------------------------
    y_harm, y_perc = librosa.effects.hpss(y)

    # --- beats & tempo (from the percussive component) ---------------------
    tempo, beat_frames = librosa.beat.beat_track(
        y=y_perc, sr=sr, hop_length=hop, units="frames", trim=False
    )
    tempo = float(np.atleast_1d(tempo)[0])
    beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop)
    beat_times = _ensure_grid(beat_times, duration, tempo)

    beats = _intervals(beat_times, duration, confidence=0.8)

    # --- bars (assume 4/4) & tatums (2 subdivisions per beat) --------------
    time_signature = 4
    bar_times = beat_times[::time_signature]
    bars = _intervals(bar_times, duration, confidence=0.6)

    tatum_times = _subdivide(beat_times, 2, duration)
    tatums = _intervals(tatum_times, duration, confidence=0.5)

    # --- segments (onsets from percussive; chroma from harmonic) -----------
    segments = _segments(y, y_harm, y_perc, sr, hop, duration)

    # --- sections (large agglomerative regions, harmonic content) ----------
    sections = _sections(y, y_harm, sr, hop, duration, tempo, time_signature)

    return {
        "sections": sections,
        "bars": bars,
        "beats": beats,
        "tatums": tatums,
        "segments": segments,
    }, duration


def _ensure_grid(times: np.ndarray, duration: float, tempo: float) -> np.ndarray:
    """Guarantee a non-empty, monotonic beat grid even for percussion-poor audio."""
    times = np.asarray(times, dtype=float)
    times = times[(times >= 0) & (times < duration)]
    if times.size >= 2:
        return times
    # fall back to a synthetic grid at the estimated (or default) tempo
    bpm = tempo if tempo and tempo > 30 else 120.0
    step = 60.0 / bpm
    n = max(2, int(duration / step))
    return np.linspace(0, duration, n, endpoint=False)


def _subdivide(times: np.ndarray, n: int, duration: float) -> np.ndarray:
    out = []
    for i in range(len(times)):
        start = times[i]
        end = times[i + 1] if i + 1 < len(times) else duration
        for k in range(n):
            out.append(start + (end - start) * k / n)
    return np.asarray(out, dtype=float)


def _intervals(times: np.ndarray, duration: float, confidence: float):
    times = np.asarray(times, dtype=float)
    out = []
    for i in range(len(times)):
        start = float(times[i])
        end = float(times[i + 1]) if i + 1 < len(times) else duration
        dur = max(0.0, end - start)
        if dur <= 0:
            continue
        out.append({"start": round(start, 5), "duration": round(dur, 5),
                    "confidence": confidence})
    return out


def _frame_features(y_full, y_harm, sr, hop):
    """Chroma (12, from the harmonic signal), MFCC (12) and RMS on a common grid.

    librosa features can disagree by a frame; trim everything to the shortest so
    boolean masks and vstack stay aligned.
    """
    chroma = librosa.feature.chroma_cqt(y=y_harm, sr=sr, hop_length=hop)      # 12 x T
    mfcc = librosa.feature.mfcc(y=y_full, sr=sr, hop_length=hop, n_mfcc=12)   # 12 x T
    rms = librosa.feature.rms(y=y_full, hop_length=hop)[0]                    # T
    t = min(chroma.shape[1], mfcc.shape[1], rms.shape[0])
    chroma, mfcc, rms = chroma[:, :t], mfcc[:, :t], rms[:t]
    times = librosa.frames_to_time(np.arange(t), sr=sr, hop_length=hop)
    return chroma, mfcc, rms, times


def _segments(y, y_harm, y_perc, sr, hop, duration):
    """Onset-bounded segments with 12-bin chroma (pitches) and 12 MFCC (timbre).

    Onsets are detected on the percussive component for sharper boundaries."""
    onset_frames = librosa.onset.onset_detect(y=y_perc, sr=sr, hop_length=hop, backtrack=True)
    onset_times = librosa.frames_to_time(onset_frames, sr=sr, hop_length=hop)
    bounds = np.concatenate([[0.0], onset_times, [duration]])
    bounds = np.unique(np.clip(bounds, 0, duration))
    # keep segments from getting absurdly short/long
    if bounds.size < 2:
        bounds = np.linspace(0, duration, max(2, int(duration / 0.25)))

    chroma, mfcc, rms, times = _frame_features(y, y_harm, sr, hop)

    segments = []
    for i in range(len(bounds) - 1):
        s, e = float(bounds[i]), float(bounds[i + 1])
        dur = e - s
        if dur < 0.05:
            continue
        mask = (times >= s) & (times < e)
        if not mask.any():
            # nearest single frame
            idx = int(np.argmin(np.abs(times - s)))
            mask = np.zeros_like(times, dtype=bool)
            mask[idx] = True

        pitch = chroma[:, mask].mean(axis=1)
        pitch = pitch / (pitch.max() + 1e-9)                # 0..1, Spotify-style
        timbre = mfcc[:, mask].mean(axis=1)                 # unbounded coefficients
        seg_rms = float(rms[mask].mean())
        loud = _rms_to_db(seg_rms)

        segments.append({
            "start": round(s, 5),
            "duration": round(dur, 5),
            "confidence": 0.5,
            "loudness_start": round(loud, 3),
            "loudness_max_time": round(dur / 2.0, 5),
            "loudness_max": round(loud, 3),
            "pitches": [round(float(x), 5) for x in pitch],
            "timbre": [round(float(x), 4) for x in timbre],
        })
    if not segments:
        raise ValueError("no segments produced")
    return segments


def _sections(y, y_harm, sr, hop, duration, tempo, time_signature):
    chroma, mfcc, rms, frame_times = _frame_features(y, y_harm, sr, hop)
    feat = np.vstack([librosa.util.normalize(chroma, axis=1),
                      librosa.util.normalize(mfcc, axis=1)])

    n_frames = feat.shape[1]
    target = int(np.clip(round(duration / 20.0), 2, 12))    # ~1 section / 20s
    target = min(target, max(2, n_frames - 1))
    try:
        bound_frames = librosa.segment.agglomerative(feat, target)
    except Exception:
        bound_frames = np.linspace(0, n_frames - 1, target + 1).astype(int)
    bound_times = librosa.frames_to_time(bound_frames, sr=sr, hop_length=hop)
    bound_times = np.unique(np.concatenate([[0.0], bound_times, [duration]]))
    bound_times = np.clip(bound_times, 0, duration)

    out = []
    for i in range(len(bound_times) - 1):
        s, e = float(bound_times[i]), float(bound_times[i + 1])
        dur = e - s
        if dur < 1.0:
            continue
        mask = (frame_times >= s) & (frame_times < e)
        if not mask.any():
            continue
        key, mode, key_conf = _estimate_key_mode(chroma[:, mask].mean(axis=1))
        loud = _rms_to_db(float(rms[mask].mean()))
        out.append({
            "start": round(s, 5),
            "duration": round(dur, 5),
            "confidence": 0.5,
            "loudness": round(loud, 3),
            "tempo": round(float(tempo), 3),
            "tempo_confidence": 0.5,
            "key": int(key),
            "key_confidence": round(float(key_conf), 3),
            "mode": int(mode),
            "mode_confidence": round(float(key_conf), 3),
            "time_signature": int(time_signature),
            "time_signature_confidence": 0.5,
        })
    if not out:
        out = [{
            "start": 0.0, "duration": round(duration, 5), "confidence": 0.5,
            "loudness": _rms_to_db(float(rms.mean())), "tempo": round(float(tempo), 3),
            "tempo_confidence": 0.5, "key": 0, "key_confidence": 0.0, "mode": 1,
            "mode_confidence": 0.0, "time_signature": int(time_signature),
            "time_signature_confidence": 0.5,
        }]
    return out


if __name__ == "__main__":
    import json
    import sys

    analysis, dur = analyze(sys.argv[1])
    print(json.dumps({"analysis": analysis, "audio_summary": {"duration": dur}}))
