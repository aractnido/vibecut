r"""floor_energy.py -- crowd movement on the dance floor, per second, for fixed cameras that see the floor.

A band video usually wants the band by default and the crowd only when it gets going, so the director needs floor
movement kept apart from the band's. A fixed camera's floor is a fixed region of its frame. Find it by plotting a
whole-show heatmap of where people's feet land (box bottom centres from extract_people.py): band members are dense
static hotspots, guests spread across the floor. Write the regions into show.json:

  "floor": {"<cam>": [{"x_max": 0.40}, {"x_min": 0.72}, {"y_min": 0.88}]}

Each entry is a rectangle in frame fractions (any of x_min/x_max/y_min/y_max); a person is on the floor when the
median of their feet position falls in ANY rectangle.

Per floor track, per second (10 fps samples):
  present  seen in >= 5 of the second's 10 frames
  bounce   RMS of the shoulder-midpoint path around its own straight-line fit within the second (walking removed,
           bouncing and swaying kept), / the track's median box height (near/far scale-free)
  dancing  bounce >= DANCE_BOUNCE (0.03, calibrated on a full show: above the pauses' p90, near the songs' p75)
Writes floor/<cam>/<clip>.json: n_present[], n_dancing[], bounce_mean[] (one value per second).
"""
import glob
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import show_config  # noqa: E402

DANCE_BOUNCE = 0.03


def on_floor(rects, x, y):
    return any((x >= r.get("x_min", 0)) and (x <= r.get("x_max", 1)) and (y >= r.get("y_min", 0)) and
               (y <= r.get("y_max", 1)) for r in rects)


def main():
    cfg = show_config.load()
    for cam, rects in cfg["floor"].items():
        people = show_config.path(cfg["cameras"][cam]["people_dir"])
        out_dir = show_config.path(os.path.join("floor", cam))
        os.makedirs(out_dir, exist_ok=True)
        for f in sorted(glob.glob(os.path.join(people, "*.npz"))):
            d = np.load(f)
            tid, fi, box, kp = d["track_id"], d["frame_idx"], d["box"], d["kp"]
            fw = float(d["frame_w"]) if "frame_w" in d.files else 1920.0
            fh = float(d["frame_h"]) if "frame_h" in d.files else 1080.0
            n_sec = int(fi.max()) // 10 + 1 if len(fi) else 0
            n_present = np.zeros(n_sec, int)
            n_dancing = np.zeros(n_sec, int)
            bounce_sum = np.zeros(n_sec)
            for t in np.unique(tid):
                rows = np.flatnonzero(tid == t)
                fx = (box[rows, 0] + box[rows, 2]) / 2 / fw
                fy = box[rows, 3] / fh
                if not on_floor(rects, float(np.median(fx)), float(np.median(fy))):
                    continue
                h = np.median(box[rows, 3] - box[rows, 1]) + 1e-6
                sh_ok = (kp[rows, 5, 2] > 0.3) & (kp[rows, 6, 2] > 0.3)
                mid = (kp[rows, 5, :2] + kp[rows, 6, :2]) / 2
                sec = fi[rows] // 10
                for s in np.unique(sec):
                    m = (sec == s) & sh_ok
                    if m.sum() < 5:
                        continue
                    n_present[s] += 1
                    tt = fi[rows][m].astype(float)
                    resid = [mid[m, a] - np.polyval(np.polyfit(tt, mid[m, a], 1), tt) for a in (0, 1)]
                    b = float(np.sqrt(np.mean(np.square(resid[0]) + np.square(resid[1])))) / h
                    bounce_sum[s] += b
                    n_dancing[s] += b >= DANCE_BOUNCE
            name = os.path.basename(f)[:-4]
            json.dump({"fps": 1, "n_present": n_present.tolist(), "n_dancing": n_dancing.tolist(),
                       "bounce_mean": np.round(bounce_sum / np.maximum(n_present, 1), 4).tolist()},
                      open(os.path.join(out_dir, name + ".json"), "w", encoding="utf-8"))
            print(f"{cam} {name[:34]:34s} on floor max {n_present.max() if n_sec else 0:2d}, "
                  f"dancing max {n_dancing.max() if n_sec else 0:2d}")


if __name__ == "__main__":
    main()
