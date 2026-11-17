r"""sync_to_anchor.py -- place every clip of every camera on the anchor camera's timeline, by audio, and write
timeline.json. For shows where no single camera ran continuously (no camcorder master): the anchor is any camera
that has ONE FILE PER SONG covering every song (e.g. a GoPro set to chapter per song).

Roster and paths come from show.json (see show_config.py / examples/show.example.json):
  anchor.camera, anchor.dir (its per-song originals), anchor.clock_fix_s (seconds to ADD to its creation_time
    to get real UTC -- find it by comparing against a phone with network time; constant, not drift)
  cameras.<cam>.audio_dir   originals (sync always reads original audio)
  cameras.<cam>.video_dir   processed video to render (lens-corrected / stabilized), same basenames
  cameras.<cam>.per_song_files  true for chaptered cameras: their clock is compared to the anchor's per song
  cameras.<cam>.clock       "creation_time" (default) or {"csv": path, "file_col": ..., "utc_col": ...}
                            (e.g. a rename plan that kept each phone file's original capture time)

How a clip is placed (each step exists because a simpler one failed on real footage):
  1. Predict where the clip starts inside its song's anchor file (song = the clip's NN- filename prefix):
     chaptered cameras via the median clock difference to the anchor across songs; other cameras by their own
     clock, which must then be roughly real (phones with network time are).
  2. GCC-PHAT on prep()-conditioned audio (audio_sync_utils) in a NARROW window (+-SEARCH s) around the
     prediction -- narrow on purpose, so a repeated chorus elsewhere can't win -- at four windows spread over the
     clip; verified = at least two windows agree within AGREE s. If the narrow search can't confirm, retry once
     at +-WIDE_SEARCH.
  3. For trusted-clock cameras, reject a "verified" offset more than MAX_CLOCK_DEV s away from that camera's
     median clock error: short clips' windows overlap, so two of them can agree on the SAME wrong chorus repeat
     (10 of 89 clips at the show this was built on; a frame check confirmed the clock, not the audio).
Unverified clips stay in timeline.json with verified=false; downstream steps ignore them.
"""
import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

# CPU FFT on purpose: a CuPy context opened while an NVENC batch ran coincided with a GPU reset ("invalid
# device") that killed the batch mid-clip. Sync is cheap on the CPU.
sys.modules["cupy"] = None
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audio_sync_utils import SR, aud, gcc_phat_robust  # noqa: E402
import show_config  # noqa: E402

SEARCH, WIDE_SEARCH = 6.0, 25.0
WIN = 20.0
AGREE = 0.15
MIN_Z = 8.0
MAX_CLOCK_DEV = 2.0
VIDEO_EXT = (".mp4", ".mov")


def probe(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:format_tags=creation_time",
                        "-of", "json", p], capture_output=True, text=True)
    fmt = json.loads(r.stdout)["format"]
    ct = fmt.get("tags", {}).get("creation_time")
    return float(fmt["duration"]), (datetime.fromisoformat(ct.replace("Z", "+00:00")) if ct else None)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def measure(clip, clip_dur, ref, pred, at, search):
    L = min(WIN, clip_dur - at - 0.2)
    if L < 4:
        return None, 0.0
    sig = aud(clip, at, L)
    r0 = max(0.0, pred + at - search)
    r1 = min(len(ref) / SR, pred + at + L + search)
    if r1 - r0 < L * 0.6:
        return None, 0.0
    lag, z = gcc_phat_robust(sig, ref[int(r0 * SR):int(r1 * SR)])
    off = r0 - at - lag
    if abs(off - pred) > search - 0.25:   # pinned to the window edge: the real match lies outside it
        return None, z
    return off, z


def place(audio_path, ref, pred):
    dur, _ = probe(audio_path)
    last = max(0.5, dur - WIN - 0.7)
    spots = sorted({round(x, 1) for x in (0.5, last * 0.33, last * 0.66, last)})
    for search in (SEARCH, WIDE_SEARCH):
        meas = [measure(audio_path, dur, ref, pred, at, search) for at in spots]
        cands = [(o, z) for o, z in meas if o is not None and z >= MIN_Z]
        best, support = None, 0
        for o, z in cands:
            agree = [c for c in cands if abs(c[0] - o) <= AGREE]
            if len(agree) > support or (len(agree) == support and best and z > best[1]):
                best, support = (float(np.median([c[0] for c in agree])), z), len(agree)
        if support >= 2:
            return best[0], dur, True, meas
    return (best[0] if best else pred), dur, False, meas


