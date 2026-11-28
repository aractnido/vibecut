"""Step 1 of the reference-video synthesis: read a reference music video (e.g. the song's official video) so the
director can borrow its editing grammar (shot lengths, subjects, zooms, flashes) instead of reinventing it.

GPU: NVDEC + scale_cuda -> NV12 -> RGB on CUDA (stage_text helpers); every per-frame measure in torch.
Per decoded frame: mean luma, near-black share (whole frame and lower 40 %), saturated-hue histogram, a 32-bin colour
histogram (for cut detection), a gray 230 px frame kept on the GPU (float16) for the motion step, and the
frame-to-frame differences (pixel MAD at 115 px, chi-square of the colour histogram). Duplicate frames (screen
recordings repeat frames) are flagged.
Out: <outdir>/scan.npz (+ gray frames as gray230.npy float16).
Usage: python ref_video_scan.py <video> <outdir>"""
import os, subprocess, sys, time
import numpy as np, torch, torch.nn.functional as F
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stage_text as STX

SRC, OUT = sys.argv[1], sys.argv[2]
os.makedirs(OUT, exist_ok=True)
W, H = 460, 358
dev = "cuda"
r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate,color_range:format=duration",
                    "-of", "default=nw=1", SRC], capture_output=True, text=True).stdout
d = dict(l.split("=", 1) for l in r.strip().splitlines() if "=" in l)
a, b = d["avg_frame_rate"].split("/"); fps = float(a) / float(b); dur = float(d["duration"])
full = d.get("color_range") in ("pc", "full")
n = int(dur * fps) + 2
rd = STX.NV12Reader.__new__(STX.NV12Reader)
import queue, threading
rd.W, rd.H, rd.n, rd.size = W, H, n, W * H * 3 // 2
rd.p = subprocess.Popen(["ffmpeg", "-v", "error", "-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", SRC,
                         "-vf", f"scale_cuda={W}:{H}:format=nv12,hwdownload,format=nv12", "-f", "rawvideo", "-"],
                        stdout=subprocess.PIPE, bufsize=0)
rd.free, rd.full = queue.Queue(), queue.Queue(8)
for _ in range(9):
    rd.free.put(torch.empty(rd.size, dtype=torch.uint8).pin_memory())
rd.th = threading.Thread(target=rd._run, daemon=True); rd.th.start()

gray, lum, black, black_low, hist, huehist, sat_share = [], [], [], [], [], [], []
t0 = time.time()
with torch.inference_mode():
    for k, nv in enumerate(rd):
        rgb = STX.nv12_to_rgb(nv, W, H, full)                       # [3,H,W] 0..1
        L = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
        gray.append(F.avg_pool2d(L[None, None], 2)[0, 0].half())     # 230 x 179
        lum.append(L.mean())
        black.append((L < 0.06).float().mean())
        black_low.append((L[int(H * 0.6):] < 0.06).float().mean())
        q = (rgb.clamp(0, 0.999) * 4).long()                         # 4x4x4 = 64-bin colour histogram (on 115 px)
        q = F.avg_pool2d(q.float()[None], 4)[0].round().long().clamp(0, 3)
        idx = (q[0] * 16 + q[1] * 4 + q[2]).flatten()
        hist.append(torch.bincount(idx, minlength=64).float() / idx.numel())
        mx, _ = rgb.max(0); mn, _ = rgb.min(0); sat = (mx - mn) / (mx + 1e-6)
        r_, g_, b_ = rgb
        hue = torch.where(mx == r_, ((g_ - b_) / (mx - mn + 1e-6)) % 6, torch.where(mx == g_, (b_ - r_) / (mx - mn + 1e-6) + 2, (r_ - g_) / (mx - mn + 1e-6) + 4)) * 60
        m = (sat > 0.25) & (mx > 0.15)
        huehist.append(torch.histc(hue[m], bins=12, min=0, max=360) / max(1, int(m.sum())))
        sat_share.append(m.float().mean())
        if k % 1500 == 0:
            print(f"  {k}/{n} {k / max(1e-6, time.time() - t0):.0f} fps", flush=True)
G = torch.stack(gray)                                              # [N, 179, 230] half
N = G.shape[0]
small = F.avg_pool2d(G.float()[:, None], 2)[:, 0]                  # 115 x 89
mad = torch.cat([torch.zeros(1, device=dev), (small[1:] - small[:-1]).abs().mean((1, 2))])
Hh = torch.stack(hist)
chi = torch.cat([torch.zeros(1, device=dev), (((Hh[1:] - Hh[:-1]) ** 2) / (Hh[1:] + Hh[:-1] + 1e-6)).sum(1) * 0.5])
np.save(os.path.join(OUT, "gray230.npy"), G.cpu().numpy())
_wh = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                      "-of", "csv=p=0", SRC], capture_output=True, text=True).stdout.strip().split(",")
np.savez(os.path.join(OUT, "scan.npz"), fps=fps, duration=dur, n=N, W=int(_wh[0]), H=int(_wh[1]), src=SRC,
         lum=torch.stack(lum).cpu().numpy(), black=torch.stack(black).cpu().numpy(),
         black_low=torch.stack(black_low).cpu().numpy(), hist=Hh.cpu().numpy(), huehist=torch.stack(huehist).cpu().numpy(),
         sat_share=torch.stack(sat_share).cpu().numpy(), mad=mad.cpu().numpy(), chi=chi.cpu().numpy())
madn = mad.cpu().numpy()
dup = (madn < 0.002).sum()
print(f"{N} frames in {time.time() - t0:.1f} s ({N / (time.time() - t0):.0f} fps); duplicates (MAD<0.002) {dup} ({dup / N:.0%})")
print("MAD percentiles 50/90/99/99.9:", np.percentile(madn, [50, 90, 99, 99.9]).round(4))
c = chi.cpu().numpy(); print("chi percentiles 50/90/99/99.5/99.9:", np.percentile(c, [50, 90, 99, 99.5, 99.9]).round(4))
