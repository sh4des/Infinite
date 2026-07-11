/*
 * infinite.js — a self-contained Infinite Jukebox engine.
 *
 * Given a JukeboxTrack (Spotify-shaped analysis) and a decoded AudioBuffer it:
 *   1. builds a feature vector per beat from the overlapping segments,
 *   2. finds "edges" — pairs of beats similar enough to splice between,
 *   3. plays beat-by-beat through the Web Audio API, occasionally taking an
 *      edge so playback loops forever without an obvious seam.
 *
 * No external libraries. The algorithm follows Paul Lamere's original
 * Infinite Jukebox: similarity over pitch+timbre, adaptive edge threshold,
 * probabilistic jumps with a guaranteed jump at the end of the track.
 */
(function (global) {
  "use strict";

  function mean(vectors, dim) {
    const out = new Array(dim).fill(0);
    if (!vectors.length) return out;
    for (const v of vectors) for (let i = 0; i < dim; i++) out[i] += v[i];
    for (let i = 0; i < dim; i++) out[i] /= vectors.length;
    return out;
  }

  // Attach to each beat the mean pitch (12) and timbre (12) of the segments it
  // overlaps, producing a 24-d feature vector used for similarity.
  function buildBeatFeatures(analysis) {
    const beats = analysis.beats;
    const segs = analysis.segments;
    let s = 0;
    for (const beat of beats) {
      const end = beat.start + beat.duration;
      const pitches = [];
      const timbres = [];
      while (s > 0 && segs[s].start > beat.start) s--;
      for (let j = s; j < segs.length; j++) {
        const seg = segs[j];
        if (seg.start + seg.duration < beat.start) continue;
        if (seg.start > end) break;
        pitches.push(seg.pitches);
        timbres.push(seg.timbre);
      }
      const p = mean(pitches, 12);
      const t = mean(timbres, 12);
      beat.feature = p.concat(t);
    }
    // z-score the timbre dimensions (indices 12..23) across all beats so no
    // single loud dimension dominates the distance.
    for (let d = 12; d < 24; d++) {
      let m = 0;
      for (const b of beats) m += b.feature[d];
      m /= beats.length || 1;
      let v = 0;
      for (const b of beats) v += (b.feature[d] - m) ** 2;
      const sd = Math.sqrt(v / (beats.length || 1)) || 1;
      for (const b of beats) b.feature[d] = (b.feature[d] - m) / sd;
    }
  }

  function distance(a, b) {
    let sum = 0;
    for (let i = 0; i < a.length; i++) {
      const d = a[i] - b[i];
      sum += d * d;
    }
    return Math.sqrt(sum);
  }

  // Build jump edges. We compare a short forward window of beats (context) so a
  // jump lands somewhere that continues plausibly, not just one matching beat.
  function buildEdges(beats, opts) {
    const n = beats.length;
    const window = opts.window || 4;
    const minGap = opts.minGap || Math.max(4, Math.floor(n / 20));
    const feats = beats.map((b) => b.feature);

    function windowDist(i, j) {
      let sum = 0;
      let cnt = 0;
      for (let k = 0; k < window; k++) {
        if (i + k >= n || j + k >= n) break;
        sum += distance(feats[i + k], feats[j + k]);
        cnt++;
      }
      return cnt ? sum / cnt : Infinity;
    }

    const candidates = [];
    for (let i = 0; i < n; i++) {
      for (let j = i + minGap; j < n; j++) {
        const d = windowDist(i, j);
        if (isFinite(d)) candidates.push([d, i, j]);
      }
    }
    candidates.sort((a, b) => a[0] - b[0]);

    // keep the best edges up to a target branch count (~n/6, like the original)
    const target = Math.min(candidates.length, Math.max(8, Math.floor(n / 6) * 2));
    for (const b of beats) b.edges = [];
    let kept = 0;
    for (const [d, i, j] of candidates) {
      if (kept >= target) break;
      // bidirectional: you can jump forward i->j or loop back j->i
      beats[j].edges.push({ to: i, dist: d });
      beats[i].edges.push({ to: j, dist: d });
      kept++;
    }
    for (const b of beats) b.edges.sort((x, y) => x.dist - y.dist);
    return kept;
  }

  function InfiniteJukebox(track, buffer, callbacks) {
    const ctx = InfiniteJukebox._ctx || (InfiniteJukebox._ctx = new (global.AudioContext || global.webkitAudioContext)());
    this.ctx = ctx;
    this.track = track;
    this.buffer = buffer;
    this.beats = track.analysis.beats;
    this.cb = callbacks || {};
    this.playing = false;
    this.jumpProb = 0.35;
    this.cur = 0;
    this.lastJumpFrom = -1;
    this.stats = { beatsPlayed: 0, jumps: 0 };

    buildBeatFeatures(track.analysis);
    this.edgeCount = buildEdges(this.beats, {});

    this.gain = ctx.createGain();
    this.gain.gain.value = 0.85;
    this.gain.connect(ctx.destination);
  }

  InfiniteJukebox.prototype.chooseNext = function (i) {
    const beat = this.beats[i];
    const atEnd = i >= this.beats.length - 1;
    const edges = beat.edges || [];
    const canJump = edges.length > 0;

    // Must jump at the very end so it never stops.
    const wantJump = atEnd ? canJump : canJump && Math.random() < this.jumpProb;

    if (wantJump && this.lastJumpFrom !== i) {
      // weighted toward closer matches, but keep some variety
      const pick = edges[Math.floor(Math.random() * Math.min(edges.length, 4))];
      this.lastJumpFrom = i;
      this.stats.jumps++;
      if (this.cb.onJump) this.cb.onJump(i, pick.to);
      return { next: pick.to, jumped: true };
    }
    this.lastJumpFrom = -1;
    if (atEnd) return { next: 0, jumped: true }; // safety net
    return { next: i + 1, jumped: false };
  };

  InfiniteJukebox.prototype._scheduleBeat = function (i, when) {
    const beat = this.beats[i];
    const src = this.ctx.createBufferSource();
    src.buffer = this.buffer;
    const g = this.ctx.createGain();
    // short fades to avoid clicks at splice points
    const fade = Math.min(0.006, beat.duration / 4);
    g.gain.setValueAtTime(0, when);
    g.gain.linearRampToValueAtTime(1, when + fade);
    g.gain.setValueAtTime(1, when + beat.duration - fade);
    g.gain.linearRampToValueAtTime(0, when + beat.duration);
    src.connect(g);
    g.connect(this.gain);
    src.start(when, beat.start, beat.duration + fade);
    src.stop(when + beat.duration + fade);
  };

  InfiniteJukebox.prototype._loop = function () {
    if (!this.playing) return;
    const ahead = 0.2;
    while (this.nextTime < this.ctx.currentTime + ahead) {
      const i = this.cur;
      this._scheduleBeat(i, this.nextTime);
      const startAt = this.nextTime;
      const beat = this.beats[i];
      this.stats.beatsPlayed++;
      // notify the visualizer in sync with audio
      const delay = (startAt - this.ctx.currentTime) * 1000;
      if (this.cb.onBeat) {
        global.setTimeout(() => this.cb.onBeat(i, this.stats), Math.max(0, delay));
      }
      this.nextTime += beat.duration;
      this.cur = this.chooseNext(i).next;
    }
    this._timer = global.setTimeout(() => this._loop(), 25);
  };

  InfiniteJukebox.prototype.play = function () {
    if (this.playing) return;
    if (this.ctx.state === "suspended") this.ctx.resume();
    this.playing = true;
    this.nextTime = this.ctx.currentTime + 0.1;
    this._loop();
  };

  InfiniteJukebox.prototype.pause = function () {
    this.playing = false;
    if (this._timer) global.clearTimeout(this._timer);
    this.ctx.suspend();
  };

  InfiniteJukebox.prototype.resume = function () {
    if (this.playing) return;
    this.playing = true;
    this.ctx.resume();
    this.nextTime = Math.max(this.nextTime, this.ctx.currentTime + 0.1);
    this._loop();
  };

  InfiniteJukebox.prototype.setVolume = function (v) {
    this.gain.gain.value = v;
  };

  InfiniteJukebox.prototype.setJumpProbability = function (p) {
    this.jumpProb = Math.max(0, Math.min(1, p));
  };

  global.InfiniteJukebox = InfiniteJukebox;
})(window);
