r"""portrait_zoom_band.py -- THE way to show a vertical phone shot: the band zoom. GPU only.

A vertical phone clip opens full height (cut below the venue ceiling) on a blurred fill, then zooms slowly, on the
song's phrases, until the band nearly fills a landscape frame. The side fills shrink as the picture widens, and the
zoom stops where the phone's real detail runs out. Built over many review rounds on a full show; the rules live in
portrait_zoom.py (limits from the band, quality from measured detail, timing in phrases, slow-sway stabilization).

This script is the decision layer every renderer should use:
  measure(clip, pose)  -> per-clip facts: portrait?, effective detail f_eff, max zoom, band limits, shake.
  zoom_plan(...)       -> the zoom for one shot: it ENDS on the last bar line before the shot's end (thin fills at the
                          cut), length from the liked speed (~0.05 log-zoom/s, >= 2 phrases; bigger zooms get more),
                          in whole bars; a partial trip at the same speed when the shot is too short. Not eligible (shake p95 > 150 px, or less than one
                          phrase after the first bar line) -> the plain framing: full height on the fill, still with
                          the start crop below the ceiling.
  render(...)          -> portrait_zoom.fragment() (cached, stabilized, fill derived from the same window).

Usage:
  py portrait_zoom_band.py measure <clip> <pose.npz>
  py portrait_zoom_band.py render <clip> <pose.npz> <in_s> <out_s> <song_t_of_in> <phrase_boundaries> <out.mp4>
        [--top auto|FRACTION] [--size 2560x1440]
    phrase_boundaries: a song map JSON with "phrases" [{"t0", "t1"}...] (song_map.py) or a comma list of song
    times, one per 4-bar phrase. pose.npz: extract_people.py output for the clip (vertical shape kept).
"""
import argparse
import json
import math
import os
import shutil
import subprocess
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portrait_zoom as PZ  # noqa: E402
import window_stabilize as WS  # noqa: E402

ZOOM_SPEED = 0.05        # log-zoom per second: the zoom the user called "perfect" (2.86x over 3 phrases)
MAX_SHAKE = 150.0        # px p95 raw shake: shakier clips keep the plain framing
MIN_PHRASES = 2


def measure(clip, pose, out_h=1440):
    """Per-clip facts the zoom needs (cache this per show: it decodes the clip on the GPU)."""
    sw, sh = WS._dims(clip)
    rec = {"clip": clip, "pose": pose, "size": [sw, sh], "portrait": sh > sw}
    if not rec["portrait"]:
        return rec
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", clip],
                               capture_output=True, text=True).stdout)
    f_eff = PZ.effective_detail(clip, 0.0, dur)
    min_h = out_h * PZ.REAL_MIN / f_eff
    rec.update(duration=round(dur, 2), f_eff=f_eff, min_window_h=round(min_h), max_zoom=round(sh / min_h, 2))
    if pose and os.path.exists(pose):
        t, top, bot = PZ.band_limits(pose)
        rec["band_summary"] = {"top_min": round(float(top.min()), 3), "bottom_max": round(float(bot.max()), 3),
                               "band_h_px_median": round(float(np.median((bot - top) * sh)))}
    sk = WS.shake(clip, 0.0, dur)
    rec["shake_p95_px"] = [round(float(np.percentile(np.abs(sk[k]), 95)), 1) for k in ("dx", "dy")]
    return rec


def phrase_bounds(spec):
    """Song-time phrase boundaries from a song map JSON or a comma list."""
    if os.path.exists(spec):
        m = json.load(open(spec, encoding="utf-8"))
        ph = m["phrases"]
        return [p["t0"] for p in ph] + [ph[-1]["t1"]]
    return [float(x) for x in spec.split(",") if x.strip()]


def zoom_plan(meta, song_t, length, bounds):
    """The zoom for one shot (times relative to the shot's first frame), or None for the plain framing."""
    if not meta.get("portrait") or not meta.get("max_zoom") or max(meta.get("shake_p95_px", [1e9])) > MAX_SHAKE:
        return None
    phrase = float(np.median(np.diff(bounds)))
    bars = [b0 + j * (b1 - b0) / 4 for b0, b1 in zip(bounds, bounds[1:]) for j in range(4)]
    starts = [x - song_t for x in bars if x - song_t > 0.1]
    ends = [x for x in starts if x <= length - 0.5]
    if not starts or not ends:
        return None
    bar = phrase / 4
    n_phr = max(MIN_PHRASES, math.ceil(math.log(meta["max_zoom"]) / (ZOOM_SPEED * phrase)))
    n_bars = int(round((ends[-1] - starts[0]) / bar))
    if n_bars < 4:                       # less than one phrase
        return None
    # The zoom ENDS on the last bar line before the shot's end, as many whole bars as fit at the liked speed: a
    # short shot that zoomed only at its start cut out with big fills into a full-width camera ("the sides jump").
    use = min(4 * n_phr, n_bars)
    return {"m0": ends[-1] - use * bar, "m1": ends[-1], "amount": min(1.0, use / (4 * n_phr)), "phrases": use / 4}


def render(clip, pose, a, b, song_t, bounds, out, top="auto", size=(2560, 1440), meta=None):
    meta = meta or measure(clip, pose, size[1])
    zp = zoom_plan(meta, song_t, b - a, bounds)
    n = round((b - a) * 30)
    if zp is None:                       # plain framing: the start window held still (amount 0)
        zp = {"m0": 0.0, "m1": 1.0, "amount": 0.0, "phrases": 0}
    path, hit = PZ.fragment(clip, pose, a, b, n, zp["m0"], zp["m1"], size[0], size[1], amount=zp["amount"],
                            ref=(a, b), top=top)
    shutil.copy(path, out)
    return zp, hit


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("measure")
    m.add_argument("clip"); m.add_argument("pose")
    r = sub.add_parser("render")
    for k in ("clip", "pose", "a", "b", "song_t", "phrases", "out"):
        r.add_argument(k)
    r.add_argument("--top", default="auto", help="start crop: auto (from the skeletons) or a fraction of the height")
    r.add_argument("--size", default="2560x1440")
    args = ap.parse_args()
    if args.cmd == "measure":
        print(json.dumps(measure(args.clip, args.pose), indent=1))
        return
    w, h = (int(x) for x in args.size.lower().split("x"))
    zp, hit = render(args.clip, args.pose, float(args.a), float(args.b), float(args.song_t),
                     phrase_bounds(args.phrases), args.out,
                     args.top if args.top == "auto" else float(args.top), (w, h))
    print(("plain framing (not eligible)" if not zp["amount"] else
           f"zoom {zp['phrases']:.2f} phrases {zp['m0']:.2f}-{zp['m1']:.2f}s, amount {zp['amount']:.2f}"),
          "(cache hit)" if hit else "", "->", args.out)


if __name__ == "__main__":
    main()
