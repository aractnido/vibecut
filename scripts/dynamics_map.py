"""Dynamics map of a song, for the director agent: what the music does, beat by beat, so cuts and camera moves can
follow it (an explosive, dynamic song needs direction that follows its dynamics, not a clock).

Song time = the anchor per-song file time (the stems and the standardized audio _derived/std/NN-*.wav are on it).
Per beat (grid from metadata/song_map/NN.json bars, 4 beats per bar):
  - loudness (A-weighted, dB) of the mix (sum of stems) and of each stem (vocals, drums, bass, other = guitars/keys),
    and "crunch" (other stem, 2-6 kHz: the distorted guitar; in a room recording loudness hardly moves, distortion does);
  - onset strength (spectral flux); accent = how much louder a beat is than the one before.
Per bar: loudness, crunch, drum onsets, intensity 0-1 (z-scored mix + crunch + 0.5 x drum density); builds and drops.
Events (10 ms): transients on the guitar stem (pre-chorus blasts) and on the mix.
Crowd: vocal-band (300-3500 Hz) level at crowd-side mics minus a stage-side mic, per beat, relative to the sung median
(NaN where a mic wasn't recording). Lead pitch: pyin on the vocal stem; high-voice spans (f0 > ~E4).
Sections: metadata/sections/NN.json ([{"name", "t"}], each start snapped to the nearest bar line); without it, the
song map's own section letters.

GPU: STFTs and per-frame features in torch on CUDA. CPU, named: pyin (librosa; no GPU version here, seconds per song)
and the matplotlib chart.
Out: metadata/dynamics/NN.json + _derived/check/dynamics/NN.png.
Usage: python dynamics_map.py NN [--stage STAGE_MIC] [--crowd MIC1,MIC2]   (names of _derived/std/NN-<name>.wav)
Show root: VIBECUT_SHOW_ROOT (default: the current directory)."""
import json, os, sys
import numpy as np, soundfile as sf, torch

import argparse
_ap = argparse.ArgumentParser()
_ap.add_argument("num")
_ap.add_argument("--stage", default="", help="stage-side mic (_derived/std/NN-<name>.wav)")
_ap.add_argument("--crowd", default="", help="crowd-side mics, comma separated")
ARGS = _ap.parse_args()
ROOT = os.environ.get("VIBECUT_SHOW_ROOT", os.getcwd())
NUM = ARGS.num
dev = "cuda"
HOP = 0.01                                               # s per feature frame
STEMS = os.path.join(ROOT, "_derived", "scratch", "stems", NUM)
STD = os.path.join(ROOT, "_derived", "std")
SM = json.load(open(os.path.join(ROOT, "metadata", "song_map", NUM + ".json"), encoding="utf-8"))
LY = [l for b in json.load(open(os.path.join(ROOT, "metadata", "lyrics", NUM + ".json"), encoding="utf-8"))["blocks"]
      for l in b["lines"]]
_secf = os.path.join(ROOT, "metadata", "sections", NUM + ".json")
if os.path.exists(_secf):
    SECTIONS = [(x["name"], float(x["t"])) for x in json.load(open(_secf, encoding="utf-8"))["sections"]]
else:                                                    # the song map's own section letters, one entry per run
    SECTIONS, _last = [], None
    for ph in SM.get("phrases", []):
        if ph.get("section") != _last:
            SECTIONS.append((f"{ph.get('section')}{len(SECTIONS) + 1}", 0.0 if not SECTIONS else float(ph["t0"])))
            _last = ph.get("section")
    SECTIONS = SECTIONS or [("song", 0.0)]


def mono(path):
    d, sr = sf.read(path, dtype="float32")
    return (d.mean(1) if d.ndim > 1 else d), sr


def a_weight_db(f):
    f2 = f ** 2
    ra = (12194 ** 2 * f2 ** 2) / ((f2 + 20.6 ** 2) * torch.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2)) * (f2 + 12194 ** 2))
    return 20 * torch.log10(ra.clamp(min=1e-12)) + 2.0


def feats(x, sr, band=None):
    """Per 10 ms frame: A-weighted level (dB) and log spectral flux. band=(lo, hi) Hz: unweighted level in the band."""
    n_fft = 2048 if sr > 30000 else 1024
    hop = int(round(sr * HOP))
    t = torch.from_numpy(x).to(dev)
    X = torch.stft(t, n_fft, hop, window=torch.hann_window(n_fft, device=dev), return_complex=True, center=True)
    P = X.real ** 2 + X.imag ** 2
    f = torch.fft.rfftfreq(n_fft, 1 / sr).to(dev)
    if band:
        m = ((f >= band[0]) & (f <= band[1])).float()
        lev = 10 * torch.log10((P * m[:, None]).sum(0) + 1e-10)
    else:
        lev = 10 * torch.log10((P * (10 ** (a_weight_db(f) / 10))[:, None]).sum(0) + 1e-10)
    L = torch.log1p(1e3 * P / P.mean().clamp(min=1e-12))
    fl = torch.cat([torch.zeros(1, device=dev), (L[:, 1:] - L[:, :-1]).clamp(min=0).mean(0)])
    return lev, fl


