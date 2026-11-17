r"""render_cached.py -- render songs from the scene script (highlights_edl.json) through a fragment cache.

Fragments on demand: every shot becomes one video fragment, keyed by everything that decides its pixels --
source file (path + size + mtime), exact clip-local in/out, the camera's conversion + framing chain, the title
banner it carries, and the encoder settings / render.version:
  - hit:  <cache_dir>/<key>.mp4 exists -> reused as is
  - miss: encoded now (only frames that end up in the video ever get processed), then stored
A song is the fragments joined with stream copy plus its audio muxed in -- no full-song re-encode, so a change to
one shot re-encodes only the shots whose source or boundaries changed (a re-run of an unchanged song is seconds).

Per camera, from show.json (cameras.<cam>):
  prep       "sdr"         already SDR (fixed cams): tagged BT.709
             "hlg_to_sdr"  phone HLG HDR -> SDR BT.709 on the GPU (libplacebo/Vulkan, Hable curve). The final video
                           is SDR: converting SDR cameras UP to HLG instead can't add range and looked washed out.
  treatment  "inset2k"     2560x1440 centred on a blurred fill (no upscaling of soft wide-angle glass)
             "portrait"    full height on a blurred fill (vertical phone video, rotation baked in on decode)
             "fill"        scale-to-fill and crop (landscape phones / good cameras)
Everything to 30 fps, AV1 NVENC 10-bit 3840x2160 -- one identical format, which is what makes stream-copy joins
safe. The song title banner (title only) shows for render.title_hold_s + a 0.5 s fade, spanning shots if the first
one is short. Audio = the scene script's audio file, trimmed to the joined video's MEASURED duration (per-shot
frame rounding never sums to the nominal length -- that drift fix is load-bearing).

Usage: py render_cached.py [song_num ...] [--jobs N]   (default: every song, 3 songs at a time)
"""
import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import show_config  # noqa: E402

CFG = show_config.load()
EDL = show_config.path("highlights_edl.json")
CACHE = show_config.path(CFG["render"]["cache_dir"])
OUT_DIR = show_config.path(CFG["render"]["out_dir"])
TMP = show_config.path(os.path.join("scratch", "assemble"))
FONT = CFG["render"].get("font", "C:/Windows/Fonts/segoeuib.ttf")
RENDER_VERSION = CFG["render"].get("version", "1")   # bump to invalidate every cached fragment

OUT_W, OUT_H = 3840, 2160
NVENC = ["-c:v", "av1_nvenc", "-preset", "p7", "-rc", "vbr", "-cq", "23", "-b:v", "0",
         "-pix_fmt", "yuv420p10le", "-color_range", "tv", "-color_primaries", "bt709",
         "-color_trc", "bt709", "-colorspace", "bt709"]
PREPS = {
    "hlg_to_sdr": "libplacebo=tonemapping=hable:colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv:format=yuv420p,fps=30",
    "sdr": "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv,fps=30",
}
PREP = {cam: PREPS[c.get("prep", "sdr")] for cam, c in CFG["cameras"].items()}
TREATMENT = {cam: c.get("treatment", "fill") for cam, c in CFG["cameras"].items()}
VF = {
    "inset2k": (f"split=2[bg][fg];[bg]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
                f"crop={OUT_W}:{OUT_H},gblur=sigma=20[bgb];[fg]scale=2560:1440[fgs];"
                f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2:format=auto,fps=30,format=yuv420p10le"),
    "portrait": (f"split=2[bg][fg];[bg]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
                 f"crop={OUT_W}:{OUT_H},gblur=sigma=20[bgb];[fg]scale=-2:{OUT_H}[fgs];"
                 f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2:format=auto,fps=30,format=yuv420p10le"),
    "fill": (f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},"
             f"setsar=1,fps=30,format=yuv420p10le"),
}
TITLE_HOLD, TITLE_FADE = float(CFG["render"].get("title_hold_s", 4.0)), 0.5
def esc(p):
    return p.replace("\\", "/").replace(":", "\\:")


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + " ".join(cmd)[:400] + "\n" + r.stderr[-1500:])


def duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    return float(r.stdout.strip())


def frag_key(seg, title):
    st = os.stat(seg["clip_file"])
    spec = {"src": os.path.normcase(seg["clip_file"]), "size": st.st_size, "mtime": int(st.st_mtime),
            "in": round(seg["local_start"], 3), "out": round(seg["local_end"], 3),
            "chain": PREP[seg["source"]] + "|" + VF[TREATMENT[seg["source"]]], "title": title, "enc": NVENC,
            "v": RENDER_VERSION}
    return hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:20], spec


