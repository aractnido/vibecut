"""stage_text.py -- text (or any RGBA sprite) on a real stage plane, locked to the stage through digital zooms and pans.

Why: a static camera's digital zoom/pan is a moving crop of its full frame. Placing text in one shot's pixels breaks as
soon as the crop changes (the text falls outside a tighter shot, or sits at the wrong angle). Here the text lives on a
plane calibrated once on the FULL frame; every output frame only adds its own crop. A flat plane seen through any crop
or scale maps to the output by one 3x3 homography per frame:

    H(f) = S(f) . Hs        Hs: sprite px -> full-frame px (camera + plane, fixed)
                            S(f): full-frame px -> output px (the frame's crop: x, y, width; scale = out_w / width)

So the precomputed data a renderer needs is an [N, 3, 3] float track (h_track), and drawing is one warp per frame.

Camera file (one per static camera per show, from a plane the user aligned to the stage front on the full frame):
    {"image": [W, H], "focal_px": f, "stage_front": {"yaw": deg, "pitch": deg, "roll": deg}}
Placement on that camera: the plane's centre pixel (x, y) on the full frame and its width (px at depth f). Moving x/y
slides the plane while keeping the stage's angle; the camera's yaw/pitch/roll never change per text.

GPU: the warp + composite is torch grid_sample on CUDA, limited to the sprite's projected bounding box. The only CPU
work is drawing the sprite (PIL, once per word, not per frame) and the 3x3 maths (numpy, microseconds).

CLI (a check clip: zoom/pan keyframes over a static camera, text on the plane):
    python stage_text.py --camera cam.json --video clip.mp4 --start 210.5 --seconds 5 --text "Hello, World!"
        --font font.ttf --x 1920 --y 650 --width 2000 --keys "0,1,1920,1080;1.6,2.3,1150,900;5,2.3,2750,900"
        --out check.mp4
Writes check.mp4 and check_H.npz (H [N,3,3], sprite_wh).
"""
import argparse
import json
import math
import subprocess
import time

import numpy as np


# ---------------------------------------------------------------- geometry (numpy, CPU: microseconds)
def load_camera(path):
    c = json.load(open(path, encoding="utf-8"))
    W, H = c["image"]
    return {"W": W, "H": H, "f": float(c["focal_px"]), **{k: float(v) for k, v in c["stage_front"].items()}}


def rotation(yaw, pitch, roll):
    """Same convention as the stage-plane tool: R = Rz(roll) Ry(yaw) Rx(pitch), degrees."""
    y, p, r = (math.radians(a) for a in (yaw, pitch, roll))
    Ry = np.array([[math.cos(y), 0, math.sin(y)], [0, 1, 0], [-math.sin(y), 0, math.cos(y)]])
    Rx = np.array([[1, 0, 0], [0, math.cos(p), -math.sin(p)], [0, math.sin(p), math.cos(p)]])
    Rz = np.array([[math.cos(r), -math.sin(r), 0], [math.sin(r), math.cos(r), 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


def plane_quad(cam, x, y, width, aspect):
    """Full-frame corners (TL, TR, BR, BL) of a plane centred on pixel (x, y), `width` px wide at depth f."""
    W, H, f = cam["W"], cam["H"], cam["f"]
    R = rotation(cam["yaw"], cam["pitch"], cam["roll"])
    h = width * aspect
    C = np.array([x - W / 2, y - H / 2, f])
    out = []
    for u, v in ((0, 0), (1, 0), (1, 1), (0, 1)):
        q = R @ np.array([(u - 0.5) * width, (v - 0.5) * h, 0.0]) + C
        out.append((W / 2 + f * q[0] / q[2], H / 2 + f * q[1] / q[2]))
    return np.float64(out)


def homography(src, dst):
    """3x3 H with dst ~ H src for 4 point pairs (DLT, exact for 4 points)."""
    A = []
    for (x, y), (u, v) in zip(src, dst):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y, -u])
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y, -v])
    Hm = np.linalg.svd(np.float64(A))[2][-1].reshape(3, 3)
    return Hm / Hm[2, 2]


def sprite_homography(quad, sw, sh):
    """Hs: sprite pixel (u, v) -> full-frame pixel, the sprite's corners onto the plane's quad."""
    return homography([(0, 0), (sw, 0), (sw, sh), (0, sh)], quad)