def per_span(arr_db, edges, power=True):
    """Mean of a 10 ms series over spans [edges[i], edges[i+1]) (dB averaged in power when power=True)."""
    a = arr_db.cpu().numpy()
    out = []
    for a0, a1 in zip(edges, edges[1:]):
        i0, i1 = int(a0 / HOP), max(int(a0 / HOP) + 1, int(a1 / HOP))
        s = a[i0:i1]
        out.append(float(10 * np.log10(np.mean(10 ** (s / 10)))) if power else float(np.mean(s)) if len(s) else 0.0)
    return np.array(out)


# ---------------------------------------------------------------- grid
bars = list(SM["bars"])
dur = float(SM["duration"])
bs = float(SM["bar_s"])
while bars[-1] + bs < dur:
    bars.append(round(bars[-1] + bs, 3))
beats = []
for b0, b1 in zip(bars, bars[1:] + [bars[-1] + bs]):
    beats += [b0 + k * (b1 - b0) / 4 for k in range(4)]
beats = [b for b in beats if b < dur]
bedges = beats + [min(dur, beats[-1] + bs / 4)]
baredges = bars + [min(dur, bars[-1] + bs)]

# ---------------------------------------------------------------- stems + mix
st = {k: mono(os.path.join(STEMS, k + ".flac")) for k in ("vocals", "drums", "bass", "other")}
sr = st["vocals"][1]
n = min(len(v[0]) for v in st.values())
mix = sum(v[0][:n] for v in st.values())
F = {k: feats(v[0][:n], sr) for k, v in st.items()}
F["mix"] = feats(mix, sr)
ref = float(np.percentile(F["mix"][0].cpu().numpy(), 95))   # 0 dB = the song's loud end (p95 of the mix)
beat = {k: (per_span(F[k][0], bedges) - ref).round(2).tolist() for k in F}
beat_flux = {k: per_span(F[k][1], bedges, power=False).round(4).tolist() for k in ("mix", "drums", "other")}
bar_db = per_span(F["mix"][0], baredges) - ref
mixb = np.array(beat["mix"])
# crunch: distorted-guitar energy (other stem, 2-6 kHz). In a room recording the loudness hardly moves (the PA and the
# room compress it); the explosion is the distortion coming in, so this carries the dynamics loudness misses.
cr_lev = feats(st["other"][0][:n], sr, band=(2000, 6000))[0]
cref = float(np.percentile(cr_lev.cpu().numpy(), 95))
beat["crunch"] = (per_span(cr_lev, bedges) - cref).round(2).tolist()
bar_crunch = per_span(cr_lev, baredges) - cref
accent = np.concatenate([[0.0], np.diff(mixb)]).clip(min=0).round(2).tolist()

# drum onsets per bar (peaks of the drum flux)
dfl = F["drums"][1].cpu().numpy()
thr = np.percentile(dfl, 90)
pk = [i for i in range(1, len(dfl) - 1) if dfl[i] > thr and dfl[i] >= dfl[i - 1] and dfl[i] >= dfl[i + 1]]
pk_t = np.array(pk) * HOP
drum_onsets_bar = [int(((pk_t >= a) & (pk_t < b)).sum()) for a, b in zip(baredges, baredges[1:])]

# intensity per bar (0..1): mix level + crunch + drum density, each z-scored over the song, then min-max scaled
def z(v): v = np.asarray(v, float); return (v - v.mean()) / (v.std() + 1e-9)
inten = z(bar_db) + z(bar_crunch) + 0.5 * z(drum_onsets_bar)
inten = ((inten - inten.min()) / (inten.max() - inten.min() + 1e-9)).round(3)

# builds / drops (bar level)
moves = []
for i in range(2, len(bar_db)):
    d = bar_db[i] - np.mean(bar_db[i - 2:i])
    if abs(d) >= 4.0:
        moves.append({"t": round(bars[i], 3), "bar": i, "type": "build" if d > 0 else "drop", "db": round(float(d), 1)})

# ---------------------------------------------------------------- transients (10 ms): guitar blasts, mix hits
def transients(lev, min_jump, pre=0.30, win=0.06, min_gap=0.15, floor_pct=60):
    a = lev.cpu().numpy() - ref
    floor = np.percentile(a, floor_pct)
    out, last = [], -9
    w, p = int(win / HOP), int(pre / HOP)
    for i in range(p, len(a) - w):
        base = np.median(a[i - p:i])
        peak = a[i:i + w].max()
        if peak - base >= min_jump and peak >= floor and a[i] - a[i - 1] > 3 and i * HOP - last >= min_gap:
            out.append((round(i * HOP, 2), round(float(peak - base), 1), round(float(peak), 1)))
            last = i * HOP
    return out


