r"""edit_stats.py -- is the edit cutting too much, or randomly? Statistics from the per-song render sidecars.

Reads every <song>.mp4.json (shots with source + render_time_start/end, phrase_boundaries, style) and reports:
  - camera changes per minute and camera-take length (consecutive pieces on one camera merged): median, p10/p90,
    histogram -- reference: music videos cut every 2-3 s, live concert films ~4-8 s;
  - per style (harsh/soft) -- soft songs should hold LONGER, not just dissolve;
  - per camera: takes, share of time, median take;
  - cuts that land on a phrase boundary (+-0.15 s);
  - ping-pong A -> B -> A with B shorter than 8 s, and very short takes (< 4 s) -- what reads as random;
  - the camera transition matrix and next-camera entropy (vs. random among the other cameras).
Usage: py edit_stats.py <folder with *.mp4.json>"""
import collections
import glob
import json
import math
import os
import sys

import numpy as np


def main(folder):
    songs = [json.load(open(f, encoding="utf-8")) for f in sorted(glob.glob(os.path.join(folder, "*.mp4.json")))]
    L, by_style, by_cam, trans = [], collections.defaultdict(list), collections.defaultdict(list), collections.Counter()
    ping = short = on_phrase = cuts = 0
    total = 0.0
    for s in songs:
        takes = []
        for x in s["shots"]:
            d = x["render_time_end"] - x["render_time_start"]
            if takes and takes[-1][0] == x["source"]:
                takes[-1][1] += d
            else:
                takes.append([x["source"], d, x["render_time_start"]])
        total += s["shots"][-1]["render_time_end"]
        for c, d, _ in takes:
            L.append(d)
            by_style[s.get("style", "-")].append(d)
            by_cam[c].append(d)
            short += d < 4
        for a, b in zip(takes, takes[1:]):
            trans[(a[0], b[0])] += 1
        ping += sum(a[0] == c[0] and b[1] < 8 for a, b, c in zip(takes, takes[1:], takes[2:]))
        pb = np.array(s.get("phrase_boundaries", []))
        for t in takes[1:]:
            cuts += 1
            on_phrase += bool(len(pb)) and float(np.min(np.abs(pb - t[2]))) < 0.15
    L = np.array(L)
    print(f"{len(songs)} songs, {total / 60:.1f} min, {cuts} camera changes = {cuts / (total / 60):.2f}/min")
    print(f"take length: median {np.median(L):.1f} s, p10 {np.percentile(L, 10):.1f}, p90 {np.percentile(L, 90):.1f}, "
          f"min {L.min():.1f}, max {L.max():.1f}")
    print("histogram:", {f"{a}-{b}s": int(((L >= a) & (L < b)).sum()) for a, b in
                         [(0, 4), (4, 8), (8, 16), (16, 32), (32, 64), (64, 9999)]})
    for k, v in by_style.items():
        print(f"style {k}: {len(v)} takes, median {np.median(v):.1f} s")
    for k, v in by_cam.items():
        print(f"camera {k}: {len(v)} takes, {sum(v) / L.sum():.0%} of the time, median {np.median(v):.1f} s")
    print(f"cuts on a phrase boundary: {on_phrase}/{cuts} = {on_phrase / max(cuts, 1):.0%}")
    print(f"ping-pong A-B-A (B < 8 s): {ping}   takes under 4 s: {short}")
    cams, tot = sorted(by_cam), sum(trans.values())
    print("transitions (% of cuts):")
    for a in cams:
        print(f"  {a:>10s} -> " + "  ".join(f"{b}: {100 * trans[(a, b)] / tot:.1f}" for b in cams if b != a))
    H = 0.0
    for a in cams:
        n = sum(trans[(a, b)] for b in cams)
        if n:
            H += n / tot * -sum(trans[(a, b)] / n * math.log2(trans[(a, b)] / n) for b in cams if trans[(a, b)])
    print(f"next-camera entropy {H:.2f} bits (random among the others: {math.log2(max(len(cams) - 1, 1)):.2f})")


if __name__ == "__main__":
    main(sys.argv[1])