def crop_matrices(crops, out_w):
    """S(f) for crops [N, 3] = (x, y, width) in full-frame px: full-frame px -> output px."""
    crops = np.asarray(crops, np.float64)
    s = out_w / crops[:, 2]
    S = np.zeros((len(crops), 3, 3))
    S[:, 0, 0] = S[:, 1, 1] = s
    S[:, 0, 2] = -crops[:, 0] * s
    S[:, 1, 2] = -crops[:, 1] * s
    S[:, 2, 2] = 1
    return S


def h_track(Hs, crops, out_w):
    """[N, 3, 3] sprite px -> output px, one per frame. This is the precomputed data a renderer consumes."""
    return crop_matrices(crops, out_w) @ Hs


def keyframe_crops(keys, n, fps, W, H):
    """Crops [n, 3] from keyframes [(t, zoom, centre_x, centre_y)], smoothstep between keys, clamped in frame."""
    keys = sorted(keys)
    out = []
    for i in range(n):
        t = i / fps
        k = next((j for j in range(len(keys) - 1) if t <= keys[j + 1][0]), len(keys) - 2)
        (t0, z0, x0, y0), (t1, z1, x1, y1) = keys[k], keys[k + 1]
        u = min(max((t - t0) / max(t1 - t0, 1e-9), 0), 1)
        e = u * u * (3 - 2 * u)
        z, mx, my = z0 + (z1 - z0) * e, x0 + (x1 - x0) * e, y0 + (y1 - y0) * e
        cw, ch = W / z, H / z
        out.append((min(max(mx - cw / 2, 0), W - cw), min(max(my - ch / 2, 0), H - ch), cw))
    return np.float64(out)


# ---------------------------------------------------------------- sprite (PIL, CPU, once per word)
def text_sprite(text, font_path, size=420, fill=(255, 255, 255), stroke=(20, 20, 26), stroke_w=10, shadow=0.55):
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    font = ImageFont.truetype(font_path, size)
    x0, y0, x1, y1 = font.getbbox(text, stroke_width=stroke_w)
    pad = int(size * 0.15)
    sw, sh = x1 - x0 + 2 * pad, y1 - y0 + 2 * pad
    img = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((pad - x0, pad - y0), text, font=font, fill=fill + (255,),
                             stroke_width=stroke_w, stroke_fill=stroke + (255,))
    if shadow:
        a = img.split()[3].filter(ImageFilter.GaussianBlur(size * 0.04)).point(lambda v: int(v * shadow))
        base = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
        base.putalpha(a)
        img = Image.alpha_composite(base, img)
    return np.asarray(img)


# ---------------------------------------------------------------- GPU warp + composite
class PlaneCompositor:
    """Warps one RGBA sprite through H(f) onto output frames on the GPU (torch grid_sample, bilinear).
    Only the sprite's projected bounding box is sampled, so the cost scales with the text's screen size."""

    def __init__(self, sprite_rgba, out_w, out_h, device="cuda"):
        import torch
        self.t = torch
        self.dev = device
        s = torch.from_numpy(np.ascontiguousarray(sprite_rgba)).to(device).permute(2, 0, 1)[None].float() / 255
        s[:, :3] *= s[:, 3:4]                                   # premultiplied alpha
        self.S = s
        self.sh, self.sw = sprite_rgba.shape[:2]
        self.ow, self.oh = out_w, out_h

    def bbox(self, Hf):
        c = Hf @ np.float64([[0, self.sw, self.sw, 0], [0, 0, self.sh, self.sh], [1, 1, 1, 1]])
        if np.any(c[2] <= 0):
            return None
        x, y = c[0] / c[2], c[1] / c[2]
        x0, x1 = max(int(math.floor(x.min())), 0), min(int(math.ceil(x.max())) + 1, self.ow)
        y0, y1 = max(int(math.floor(y.min())), 0), min(int(math.ceil(y.max())) + 1, self.oh)
        return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None

    def composite(self, frame, Hf):
        """frame: float [3, oh, ow] on the GPU in 0..1, modified in place. Hf: 3x3 sprite px -> output px."""
        torch = self.t
        bb = self.bbox(Hf)
        if bb is None:
            return frame
        x0, y0, x1, y1 = bb
        Hi = torch.from_numpy(np.linalg.inv(Hf)).to(self.dev)
        ys, xs = torch.meshgrid(torch.arange(y0, y1, device=self.dev, dtype=torch.float64) + 0.5,
                                torch.arange(x0, x1, device=self.dev, dtype=torch.float64) + 0.5, indexing="ij")
        q = torch.stack([xs, ys, torch.ones_like(xs)], -1) @ Hi.T
        g = torch.stack([q[..., 0] / q[..., 2] / self.sw * 2 - 1, q[..., 1] / q[..., 2] / self.sh * 2 - 1], -1)
        fg = torch.nn.functional.grid_sample(self.S, g[None].float(), mode="bilinear", padding_mode="zeros",
                                             align_corners=False)[0]
        roi = frame[:, y0:y1, x0:x1]
        roi.mul_(1 - fg[3:4]).add_(fg[:3])
        return frame


