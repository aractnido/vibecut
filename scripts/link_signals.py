r"""link_signals.py -- point timeline.json at each clip's processed video and movement data.

For every camera in show.json:
  cameras.<cam>.video_dir   processed video (lens-corrected / stabilized), same basename as the original.
                            Used only when its duration matches the original within 0.1 s: a processed clip must
                            stay frame-for-frame on the original's time axis, or its sync is wrong.
  cameras.<cam>.people_dir  extract_people.py output; <basename>-activity.json becomes the clip's "activity".
`audio_file` is never changed: sync and the soundtrack always read the original recording.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import show_config  # noqa: E402


def duration(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p],
                       capture_output=True, text=True)
    return float(r.stdout.strip() or 0)


def main():
    cfg = show_config.load()
    tl_path = show_config.path("timeline.json")
    tl = json.load(open(tl_path, encoding="utf-8"))
    for cam, clips in tl["cameras"].items():
        ccfg = cfg["cameras"].get(cam, {})
        vdir, pdir = show_config.path(ccfg.get("video_dir")), show_config.path(ccfg.get("people_dir"))
        linked = processed = 0
        for c in clips:
            name = os.path.basename(c["audio_file"])
            if vdir:
                v = os.path.join(vdir, name)
                if os.path.exists(v) and os.path.normcase(v) != os.path.normcase(c["file"]):
                    if abs(duration(v) - duration(c["audio_file"])) <= 0.1:
                        c["file"] = v
                    else:
                        print(f"  {cam} {name}: processed length differs from the original -- not used")
                processed += os.path.normcase(os.path.dirname(c["file"])) == os.path.normcase(vdir)
            if pdir:
                act = os.path.join(pdir, os.path.splitext(name)[0] + "-activity.json")
                if os.path.exists(act):
                    c["activity"] = act
                    linked += 1
        print(f"{cam}: {len(clips)} clips, {processed} on processed video, {linked} with activity")
    json.dump(tl, open(tl_path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print("updated", tl_path)


if __name__ == "__main__":
    main()