def clock_times(cam_cfg, folder):
    """{basename: clock UTC} for one camera, from creation_time or a CSV."""
    clk = cam_cfg.get("clock", "creation_time")
    out = {}
    if isinstance(clk, dict):
        for r in csv.DictReader(open(show_config.path(clk["csv"]), encoding="utf-8-sig")):
            t = datetime.fromisoformat(r[clk["utc_col"]].replace("Z", "+00:00"))
            out[r[clk["file_col"]]] = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    else:
        for f in os.listdir(folder):
            if f.lower().endswith(VIDEO_EXT):
                out[f] = probe(os.path.join(folder, f))[1]
    return out


def main():
    cfg = show_config.load()
    a = cfg["anchor"]
    anchor, adir = a["camera"], show_config.path(a["dir"])
    acfg = cfg["cameras"].get(anchor, {})
    songs = {}
    for f in sorted(os.listdir(adir)):
        if f.lower().endswith(VIDEO_EXT) and f[:2].isdigit():
            dur, ct = probe(os.path.join(adir, f))
            songs[f[:2]] = {"file": f, "title": os.path.splitext(f[3:])[0].rsplit(" - ", 1)[0], "dur": dur,
                            "raw": ct, "real": ct + timedelta(seconds=a.get("clock_fix_s", 0.0))}

    def video_for(cam_cfg, name, fallback):
        vd = show_config.path(cam_cfg.get("video_dir"))
        return os.path.join(vd, name) if vd and os.path.exists(os.path.join(vd, name)) else fallback

    cams = {anchor: [{"file": video_for(acfg, s["file"], os.path.join(adir, s["file"])),
                      "audio_file": os.path.join(adir, s["file"]), "song_num": n, "title": s["title"],
                      "start": iso(s["real"]), "end": iso(s["real"] + timedelta(seconds=s["dur"])),
                      "offset_in_song": 0.0, "verified": True, "placed_by": "anchor"} for n, s in songs.items()]}

    for cam, ccfg in cfg["cameras"].items():
        if cam == anchor:
            continue
        folder = show_config.path(ccfg["audio_dir"])
        clocks = clock_times(ccfg, folder)
        files = sorted(f for f in clocks if f[:2].isdigit() and f[:2] in songs and not f.startswith("00-"))
        if ccfg.get("per_song_files"):
            diffs = sorted((clocks[f] - songs[f[:2]]["raw"]).total_seconds() for f in files)
            offset = diffs[len(diffs) // 2] if diffs else 0.0
            print(f"{cam}: clock minus anchor clock, median {offset:.1f}s")

            def pred_of(f):
                return (clocks[f] - songs[f[:2]]["raw"]).total_seconds() - offset
        else:
            def pred_of(f):
                return (clocks[f] - songs[f[:2]]["real"]).total_seconds() + ccfg.get("clock_offset_s", 0.0)
        out = []
        for f in files:
            num = f[:2]
            if "audio" not in songs[num]:
                songs[num]["audio"] = aud(os.path.join(adir, songs[num]["file"]))
            pred = pred_of(f)
            src = os.path.join(folder, f)
            off, dur, ok, meas = place(src, songs[num]["audio"], pred)
            start = songs[num]["real"] + timedelta(seconds=off)
            out.append({"file": video_for(ccfg, f, src), "audio_file": src, "song_num": num,
                        "title": songs[num]["title"], "start": iso(start), "end": iso(start + timedelta(seconds=dur)),
                        "offset_in_song": round(off, 3), "pred": round(pred, 3), "verified": ok,
                        "placed_by": "audio" if ok else "clock"})
            print(f"  {'OK ' if ok else '-- '} {cam:10s} {f[:44]:44s} pred {pred:7.2f} -> {off:7.2f}")
        if not ccfg.get("per_song_files"):
            ok = [c for c in out if c["verified"]]
            if ok:
                med = float(np.median([c["offset_in_song"] - c["pred"] for c in ok]))
                for c in ok:
                    if abs(c["offset_in_song"] - c["pred"] - med) > MAX_CLOCK_DEV:
                        c["verified"], c["placed_by"] = False, "far_from_clock"
                        print(f"  rejected {cam} {os.path.basename(c['audio_file'])}: "
                              f"{c['offset_in_song'] - c['pred']:+.2f}s from its clock (median {med:+.2f}s)")
        cams[cam] = out
    json.dump({"anchor": anchor, "cameras": cams}, open(show_config.path("timeline.json"), "w", encoding="utf-8"),
              indent=1, ensure_ascii=False)
    n = sum(len(v) for k, v in cams.items() if k != anchor)
    ok = sum(c["verified"] for k, v in cams.items() if k != anchor for c in v)
    print(f"placed {n} clips, {ok} verified; wrote timeline.json")


if __name__ == "__main__":
    main()
