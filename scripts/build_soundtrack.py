r"""build_soundtrack.py -- one soundtrack WAV per song, mixed from the cameras' own audio, for shows without a
board/camcorder master (or where the user prefers the cameras' sound).

The user picks the audio sources in show.json (ASK them -- don't assume a camera combination):
  soundtrack.priority  cameras to prefer wherever they recorded, in order (e.g. phones near the PA)
  soundtrack.bed       the camera that fills every gap (a fixed cam that recorded every song)
  anchor.camera        last resort for stretches the bed doesn't cover
Time base = the song's anchor file (t=0 is its first sample) -- the same axis the scene script uses, so the WAV
drops straight in as the song's audio.

  - Only clips whose offset sync_to_anchor.py VERIFIED may carry audio: a priority clip 50 ms off would echo
    against the bed at every crossfade.
  - Each priority clip loses EDGE s at both ends (start/stop handling noise, AGC settling); a stretch shorter than
    MIN_PHONE s is dropped (a 3 s burst of another mic reads as a glitch); overlapping priority clips: the one
    already playing keeps going until it runs out, else the higher-priority camera, else the longest remaining.
  - Levelling: each stretch gets the gain that matches its loudness to the bed over the SAME seconds (gated RMS);
    the song is then set to TARGET_DBFS and peak-limited; switches are equal-power crossfades of XFADE s.
  - Every camera's own audio is also written on the same axis (soundtrack/stems/NN-<cam>.wav) for EQ / stereo
    experiments, and the recipe (stretches, gains, level steps at each switch) to soundtrack/NN.json -- so a
    better mix can replace the audio track later without touching the video.
"""
import json
import os
import sys

import numpy as np
from scipy.io import wavfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import show_config  # noqa: E402

CFG = show_config.load()
ANCHOR = CFG["anchor"]["camera"]
BED = CFG["soundtrack"].get("bed") or ANCHOR
PRIORITY = list(CFG["soundtrack"].get("priority") or [])
OUT = show_config.path("soundtrack")
SR = 48000
EDGE = 0.5
MIN_PHONE = 6.0
XFADE = 0.6
TARGET_DBFS = -18.0
PEAK = 0.97


def load(path, dur=None):
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0", "-ac", "2", "-ar", str(SR), "-f", "f32le", "-"]
    a = np.frombuffer(subprocess.run(cmd, capture_output=True).stdout, dtype=np.float32).reshape(-1, 2)
    return a


def place(audio, offset, n):
    """Put a clip's samples on the song axis; returns (n,2) array and a coverage mask."""
    out = np.zeros((n, 2), np.float32)
    mask = np.zeros(n, bool)
    s = int(round(offset * SR))
    a0, b0 = max(0, -s), max(0, s)
    k = min(len(audio) - a0, n - b0)
    if k > 0:
        out[b0:b0 + k] = audio[a0:a0 + k]
        mask[b0:b0 + k] = True
    return out, mask