def fragment(seg, title, work):
    """Return (path, hit) for one shot, encoding it on a cache miss."""
    key, spec = frag_key(seg, title)
    path = os.path.join(CACHE, key + ".mp4")
    if os.path.exists(path) and os.path.exists(path + ".json"):
        return path, True
    dur = seg["local_end"] - seg["local_start"]
    chain = "[0:v]" + PREP[seg["source"]] + "," + VF[TREATMENT[seg["source"]]]
    hw = ["-init_hw_device", "vulkan"] if PREP[seg["source"]].startswith("libplacebo") else []
    if title:
        text, off = title
        tf = os.path.join(work, "title.txt")
        open(tf, "w", encoding="utf-8").write(text)
        tt = f"(t+{off})"  # song time, not shot time
        chain += (f",drawtext=fontfile='{esc(FONT)}':textfile='{esc(tf)}':fontcolor=white:fontsize=96:box=1:"
                  f"boxcolor=black@0.45:boxborderw=28:x=80:y=h-th-80:enable='lt({tt},{TITLE_HOLD + TITLE_FADE})':"
                  f"alpha='if(lt({tt},{TITLE_HOLD}),1,max(0,1-({tt}-{TITLE_HOLD})/{TITLE_FADE}))'")
    tmp = path + ".part.mp4"
    # -ss after -i: frame-exact seek (fast seek before -i can land up to a GOP early).
    run(["ffmpeg", "-y", "-v", "error"] + hw + ["-i", seg["clip_file"], "-ss", str(seg["local_start"]), "-t", str(dur),
         "-filter_complex", chain + "[v]", "-map", "[v]", "-an"] + NVENC + [tmp])
    os.replace(tmp, path)
    json.dump(spec, open(path + ".json", "w", encoding="utf-8"), indent=1)
    return path, False


def render(song):
    num = song["song_num"]
    work = os.path.join(TMP, num)
    os.makedirs(work, exist_ok=True)
    title = song["title"]  # banner: the song title only
    frags, hits, at = [], 0, 0.0
    for i, seg in enumerate(song["segments"]):
        banner = [title, round(at, 3)] if at < TITLE_HOLD + TITLE_FADE else None
        p, hit = fragment(seg, banner, work)
        at += seg["local_end"] - seg["local_start"]
        frags.append(p)
        hits += hit
    lst = os.path.join(work, "list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in frags:
            f.write(f"file '{p.replace(chr(92), '/')}'\n")
    video = os.path.join(work, "video.mp4")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", video])
    vdur = duration(video)
    nominal = song["highlight_local_end"] - song["highlight_local_start"]
    audio = os.path.join(work, "audio.m4a")
    run(["ffmpeg", "-y", "-v", "error", "-ss", str(song["highlight_local_start"]), "-i", song["nikon_file"],
         "-t", str(vdur), "-vn", "-c:a", "aac", "-b:a", "320k", audio])
    safe = song["title"].replace("/", "-").replace("?", "").replace(":", "")
    out = os.path.join(OUT_DIR, f"{num}-{safe}.mp4")
    run(["ffmpeg", "-y", "-v", "error", "-i", video, "-i", audio, "-map", "0:v", "-map", "1:a", "-c", "copy",
         "-shortest", out])
    cum, trans = 0.0, []
    for i, seg in enumerate(song["segments"]):
        d = seg["local_end"] - seg["local_start"]
        trans.append({"index": i, "render_time_start": round(cum, 3), "render_time_end": round(cum + d, 3),
                      "source": seg["source"], "treatment": TREATMENT[seg["source"]], "clip_file": seg["clip_file"],
                      "clip_local_start": seg["local_start"], "clip_local_end": seg["local_end"],
                      "real_world_start": seg["abs_start"], "real_world_end": seg["abs_end"],
                      "director_reason": seg.get("reason"), "fragment": os.path.basename(frags[i])})
        cum += d
    json.dump({"video": os.path.basename(out), "generated_at": datetime.now(timezone.utc).isoformat(),
               "song_num": num, "title": song["title"], "audio_source": song["nikon_file"],
               "audio_note": "replace freely: the video is a stream-copy join, see soundtrack/NN.json for the mix",
               "video_duration": round(vdur, 3), "nominal_duration": round(nominal, 3),
               "cache_hits": hits, "cache_misses": len(frags) - hits, "transitions": trans},
              open(out + ".json", "w", encoding="utf-8"), indent=1)
    return f"{num} OK  {len(frags)} shots, {hits} from cache, {len(frags) - hits} encoded, video {vdur:.2f}s vs script {nominal:.2f}s"


def main():
    args = sys.argv[1:]
    jobs = 3
    if "--jobs" in args:
        jobs = int(args[args.index("--jobs") + 1])
        del args[args.index("--jobs"):args.index("--jobs") + 2]
    os.makedirs(CACHE, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    songs = json.load(open(EDL, encoding="utf-8"))["songs"]
    if args:
        songs = [s for s in songs if s["song_num"] in set(args)]

    def safe(song):
        try:
            return render(song)
        except Exception as e:  # one bad song shouldn't stop the batch
            return f"{song['song_num']} FAILED {e}"[:600]

    with ThreadPoolExecutor(jobs) as pool:
        for line in pool.map(safe, songs):
            print(line, flush=True)


if __name__ == "__main__":
    main()
