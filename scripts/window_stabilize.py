"""Residual shake removal inside a crop window  -- general, for any shot rendered as a moving
crop of a bigger frame (phone vertical->horizontal zooms, GoPro pushes).

Idea: a crop window that uses part of the frame has spare margin around it. Measure how the picture shakes and
move the window the opposite way each frame: no extra crop, no enlargement, so no quality cost. Vertical margin
is whatever the window leaves; horizontally a full-width window is narrowed by the shake size (narrowing costs
only a little width, i.e. more fill -- sharpness depends on the height).

Measurement (frames decoded on the GPU, quarter size, on the shot's own 30 fps grid -- the same frames the render
uses): frame-to-frame global translation ON THE GPU (torch): pyramid Lucas-Kanade / Gauss-Newton with Tukey weights,
so moving people drop out, in a region of interest (default rows 25-95 %: band and stage, not the ceiling).
Phase correlation was tried and loses horizontally: evenly spaced wall planks alias the peak. The cumulative path is
smoothed with a Gaussian (SIGMA_S); shake = raw - smooth. Rotation is measured and reported but not corrected
(libplacebo has no per-frame small-angle rotation).

API:  shake(clip, a, b)  -> dict(t, dx, dy, rot_deg) per output frame, source pixels (displayed orientation);
      dx/dy = how far the CONTENT moved off its smooth path; the window FOLLOWS it (crop_x += dx, crop_y += dy)
      so the content stays put on screen.
Validated on a handheld phone shot (38-47 s of a song): mean abs frame difference after alignment 7.32 (none),
5.29 (GPU global LK, used), 5.99 (GPU phase correlation), 9.07 (CPU corners+LK+RANSAC, worse than nothing).
Cached in cache/shake/<key>.npz (key = clip size/mtime, a, b, smoothing, ROI, version).

Use correction(), not the raw shake: removing ALL of it frame by frame made the picture warp "like water when you
throw a stone" -- the phone's rolling shutter bends each frame, and parallax makes the depths around the locked one
slide. The user compared half / vertical-only / slow-sway-only / slow-sway-half and picked slow sway only at HALF
strength: the window moves smoothly, the jitter (which hides the bend) stays, the visible sway halves."""
import hashlib
import json
import os
import subprocess

import numpy as np
import torch
import torch.nn.functional as Fn

CACHE = os.environ.get("VIVECUT_SHAKE_CACHE", os.path.join("cache", "shake"))   # relative to the show folder
VERSION = "shake-3-glk"
SIGMA_S = float(os.environ.get("SHAKE_SIGMA", "0.6"))   # seconds: slower motion is kept (walking, drift)
ROI = (0.25, 0.95)
DOWN = 4


def _dims(clip):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height:stream_side_data=rotation", "-of", "json", clip],
                       capture_output=True, text=True)
    st = json.loads(r.stdout)["streams"][0]
    rot = next((abs(int(d.get("rotation", 0))) for d in st.get("side_data_list", []) if "rotation" in d), 0)
    w, h = st["width"], st["height"]
    return (h, w) if rot in (90, 270) else (w, h)


def _frames(clip, a, b, sw, sh):
    n = round((b - a) * 30)
    w, h = sw // DOWN, sh // DOWN
    vf = (f"trim=start={max(0.0, a - 1):.4f}:end={b + 1:.4f},fps=30:start_time=0,"
          f"trim=start={(round(a * 30) - 0.5) / 30:.5f},setpts=PTS-STARTPTS,trim=end_frame={n},"
          f"scale={w}:{h},format=gray")
    raw = subprocess.run(["ffmpeg", "-v", "error", "-hwaccel", "cuda", "-ss", f"{max(0.0, a - 3):.3f}", "-copyts",
                          "-start_at_zero", "-i", clip, "-vf", vf, "-f", "rawvideo", "-"],
                         capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w)