def sec_of(t):
    name = SECTIONS[0][0]
    for s, t0 in SEC:
        if t >= t0:
            name = s
    return name


def snap(t):
    j = int(np.argmin([abs(b - t) for b in bars]))
    return bars[j] if abs(bars[j] - t) <= 1.5 else t


SEC = [(s, (0.0 if t0 == 0 else round(snap(t0 - 0.3), 3))) for s, t0 in SECTIONS]
sections = [{"name": s, "t0": t0, "t1": (SEC[i + 1][1] if i + 1 < len(SEC) else dur),
             "first_line": t_line, "mix_db": round(float(np.mean([mixb[k] for k, b in enumerate(beats) if t0 <= b < (SEC[i + 1][1] if i + 1 < len(SEC) else dur)])), 1)}
            for i, ((s, t0), (_, t_line)) in enumerate(zip(SEC, SECTIONS))]


def beat_pos(t):
    k = max(0, int(np.searchsorted(beats, t, side="right")) - 1)
    return k, round((t - beats[k]) / (bs / 4), 2)


gtr = [{"t": t, "jump_db": j, "level_db": lv, "section": sec_of(t), "beat": beat_pos(t)[0], "beat_frac": beat_pos(t)[1]}
       for t, j, lv in transients(F["other"][0], min_jump=10.0)]
hits = [{"t": t, "jump_db": j, "level_db": lv, "section": sec_of(t)} for t, j, lv in transients(F["mix"][0], min_jump=8.0)]

# ---------------------------------------------------------------- crowd vs stage (standardized mics, same timeline)
crowd = {}
stage = (feats(mono(os.path.join(STD, f"{NUM}-{ARGS.stage}.wav"))[0], 48000, band=(300, 3500))[0]
         if ARGS.stage else None)
for cam in ([c for c in ARGS.crowd.split(",") if c] if stage is not None else []):
    p = os.path.join(STD, f"{NUM}-{cam}.wav")
    if os.path.exists(p):
        lev = feats(mono(p)[0], 48000, band=(300, 3500))[0]
        m = min(len(lev), len(stage))
        d = per_span(lev[:m] - stage[:m], bedges, power=False)
        alive = per_span(lev[:m], bedges) > float(np.percentile(lev.cpu().numpy(), 50)) - 30
        crowd[cam] = np.where(alive, d, np.nan)              # NaN = that mic wasn't recording
voc = np.array(beat["vocals"])
sung = voc > np.percentile(voc, 40)
crowd_idx = {k: [None if np.isnan(x) else round(float(x), 2) for x in (v - np.nanmedian(v[sung[:len(v)]]))]
             for k, v in crowd.items()}

# ---------------------------------------------------------------- lead pitch (CPU: librosa pyin)
import librosa
vx = st["vocals"][0][:n]
f0, vflag, vprob = librosa.pyin(vx, fmin=70, fmax=1000, sr=sr, frame_length=2048, hop_length=int(sr * HOP))
f0_beat, voiced_beat = [], []
for a0, a1 in zip(bedges, bedges[1:]):
    i0, i1 = int(a0 / HOP), max(int(a0 / HOP) + 1, int(a1 / HOP))
    seg, vf = f0[i0:i1], vflag[i0:i1]
    good = seg[vf & ~np.isnan(seg)]
    f0_beat.append(round(float(np.median(good)), 1) if len(good) >= 3 else None)
    voiced_beat.append(round(float(vf.mean()), 2) if len(vf) else 0.0)

hv, cur = [], None                                       # high-voice spans: voiced f0 > 330 Hz (about E4) >= 0.25 s
for i, (fv, vv) in enumerate(zip(f0, vflag)):
    on = bool(vv) and not np.isnan(fv) and fv > 330
    if on and cur is None:
        cur = [i, i, [fv]]
    elif on:
        cur[1] = i; cur[2].append(fv)
    elif cur is not None and i - cur[1] > 8:             # allow 80 ms gaps
        if (cur[1] - cur[0]) * HOP >= 0.25:
            hv.append({"t0": round(cur[0] * HOP, 2), "t1": round(cur[1] * HOP, 2), "f0_median": round(float(np.median(cur[2])), 0),
                       "note": librosa.hz_to_note(float(np.median(cur[2])), unicode=False), "section": sec_of(cur[0] * HOP)})
        cur = None
