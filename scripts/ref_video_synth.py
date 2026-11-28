"""Step 2 of the reference-video synthesis (a reference video -> a shot-by-shot grammar for the director).
Input: ref_video_scan.py outputs (gray230.npy, scan.npz) in <refdir>; our song's stems and dynamics map (NN).

1. Unique frames (files often repeat frames; e.g. one reference held only ~16 new frames/s).
2. Cuts: structural correlation (ZNCC of band-passed 115 px gray) between consecutive unique frames; a cut needs the
   frames before AND after to disagree (a one-frame flash inside a shot is rejected). Gradual transitions (fades to or
   from black/white, dissolves) from the luminance trend and a two-frame blend test.
3. Motion per shot: global similarity (zoom, rotation, pan) between consecutive unique frames by batched Gauss-Newton
   Lucas-Kanade on a 3-level pyramid, on the GPU (torch), robust (Huber) weights, high-passed frames (light flashes
   don't count as motion). Self-test on a synthetic warp first.
4. People per shot: YOLO11x-pose (CUDA) on 3 full-resolution frames (NVDEC): persons, largest box height share, head
   size share (shot size: ECU/CU/MCU/MS/WS), centre of the main subject.
5. Light per shot: mean luma, near-black share of the lower 40 % (silhouette foreground), dominant hue.
6. Music: the official audio's beat grid (librosa, CPU) and a chroma DTW to our performance (GPU STFT/chroma, CPU DTW)
   -> every shot's start/end in OUR song time, our bar, our section.
Subjects (who/what each shot shows) are labelled by a person or an agent looking at the contact sheets and written
into the JSON's shots[i]["subject"] afterwards.
Out: metadata/reference/NN-<name>.json, contact sheets _derived/check/ref<NN>/shots_XX.jpg.
Usage: python ref_video_synth.py <refdir> NN [name]   (show root: VIBECUT_SHOW_ROOT, default the current directory)"""
import json, math, os, subprocess, sys, time
import numpy as np, torch, torch.nn.functional as F
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stage_text as STX

ROOT = os.environ.get("VIBECUT_SHOW_ROOT", os.getcwd())
REF, NUM = sys.argv[1], sys.argv[2]
NAME = sys.argv[3] if len(sys.argv) > 3 else "official"
SRC = str(np.load(os.path.join(REF, "scan.npz"))["src"])
CHK = os.path.join(ROOT, "_derived", "check", f"ref{NUM}")
os.makedirs(CHK, exist_ok=True)
dev = "cuda"
z = np.load(os.path.join(REF, "scan.npz"))
fps, dur = float(z["fps"]), float(z["duration"])
G = torch.from_numpy(np.load(os.path.join(REF, "gray230.npy"))).to(dev).float()   # [N,179,230]
N, GH, GW = G.shape
mad = z["mad"]
U = np.array([0] + [i for i in range(1, N) if mad[i] >= 0.002])
T = U / fps


