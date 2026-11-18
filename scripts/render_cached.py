r"""render_cached.py -- render songs from the scene script (highlights_edl.json) through a fragment cache, on the GPU.

Fragments on demand: every shot becomes one video fragment, keyed by everything that decides its pixels -- source
file (path + size + mtime), exact clip-local in/out, its frame count, the camera's decode + filter chain, the title
banner it carries, and the encoder settings / render.version:
  - hit:  <cache_dir>/<key>.mp4 exists -> reused as is
  - miss: encoded now (only frames that end up in the video are ever processed), then stored
A song is the fragments joined with stream copy plus its audio muxed in -- no full-song re-encode, so a change to one
shot re-encodes only the shots whose source or boundaries changed (an unchanged song re-assembles in seconds).

GPU FIRST -- CPU only where no GPU filter exists. The first version decoded on the CPU and blurred a full 4K frame per
output frame: ~4 fps per GoPro shot, ~22 shots/hour. Now (~5-7x faster, ~0.8-1.1x real time per shot):
  - decode on the GPU (NVDEC) for every camera;
  - fixed SDR cameras ("inset2k" + "sdr"): both picture sizes scaled with scale_cuda, frames stay on the GPU;
  - everything else (phones, any HLG source): one libplacebo (Vulkan) pass does the HLG->SDR tone map (when
    prep = "hlg_to_sdr") AND the resize;
  - the blurred background is made from a 960x540 copy (sigma 5 = the old sigma 20 at 4K) then enlarged -- gblur is
    the one CPU step, at 1/16 of the pixels;
  - encode AV1 on NVENC.
Per camera, from show.json (cameras.<cam>):
  prep       "sdr" | "hlg_to_sdr"   (final video is SDR BT.709; converting SDR cameras UP to HLG looked washed out)
  treatment  "inset2k"   2560x1440 centred on a blurred fill (no upscaling of soft wide-angle glass)
             "portrait"  full height on a blurred fill (vertical video; rotation is baked in on decode)
             "fill"      scale-to-fill and crop
Everything lands as AV1 NVENC 10-bit 3840x2160 30 fps -- one identical format, which makes stream-copy joins safe.

Cutting a shot (each rule fixed a real bug):
  - "-i clip -ss X" (output seek) is frame-exact but runs EVERY frame from the clip start through the whole filter
    chain and drops them at the output -- a shot 4 minutes into a clip filtered 4 minutes of discarded 4K frames.
  - "-ss X -i clip" (input seek) alone restarts the timestamps, which changes which frames a 29.97->30 fps
    conversion duplicates.
  - So: "-ss X-3 -copyts -start_at_zero -i clip" (jump close, keep the clip's own clock), trim with margin,
    fps=30:start_time=0 (grid anchored at the clip's t=0), then cut on that grid.
  - Each shot's frame count comes from the SONG's running timeline (frames to its end minus frames to its start),
    so every cut lands within half a frame of the script and rounding never accumulates over a song.
The banner (title only) shows for render.title_hold_s + a 0.5 s fade, spanning shots if the first one is short.
Audio = the scene script's audio file, trimmed to the joined video's MEASURED duration.

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
GPU_DECODE = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]   # frames stay on the GPU
GPU_DECODE_DL = ["-hwaccel", "cuda"]                                  # GPU decode, frames handed to libplacebo
VULKAN = ["-init_hw_device", "vulkan"]
OUTPUT_TAGS = "colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv:format=yuv420p"
BG_SMALL = "gblur=sigma=5,scale=3840:2160"
OVERLAY = "[bgb][fg]overlay=(W-w)/2:(H-h)/2:format=auto,format=yuv420p10le"


def chain_for(cam):
    """(ffmpeg input options, filter graph from [0:v]) for one camera's prep + treatment."""
    c = CFG["cameras"][cam]
    prep, treat = c.get("prep", "sdr"), c.get("treatment", "fill")
    if treat == "inset2k" and prep == "sdr":
        return GPU_DECODE, ("[0:v]split=2[bg][fg];"
                            "[bg]scale_cuda=960:540,hwdownload,format=nv12," + BG_SMALL + "[bgb];"
                            "[fg]scale_cuda=2560:1440,hwdownload,format=nv12[fgs];"
                            "[bgb][fgs]overlay=(W-w)/2:(H-h)/2:format=auto,format=yuv420p10le")
    lp = "libplacebo=" + ("tonemapping=hable:" if prep == "hlg_to_sdr" else "") + OUTPUT_TAGS
    if treat == "inset2k":
        return GPU_DECODE_DL + VULKAN, ("[0:v]" + lp + ":w=2560:h=1440,split=2[bg][fg];"
                                        "[bg]scale=960:540," + BG_SMALL + "[bgb];" + OVERLAY)
    if treat == "portrait":
        return GPU_DECODE_DL + VULKAN, ("[0:v]" + lp + ":w='trunc(iw*2160/ih/2)*2':h=2160,split=2[bg][fg];"
                                        "[bg]scale=960:540:force_original_aspect_ratio=increase,crop=960:540,"
                                        + BG_SMALL + "[bgb];" + OVERLAY)
    return GPU_DECODE_DL + VULKAN, ("[0:v]" + lp + ":w=3840:h=2160:force_original_aspect_ratio=increase,"
                                    "crop=3840:2160,setsar=1,format=yuv420p10le")