def gated_rms_db(x):
    """Loudness of a stretch: RMS over 400 ms blocks, ignoring blocks 30 dB below the loudest."""
    mono = x.mean(axis=1)
    blk = int(0.4 * SR)
    if len(mono) < blk:
        return None
    blocks = mono[:len(mono) // blk * blk].reshape(-1, blk)
    db = 10 * np.log10((blocks ** 2).mean(axis=1) + 1e-12)
    keep = db[db > db.max() - 30]
    return float(10 * np.log10(np.mean(10 ** (keep / 10)))) if len(keep) else None


def main(only=None):
    tl = json.load(open(show_config.path("timeline.json"), encoding="utf-8"))
    os.makedirs(OUT, exist_ok=True)
    cams = tl["cameras"]
    for g1 in cams[ANCHOR]:
        num = g1["song_num"]
        if only and num not in only:
            continue
        bed1 = load(g1["audio_file"])
        n = len(bed1)
        tracks = {ANCHOR: (bed1, np.ones(n, bool))}
        if BED != ANCHOR:
            g2 = [c for c in cams.get(BED, []) if c["song_num"] == num and c["verified"]]
            if g2:
                tracks[BED] = place(load(g2[0]["audio_file"]), g2[0]["offset_in_song"], n)

        phone_clips = []
        for cam in PRIORITY:
            for c in cams.get(cam, []):
                if c["song_num"] == num and c["verified"]:
                    a, m = place(load(c["audio_file"]), c["offset_in_song"], n)
                    idx = np.flatnonzero(m)
                    if len(idx) == 0:
                        continue
                    lo, hi = idx[0] + int(EDGE * SR), idx[-1] - int(EDGE * SR)
                    if hi - lo < MIN_PHONE * SR:
                        continue
                    m[:] = False
                    m[lo:hi] = True
                    key = f"{cam}:{os.path.basename(c['file'])}"
                    tracks[key] = (a, m)
                    phone_clips.append(key)

        # Every camera's own audio on the same axis, for EQ / stereo experiments: stems/NN-<cam>.wav
        # (a camera's clips share one file, silent where it wasn't recording).
        stem_dir = os.path.join(OUT, "stems")
        os.makedirs(stem_dir, exist_ok=True)
        per_cam = {}
        for key, (a, m) in tracks.items():
            cam = key.split(":")[0]
            buf = per_cam.setdefault(cam, np.zeros((n, 2), np.float32))
            free = m & ~np.any(buf != 0, axis=1)
            buf[free] = a[free]
        for cam, buf in per_cam.items():
            wavfile.write(os.path.join(stem_dir, f"{num}-{cam}.wav"), SR, buf)

        # Choose a source per sample: priority clip (keep the current one while it lasts), else bed, else anchor.
        choice = np.empty(n, object)
        cur = None
        step = int(0.1 * SR)
        for i in range(0, n, step):
            j = min(n, i + step)
            live = [k for k in phone_clips if tracks[k][1][i:j].all()]
            if cur in live:
                pick = cur
            elif live:
                pick = min(live, key=lambda k: (PRIORITY.index(k.split(":")[0]), -np.flatnonzero(tracks[k][1])[-1]))
            elif BED in tracks and tracks[BED][1][i:j].all():
                pick = BED
            else:
                pick = ANCHOR
            choice[i:j] = pick
            cur = pick if pick in phone_clips else cur if cur in live else None

        # Collapse into stretches; fold too-short phone stretches into their neighbour.
        stretches = []
        i = 0
        while i < n:
            j = i
            while j < n and choice[j] == choice[i]:
                j += step
            stretches.append([choice[i], i, min(j, n)])
            i = min(j, n)
        merged = []
        for s in stretches:
            if merged and (s[2] - s[1]) < SR:  # a sub-second sliver (e.g. the bed ends 0.2 s early)
                merged[-1][2] = s[2]
                continue
            if s[0] in phone_clips and (s[2] - s[1]) < MIN_PHONE * SR:
                s[0] = BED if BED in tracks and tracks[BED][1][s[1]:s[2]].all() else ANCHOR
            if merged and merged[-1][0] == s[0]:
                merged[-1][2] = s[2]
            else:
                merged.append(s)

        # Level each stretch against the bed (or the anchor) over the same samples.
        ref_key = BED if BED in tracks else ANCHOR
        for s in merged:
            src, lo, hi = s
            if src == ref_key:
                s.append(0.0)
                continue
            ref_seg = tracks[ref_key][0][lo:hi]
            if not tracks[ref_key][1][lo:hi].all():
                ref_seg = tracks[ANCHOR][0][lo:hi]
            a, b = gated_rms_db(ref_seg), gated_rms_db(tracks[src][0][lo:hi])
            s.append(round(a - b, 2) if a is not None and b is not None else 0.0)

        # Mix with equal-power crossfades at each boundary.
        mix = np.zeros((n, 2), np.float32)
        half = int(XFADE / 2 * SR)
        for k, (src, lo, hi, gain) in enumerate(merged):
            w = np.ones(n, np.float32)
            w[:max(0, lo - half)] = 0
            w[min(n, hi + half):] = 0
            if k > 0:
                a0, a1 = max(0, lo - half), min(n, lo + half)
                ramp = np.linspace(0, 1, a1 - a0, dtype=np.float32)
                w[a0:a1] = np.sin(ramp * np.pi / 2)
            else:
                w[:lo] = 0 if lo else w[:lo]
            if k < len(merged) - 1:
                b0, b1 = max(0, hi - half), min(n, hi + half)
                ramp = np.linspace(1, 0, b1 - b0, dtype=np.float32)
                w[b0:b1] = np.sin(ramp * np.pi / 2)
            mix += tracks[src][0] * (w * 10 ** (gain / 20))[:, None]

        level = gated_rms_db(mix)
        g = 10 ** ((TARGET_DBFS - level) / 20) if level is not None else 1.0
        mix *= g
        peak = float(np.abs(mix).max())
        if peak > PEAK:
            mix *= PEAK / peak
        wavfile.write(os.path.join(OUT, f"{num}.wav"), SR, mix)
        # Smoothness check: loudness 1 s before vs 1 s after each switch, in the finished mix.
        steps = []
        for st in merged[1:]:
            b = st[1]
            pre, post = gated_rms_db(mix[max(0, b - SR):b]), gated_rms_db(mix[b:b + SR])
            if pre is not None and post is not None:
                steps.append(round(post - pre, 1))
        doc = {"song_num": num, "title": g1["title"], "samplerate": SR, "duration": n / SR,
               "song_gain_db": round(20 * np.log10(g), 2), "peak_limited": peak > PEAK,
               "switch_level_steps_db": steps,
               "stretches": [{"source": s[0], "start": round(s[1] / SR, 2), "end": round(s[2] / SR, 2),
                              "gain_db": s[3]} for s in merged]}
        json.dump(doc, open(os.path.join(OUT, f"{num}.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        phone_s = sum(s[2] - s[1] for s in merged if s[0] in phone_clips) / SR
        print(f"{num} {g1['title'][:28]:28s} {len(merged):2d} stretches, priority audio {phone_s:5.1f}s "
              f"of {n / SR:5.1f}s  steps {steps}  " + " | ".join(f"{s[0].split(':')[0]} {s[1] / SR:.0f}-{s[2] / SR:.0f}s {s[3]:+.1f}dB" for s in merged))


if __name__ == "__main__":
    main(set(sys.argv[1:]) or None)
