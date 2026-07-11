/*
 * infinite.js — a self-contained Infinite Jukebox engine.
 *
 * Given a JukeboxTrack (Spotify-shaped analysis) and a decoded AudioBuffer it:
 *   1. builds a feature vector per beat from the overlapping segments,
 *   2. finds "edges" — pairs of beats similar enough to splice between, with
 *      musical guards so jumps sound natural (same position in the bar, similar
 *      loudness, a jump cooldown),
 *   3. plays beat-by-beat through the Web Audio API with an equal-power
 *      crossfade at every splice so jumps are seamless, looping forever.
 *
 * No external libraries.
 */
(function (global) {
  "use strict";

  var CROSSFADE = 0.11;   // seconds of overlap blended at each jump splice
  var TINY_FADE = 0.004;  // click guard on ordinary (sequential) beats

  function mean(vectors, dim) {
    var out = new Array(dim).fill(0);
    if (!vectors.length) return out;
    for (var v = 0; v < vectors.length; v++)
      for (var i = 0; i < dim; i++) out[i] += vectors[v][i];
    for (var k = 0; k < dim; k++) out[k] /= vectors.length;
    return out;
  }

  // Attach to each beat: a 24-d pitch+timbre feature, mean loudness, and its
  // position within the bar (phase), so we can keep the groove aligned.
  function buildBeatFeatures(analysis) {
    var beats = analysis.beats;
    var segs = analysis.segments;
    var s = 0;
    for (var b = 0; b < beats.length; b++) {
      var beat = beats[b];
      beat.index = b;
      var end = beat.start + beat.duration;
      var pitches = [], timbres = [], loud = [];
      while (s > 0 && segs[s].start > beat.start) s--;
      for (var j = s; j < segs.length; j++) {
        var seg = segs[j];
        if (seg.start + seg.duration < beat.start) continue;
        if (seg.start > end) break;
        pitches.push(seg.pitches);
        timbres.push(seg.timbre);
        loud.push(seg.loudness_max);
      }
      beat.feature = mean(pitches, 12).concat(mean(timbres, 12));
      beat.loudness = loud.length ? loud.reduce(function (a, c) { return a + c; }, 0) / loud.length : -60;
    }
    // z-score timbre dims so no single loud coefficient dominates distance
    for (var d = 12; d < 24; d++) {
      var m = 0;
      for (var x = 0; x < beats.length; x++) m += beats[x].feature[d];
      m /= beats.length || 1;
      var vv = 0;
      for (var y = 0; y < beats.length; y++) vv += Math.pow(beats[y].feature[d] - m, 2);
      var sd = Math.sqrt(vv / (beats.length || 1)) || 1;
      for (var z = 0; z < beats.length; z++) beats[z].feature[d] = (beats[z].feature[d] - m) / sd;
    }
    assignBarPhase(analysis);
    assignSections(analysis);
  }

  // Classify sections as chorus-like (repeated) vs verse-like (unique), and tag
  // each beat with its section, whether that section repeats, and how far it is
  // from the section's start/end. This lets the jumper keep a verse intact and
  // only splice at section boundaries — while allowing free jumps inside a
  // repeated chorus (where a splice is inaudible because the part recurs anyway).
  function assignSections(analysis) {
    var beats = analysis.beats;
    var sections = analysis.sections || [];
    var i, b;

    if (sections.length < 2) {
      for (i = 0; i < beats.length; i++) {
        beats[i].sec = 0; beats[i].inChorus = false;
        beats[i].fromSecStart = i; beats[i].toSecEnd = beats.length - 1 - i;
      }
      return;
    }

    // map each beat to a section index
    var si = 0;
    var secBeats = [];
    for (i = 0; i < sections.length; i++) secBeats.push([]);
    for (b = 0; b < beats.length; b++) {
      while (si < sections.length - 1 && beats[b].start >= sections[si + 1].start) si++;
      beats[b].sec = si;
      secBeats[si].push(b);
    }

    // mean pitch+timbre feature per section
    var secFeat = secBeats.map(function (idxs) {
      var f = new Array(24).fill(0);
      idxs.forEach(function (gi) {
        var bf = beats[gi].feature;
        for (var k = 0; k < 24; k++) f[k] += bf[k];
      });
      if (idxs.length) for (var k = 0; k < 24; k++) f[k] /= idxs.length;
      return f;
    });

    // adaptive similarity threshold from the lower quartile of section-pair dists
    var dists = [];
    for (i = 0; i < sections.length; i++)
      for (var j = i + 1; j < sections.length; j++)
        dists.push(distance(secFeat[i], secFeat[j]));
    dists.sort(function (a, c) { return a - c; });
    var thr = dists.length ? dists[Math.floor(dists.length * 0.25)] : Infinity;

    // a section is "chorus" if it closely matches another section of similar length
    for (i = 0; i < sections.length; i++) {
      var rep = false;
      for (var j2 = 0; j2 < sections.length; j2++) {
        if (i === j2) continue;
        var la = sections[i].duration, lb = sections[j2].duration;
        var lenOk = Math.abs(la - lb) / Math.max(la, lb, 0.001) < 0.35;
        if (lenOk && distance(secFeat[i], secFeat[j2]) <= thr) { rep = true; break; }
      }
      sections[i].isChorus = rep;
    }

    // annotate beats with chorus flag and distance to section edges
    for (i = 0; i < sections.length; i++) {
      var idxs = secBeats[i];
      for (var p = 0; p < idxs.length; p++) {
        var beat = beats[idxs[p]];
        beat.inChorus = !!sections[i].isChorus;
        beat.fromSecStart = p;
        beat.toSecEnd = idxs.length - 1 - p;
      }
    }
  }

  // Determine each beat's index within its bar (0 = downbeat). Falls back to a
  // 4/4 grid if the analysis has no usable bars.
  function assignBarPhase(analysis) {
    var beats = analysis.beats;
    var bars = analysis.bars || [];
    if (bars.length < 2) {
      for (var i = 0; i < beats.length; i++) beats[i].phase = i % 4;
      return;
    }
    var bi = 0, phase = 0;
    for (var b = 0; b < beats.length; b++) {
      while (bi < bars.length - 1 && beats[b].start >= bars[bi + 1].start) {
        bi++;
        phase = 0;
      }
      beats[b].phase = phase;
      phase++;
    }
  }

  function distance(a, b) {
    var sum = 0;
    for (var i = 0; i < a.length; i++) {
      var d = a[i] - b[i];
      sum += d * d;
    }
    return Math.sqrt(sum);
  }

  // Build jump edges. A jump i->j is a candidate only when the two beats sit at
  // the same position in the bar and are close in loudness; we then rank by the
  // similarity of a short forward window (so the music continues plausibly).
  function buildEdges(beats, opts) {
    var n = beats.length;
    var window = opts.window || 6;
    var minGap = opts.minGap || Math.max(4, Math.floor(n / 20));
    var loudTol = opts.loudTol || 6;        // dB
    var bnd = opts.boundaryTol != null ? opts.boundaryTol : 2;  // beats from a section edge
    var feats = beats.map(function (x) { return x.feature; });

    function windowDist(i, j) {
      var sum = 0, cnt = 0;
      for (var k = 0; k < window; k++) {
        if (i + k >= n || j + k >= n) break;
        sum += distance(feats[i + k], feats[j + k]);
        cnt++;
      }
      return cnt ? sum / cnt : Infinity;
    }

    // Where may a jump ORIGINATE? Inside a chorus (repeats anyway), or right at
    // the end of a section (so a verse plays through, then splices at the seam).
    function canSrc(bt) {
      return bt.inChorus || (bt.toSecEnd != null && bt.toSecEnd <= bnd);
    }
    // Where may a jump LAND? Inside a chorus, or at the start of a section (so we
    // never drop into the middle of a verse).
    function canDst(bt) {
      return bt.inChorus || (bt.fromSecStart != null && bt.fromSecStart <= bnd);
    }

    // Build the edge set; `gate` toggles the verse/chorus restriction so we can
    // fall back to unrestricted edges if gating leaves the graph too sparse.
    function build(gate) {
      for (var q = 0; q < n; q++) beats[q].edges = [];
      var cand = [];
      for (var i = 0; i < n; i++) {
        for (var j = i + minGap; j < n; j++) {
          if (beats[i].phase !== beats[j].phase) continue;                 // stay on-beat
          if (Math.abs(beats[i].loudness - beats[j].loudness) > loudTol) continue; // similar energy
          var fwd = !gate || (canSrc(beats[i]) && canDst(beats[j]));       // i -> j
          var bwd = !gate || (canSrc(beats[j]) && canDst(beats[i]));       // j -> i
          if (!fwd && !bwd) continue;
          var d = windowDist(i, j);
          if (isFinite(d)) cand.push([d, i, j, fwd, bwd]);
        }
      }
      cand.sort(function (a, b) { return a[0] - b[0]; });
      var target = Math.min(cand.length, Math.max(12, Math.floor(n / 4)));
      var perBeatCap = 6, kept = 0;
      for (var c = 0; c < cand.length && kept < target; c++) {
        var dd = cand[c][0], a = cand[c][1], bb = cand[c][2];
        if (beats[a].edges.length >= perBeatCap && beats[bb].edges.length >= perBeatCap) continue;
        if (cand[c][3]) beats[a].edges.push({ to: bb, dist: dd });        // forward
        if (cand[c][4]) beats[bb].edges.push({ to: a, dist: dd });        // backward
        kept++;
      }
      for (var e = 0; e < n; e++)
        beats[e].edges.sort(function (x, y) { return x.dist - y.dist; });
      return kept;
    }

    var kept = build(true);
    // If the verse/chorus gate starved the graph (few or no jumps), relax it so
    // the track still loops rather than playing straight through once.
    if (kept < Math.max(6, Math.floor(n / 30))) kept = build(false);
    return kept;
  }

  function InfiniteJukebox(track, buffer, callbacks) {
    var ctx = InfiniteJukebox._ctx ||
      (InfiniteJukebox._ctx = new (global.AudioContext || global.webkitAudioContext)());
    this.ctx = ctx;
    this.track = track;
    this.buffer = buffer;
    this.beats = track.analysis.beats;
    this.cb = callbacks || {};
    this.playing = false;
    this.jumpProb = 0.18;            // gentler than a coin-flip -> more musical
    this.cooldown = 0;               // beats to wait before the next jump
    this.minRun = 4;                 // minimum beats between jumps
    this.cur = 0;
    this.arrivedByJump = false;
    this.stats = { beatsPlayed: 0, jumps: 0 };

    buildBeatFeatures(track.analysis);
    this.edgeCount = buildEdges(this.beats, {});

    this.gain = ctx.createGain();
    this.gain.gain.value = 0.85;
    this.gain.connect(ctx.destination);
  }

  InfiniteJukebox.prototype.chooseNext = function (i) {
    var beat = this.beats[i];
    var atEnd = i >= this.beats.length - 1;
    var edges = beat.edges || [];

    if (this.cooldown > 0) this.cooldown--;

    var mayJump = edges.length > 0 && this.cooldown === 0;
    var wantJump = atEnd ? edges.length > 0 : (mayJump && Math.random() < this.jumpProb);

    if (wantJump) {
      // weight toward the closest matches, but keep a little variety
      var pool = Math.min(edges.length, 3);
      var pick = edges[Math.floor(Math.random() * pool)];
      this.cooldown = this.minRun;
      this.stats.jumps++;
      if (this.cb.onJump) this.cb.onJump(i, pick.to);
      return { next: pick.to, jumped: true };
    }
    if (atEnd) return { next: 0, jumped: true };   // safety net: never stop
    return { next: i + 1, jumped: false };
  };

  // Schedule beat i at time `when`. inJump/outJump say whether the transition
  // into / out of this beat is a splice, so we crossfade those edges.
  InfiniteJukebox.prototype._scheduleBeat = function (i, when, inJump, outJump) {
    var beat = this.beats[i];
    var fin = inJump ? CROSSFADE : TINY_FADE;
    var fout = outJump ? CROSSFADE : TINY_FADE;
    var src = this.ctx.createBufferSource();
    src.buffer = this.buffer;
    var g = this.ctx.createGain();
    // equal-power-ish fades via linear ramps on short windows
    g.gain.setValueAtTime(0.0001, when);
    g.gain.linearRampToValueAtTime(1, when + fin);
    g.gain.setValueAtTime(1, when + beat.duration - fout);
    g.gain.linearRampToValueAtTime(0.0001, when + beat.duration + (outJump ? fout : 0));
    src.connect(g);
    g.connect(this.gain);
    // read a little extra so the out-crossfade has real audio to fade, not silence
    var playDur = beat.duration + (outJump ? fout : TINY_FADE);
    src.start(when, beat.start, playDur);
    src.stop(when + playDur + 0.02);
  };

  InfiniteJukebox.prototype._loop = function () {
    if (!this.playing) return;
    var ahead = 0.25;
    while (this.nextTime < this.ctx.currentTime + ahead) {
      var i = this.cur;
      var beat = this.beats[i];
      var decision = this.chooseNext(i);
      var startAt = this.nextTime;

      this._scheduleBeat(i, startAt, this.arrivedByJump, decision.jumped);
      this.stats.beatsPlayed++;

      var self = this;
      var delay = (startAt - this.ctx.currentTime) * 1000;
      (function (idx) {
        global.setTimeout(function () {
          if (self.cb.onBeat) self.cb.onBeat(idx, self.stats);
        }, Math.max(0, delay));
      })(i);

      // On a jump, pull the next beat CROSSFADE earlier so it overlaps this
      // beat's ring-out -> a real crossfade rather than a hard cut.
      this.nextTime += beat.duration - (decision.jumped ? CROSSFADE : 0);
      this.arrivedByJump = decision.jumped;
      this.cur = decision.next;
    }
    this._timer = global.setTimeout(this._loop.bind(this), 25);
  };

  InfiniteJukebox.prototype.play = function () {
    if (this.playing) return;
    if (this.ctx.state === "suspended") this.ctx.resume();
    this.playing = true;
    this.nextTime = this.ctx.currentTime + 0.12;
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
    this.nextTime = Math.max(this.nextTime || 0, this.ctx.currentTime + 0.12);
    this._loop();
  };

  // Stop this track's scheduler and detach it, WITHOUT suspending the shared
  // AudioContext (so another track can start immediately — used by shuffle).
  InfiniteJukebox.prototype.destroy = function () {
    this.playing = false;
    if (this._timer) global.clearTimeout(this._timer);
    try { this.gain.disconnect(); } catch (e) {}
  };

  InfiniteJukebox.prototype.setVolume = function (v) { this.gain.gain.value = v; };
  InfiniteJukebox.prototype.setJumpProbability = function (p) {
    this.jumpProb = Math.max(0, Math.min(1, p));
  };

  global.InfiniteJukebox = InfiniteJukebox;
})(window);