# ---------------------------------------------------------------- GPU-resident video I/O (NV12 in, NV12 out)
# Measured on a 4K static camera (RTX 4090, 1080p out): 13 fps -> 65 fps. The warp is ~1 ms/frame; moving 4K RGB through pipes was the
# whole cost (rgb24 decode read 45 ms: ffmpeg converts YUV->RGB on the CPU; upload 14; download 8). So: NVDEC + scale_cuda
# hand over 8-bit NV12 (half the bytes, no CPU colour conversion), YUV<->RGB runs on the GPU, the output goes back as
# NV12, pinned host buffers + readinto avoid copies, and reader/writer threads overlap the pipes with the GPU work.
_M709 = {"r": (0.0, 1.5748), "g": (-0.1873, -0.4681), "b": (1.8556, 0.0)}     # BT.709 YCbCr -> RGB (Cb, Cr) weights


def nv12_to_rgb(buf, W, H, full_range):
    """uint8 NV12 [H*3/2, W] on the GPU -> float RGB [3, H, W] 0..1 (BT.709)."""
    import torch
    y = buf[:H].float()
    uv = buf[H:].view(H // 2, W // 2, 2).permute(2, 0, 1).float()
    uv = torch.nn.functional.interpolate(uv[None], size=(H, W), mode="bilinear", align_corners=False)[0]
    if full_range:
        y, cb, cr = y / 255, (uv[0] - 128) / 255, (uv[1] - 128) / 255
    else:
        y, cb, cr = (y - 16) / 219, (uv[0] - 128) / 224, (uv[1] - 128) / 224
    return torch.stack([y + _M709[c][0] * cb + _M709[c][1] * cr for c in "rgb"]).clamp_(0, 1)


def rgb_to_nv12(rgb):
    """float RGB [3, H, W] 0..1 -> uint8 NV12 [H*3/2, W], BT.709 limited range (tag the encode bt709/tv)."""
    import torch
    r, g, b = rgb[0], rgb[1], rgb[2]
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    cb = (b - y) / 1.8556
    cr = (r - y) / 1.5748
    H, W = y.shape
    uv = torch.nn.functional.avg_pool2d(torch.stack([cb, cr])[None], 2)[0]        # 4:2:0
    out = torch.empty((H * 3 // 2, W), dtype=torch.uint8, device=rgb.device)
    out[:H] = (16 + 219 * y).round_().clamp_(0, 255).to(torch.uint8)
    out[H:] = (128 + 224 * uv).round_().clamp_(0, 255).to(torch.uint8).permute(1, 2, 0).reshape(H // 2, W)
    return out


class NV12Reader:
    """NVDEC decode -> 8-bit NV12 frames in pinned host memory, read ahead on a thread. Iterate for GPU uint8 tensors."""

    def __init__(self, path, start, n, W, H, depth=4):
        import queue, threading, torch
        self.W, self.H, self.n = W, H, n
        self.size = W * H * 3 // 2
        self.p = subprocess.Popen(["ffmpeg", "-v", "error", "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                                   "-ss", str(start), "-i", path, "-frames:v", str(n),
                                   "-vf", "scale_cuda=format=nv12,hwdownload,format=nv12", "-f", "rawvideo", "-"],
                                  stdout=subprocess.PIPE, bufsize=0)
        self.free, self.full = queue.Queue(), queue.Queue(depth)
        for _ in range(depth + 1):
            self.free.put(torch.empty(self.size, dtype=torch.uint8).pin_memory())
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()

    def _run(self):
        for _ in range(self.n):
            b = self.free.get()
            mv, got = memoryview(b.numpy()), 0
            while got < self.size:
                k = self.p.stdout.readinto(mv[got:])
                if not k:
                    break
                got += k
            if got < self.size:
                break
            self.full.put(b)
        self.full.put(None)

    def __iter__(self):
        while True:
            b = self.full.get()
            if b is None:
                return
            g = b.to("cuda", non_blocking=True).view(self.H * 3 // 2, self.W)
            import torch
            torch.cuda.current_stream().synchronize()                # host buffer reusable once copied
            self.free.put(b)
            yield g


class NV12Writer:
    """GPU NV12 frames -> NVENC, written from a thread via pinned host buffers."""

    def __init__(self, out, W, H, fps, codec="h264_nvenc", cq=19, depth=4):
        import queue, threading, torch
        self.size = W * H * 3 // 2
        # colour tags on the INPUT side: as output options ffmpeg inserts a CPU range conversion (measured: Y -7)
        tags = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv"]
        self.p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "nv12",
                                   "-s", f"{W}x{H}", "-r", str(fps), *tags, "-i", "-", "-c:v", codec,
                                   "-cq", str(cq), out], stdin=subprocess.PIPE, bufsize=0)
        self.free, self.full = queue.Queue(), queue.Queue(depth)
        for _ in range(depth + 1):
            self.free.put(torch.empty(self.size, dtype=torch.uint8).pin_memory())
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()

    def _run(self):
        while True:
            item = self.full.get()
            if item is None:
                break
            b, ev = item
            ev.synchronize()
            self.p.stdin.write(memoryview(b.numpy()))
            self.free.put(b)

    def write(self, nv12):
        import torch
        b = self.free.get()
        b.copy_(nv12.reshape(-1), non_blocking=True)
        ev = torch.cuda.Event()
        ev.record()
        self.full.put((b, ev))

    def close(self):
        self.full.put(None)
        self.th.join()
        self.p.stdin.close()
        self.p.wait()


def crop_scale(rgb, crop, out_w, out_h):
    """float RGB [3, H, W] -> [3, out_h, out_w]: the crop (x, y, width) resampled on the GPU (bilinear)."""
    import torch
    _, H, W = rgb.shape
    cx, cy, cw = crop
    ch = cw * H / W
    gy = torch.linspace(cy, cy + ch, out_h, device=rgb.device) / (H - 1) * 2 - 1
    gx = torch.linspace(cx, cx + cw, out_w, device=rgb.device) / (W - 1) * 2 - 1
    g = torch.stack(torch.meshgrid(gy, gx, indexing="ij")[::-1], -1)[None]
    return torch.nn.functional.grid_sample(rgb[None], g, mode="bilinear", align_corners=True)[0]


def full_range_of(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=color_range",
                        "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
    return r in ("pc", "full")


# ---------------------------------------------------------------- check-clip CLI
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--camera", required=True)
    ap.add_argument("--video", required=True)
    ap.add_argument("--start", type=float, required=True, help="seconds into the clip")
    ap.add_argument("--seconds", type=float, default=5)
    ap.add_argument("--fps", type=float, default=30)
    ap.add_argument("--text", required=True)
    ap.add_argument("--font", required=True)
    ap.add_argument("--x", type=float, required=True)
    ap.add_argument("--y", type=float, required=True)
    ap.add_argument("--width", type=float, required=True)
    ap.add_argument("--keys", required=True, help="t,zoom,cx,cy;... (seconds, x, full-frame px)")
    ap.add_argument("--out-size", default="1920x1080")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    import torch
    cam = load_camera(a.camera)
    W, H = cam["W"], cam["H"]
    OW, OH = (int(v) for v in a.out_size.split("x"))
    n = int(round(a.seconds * a.fps))
    spr = text_sprite(a.text, a.font)
    sh, sw = spr.shape[:2]
    Hs = sprite_homography(plane_quad(cam, a.x, a.y, a.width, sh / sw), sw, sh)
    crops = keyframe_crops([tuple(map(float, k.split(","))) for k in a.keys.split(";")], n, a.fps, W, H)
    Ht = h_track(Hs, crops, OW)
    np.savez(a.out.rsplit(".", 1)[0] + "_H.npz", H=np.float32(Ht), sprite_wh=[sw, sh], crops=np.float32(crops))
    comp = PlaneCompositor(spr, OW, OH)
    full = full_range_of(a.video)
    t0 = time.time()
    rd = NV12Reader(a.video, a.start, n, W, H)
    wr = NV12Writer(a.out, OW, OH, a.fps)
    f = 0
    with torch.inference_mode():
        for f, nv in enumerate(rd):
            o = crop_scale(nv12_to_rgb(nv, W, H, full), crops[f], OW, OH)
            comp.composite(o, Ht[f])
            wr.write(rgb_to_nv12(o))
    wr.close()
    dt = time.time() - t0
    print(f"{f + 1} frames in {dt:.1f} s ({(f + 1) / dt:.1f} fps) -> {a.out}")


if __name__ == "__main__":
    main()
