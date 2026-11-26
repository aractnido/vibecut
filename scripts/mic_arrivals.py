r"""mic_arrivals.py -- arrival-time structure between room mics, per band: where the sound sources are relative to each
mic, from audio alone (no multitrack needed).

Input: aligned per-song WAVs, one per source, all on the same song timeline (e.g. <show>/std/NN-<source>.wav from the
audio standardization step: offset + drift fitted, verified within ~1 ms).
For every mic pair and band (bass 40-200 Hz, low-mid 200-1k, mid 1k-4k, high 4k-10k) it averages tapered GCC-PHAT over
2 s windows (hop 1 s) where both mics recorded, and lists the strongest peaks within +-40 ms.

How to read it (lag > 0 = the sound reaches mic B later than mic A):
  - one sharp peak = both mics dominated by the same source (often one PA speaker);
  - extra peaks at FIXED lags in every song = other sources (the far PA, a stage amp, the drums, subs). The lag
    DIFFERENCES are geometry (1 ms = 0.343 m); the cameras' own clocks cancel out. Confirm on two songs.
  - a mic with several peaks of similar height hears several sources equally: the "room" mic. Layering a close mic
    under it can only match one of those arrivals (comb filtering otherwise).
CPU on purpose (numpy FFT on short windows, process pool): a song takes seconds.

Usage: py mic_arrivals.py <std_dir> NN <sourceA> <sourceB> [<sourceA2> <sourceB2> ...] [--out <png>]
       e.g. mic_arrivals.py D:/show/_derived/std 22 anchor GoPro2 anchor Nikon Phone1 anchor
Out: <png> (default <std_dir>/arrivals-NN.png) and the same name .json.
"""
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, find_peaks, sosfiltfilt, windows

SR = 48000
BANDS = {"bass 40-200 Hz": (40, 200), "low-mid 200-1k": (200, 1000), "mid 1k-4k": (1000, 4000),
         "high 4k-10k": (4000, 10000)}
MAXLAG = int(0.040 * SR)
WIN, HOP = 2 * SR, SR


def load(path):
    sr, x = wavfile.read(path)
    if sr != SR:
        raise SystemExit(f"{path}: expected {SR} Hz (run the audio standardization first)")
    return x.mean(1) if x.ndim == 2 else x


def gcc_avg(args):
    a, b, lo, hi = args
    sos = butter(4, [lo, hi], "bandpass", fs=SR, output="sos")
    a, b = sosfiltfilt(sos, a), sosfiltfilt(sos, b)
    tw = windows.tukey(WIN, 0.2)
    acc, n = np.zeros(2 * MAXLAG + 1), 0
    for s in range(0, min(len(a), len(b)) - WIN, HOP):
        x, y = a[s:s + WIN], b[s:s + WIN]
        if np.abs(x).max() < 1e-4 or np.abs(y).max() < 1e-4:      # one mic not recording here
            continue
        X, Y = np.fft.rfft(x * tw, 2 * WIN), np.fft.rfft(y * tw, 2 * WIN)
        P = Y * np.conj(X)
        P /= np.abs(P) + 1e-12
        c = np.fft.irfft(P, 2 * WIN)
        acc += np.concatenate([c[-MAXLAG:], c[:MAXLAG + 1]])
        n += 1
    return acc / max(n, 1), n


def main():
    args = sys.argv[1:]
    out = None
    if "--out" in args:
        out = args[args.index("--out") + 1]
        del args[args.index("--out"):args.index("--out") + 2]
    if len(args) < 4 or len(args[2:]) % 2:
        raise SystemExit(__doc__)
    std, num, names = args[0], args[1], args[2:]
    pairs = list(zip(names[::2], names[1::2]))
    data = {k: load(os.path.join(std, f"{num}-{k}.wav")) for k in set(names)}
    jobs = [(data[a], data[b], lo, hi) for a, b in pairs for lo, hi in BANDS.values()]
    with ProcessPoolExecutor(max(1, min(len(jobs), (os.cpu_count() or 4) - 2, 61))) as pool:
        res = list(pool.map(gcc_avg, jobs))
    lags = np.arange(-MAXLAG, MAXLAG + 1) / SR * 1000
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(len(pairs), 1, figsize=(13, 3.0 * len(pairs)), sharex=True)
    report, i = {}, 0
    for ax, (a, b) in zip(np.atleast_1d(axes), pairs):
        key = f"{a} -> {b}"
        report[key] = {}
        for (band, _), col in zip(BANDS.items(), ["#c0392b", "#e67e22", "#2e86c1", "#16a085"]):
            c, n = res[i]
            i += 1
            c = c / (np.abs(c).max() + 1e-12)
            pk, pr = find_peaks(c, height=0.25, distance=int(0.0015 * SR))
            top = sorted(zip(pr["peak_heights"], lags[pk]), reverse=True)[:4]
            report[key][band] = {"windows": n, "peaks_ms": [round(float(l), 2) for _, l in top],
                                 "heights": [round(float(h), 2) for h, _ in top]}
            ax.plot(lags, c, color=col, lw=1.2, label=f"{band}: " + ", ".join(f"{l:+.1f}" for _, l in top) + " ms")
        ax.axvline(0, color="#888", lw=0.6)
        ax.set_title(f"{key}   (lag > 0: sound reaches {b} later)", fontsize=11, loc="left")
        ax.legend(fontsize=8.5, loc="upper right")
        ax.set_ylim(-0.3, 1.05)
    np.atleast_1d(axes)[-1].set_xlabel("lag (ms)   1 ms = 0.343 m")
    fig.suptitle(f"Song {num}: arrival-time structure between mics (GCC-PHAT, averaged over the song)", fontsize=13)
    fig.tight_layout()
    png = out or os.path.join(std, f"arrivals-{num}.png")
    fig.savefig(png, dpi=110)
    json.dump(report, open(os.path.splitext(png)[0] + ".json", "w"), indent=1)
    for k, v in report.items():
        print(k)
        for band, r in v.items():
            print(f"   {band:16s} peaks {r['peaks_ms']} (heights {r['heights']}, {r['windows']} windows)")
    print("plot:", png)


if __name__ == "__main__":
    main()
