r"""portrait_zoom.py -- a vertical phone shot that slowly becomes (almost) horizontal, as a cached fragment. GPU only.

What the user asked for: a vertical clip, shown full height on a blurred fill, slowly zooms in until it is nearly
landscape, by cutting away the top (ceiling, wall) and a little of the bottom (floor, cables). Built by review:

1. LIMITS COME FROM THE BAND, NOT FROM A FIXED LINE. A hand-set top line (50 %) was right at the start of the shot
   and cut everyone's heads 40 s later, after the phone operator stepped closer. Per frame (pose data, e.g.
   extract_people.py's npz): top = highest person box top, bottom = lowest ankle (else box bottom); a +-2 s
   envelope (min for the top, max for the bottom) then a Gaussian (1.5 s) -- the window starts moving BEFORE anyone
   would be cut. Top gets HEADROOM (4 %) and is hard; bottom gets FOOTROOM (3 %), capped at BOT_MAX (92 %), soft.
2. QUALITY LIMIT FROM MEASURED DETAIL, NOT A PERCENTAGE. "Stop at 85 %" was arbitrary; the rule is never to
   enlarge a pixel -- and a phone's nominal 4K is not real 4K (noise reduction, HEVC, low light, stabilization).
   effective_detail() measures it: shrink the band region by f, enlarge it back; the smallest f that still
   matches (SSIM >= 0.9925, interpolated) is the real detail (~0.5 on a dim stage). The screen must get at least REAL_MIN
   (0.65 -- SSIM ~0.996, the framing the user called "balanced") of a true output picture:
       window height (source px) >= OUT_H * REAL_MIN / f_eff
   When the band needs less height than that, open half below the feet (up to BOT_MAX) and the rest above the
   heads (more wall/logo, less floor). Width is always the full frame: sharpness depends only on the height,
   so the picture ends a little narrower than the output with thin fill strips.
3. TIME IT IN PHRASES, NOT SECONDS. A 2.9x zoom from far away in 10 s felt dizzy, 13 s still too fast; the one the
   user called perfect ran 3 phrases (12 bars) from a phrase boundary to a phrase boundary (~0.05 log-zoom/s).
   Bigger zooms get more phrases. Log-space zoom toward a fixed point (top and bottom move in proportion),
   smootherstep. After the zoom the window keeps following the band.
4. SHAKE: window_stabilize.correction() -- slow sway only, half strength (full per-frame removal warped the image).
   The fill is DERIVED from the same stabilized window ("stabilize the data, let the derived data follow").

Cache: fragment() keys the output on everything that decides its pixels -- source (path, size, mtime), in/out,
frame count, pose file (size, mtime), every limit/quality/zoom/shake parameter, measured f_eff, output size,
encoder args and VERSION. Hit -> reused; miss -> rendered and stored with its spec beside it (<key>.mp4.json).
Same frame grid as render_cached.py (fps=30:start_time=0 on the clip's own clock), so fragments join with -c copy.

CLI test:  py portrait_zoom.py <clip> <pose.npz> <in_s> <out_s> <zoom_start_s> <zoom_end_s> <out.mp4>
           (zoom times are shot time; output 2560x1440 unless --size WxH)
"""
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys

import numpy as np
import torch
import torch.nn.functional as Fn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import window_stabilize as WS  # noqa: E402

VERSION = "pz-2"
HEADROOM, FOOTROOM, BOT_MAX = 0.04, 0.03, 0.92
REAL_MIN = 0.65
SSIM_OK = 0.9925   # calibrated: the reviewed dim-stage phone clip measures f_eff 0.50 with this GPU test
CACHE = os.environ.get("VIVECUT_PZ_CACHE", os.path.join("cache", "portrait_zoom"))
NVENC = ["-c:v", "av1_nvenc", "-preset", "p7", "-rc", "vbr", "-cq", "23", "-b:v", "0",
         "-pix_fmt", "yuv420p10le", "-color_range", "tv", "-color_primaries", "bt709",
         "-color_trc", "bt709", "-colorspace", "bt709"]
TONEMAP = "tonemapping=hable:colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv"


def _stat(p):
    st = os.stat(p)
    return [os.path.normcase(os.path.abspath(p)), st.st_size, int(st.st_mtime)]