CHAINS = {cam: chain_for(cam) for cam in CFG["cameras"]}
TREATMENT = {cam: c.get("treatment", "fill") for cam, c in CFG["cameras"].items()}
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


def frag_key(seg, title, n_frames):
    st = os.stat(seg["clip_file"])
    spec = {"src": os.path.normcase(seg["clip_file"]), "size": st.st_size, "mtime": int(st.st_mtime),
            "in": round(seg["local_start"], 3), "out": round(seg["local_end"], 3), "frames": n_frames,
            "chain": " ".join(CHAINS[seg["source"]][0]) + "|" + CHAINS[seg["source"]][1],
            "title": title, "enc": NVENC,
            "v": RENDER_VERSION}
    return hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:20], spec


def fragment(seg, title, work, n_frames):
    """Return (path, hit) for one shot, encoding it on a cache miss."""
    key, spec = frag_key(seg, title, n_frames)
    path = os.path.join(CACHE, key + ".mp4")
    if os.path.exists(path) and os.path.exists(path + ".json"):
        return path, True
    dur = seg["local_end"] - seg["local_start"]
    hw, chain = CHAINS[seg["source"]]
    # Only the shot's own frames go through the filters. "-ss X -i" alone resets timestamps (which changed the
    # 29.97->30 duplicate pattern) and "-i ... -ss X" filtered every frame from the clip start and dropped them
    # at the output. So: jump 3 s early keeping the source timestamps (-copyts), trim with margin, put the 30 fps
    # grid at the clip's t=0 (same frames as before), trim exactly to [in, out), restart the clock at 0.
    a, b = seg["local_start"], seg["local_end"]
    cut = (f"[0:v]trim=start={max(0.0, a - 1):.4f}:end={b + 1:.4f},fps=30:start_time=0,"
           f"trim=start={(round(a * 30) - 0.5) / 30:.5f},setpts=PTS-STARTPTS,trim=end_frame={n_frames}[cut];")
    # ^ cut on the 30 fps frame grid: frames round(in*30) .. round(out*30)-1, so every shot has exactly the frames
    #   its span rounds to and the next shot starts where this one ended (no frame lost or doubled at a cut).
    chain = cut + chain.replace("[0:v]", "[cut]", 1)
    if title:
        text, off = title
        tf = os.path.join(work, "title.txt")
        open(tf, "w", encoding="utf-8").write(text)
        tt = f"(t+{off})"  # song time, not shot time
        chain += (f",drawtext=fontfile='{esc(FONT)}':textfile='{esc(tf)}':fontcolor=white:fontsize=96:box=1:"
                  f"boxcolor=black@0.45:boxborderw=28:x=80:y=h-th-80:enable='lt({tt},{TITLE_HOLD + TITLE_FADE})':"
                  f"alpha='if(lt({tt},{TITLE_HOLD}),1,max(0,1-({tt}-{TITLE_HOLD})/{TITLE_FADE}))'")
    tmp = path + ".part.mp4"
    run(["ffmpeg", "-y", "-v", "error"] + hw + ["-ss", f"{max(0.0, a - 3):.3f}", "-copyts", "-start_at_zero", "-i", seg["clip_file"],
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
        d = seg["local_end"] - seg["local_start"]
        n = round((at + d) * 30) - round(at * 30)   # frames from the SONG's running timeline: no drift
        p, hit = fragment(seg, banner, work, n)
        at += d
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