def global_lk(x, levels=3, iters=6):
    """x: (N,H,W) float cuda. Returns d (N-1,2) = shift of frame i+1 relative to i (pixels), robust Gauss-Newton."""
    a0, b0 = x[:-1, None], x[1:, None]
    d = torch.zeros(len(a0), 2, device=x.device)
    for lv in reversed(range(levels)):
        s = 2 ** lv
        a = Fn.avg_pool2d(a0, s) if s > 1 else a0
        b = Fn.avg_pool2d(b0, s) if s > 1 else b0
        a = Fn.avg_pool2d(a, 3, 1, 1); b = Fn.avg_pool2d(b, 3, 1, 1)
        n, _, h, w = a.shape
        gy, gx = torch.meshgrid(torch.linspace(-1, 1, h, device=x.device), torch.linspace(-1, 1, w, device=x.device), indexing="ij")
        Ix = (a[..., :, 2:] - a[..., :, :-2])[..., 1:-1, :] / 2
        Iy = (a[..., 2:, :] - a[..., :-2, :])[..., :, 1:-1] / 2
        ac = a[..., 1:-1, 1:-1]
        for _ in range(iters):
            dl = d / s
            grid = torch.stack([gx + dl[:, 0, None, None] * 2 / (w - 1), gy + dl[:, 1, None, None] * 2 / (h - 1)], -1)
            bw = Fn.grid_sample(b, grid, align_corners=True, padding_mode="border")[..., 1:-1, 1:-1]
            r = bw - ac
            c = 4.685 * 1.4826 * r.abs().flatten(1).median(1).values.clamp(min=1e-3)[:, None, None, None]
            wgt = ((1 - (r / c) ** 2).clamp(min=0)) ** 2            # Tukey: moving people get weight 0
            A11 = (wgt * Ix * Ix).sum((1, 2, 3)); A12 = (wgt * Ix * Iy).sum((1, 2, 3)); A22 = (wgt * Iy * Iy).sum((1, 2, 3))
            b1 = -(wgt * Ix * r).sum((1, 2, 3)); b2 = -(wgt * Iy * r).sum((1, 2, 3))
            det = (A11 * A22 - A12 ** 2).clamp(min=1e-6)
            step = torch.stack([(A22 * b1 - A12 * b2) / det, (A11 * b2 - A12 * b1) / det], 1)
            d = d + step * s
    return d


def _smooth(x, sigma):
    r = int(3 * sigma)
    g = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    g /= g.sum()
    return np.convolve(np.pad(x, r, mode="reflect"), g, mode="valid")


def shake(clip, a, b):
    st = os.stat(clip)
    key = hashlib.sha1(json.dumps([os.path.normcase(clip), st.st_size, int(st.st_mtime), round(a, 3), round(b, 3),
                                   SIGMA_S, ROI, DOWN, VERSION]).encode()).hexdigest()[:20]
    path = os.path.join(CACHE, key + ".npz")
    if os.path.exists(path):
        d = np.load(path)
        return {k: d[k] for k in d.files}
    sw, sh = _dims(clip)
    fr = _frames(clip, a, b, sw, sh)
    y0, y1 = int(ROI[0] * fr.shape[1]), int(ROI[1] * fr.shape[1])
    roi = fr[:, y0:y1]
    steps = []
    for i in range(0, len(roi) - 1, 128):                  # chunks of 129 frames (1 overlap), uploaded per chunk
        x = torch.from_numpy(np.ascontiguousarray(roi[i:i + 129])).to("cuda", torch.float32)
        steps.append(global_lk(x).cpu().numpy())
    st_ = np.concatenate(steps) * DOWN
    dx, dy = [0.0] + list(st_[:, 0]), [0.0] + list(st_[:, 1])
    dr = [0.0] * len(dx)
    px, py, pr = np.cumsum(dx), np.cumsum(dy), np.cumsum(dr)
    s = SIGMA_S * 30
    out = {"t": np.arange(len(px)) / 30.0, "dx": px - _smooth(px, s), "dy": py - _smooth(py, s),
           "rot_deg": pr - _smooth(pr, s)}
    os.makedirs(CACHE, exist_ok=True)
    np.savez(path, **out)
    return out


def correction(sk, slow_s=0.15, gain=0.5, axes="xy"):
    """Window offsets to apply (source px): sway slower than ~slow_s seconds, times gain (user's pick: 0.15 s, 0.5)."""
    out = {"t": sk["t"]}
    for k in ("dx", "dy"):
        v = _smooth(sk[k], slow_s * 30) if slow_s > 0 else sk[k]
        out[k] = v * gain if k[1] in axes else v * 0
    return out


def table_expr(t, v, eps=0.25):
    """Per-frame step function for a libplacebo expression: sum of between(t, ...)*v, near-zero frames dropped.
    Terms are grouped in parenthesised blocks of 32 so ffmpeg's left-deep '+' tree stays shallow."""
    terms = [f"between(t,{ti - 0.5 / 30:.4f},{ti + 0.5 / 30 - 1e-4:.4f})*{vi:.2f}"
             for ti, vi in zip(t, v) if abs(vi) >= eps]
    if not terms:
        return "(0)"
    blocks = ["(" + "+".join(terms[i:i + 32]) + ")" for i in range(0, len(terms), 32)]
    return "(" + "+".join(blocks) + ")"


if __name__ == "__main__":
    import sys
    s = shake(sys.argv[1], float(sys.argv[2]), float(sys.argv[3]))
    for k in ("dx", "dy", "rot_deg"):
        v = s[k]
        print(f"{k}: p95 {np.percentile(np.abs(v), 95):.1f}  max {np.abs(v).max():.1f}")
    sec = np.abs(s["dy"]).reshape(-1)[: len(s["t"]) // 30 * 30].reshape(-1, 30).max(1)
    print("worst seconds (dy px):", sorted(enumerate(sec.round(1)), key=lambda x: -x[1])[:6])