def band_limits(npz):
    """Per pose frame: (t, top, bottom) as fractions of the frame height, already enveloped and smoothed."""
    d = np.load(npz)
    fps, fh = float(d["fps"]), float(d["frame_h"])
    fi, kp, box = d["frame_idx"], d["kp"], d["box"]
    n = int(fi.max()) + 1
    top, bot = np.full(n, np.nan), np.full(n, np.nan)
    for f in range(n):
        m = fi == f
        if not m.any():
            continue
        bx, k = box[m], kp[m]
        big = (bx[:, 3] - bx[:, 1]) > 0.05 * fh            # drop tiny false detections
        if not big.any():
            continue
        bx, k = bx[big], k[big]
        top[f] = bx[:, 1].min() / fh
        an = np.where(k[:, 15:17, 2] > 0.3, k[:, 15:17, 1], np.nan)
        feet = np.where(np.isfinite(an).any(1), np.nanmax(np.where(np.isfinite(an), an, -1), 1), bx[:, 3])
        bot[f] = feet.max() / fh
    t = np.arange(n) / fps
    ok = np.isfinite(top)
    top, bot = np.interp(t, t[ok], top[ok]), np.interp(t, t[ok], bot[ok])
    w = int(2 * fps)
    env_t = np.array([top[max(0, i - w):i + w + 1].min() for i in range(n)])
    env_b = np.array([bot[max(0, i - w):i + w + 1].max() for i in range(n)])
    g = np.exp(-0.5 * (np.arange(-3 * fps * 1.5, 3 * fps * 1.5 + 1) / (fps * 1.5)) ** 2)
    g /= g.sum()
    sm = lambda x: np.convolve(np.pad(x, len(g) // 2, mode="edge"), g, mode="valid")  # noqa: E731
    return t, np.clip(sm(env_t) - HEADROOM, 0, 1), np.clip(sm(env_b) + FOOTROOM, 0, BOT_MAX)


def _ssim(a, b):
    """SSIM of (N,1,H,W) tensors in 0..255, Gaussian 11x11 sigma 1.5, on the GPU."""
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    g = torch.exp(-0.5 * (torch.arange(-5, 6, device=a.device, dtype=a.dtype) / 1.5) ** 2)
    g = (g / g.sum())
    k = (g[:, None] * g[None, :])[None, None]
    bl = lambda x: Fn.conv2d(x, k, padding=5)  # noqa: E731
    ma, mb = bl(a), bl(b)
    va, vb, cab = bl(a * a) - ma * ma, bl(b * b) - mb * mb, bl(a * b) - ma * mb
    return (((2 * ma * mb + c1) * (2 * cab + c2)) / ((ma * ma + mb * mb + c1) * (va + vb + c2))).mean().item()


def effective_detail(clip, a, b, rows=(0.42, 0.86)):
    """Real detail as a fraction of the nominal pixels (cached next to the shake cache)."""
    key = hashlib.sha1(json.dumps(_stat(clip) + [round(a, 3), round(b, 3), rows, SSIM_OK, VERSION]).encode()).hexdigest()[:20]
    path = os.path.join(CACHE, "detail_" + key + ".json")
    if os.path.exists(path):
        return json.load(open(path))["f_eff"]
    sw, sh = WS._dims(clip)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-hwaccel", "cuda", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", clip,
                          "-vf", "fps=0.25,format=gray", "-f", "rawvideo", "-"], capture_output=True).stdout
    fr = np.frombuffer(raw, np.uint8).reshape(-1, sh, sw)[:, int(rows[0] * sh):int(rows[1] * sh)]
    x = torch.from_numpy(np.ascontiguousarray(fr)).to("cuda", torch.float32)[:, None]
    curve = {}
    for f in (0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3):
        sm = Fn.interpolate(x, scale_factor=f, mode="bicubic", antialias=True, align_corners=False)
        back = Fn.interpolate(sm, size=x.shape[-2:], mode="bicubic", align_corners=False).clamp(0, 255)
        curve[f] = _ssim(x, back)
    fs = sorted(curve)                                                    # 0.3 .. 0.9
    f_eff = 1.0
    for lo, hi in zip(fs, fs[1:]):                                        # first crossing from below, interpolated
        if curve[lo] < SSIM_OK <= curve[hi]:
            f_eff = lo + (hi - lo) * (SSIM_OK - curve[lo]) / (curve[hi] - curve[lo])
            break
    else:
        if curve[fs[0]] >= SSIM_OK:
            f_eff = fs[0]
    f_eff = round(f_eff, 3)
    os.makedirs(CACHE, exist_ok=True)
    json.dump({"clip": clip, "a": a, "b": b, "curve": curve, "f_eff": f_eff}, open(path, "w"), indent=1)
    return f_eff


