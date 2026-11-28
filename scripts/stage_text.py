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
    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-hwaccel", "cuda", "-ss", str(a.start), "-i", a.video,
                            "-frames:v", str(n), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{OW}x{OH}",
                            "-r", str(a.fps), "-i", "-", "-c:v", "h264_nvenc", "-cq", "19", "-pix_fmt", "yuv420p",
                            a.out], stdin=subprocess.PIPE)
    F = torch.nn.functional
    t0 = time.time()
    for f in range(n):
        buf = dec.stdout.read(W * H * 3)
        if len(buf) < W * H * 3:
            break
        cx, cy, cw = crops[f]
        ch = cw * H / W
        img = torch.frombuffer(bytearray(buf), dtype=torch.uint8).cuda().view(H, W, 3).permute(2, 0, 1)[None].float()
        gy = (torch.linspace(cy, cy + ch, OH, device="cuda") / (H - 1)) * 2 - 1
        gx = (torch.linspace(cx, cx + cw, OW, device="cuda") / (W - 1)) * 2 - 1
        g = torch.stack(torch.meshgrid(gy, gx, indexing="ij")[::-1], -1)[None]
        o = F.grid_sample(img, g, mode="bilinear", align_corners=True)[0] / 255
        comp.composite(o, Ht[f])
        enc.stdin.write((o.permute(1, 2, 0).clamp(0, 1) * 255).byte().cpu().numpy().tobytes())
    enc.stdin.close()
    enc.wait()
    dt = time.time() - t0
    print(f"{f + 1} frames in {dt:.1f} s ({(f + 1) / dt:.1f} fps) -> {a.out}")


if __name__ == "__main__":
    main()
