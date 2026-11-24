# vibecut 🎬🎥

Automatically direct a multicam edit of your live show, instead of scrubbing through five angles
of a two-hour concert by hand.

You've got a camcorder running the whole set, plus a few phones and action cams scattered around
pointed at the stage. Nobody's clocks agree, nobody's framing is consistent, and nobody filmed
the whole show on one camera. 🎸 `vibecut` takes that whole pile of footage and builds a single
continuous multicam edit: it figures out where every clip really falls on a shared timeline, then
decides shot-by-shot which camera is actually the most interesting one to be on right now — who's
moving, who's in frame, who's about to do something — and cuts to it. On the beat, not on a
stopwatch. 🥁

This is an agentic skill. `SKILL.md` holds the agent's instructions, written for an AI agent to
read and drive the scripts in `scripts/`. 🤖 You bring the raw footage and tell the agent roughly
who shot what, and it figures out the sync, the scoring, and the editing.

## Why this is hard 🧩

Every camera's clock is wrong in a different way. Phone clocks drift and reset, action-cam clocks
run fast or slow by minutes over a long show, and nobody hits record at the same instant anyway.
None of that can be trusted — the only honest signal for "when did this clip really happen" is
the audio itself, cross-correlated against the one camera that ran continuously for the whole
show.

Knowing *when* a clip happened isn't the same as knowing whether it's worth showing. A camera
can be rock-steady and perfectly in frame while nobody's doing anything, and a phone footage
clip can be a shaky mess exactly when the moment is great. A good director score has to weigh
movement, energy, and shot quality together, resist flickering between cameras every two
seconds, and still give every camera at least one look rather than just parking on whichever
scores highest in a vacuum. Read `SKILL.md` for the full reasoning, the bugs that got found
fixing this on real footage (clock drift, audio aliasing onto a repeated chorus, frame-rounding
drift accumulating over a whole song), and why each design choice exists.

## Requirements ⚙️

`ffmpeg`/`ffprobe` on PATH, an NVIDIA GPU (every encode goes through NVENC, no CPU fallback), and
Python 3 with `numpy`, `librosa`, and `opencv-python`. Pose/movement extraction uses a YOLO
pose model (`ultralytics`).

## Quick start 🚀

The agent starts by asking you a few questions — which cameras you had, your **preferred video
sources** and your **preferred audio sources** (often not the same), which camera covers every
song, whether any camera sees the dance floor — and writes them into a `show.json`
(`examples/show.example.json` documents every key). Then, from your show's working folder:

```powershell
py scripts\sync_to_anchor.py          # every clip placed on one timeline, by audio
py scripts\build_soundtrack.py        # soundtrack from your preferred audio sources, per-camera stems kept
py scripts\extract_people.py <clips> <people>   # per camera: who's in frame, how much they move
py scripts\link_signals.py
py scripts\floor_energy.py            # dancing on the floor, for crowd moments
py scripts\build_highlights_edl.py    # the director writes the scene script
py scripts\render_cached.py 01        # render one song, review it, then the rest
```

Rendering goes through a fragment cache: every shot is encoded once (phone HDR converted to SDR on
the way), so when you say "not that camera here", only that shot is re-encoded.

A show with one continuous camcorder master still works the original way (`build_masters.py` →
`build_timeline.py` → `build_highlights_edl.py` → `render_song.py`, see `SKILL.md`).

## Requirements ⚙️ (in addition to the above)

`scipy` for sync and the soundtrack; an `ffmpeg` build with Vulkan/`libplacebo` for the GPU HDR→SDR
conversion.

## Status

Proven end-to-end on two real shows: a 22-song, 5-camera show with a camcorder master, and a
25-song show with no master at all (two fixed action cams + two phones, one of them vertical).