def plan(clip, npz, a, b, m0, m1, out_h, step=0.5, amount=1.0, ref=None):
    """Keyframes (shot time u, crop_y, crop_h) every `step` s + the parameters that decided them."""
    sw, sh = WS._dims(clip)
    f_eff = effective_detail(clip, *(ref or (a, b)))   # whole shot: every piece of it frames identically
    min_h = out_h * REAL_MIN / f_eff
    t, top, bot = band_limits(npz)
    keys = []
    for u in np.arange(0, (b - a) + step, step):
        tc = u + a
        tp, bt = np.interp(tc, t, top) * sh, np.interp(tc, t, bot) * sh
        extra = max(0.0, min_h - (bt - tp))
        down = min(extra / 2, BOT_MAX * sh - bt) if bt < BOT_MAX * sh else 0.0
        bt += down
        tp = max(0.0, tp - (extra - down))
        che = max(bt - tp, min_h)
        p = min(1.0, max(0.0, (u - m0) / (m1 - m0)))
        p = amount * p * p * p * (p * (p * 6 - 15) + 10)   # amount < 1: a partial zoom (same speed, shorter trip)
        ch = math.exp(math.log(sh) + (math.log(che) - math.log(sh)) * p)
        cy = tp * (sh - ch) / (sh - che) if sh - che > 1 else tp
        keys.append((float(u), float(cy), float(ch)))
    return keys, {"f_eff": f_eff, "min_h": min_h, "sw": sw, "sh": sh}


def _lin(keys, i):
    terms = [f"between(t,{u0:.2f},{u1:.2f}-0.0001)*({a[i]:.2f}+({b[i] - a[i]:.3f})*(t-{u0:.2f})/{u1 - u0:.2f})"
             for (u0, *a), (u1, *b) in zip(keys, keys[1:])]
    blocks = ["(" + "+".join(terms[j:j + 32]) + ")" for j in range(0, len(terms), 32)]
    return "(" + "+".join(blocks) + f"+gte(t,{keys[-1][0] - 0.0001:.2f})*{keys[-1][i + 1]:.2f})"


def _banner(title, font, hold, fade, work, key):
    """title = (text, song-time offset of the fragment's first frame): bottom-left banner, fades out at hold+fade."""
    if not title:
        return ""
    os.makedirs(work, exist_ok=True)
    tf = os.path.join(work, key + ".title.txt")
    open(tf, "w", encoding="utf-8").write(title[0])
    esc = lambda p: p.replace("\\", "/").replace(":", r"\:")  # noqa: E731
    tt = f"(t+{title[1]})"
    return (f",drawtext=fontfile='{esc(font)}':textfile='{esc(tf)}':fontcolor=white:fontsize=64:box=1:"
            f"boxcolor=black@0.45:boxborderw=19:x=54:y=h-th-54:enable='lt({tt},{hold + fade})':"
            f"alpha='if(lt({tt},{hold}),1,max(0,1-({tt}-{hold})/{fade}))'")