def gb(x, s):
    k = int(s * 3) * 2 + 1; t = torch.arange(k, device=dev) - k // 2; g = torch.exp(-t ** 2 / (2 * s * s)); g /= g.sum()
    x = F.conv2d(F.pad(x, (k // 2, k // 2, 0, 0), mode="reflect"), g.view(1, 1, 1, k))
    return F.conv2d(F.pad(x, (0, 0, k // 2, k // 2), mode="reflect"), g.view(1, 1, k, 1))


# ---------------------------------------------------------------- 2. cuts and transitions
X = F.avg_pool2d(G[torch.from_numpy(U).to(dev)][:, None], 2)
Bp = gb(X, 1.0) - gb(X, 6.0)
Bp = Bp - Bp.mean((2, 3), keepdim=True); Bp = Bp / (Bp.flatten(1).norm(dim=1).view(-1, 1, 1, 1) + 1e-6)
def corr(a, b): return (Bp[a] * Bp[b]).sum().item()
M = len(U)
c1 = np.zeros(M); c1[1:] = (Bp[1:] * Bp[:-1]).sum((1, 2, 3)).cpu().numpy()
lumU = z["lum"][U]
cuts = []
for k in range(1, M - 1):
    if c1[k] < 0.30 and c1[k] <= c1[k - 1] + 1e-9 and c1[k] <= c1[k + 1] + 1e-9:
        across = corr(k + 1, k - 1) if k >= 1 else 0
        if across < 0.45 and (not cuts or T[k] - T[cuts[-1]] >= 0.25):
            cuts.append(k)
# gradual transitions: a run of moderate change whose ends disagree (dissolve / fade) and no hard cut inside
grad = []
k = 2
while k < M - 8:
    if c1[k] < 0.75 and k not in cuts:
        j = k
        while j < M - 1 and c1[j + 1] < 0.8 and (j + 1) not in cuts and T[j + 1] - T[k] < 2.5:
            j += 1
        if T[j] - T[k] >= 0.25 and corr(max(0, k - 1), min(M - 1, j + 1)) < 0.25:
            a, b = max(0, k - 1), min(M - 1, j + 1)
            m = (a + b) // 2
            Am, Bm, Cm = Bp[a].flatten(), Bp[b].flatten(), Bp[m].flatten()
            A2 = torch.stack([Am, Bm], 1)
            sol = torch.linalg.lstsq(A2, Cm[:, None]).solution
            blend_r = (A2 @ sol - Cm[:, None]).norm().item()
            la, lb, lm = lumU[a], lumU[b], lumU[m]
            kind = ("fade_from_black" if la < 0.03 else "fade_to_black" if lb < 0.03 else
                    "flash" if lm > max(la, lb) + 0.15 else "dissolve" if blend_r < 0.75 else "fast_move")
            grad.append({"t0": round(T[a], 3), "t1": round(T[b], 3), "kind": kind, "blend_residual": round(blend_r, 3)})
            k = j + 1
            continue
    k += 1
grad = [g for g in grad if g["kind"] != "fast_move"]
bounds = [0] + cuts + [M]
shots = []
for i in range(len(bounds) - 1):
    a, b = bounds[i], bounds[i + 1] - 1
    if b < a:
        continue
    shots.append({"i": len(shots), "u0": int(a), "u1": int(b), "t0": round(T[a], 3),
                  "t1": round(T[b + 1], 3) if b + 1 < M else round(dur, 3)})
print(f"{M} unique frames, {len(cuts)} hard cuts, {len(grad)} gradual transitions, {len(shots)} shots")

# ---------------------------------------------------------------- 3. motion: batched GPU Lucas-Kanade (similarity)
def hp(x):                                                    # high-pass + normalise: light changes are not motion
    y = gb(x, 0.8) - gb(x, 8.0)
    return y / (y.flatten(1).std(dim=1).view(-1, 1, 1, 1) + 1e-6)


def lk(A, B, iters=(12, 10, 8)):
    """A, B: [n,1,h,w]. Returns p [n,4] = (a, b, tx, ty) with x_A = [[1+a,-b],[b,1+a]](x_B - c) + c + t (px, finest)."""
    n = A.shape[0]
    p = torch.zeros(n, 4, device=dev)
    pyr = [(A, B)]
    for _ in range(2):
        pyr.append((F.avg_pool2d(pyr[-1][0], 2), F.avg_pool2d(pyr[-1][1], 2)))
    for lvl in range(2, -1, -1):
        a_, b_ = pyr[lvl]
        h, w = a_.shape[-2:]
        if lvl < 2:
            p[:, 2:] *= 2
        ys, xs = torch.meshgrid(torch.arange(h, device=dev, dtype=torch.float32), torch.arange(w, device=dev, dtype=torch.float32), indexing="ij")
        cx, cy = (w - 1) / 2, (h - 1) / 2
        xt, yt = xs - cx, ys - cy
        border = ((xs > 3) & (xs < w - 4) & (ys > 3) & (ys < h - 4)).float()
        for _ in range(iters[lvl]):
            a, b, tx, ty = (p[:, i].view(-1, 1, 1) for i in range(4))
            xa = (1 + a) * xt - b * yt + cx + tx
            ya = b * xt + (1 + a) * yt + cy + ty
            grid = torch.stack([xa / (w - 1) * 2 - 1, ya / (h - 1) * 2 - 1], -1)
            Aw = F.grid_sample(a_, grid, mode="bilinear", padding_mode="border", align_corners=True)
            gx = F.grid_sample(F.pad(a_[..., :, 2:] - a_[..., :, :-2], (1, 1, 0, 0)) / 2, grid, mode="bilinear", padding_mode="border", align_corners=True)[:, 0]
            gy = F.grid_sample(F.pad(a_[..., 2:, :] - a_[..., :-2, :], (0, 0, 1, 1)) / 2, grid, mode="bilinear", padding_mode="border", align_corners=True)[:, 0]
            r = (Aw[:, 0] - b_[:, 0])
            inside = ((xa >= 0) & (xa <= w - 1) & (ya >= 0) & (ya <= h - 1)).float() * border
            s = 1.4826 * (r.abs() * inside).flatten(1).median(dim=1).values.view(-1, 1, 1) + 1e-6
            wgt = inside * torch.clamp(1.5 * s / (r.abs() + 1e-6), max=1.0)                 # Huber
            J = torch.stack([gx * xt + gy * yt, -gx * yt + gy * xt, gx, gy], -1)           # [n,h,w,4]
            Hm = torch.einsum("nhwi,nhwj,nhw->nij", J, J, wgt) + 1e-3 * torch.eye(4, device=dev)
            g = torch.einsum("nhwi,nhw->ni", J, r * wgt)
            p = p - torch.linalg.solve(Hm, g)
    return p


# self-test: a known zoom + pan
with torch.no_grad():
    base = G[U[len(U) // 3]][None, None]
    h, w = base.shape[-2:]
    ys, xs = torch.meshgrid(torch.arange(h, device=dev, dtype=torch.float32), torch.arange(w, device=dev, dtype=torch.float32), indexing="ij")
    zt, txt, tyt = 1.02, 1.5, -0.8
    cx, cy = (w - 1) / 2, (h - 1) / 2
    xa = (xs - cx) / zt + cx + txt; ya = (ys - cy) / zt + cy + tyt
    warped = F.grid_sample(base, torch.stack([xa / (w - 1) * 2 - 1, ya / (h - 1) * 2 - 1], -1)[None], align_corners=True)
    pt = lk(hp(base), hp(warped))[0]
    zest = 1 / math.hypot(1 + pt[0].item(), pt[1].item())
    print(f"LK self-test: zoom true {zt} est {zest:.4f}; pan true ({txt},{tyt}) est ({pt[2].item():.2f},{pt[3].item():.2f})")

pairs = [(U[k - 1], U[k]) for s in shots for k in range(s["u0"] + 1, s["u1"] + 1)]
P = torch.zeros(len(pairs), 4, device=dev)
t0 = time.time()
with torch.no_grad():
    for i in range(0, len(pairs), 256):
        ia = torch.tensor([q[0] for q in pairs[i:i + 256]], device=dev)
        ib = torch.tensor([q[1] for q in pairs[i:i + 256]], device=dev)
        P[i:i + 256] = lk(hp(G[ia][:, None]), hp(G[ib][:, None]))
print(f"LK on {len(pairs)} frame pairs in {time.time() - t0:.1f} s")
P = P.cpu().numpy()
zoom = 1 / np.hypot(1 + P[:, 0], P[:, 1])                     # > 1 = zooming in
rot = np.degrees(np.arctan2(P[:, 1], 1 + P[:, 0]))
pan_x, pan_y = -P[:, 2] / GW, -P[:, 3] / GH                   # fraction of frame per unique step (+x = content moves right)
pi = 0
for s in shots:
    m = s["u1"] - s["u0"]
    zl = np.log(zoom[pi:pi + m]); px = pan_x[pi:pi + m]; py = pan_y[pi:pi + m]
    pi += m
    d = max(1e-3, s["t1"] - s["t0"])
    s["dur"] = round(d, 3)
    if m == 0:
        s["motion"] = {"zoom_total": 1.0, "zoom_rate": 0, "pan_x_rate": 0, "pan_y_rate": 0, "steps": 0}
        continue
    tot = float(zl.sum())
    s["motion"] = {"zoom_total": round(math.exp(tot), 3), "zoom_rate": round(tot / d, 4),
                   "zoom_cv": round(float(zl.std() / (abs(zl.mean()) + 1e-6)), 2),
                   "zoom_active_share": round(float((np.abs(zl) > 0.002).mean()), 2),
                   "pan_x_rate": round(float(px.sum() / d), 4), "pan_y_rate": round(float(py.sum() / d), 4),
                   "shake": round(float(np.hypot(np.diff(px), np.diff(py)).std()) if m > 2 else 0.0, 4),
                   "rot_deg": round(float(rot[pi - m:pi].sum()), 2), "steps": int(m)}
    zr, pr = s["motion"]["zoom_rate"], math.hypot(s["motion"]["pan_x_rate"], s["motion"]["pan_y_rate"])
    s["move"] = ("zoom_in" if zr > 0.04 else "zoom_out" if zr < -0.04 else "") + ("+pan" if pr > 0.06 and abs(zr) > 0.04 else "pan" if pr > 0.06 else "")
    s["move"] = s["move"] or ("handheld" if s["motion"]["shake"] > 0.004 else "static")

# ---------------------------------------------------------------- 5. light
lum, bl, hh = z["lum"], z["black_low"], z["huehist"]
HUES = ["red", "orange", "yellow", "lime", "green", "teal", "cyan", "azure", "blue", "violet", "magenta", "rose"]
for s in shots:
    idx = U[s["u0"]:s["u1"] + 1]
    h_ = hh[idx].mean(0)
    s["light"] = {"luma": round(float(lum[idx].mean()), 3), "black_low": round(float(bl[idx].mean()), 2),
                  "silhouette": bool(bl[idx].mean() > 0.55 and lum[idx].mean() > 0.04),
                  "hue": HUES[int(np.argmax(h_))] if h_.sum() > 0.05 else "neutral",
                  "sat_share": round(float(z["sat_share"][idx].mean()), 2)}

# ---------------------------------------------------------------- 4. people (YOLO11x-pose on 3 frames per shot)
from ultralytics import YOLO
model = YOLO(os.path.join(os.path.dirname(os.path.abspath(__file__)), "yolo11x-pose.pt"))
want = {}
for s in shots:
    for tag, uu in (("a", s["u0"]), ("m", (s["u0"] + s["u1"]) // 2), ("b", s["u1"])):
        want.setdefault(int(U[uu]), []).append((s["i"], tag))
FW, FH = int(z["W"]), int(z["H"])
rd = STX.NV12Reader(SRC, 0, N, FW, FH)
dets = {}
with torch.inference_mode():
    for fi, nv in enumerate(rd):
        if fi not in want:
            continue
        rgb = STX.nv12_to_rgb(nv, FW, FH, False)
        bgr = (rgb.flip(0).permute(1, 2, 0) * 255).byte().cpu().numpy()
        r = model.predict(bgr, imgsz=1280, half=True, verbose=False, classes=[0], conf=0.3)[0]
        boxes = r.boxes.xyxy.cpu().numpy() if r.boxes is not None else np.zeros((0, 4))
        kp = r.keypoints.data.cpu().numpy() if r.keypoints is not None else np.zeros((0, 17, 3))
        for si, tag in want[fi]:
            dets[(si, tag)] = (boxes, kp)
for s in shots:
    ppl, hb, hd, cxs = [], [], [], []
    for tag in "amb":
        boxes, kp = dets.get((s["i"], tag), (np.zeros((0, 4)), np.zeros((0, 17, 3))))
        ppl.append(len(boxes))
        if len(boxes):
            hts = (boxes[:, 3] - boxes[:, 1]) / FH
            j = int(np.argmax(hts)); hb.append(float(hts[j])); cxs.append(float((boxes[j, 0] + boxes[j, 2]) / 2 / FW))
            k_ = kp[j]; pts = k_[[0, 1, 2, 3, 4]]; ok = pts[:, 2] > 0.3
            if ok.sum() >= 2:
                hd.append(float(np.ptp(pts[ok, 0]) * 1.8 / FH + np.ptp(pts[ok, 1]) / FH))
    head = float(np.median(hd)) if hd else 0.0
    size = ("ECU" if head > 0.30 else "CU" if head > 0.16 else "MCU" if head > 0.09 else "MS" if head > 0.045 else "WS") if hd else ("WS" if max(ppl) >= 3 else "insert")
    s["people"] = {"count_max": int(max(ppl)), "count_mid": int(ppl[1]), "main_box_h": round(float(np.median(hb)), 2) if hb else 0.0,
                   "head_h": round(head, 3), "main_cx": round(float(np.median(cxs)), 2) if cxs else None, "size": size}

# ---------------------------------------------------------------- 6. music: beats + DTW to our performance
import librosa, soundfile as sf
tmp = os.path.join(REF, "audio22k.wav")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", SRC, "-ac", "1", "-ar", "22050", tmp], check=True)
ya, sr = sf.read(tmp, dtype="float32")
tempo, bt = librosa.beat.beat_track(y=ya, sr=sr, units="time", start_bpm=92)
stems = [sf.read(os.path.join(ROOT, "_derived", "scratch", "stems", NUM, k + ".flac"), dtype="float32")[0] for k in ("vocals", "drums", "bass", "other")]
yb = sum(x[:min(len(s_) for s_ in stems)] for x in stems)
HOPC = 2205                                                    # 10 Hz chroma
def chroma_gpu(y):
    t = torch.from_numpy(y).to(dev)
    X = torch.stft(t, 4096, HOPC, window=torch.hann_window(4096, device=dev), return_complex=True).abs()
    fb = torch.from_numpy(librosa.filters.chroma(sr=sr, n_fft=4096)).to(dev).float()
    c = fb @ X
    c = c + 1e-4 * c.max()                                  # silent frames: flat chroma, not a zero vector (cosine NaN)
    return (c / (c.norm(dim=0, keepdim=True) + 1e-6)).cpu().numpy()
Ca, Cb = chroma_gpu(ya), chroma_gpu(yb)
D, wp = librosa.sequence.dtw(X=Ca, Y=Cb, metric="cosine", subseq=False)   # CPU dynamic programming
wp = wp[::-1]
ta, tb = wp[:, 0] * HOPC / sr, wp[:, 1] * HOPC / sr
def to_ours(t):
    j = np.searchsorted(ta, t); j = min(max(j, 0), len(ta) - 1)
    return float(tb[j])
dyn = json.load(open(os.path.join(ROOT, "metadata", "dynamics", NUM + ".json"), encoding="utf-8"))
bars = np.array(dyn["bars"]); secs = dyn["sections"]
def sec_of(t):
    nm = secs[0]["name"]
    for s_ in secs:
        if t >= s_["t0"]:
            nm = s_["name"]
    return nm
btn = np.array(bt)
for s in shots:
    s["ours_t0"], s["ours_t1"] = round(to_ours(s["t0"]), 2), round(to_ours(s["t1"]), 2)
    s["section"] = sec_of(s["ours_t0"])
    j = int(np.argmin(abs(bars - s["ours_t0"]))); s["ours_bar"] = j; s["ours_bar_offset_s"] = round(s["ours_t0"] - bars[j], 2)
    if len(btn):
        k = int(np.argmin(abs(btn - s["t0"]))); bs_ = np.median(np.diff(btn))
        s["cut_beat_offset"] = round(float((s["t0"] - btn[k]) / bs_), 2)
    bi = int(np.searchsorted(bars, s["ours_t0"])) - 1
    s["intensity"] = dyn["bar_intensity"][max(0, min(bi, len(dyn["bar_intensity"]) - 1))]

out = {"source": SRC,
       "fps_file": fps, "unique_fps": round(M / dur, 2), "duration": dur, "official_bpm": float(np.atleast_1d(tempo)[0]),
       "official_beats": [round(float(b), 3) for b in bt], "cuts": [round(T[k], 3) for k in cuts], "gradual": grad,
       "dtw_map": {"official_t": [round(float(x), 2) for x in ta[::10]], "ours_t": [round(float(x), 2) for x in tb[::10]]},
       "shots": shots}
os.makedirs(os.path.join(ROOT, "metadata", "reference"), exist_ok=True)
json.dump(out, open(os.path.join(ROOT, "metadata", "reference", f"{NUM}-{NAME}.json"), "w", encoding="utf-8"), indent=1)

# ---------------------------------------------------------------- contact sheets (first / mid / last per shot)
from PIL import Image, ImageDraw, ImageFont
font = ImageFont.truetype(r"C:\Windows\Fonts\bahnschrift.ttf", 15)
def thumb(u):
    im = (G[U[u]].cpu().numpy() * 255).clip(0, 255).astype(np.uint8)
    return Image.fromarray(im).resize((150, 117)).convert("RGB")
per = 16
for sh in range(0, len(shots), per):
    S = Image.new("RGB", (3 * 150 + 330, per * 121), (16, 16, 18)); d = ImageDraw.Draw(S)
    for r_, s in enumerate(shots[sh:sh + per]):
        y = r_ * 121
        for c_, u in enumerate((s["u0"], (s["u0"] + s["u1"]) // 2, s["u1"])):
            S.paste(thumb(u), (c_ * 150, y))
        mo = s["motion"]
        d.text((455, y + 4), f"#{s['i']}  {s['t0']:.2f}-{s['t1']:.2f}  ({s['dur']:.1f}s)", font=font, fill=(255, 220, 90))
        d.text((455, y + 24), f"ours {s['ours_t0']:.1f} {s['section']}  int {s['intensity']:.2f}", font=font, fill=(200, 200, 200))
        d.text((455, y + 44), f"{s['move']}  zoom x{mo['zoom_total']:.2f} ({mo['zoom_rate']:+.3f}/s)", font=font, fill=(200, 200, 200))
        d.text((455, y + 64), f"pan {mo['pan_x_rate']:+.3f},{mo['pan_y_rate']:+.3f}/s  shake {mo.get('shake', 0):.3f}", font=font, fill=(200, 200, 200))
        p_ = s["people"]; l_ = s["light"]
        d.text((455, y + 84), f"{p_['size']} ppl {p_['count_max']}  {l_['hue']} L{l_['luma']:.2f}{' SIL' if l_['silhouette'] else ''}", font=font, fill=(200, 200, 200))
    S.save(os.path.join(CHK, f"shots_{sh // per + 1:02d}.jpg"), quality=86)
print(f"done: {len(shots)} shots -> metadata/reference/{NUM}-{NAME}.json, {math.ceil(len(shots) / per)} sheets")