out = {"song": NUM, "title": SM.get("title"), "bpm": SM.get("bpm"), "bar_s": bs, "duration": dur,
       "reference": "0 dB = the song's loud end (p95 of the mix, A-weighted); every level is relative to it",
       "sections": sections, "bars": bars, "bar_mix_db": bar_db.round(2).tolist(), "drum_onsets_bar": drum_onsets_bar,
       "moves": moves, "beats": [round(b, 3) for b in beats], "beat_db": beat, "beat_flux": beat_flux,
       "beat_accent_db": accent, "bar_crunch_db": bar_crunch.round(2).tolist(), "bar_intensity": inten.tolist(),
       "crowd_idx": crowd_idx, "high_voice_spans": hv, "lead_f0": f0_beat, "lead_voiced": voiced_beat,
       "guitar_transients": gtr, "mix_hits": hits,
       "source": f"dynamics_map.py (stems: _derived/scratch/stems; crowd: _derived/std {ARGS.stage} vs {ARGS.crowd})"}
os.makedirs(os.path.join(ROOT, "metadata", "dynamics"), exist_ok=True)
json.dump(out, open(os.path.join(ROOT, "metadata", "dynamics", NUM + ".json"), "w", encoding="utf-8"), indent=1)

# ---------------------------------------------------------------- chart (CPU, matplotlib)
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
B = np.array(beats)
fig, ax = plt.subplots(5, 1, figsize=(26, 14), sharex=True, gridspec_kw={"height_ratios": [3, 2, 1.4, 1.4, 1.2]})
cols = ["#2b2b33", "#3a3346"]
for a in ax:
    for i, s in enumerate(sections):
        a.axvspan(s["t0"], s["t1"], color=cols[i % 2], zorder=0)
for s in sections:
    ax[0].text(s["t0"] + 0.5, 1.5, s["name"], color="#ddd", fontsize=10, va="top")
ax[0].plot(B, beat["mix"], color="#f0c040", lw=1.4, label="mix")
ax[0].bar(bars, bar_db, width=bs * 0.92, align="edge", color="#f0c040", alpha=0.18)
for m in moves:
    ax[0].annotate(("▲" if m["type"] == "build" else "▼") + f"{m['db']:+.0f}", (m["t"], 1), color="#ff6a50" if m["type"] == "build" else "#7ab0ff", fontsize=9, ha="center")
for g in gtr:
    ax[0].axvline(g["t"], color="#ff3b30", lw=0.8, alpha=0.8)
ax[0].set_ylim(-45, 3); ax[0].set_ylabel("mix dB")
ax0b = ax[0].twinx(); ax0b.step(bars, inten, where="post", color="#ff5a1e", lw=2.2, label="intensity"); ax0b.set_ylim(-1.5, 1.05)
ax0b.tick_params(colors="#ff5a1e"); ax[0].legend(loc="lower left"); ax0b.legend(loc="lower right")
for k, c in (("vocals", "#e9edf5"), ("drums", "#ff8c1e"), ("bass", "#7ab0ff"), ("other", "#ff3b30"), ("crunch", "#ffd60a")):
    ax[1].plot(B, beat[k], color=c, lw=1, label=k)
ax[1].set_ylim(-55, 3); ax[1].set_ylabel("stems dB"); ax[1].legend(loc="lower right", ncol=4)
ax[2].bar(B, accent, width=bs / 4 * 0.8, color="#f0c040"); ax[2].set_ylabel("accent dB")
for k, c in zip(crowd_idx, ("#e8a0b8", "#a8d8c8", "#f0c040", "#7ab0ff")):
    if True:
        ax[3].plot(B[:len(crowd_idx[k])], [np.nan if v is None else v for v in crowd_idx[k]], color=c, lw=1, label=f"crowd idx {k}")
ax[3].set_ylim(-12, 12); ax[3].axhline(0, color="#888", lw=0.6); ax[3].set_ylabel("crowd vs stage dB"); ax[3].legend(loc="lower right")
fb = np.array([np.nan if v is None else v for v in f0_beat])
ax[4].plot(B, fb, color="#e9edf5", lw=1); ax[4].set_yscale("log"); ax[4].set_ylabel("lead f0 Hz")
for h in hv:
    ax[4].axvspan(h["t0"], h["t1"], color="#ff5a1e", alpha=0.35)
ax[4].set_xlabel("song s"); ax[4].set_xlim(0, dur)
for a in ax:
    a.set_facecolor("#1a1a1f"); a.tick_params(colors="#bbb"); a.yaxis.label.set_color("#ccc")
fig.patch.set_facecolor("#121215")
os.makedirs(os.path.join(ROOT, "_derived", "check", "dynamics"), exist_ok=True)
fig.savefig(os.path.join(ROOT, "_derived", "check", "dynamics", NUM + ".png"), dpi=80, bbox_inches="tight")
print(f"{NUM}: {len(beats)} beats, {len(bars)} bars, {len(moves)} builds/drops, {len(gtr)} guitar transients, "
      f"{len(hits)} mix hits, crowd {list(crowd_idx)}, {len(hv)} high-voice spans")