def fragment(clip, npz, a, b, n_frames, m0, m1, out_w=2560, out_h=1440, stab=(0.15, 0.5), work="scratch",
             fill_sigma=8.0, amount=1.0, ref=None, title=None, font="C:/Windows/Fonts/segoeuib.ttf", title_hold=4.0,
             title_fade=0.5):
    # fill_sigma: user picked 8 of 4/8/12 at 640x360 (only light and colour, no recognisable people).
    # amount < 1: partial zoom at the same speed (a shot too short for the whole trip still moves).
    # ref: (a, b) clip-local range of the WHOLE shot, when this fragment is only a piece of it (a body or a
    #      transition handle): detail and shake are measured on the whole shot, so every piece frames and
    #      stabilises identically and the pieces join without a jump.
    # title: (text, song time of the first frame) -> the song banner, for a shot that opens a song.
    """Render (or reuse) one shot. a/b: clip-local in/out; m0/m1: zoom start/end in shot time.
    Returns (path, hit)."""
    keys, info = plan(clip, npz, a, b, m0, m1, out_h, amount=amount, ref=ref)
    spec = {"src": _stat(clip), "pose": _stat(npz), "in": round(a, 3), "out": round(b, 3), "frames": n_frames,
            "zoom": [round(m0, 3), round(m1, 3)], "limits": [HEADROOM, FOOTROOM, BOT_MAX], "real_min": REAL_MIN,
            "ssim_ok": SSIM_OK, "f_eff": info["f_eff"], "stab": list(stab), "shake": [WS.VERSION, WS.SIGMA_S, WS.ROI],
            "size": [out_w, out_h], "fill_sigma": fill_sigma, "amount": round(amount, 3), "ref": [round(x, 3) for x in ref] if ref else None, "title": title, "enc": NVENC, "v": VERSION}
    key = hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:20]
    path = os.path.join(CACHE, key + ".mp4")
    if os.path.exists(path) and os.path.exists(path + ".json"):
        return path, True
    sw, sh = info["sw"], info["sh"]
    CY, CH = _lin(keys, 0), _lin(keys, 1)
    CW, CX = f"{sw}", "0"
    if stab:
        ra, rb = ref or (a, b)                          # shake path of the whole shot, sliced to this piece
        c = WS.correction(WS.shake(clip, ra, rb), slow_s=stab[0], gain=stab[1])
        i0 = int(round((a - ra) * 30))
        idx = np.clip(np.arange(i0, i0 + n_frames), 0, len(c["t"]) - 1)
        c = {"t": np.arange(n_frames) / 30.0, "dx": c["dx"][idx], "dy": c["dy"][idx]}
        mx = max(1.0, float(np.ceil(np.percentile(np.abs(c["dx"]), 99.5))))   # narrow the width by the sway
        CW, CX = f"{sw - 2 * mx:.0f}", f"({mx:.0f}+{WS.table_expr(c['t'], np.clip(c['dx'], -mx, mx))})"
        CY = f"max(0,min({sh}-{CH},{CY}+{WS.table_expr(c['t'], c['dy'])}))"
    PW = f"({CW}*{out_h}/{CH})"
    BH = sw * 9 / 16
    BY = f"max(0,min({sh - BH:.1f},{CY}+{CH}/2-{BH / 2:.1f}))"          # the fill follows the same window
    cut = (f"[0:v]trim=start={max(0.0, a - 1):.4f}:end={b + 1:.4f},fps=30:start_time=0,"
           f"trim=start={(round(a * 30) - 0.5) / 30:.5f},setpts=PTS-STARTPTS,trim=end_frame={n_frames}[cut];")
    vf = (cut + "[cut]split=2[p][q];"
          f"[p]libplacebo=w=640:h=360:crop_w='{CW}':crop_h={BH:.1f}:crop_x='{CX}':crop_y='{BY}':{TONEMAP}:"
          f"format=yuv420p,gblur=sigma={fill_sigma},scale={out_w}:{out_h}[bg];"   # blur at 640x360
          f"[q]libplacebo=w={out_w}:h={out_h}:crop_w='{CW}':crop_h='{CH}':crop_x='{CX}':crop_y='{CY}':"
          f"pos_w='{PW}':pos_h={out_h}:pos_x='({out_w}-{PW})/2':pos_y=0:fillcolor=black@0:{TONEMAP}:"
          "format=yuva420p[fg];[bg][fg]overlay=0:0:format=auto" + _banner(title, font, title_hold, title_fade, work, key) +
          ",format=yuv420p10le[v]")
    os.makedirs(CACHE, exist_ok=True)
    os.makedirs(work, exist_ok=True)
    fc = os.path.join(work, key + ".fc.txt")                              # expressions are long: script file
    open(fc, "w").write(vf)
    tmp = path + ".part.mp4"
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-hwaccel", "cuda", "-init_hw_device", "vulkan",
                        "-ss", f"{max(0.0, a - 3):.3f}", "-copyts", "-start_at_zero", "-i", clip,
                        "-filter_complex_script", fc, "-map", "[v]", "-an"] + NVENC + [tmp],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr[-1500:])
    os.replace(tmp, path)
    json.dump(dict(spec, f_eff=info["f_eff"], min_h=info["min_h"]), open(path + ".json", "w"), indent=1)
    return path, False


if __name__ == "__main__":
    args = [x for x in sys.argv[1:] if not x.startswith("--")]
    size = next((x.split("=")[1] for x in sys.argv if x.startswith("--size=")), "2560x1440")
    ow, oh = map(int, size.split("x"))
    clip, npz, a, b, m0, m1, out = args[0], args[1], *map(float, args[2:6]), args[6]
    p, hit = fragment(clip, npz, a, b, round((b - a) * 30), m0, m1, ow, oh)
    shutil.copyfile(p, out)
    print(("cache hit " if hit else "rendered ") + p, "->", out)
