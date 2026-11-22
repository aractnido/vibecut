---
name: ai-director
description: Build a fully-automated multicam edit of a whole show from several independent camera/phone sources (fixed cameras, GoPros, phones) -- syncs every clip by audio, cleans each camera (lens/roll correction, phone stabilization, HDR->SDR), measures movement and dance-floor energy, directs shot by shot on the beat from the user's preferred video sources, mixes a soundtrack from the user's preferred audio sources, and renders through a fragment cache so later changes re-encode only the shots that changed. Works with or without a continuous camcorder master. Use this whenever the user wants an automated full-show multicam edit driven by movement/energy/performer signals, as opposed to `bootlegcam`'s per-song manual-curation workflow.
---

# AI Director

Status: **end to end.** Proven on two full shows: one with a continuous camcorder master (the
original Nikon-anchor path, documented from "Stage 1" on below), and one with no master at all
(GoPros + phones only -- the `show.json` path in the next two sections).

## First: ask the user (never assume a camera combination)

Every show has a different mix of cameras and the user has opinions about them. Before writing
`show.json`, ask -- in plain words, one short list:

1. **Which cameras** were rolling, and roughly where (fixed on a stand, handheld in the crowd, vertical?).
2. **Preferred video sources**, in order ("the phone near the front is the best angle, the wide
   fixed cams are backup"). This becomes `director.priority`. Cameras not listed fill in by score.
3. **Preferred audio sources**, in order, and which camera fills the gaps. Often NOT the same as
   the video preference (a phone near the PA can sound better than it looks). This becomes
   `soundtrack.priority` / `soundtrack.bed` -- or `anchor.audio: "self"` if a camera recorded the
   board feed.
4. **Which camera covers every song** (one file per song) -- the anchor/clock.
5. **Does any camera see the dance floor**, and should the crowd get shots when it gets going?
6. **Variety**: how many shots in a row on the main camera before a look elsewhere?
7. **Performer framing preferences**, if any (e.g. someone prefers fewer shots from behind).
   Record it neutrally in `director.avoid_shots`; don't put reasons in files.
8. **Final look**: SDR (default -- see "Color" below) and the song title banner (title only by default).
9. **Output size and where things go**: 4K or 2K (at 2K a ~3K GoPro fills the frame; no inset needed),
   the review folder (render there first) and the publish folder (only on the user's go; keep 4K and
   2K side by side as e.g. `MultiCam.4K.AI` / `MultiCam.2K.AI` rather than overwriting).
10. **Per-song style -- harsh or soft.** Propose a two-column split and let the user flip songs; ask why
    when you disagree and write the reason down. Harsh = plain cuts, normal-speed moves; soft = short
    dissolves, half-speed moves. The MELODY decides, not tempo or guitar weight (a heavy riff song with
    a sung melody was soft; tempo trackers rate a gentle sing-along faster than a hard rock song, and a
    loudness/onset/flatness score put the hardest song in the soft half). Don't trust audio features here.
11. **Fixed-camera moves**: virtual push/zoom on the fixed cams? Which camera is too far for anything but
    zooms? For EACH fixed camera, show one full frame with a 10 % grid and ask what the shot must keep
    (the band, the band's logo/sign) and what it must never show (PA, empty floor, a TV, people filming)
    -- that becomes its keep rectangle.
    Is the vertical cut + fill (below) welcome, and how often (default: occasional variation only)?
12. **Crowd participation moments** the user knows about (a song where the room does a gesture): the
    floor should be on screen then. Ask; don't infer it from the lyrics.
13. **Is the final audio coming from elsewhere** (a separate mixing session)? Then render video first and
    mux the delivered per-song audio afterwards; never use a recording that has gaps between songs.

Read the user's reasons as reasons: "use the phone more, the fixed cams are far and dark" asks for
ONE change (the priority); it doesn't also ask to zoom or brighten the fixed cams.

**How to run a review with the user** (they review late at night; keep the load low): one point per
message, short prose, at most one yes/no question. Show candidates as a contact sheet, ONE frame per
moment, labelled with natural numbers (1, 2, 3 -- not timestamps), so they can answer "3, 5, 7 are real".
Never start renders while they are still looking or asking; never chain the next build after a result.

## Shows without a continuous master: the `show.json` pipeline

`examples/show.example.json` documents every key. Order of steps (each script reads `show.json`
from `$VIVECUT_SHOW_ROOT`):

| Step | Script | Notes |
|---|---|---|
| Fixed cams: lens/roll | `lens_calibrator.py` -> `apply_lens_correction_gpu.py` | interactive per camera, see below |
| Phones: shake | `detect_shakiness.py` -> `apply_stabilization.py` | **always, for every phone camera** -- not optional |
| Sync | `sync_to_anchor.py` | writes `timeline.json`; only `verified` clips are used downstream |
| Soundtrack | `build_soundtrack.py` | preferred audio first, bed fills gaps, levelled, crossfaded; per-camera stems kept |
| Movement | `extract_people.py` per camera | keeps vertical video's shape |
| Link | `link_signals.py` | timeline -> processed video + activity |
| Floor | `floor_energy.py` | dancing per second on cameras that see the floor |
| Direct | `build_highlights_edl.py` | the scene script, `highlights_edl.json` |
| Render | `render_cached.py` | fragments on demand + cache, then stream-copy join + audio |

Render ONE song first and have the user review it before the rest (`render_cached.py 01`); every
later change is cheap because unchanged shots come from the cache.

**Delivery gotchas (all hit on real files):**
- Song titles with apostrophes ("Sweet Child O' Mine", "We're Not Gonna Take It") break ffmpeg concat
  lists (both `'\''` and backslash escaping failed) and shell loops. Join through hard links with plain
  names (`01.mp4`...) and do file loops in Python.
- The full-show builder globs `NN-*.mp4` -- exclude `00-*` or a rebuild swallows the previous full show.
- Replacing one or two songs in a published full show needs no re-encode: re-join the video streams of
  the 25 song files (stream copy) and copy the existing crossfaded audio track over; lengths stay equal.
- Recompute the YouTube chapters from the actual song durations after any re-render (1 s shifts happen).
- Final audio delivered later as per-song FLACs: place each by GCC on several windows (majority of 4
  agreeing within 50 ms) and mux by stream copy; a whole-show FLAC built from untrimmed song files runs
  long -- build the crossfaded show audio from per-song windows instead (`acrossfade` 1 s, triangular).

**Color.** Phones usually record HLG HDR (BT.2020, 10-bit); fixed cams SDR. Mixed unconverted, the
phone shots look washed out next to the SDR shots. Make the final video SDR and convert the phones
(`prep: "hlg_to_sdr"`: libplacebo on the GPU, Hable curve -- visually matched the CPU zscale chain
at 2x the speed). Converting the SDR cameras UP to HLG was tried at four strengths and rejected:
it can't add range the camera never captured, and every version looked flatter than the original.

**Fragments on demand, not a standardization pass.** Converting every clip up front (HDR->SDR,
fps, rotation) processes hours of footage nobody sees and adds an encode generation. Instead each
shot's conversion + framing happens inside its fragment encode, keyed by source/in/out/chain/
title/encoder (`render_cached.py`). An unchanged song re-assembles from cache in seconds. Keep the
processed clips (`corrected/`, `stabilized/`) until the show is final -- they are the cache's sources.

**GPU first, CPU only where no GPU filter exists -- the user's standing rule, not a tuning detail.**
The first cached renderer decoded on the CPU and blurred a full 4K frame per output frame: ~4 fps per
GoPro shot, ~22 shots an hour, ~1 day for one show. `render_cached.py` now decodes on NVDEC for
every camera, scales fixed SDR cameras with `scale_cuda` (frames stay on the GPU), does phones'
HLG->SDR tone map AND resize in one `libplacebo` (Vulkan) pass, blurs the background from a 960x540
copy (the one CPU filter, at 1/16 of the pixels) and encodes on NVENC: ~0.8-1.1x real time per shot,
5-7x faster. Any step left on the CPU must be named as such and justified ("no GPU equivalent in
this ffmpeg build"), never inherited silently from an older script. **This includes analysis scripts**:
a scan that sampled frames with `cv2.VideoCapture` + seeks ran 29 minutes on the CPU; the same scan
reading frames from `ffmpeg -hwaccel cuda ... -vf fps=2 -f rawvideo -pix_fmt bgr24 -` took ~10. Before
running anything that reads video, check how it decodes, and print progress per song so a slow path
shows up in a minute. (Lens correction's full-rate 4K read is the measured exception, see below.)

**Cutting a shot out of a long clip -- three rules, each from a real bug:**
1. `-i clip -ss X` (output seek) is frame-exact but runs every frame from the clip start through the
   WHOLE filter chain and drops them at the output -- most of the render time went to filtering
   discarded 4K frames.
2. `-ss X -i clip` (input seek) alone restarts the timestamps, which changes which frames a
   29.97->30 fps conversion duplicates (not frame-identical to the reference, tested).
3. So: `-ss X-3 -copyts -start_at_zero -i clip`, trim with margin, `fps=30:start_time=0` (grid at the
   clip's t=0), cut on that grid, and take each shot's frame count from the SONG's running timeline
   (frames to its end minus frames to its start). Every cut lands within half a frame of the script
   and per-shot rounding can't accumulate (it had: one shot came out a frame short with time-based trims).

**Variable-frame-rate phones are standardized to constant 30 fps -- never trust the declared rate.**
Some phones (a Samsung at the second show) record variable frame rate: frame gaps from 2 ms to 67 ms,
and the header's `r_frame_rate` says 30, 29.97, 60 or 240 on different clips while the real rate is
~30. Check every phone camera once: `ffprobe -show_entries stream=r_frame_rate,avg_frame_rate` (they
disagree on a VFR clip), or the spread of the frame-timestamp gaps. If a camera is VFR, the standardized
fragment re-times it to constant 30 fps FROM ITS REAL TIMESTAMPS: the `fps=30:start_time=0` step in rule 3
above, applied to every source (an iPhone's steady 29.97 goes through the same grid). Any step that
computes time from a frame index or the declared rate (frame counts, `-r`, seeking by frame number) drifts
on these clips -- always work in timestamps, then let the 30 fps grid do the conversion.

**The director is a priority ORDER, not a bonus.** Crowd moment > forced variety > preferred video
sources > score. Activity has no fixed scale: 0.02-0.08 between cameras at one show, 1-2 at another
where a fixed camera saw a whole moving crowd, so a "+0.6 preferred camera" bonus still lost 22 of
~50 minutes of the preferred camera's shots. Check any hand-picked weight against the real spread of
the signal it competes with.

**Crowd moments.** `floor_energy.py` counts people dancing (shoulder bounce >= 0.03 of body height,
walking removed) inside each fixed camera's floor rectangles. A 10 s mean of 1.5 people dancing was
the 97th percentile at the second show and gave 13 crowd shots, all in the liveliest songs; 2.5 gave
one. Pick the threshold from the show's own percentiles.

**Full coverage skips near misses (known limitation).** A camera is a candidate for a shot only if it
covers the WHOLE window (the invariant below). With ~16 s windows a 23 s phone clip that starts 8 s after
one window and ends 1 s before the next is never used, however good it is. If the user asks "why wasn't
that clip used?", this is the usual answer; a fix would let the director shorten a window to fit.

**Per-clip orientation.** One phone can mix vertical and horizontal clips. Choose the framing per CLIP
from its displayed shape (width/height after the rotation tag), not per camera: a per-camera
"landscape crop" turned two vertical clips into a zoomed-in slice of the middle.

**Phone sync: the trusted-clock rule.** Phones keep good network time, but each phone has its own
steady error (~1 s behind on one Android, scatter ~0.3 s; whole-second `creation_time`). For each clip:
predict its offset from its clock plus the error MEASURED on the nearest verified clip of the same
phone; search only +-1.5 s around that (never widen -- the old +-25 s fallback locked onto a riff
repeat 21 s away); score many 4 s windows every 1.5 s (count a window at z >= 5); take the largest
cluster agreeing within 30 ms, accept if >= 3 windows and >= 40 % of the counted ones; reject any
placement that overlaps another clip from the same phone. It recovered a clip the old search had
dropped ("perfectly on sync" on review), and when re-run it also nudged OTHER clips by small amounts --
apply only the clips you set out to fix, not the whole re-measured timeline.

### Cinematic camera work on the fixed cameras (reviewed shot by shot with the user)

Virtual moves are crops of the fixed GoPros, rendered on the GPU (`libplacebo` with per-frame
`crop_x/crop_y/crop_w/crop_h` and `pos_*` expressions: sub-pixel, no jitter; NVDEC in, NVENC out).
Principles from cinematography (motivated moves, ease in/out, restraint, hold-move-hold "re-cinematography"
from virtual-PTZ research), then tuned by review:

- **One move per shot, motivated**: a slow push-in or pull-out (alternating shot to shot) toward the
  musician moving most (pose keypoint motion over the shot). Following pans between musicians
  ("re-frame when the action shifts") was tried and rejected.
- **Easing and speed**: smootherstep (zero speed and acceleration at both ends), zoom interpolated in
  log space, hold one beat at each end so the camera is STILL at every cut. Speed limit = the fastest
  moves the user liked: 0.03 log-zoom/s (they ran 0.014-0.028; 0.064 was "too fast"); a short shot makes
  a smaller move, never a faster one; pieces under 2 bars hold still.
- **Far camera = zooms only, motivated, on a little fill**: alternating in/out on every shot was
  "exaggerated, predictable" -- push in only if the main musician gets busier through the shot, pull out
  if calmer, else hold the wide (max ~1.45x). The fill is NOT the zoom: a version where the window grew
  while the picture scale stayed constant read as "moving the filling", a fake zoom. Make the picture
  scale really change and let the window shrink by only part of it:
  `pos_w = OW * min(1, sqrt(crop_w * UPS / OW))` (UPS ~1.15) -- real zoom, a thin fill only at the tightest.
- **Keep regions, not detections, for fixed cameras**: ask the user what each fixed camera's shot must
  contain (here: the band AND the band's neon logo) and what it must not (a PA speaker on one side, empty
  floor, a TV, people filming at the edge). Store one normalised rectangle per fixed camera; the wide is
  the smallest 16:9 crop holding it. Limits computed from detected people failed -- a person on the floor
  or by the PA loosened them. Pushes are crops INSIDE that wide, anchored to its TOP edge: they move
  toward the band and the logo and "never go down" to the floor; keep the outermost musician whole.
  Agree the rectangle WITH the user on a full frame: draw two yellow horizontal lines (top of the band
  incl. the logo, bottom = just below the lowest feet / stage props) and a green vertical line for the
  side never to show, and adjust until they say yes (it took three rounds: the bottom line had to drop
  below a stage prop and the performer's feet; the side line moved left so the logo wasn't cut).
  On the far camera the push target is the wide's UPPER-LEFT corner region (going up also goes left,
  toward the band, away from an empty side), and any shot longer than ~16 s always makes a slow move
  (direction from the activity trend) -- a 34 s frozen wide read as dead.
- **Busy floor widens the wide**: when >= 1 person is dancing on average over a shot (top ~12 % of shots),
  the floor-facing camera's wide reaches the crowd, still anchored high -- the user liked seeing the room
  jump, as long as the frame didn't sink to the floor.
- **Vertical cut + fill** (a 9:16 slice at nearly full sensor height on a blurred fill -- a sharp
  single-musician close-up): VARIATION only (<= 2 per song, >= 60 s apart, never in the first 20 s --
  open on an establishing shot), exactly one 4-bar phrase long.
- **Never reframe on the same camera** -- landscape -> vertical or vertical -> landscape on one camera
  reads as a cheap "pop" (a jump cut). A vertical always has a different camera on both sides: enter it
  from a phone or the song start, leave it to the preferred phone (else another camera). Entering it
  from the far wide camera was the harshest transition of all (distance, shape and angle all change).
- **Music bars matter**: find downbeats (beat phase with the strongest low-end accent) and 4-bar
  phrases (the phase whose boundaries sit on the biggest beat-synced chroma+MFCC change), then move
  every camera cut to the nearest phrase boundary within 2 bars -- if both cameras have footage there
  and the move doesn't push a camera into an avoided moment. A vertical that ran past the phrase "felt
  like the music told us it was time to change".
- **No cut while someone crosses the stage**: a dissolve while the bassist rushed to the mic was
  disliked; the fixed cameras as a stage motion sensor (torso-centre travel over 0.5 s, confident
  points only) read 0.66 body-heights/s there vs 0.13-0.24 at every liked transition. Threshold 0.35
  (flags ~4 % of a song); if every candidate time is busy, take the calmest beat within 2 beats.
- **Per-song style** (question 10): harsh songs keep plain cuts; soft songs get 20-frame dissolves
  centred on every cut (`xfade_vulkan` on the GPU; each fragment rendered with 10-frame handles, frozen
  frames only where a clip has no footage past the cut) and moves at half speed.
- **Open ideas from review, not built**: when the floor is very active, avoid fixed-cam -> fixed-cam
  cuts (go to a phone that sees the floor); the room's gesture moments (question 12) should show the floor.

### Vertical phone shot -> almost horizontal (`scripts/portrait_zoom.py`, cached)

A vertical clip normally sits full height on a blurred fill. A long vertical shot can instead slowly zoom until it
is nearly landscape, cutting away the top (ceiling, wall) and a little of the bottom (floor, cables). Each rule
below came from a review round on one 47 s shot:

- **Limits from the band, never a fixed line.** A hand-set top line was right at the start and cut every head
  40 s later, after the operator stepped closer. Use pose data (`extract_people.py`): top = the highest person's
  box top minus 4 % (hard, never cuts a head); bottom = the lowest ankle plus 3 %, capped at 92 % (soft). A ±2 s
  envelope then a 1.5 s Gaussian, so the window moves before anyone would be cut.
- **Quality limit from measured detail, not a percentage.** "Stop at 85 %" was arbitrary. The user's rule: never
  enlarge a pixel. A phone's nominal 4K is not real 4K, though: noise reduction, HEVC, low light and stabilization
  leave a dim stage with about 50 % of the nominal detail. `effective_detail()` measures it per clip on the GPU:
  shrink the band region, enlarge it back, and find where SSIM drops (threshold 0.9925, interpolated). The screen
  must get at least 65 % of a true output picture (SSIM about 0.996, the framing the user called balanced):
  `window height >= OUT_H * 0.65 / f_eff`. When the band needs less, open half below the feet and the rest above
  the heads, so the extra is more wall and logo, not floor. Width is always the full frame: only the height sets
  sharpness, so the picture ends slightly narrower than the output, with thin fill strips.
- **Time it in phrases.** A 2.9x zoom from far away in 10 s was dizzy, and 13 s was still too fast. "Perfect" was
  3 phrases (12 bars, phrase boundary to phrase boundary, about 0.05 log-zoom/s). Give bigger zooms more phrases.
  The zoom is in log space toward a fixed point, with smootherstep; after it, the window keeps following the band.
  Ending on a lyric stanza was tried (Whisper word times): it gave 8 or 16 bars and nothing better.
- **Shake: slow sway only, half strength** (`scripts/window_stabilize.py`). The window has spare margin, so it can
  follow the content at no quality cost (horizontally the window narrows by the sway, which costs only width).
  The motion is measured on the GPU with a robust global Lucas-Kanade; phase correlation aliased on evenly spaced
  wall planks, and CPU corner tracking was worse than nothing. Removing all the shake frame by frame made the
  image warp "like water": rolling shutter bends each frame, and parallax slides the other depths. Of four
  variants (half / vertical only / slow sway only / slow sway half) the user picked slow sway half.
- **The fill is derived data.** It reads the same stabilized window, so it never shakes on its own.
- **Blur the fill until nothing in it is recognisable.** At the old blur (sigma 4 at 640x360) people and a guitar
  could still be made out, enlarged on the sides, and pulled the eye. Of 4 / 8 / 12 the user picked 8: only light
  and colour remain.

**Cache:** `fragment()` keys each shot on everything that decides its pixels: source (path, size, mtime), in/out,
frame count, pose file, every limit, quality, zoom and shake parameter, the measured `f_eff`, output size, encoder
and `VERSION`. A hit is reused; a miss is rendered and stored with its spec. The shake path and the detail
measurement have their own caches. The frame grid matches `render_cached.py`, so fragments join with `-c copy`.

How it was reviewed: draw a 10 % grid on 3 frames of the shot (start, middle, end) and let the user say where the
top line goes; render 10 s tests first; then the full shot, then one variable at a time.

### Edit pacing: measure it, then add soft rules (`scripts/edit_stats.py`)

Hard rules and metadata define what is possible at each moment: who has footage, phrases, how far a clip can zoom,
shake, stage crossings. Soft rules are preferences the director weighs among those options. When the user asks
"are we cutting too much / too randomly?", run `edit_stats.py` on the published sidecars before answering.

What the first full show measured (25 songs, 107 min):
- 2.9 camera changes per minute and a median take of 15 s (p10 6.5 s, p90 43.5 s), slower than music videos
  (2-3 s) and typical concert films (4-8 s). That is fine when the preferred phone carries the story (43 % of the
  time, median take 43 s).
- 82 % of cuts land on a phrase boundary. Next-camera choice is far from random (0.90 bits vs 1.58 random).
- The edit READ as random anyway, because of three patterns:
  - Fixed cam -> fixed cam direct cuts were 59 % of all cuts: with the phone unavailable, the director bounced
    between the two GoPros.
  - 54 ping-pongs (A -> B -> A with B < 8 s, 17 % of cuts).
  - 11 takes under 4 s (the shortest 0.6 s), which look like mistakes.
- Soft songs were NOT slower (median 14.0 s harsh vs 15.4 s soft): the style changed only the transition type.

Soft rules the user approved from that analysis (to wire into the director):
1. **Avoid fixed -> fixed cuts and ping-pong.** Penalise a direct cut between two fixed cameras; prefer going
   through a phone. Penalise A -> B -> A when B would last under ~8 s: extend A or B instead.
2. **Minimum take of one 4-bar phrase** (~7 s at 130 BPM). A take shorter than that merges into its neighbour,
   unless that would break a hard rule (no footage, an avoided moment).
3. **Soft songs hold longer:** target take length ~1.5x the harsh songs' (on top of dissolves and half-speed
   moves), per the research: gentle songs hold angles, energetic ones cut faster.
Re-run `edit_stats.py` after the change; the targets are fixed->fixed well under half of cuts, ping-pong near
zero, no take under one phrase, and soft median ~1.5x harsh.

### Song map: what the music is doing, as director metadata (`scripts/song_map.py`)

Cuts landed on phrases, but the director knew nothing about what the music was doing inside them. The song map
gives it that, per bar and per 4-bar phrase. **Lyrics are a hint, never the source of truth**: the separated
stems carry the reliable signals.

Pipeline: separate, analyze, discard. HTDemucs splits the song window into vocals / drums / bass / other, in
memory on the GPU, in about 4 s per song. The stems are disposable (the user's rule: keep only the metadata;
re-run if more is needed), so nothing is written to disk. Whisper large-v3 transcribes the vocal stem, and the
features (envelopes, onsets, chroma, timbre bands) are measured with torch on the GPU. One Python environment
needs torch, demucs, openai-whisper and scipy.

| Signal | From | Director use (soft rules, not wired yet) |
|---|---|---|
| singing on/off | vocal-stem level | don't cut in the middle of a sung line; with no singing, the camera goes to the instruments |
| solo | guitars/keys loud while vocals are silent | go to the soloist; the moment for the fixed-cam vertical close-up |
| drum fill | onset burst in the last bar of a phrase | the classic cut point: cut as the fill ends |
| sections / chorus | Ward clustering of chroma + timbre + vocal share + drums + energy; repeating lyrics | chorus starts are the strongest cuts and the time for wides; verses hold on the singer |
| energy per phrase | full mix + drum density | busy sections get shorter takes, calm ones longer |
| crowd | repeating hooks + dancing counts | show the room when it sings along or dances |
| banter | voice with the band silent | not music |
| grid check | where stanza-first lines start in the phrase | flags a phrase grid shifted by a bar |

What review taught:
- **Language per song, from a table the user confirms** (they flipped nothing). Detecting it per 30 s window
  failed: banter made a Spanish song "English", and Whisper then invented English lines.
- **Whisper's hallucination rule must be the combined one.** Sung lines over music score no_speech_prob around
  0.9, so dropping on that alone threw out every lyric. Drop only when no_speech > 0.6 AND avg_logprob < -1.0,
  plus compression ratio > 2.4 and stock phrases ("Thank you", "Subscribe to the channel"...).
- **Sections need more than chords.** A live mix barely changes harmony or level between verse and chorus, and
  chroma alone put a whole song in section A. Adding timbre, vocal share and drum density recovered verse /
  chorus / breaks in a song the user knows well.
- **Fills and the grid shift are per song.** Fills were clear in some songs (7) and absent in others (0). The
  suggested phrase shift came out -1 in five songs, +1 in three and 0 elsewhere, and changed with small
  transcription differences, so check it by ear before moving any grid.
- Live vocals are hard: Whisper still misses whole sung stretches. This is why singing on/off comes from the
  stem level, not from the text.

### Director rules from the second review round (signals wired in, then tuned on real songs)

The song map, the vertical zoom metadata and the pacing statistics went into the director. Then the user reviewed
songs and every rule below came from a specific moment they pointed at.

- **The preferred phone wins wherever it covers.** Any fixed-cam take the preferred phone fully covers becomes the
  phone's. Its picture is better and it moves naturally, and the far fixed camera often shows the phone operator
  filming, which "looks silly". A camera change is also never made AWAY from the preferred phone.
- **Section starts are camera-change points.** A natural cut the user pointed at (a bridge starting after a drum
  build-up, the voice coming back in) was never a cut, for two reasons. The director scored ~9 s windows with no
  idea of song structure and picked the same fixed camera on both sides. Then ping-pong removal merged the short
  inserts around it into a 62 s take. Rule: at each section start (song map letter change), if the current take
  has run at least 1 phrase and at least 1 phrase remains, change camera there, phone first. If the boundary falls
  mid-line, cut in the breath just before the voice comes back (here 0.9 s before the boundary). This also splits
  the over-long takes the pacing rules created (cuts per minute had halved, with 30 takes over a minute).
- **Vertical zoom inside the director:**
  - **Start:** on the first BAR line after the shot starts. Not at the cut, and not at the next phrase (waiting a
    phrase made 23-30 s shots ineligible).
  - **Length:** set by the trip at the liked speed (about 0.05 log-zoom/s), at least 2 phrases. A dim clip that can
    only zoom 1.7x needs 2 phrases; a 2.9x trip needs 3.
  - **Partial zooms are fine:** a shot too short for the whole trip zooms over the whole phrases it has, at the same
    speed. "Only by being able to do it, it gives better shots: wider, more dynamic."
  - **Skipped:** horizontal clips, and clips with raw shake p95 over ~150 px.
- **Far fixed camera: two phrases never sit still.** Shots of at least 2 phrases always make a slow move,
  direction by the activity trend, with a 2-beat tolerance (beat tracker vs phrase grid). A 16 s threshold left
  exactly-two-phrase shots (15.2 s) frozen. The move planner must use the SONG's beat: a 123 BPM constant left
  over from the first test made holds and phrase lengths wrong in every other song.
- **Dissolves on the beat.** Harsh songs: half a beat, an eighth note (A/B at 6 and 10 frames; 6 felt right at
  139 BPM = 6.5 frames). Soft songs: two beats (A/B at 24/30/35 frames; about 29 at 126 BPM). Centred on the cut,
  which is a downbeat, so the crossfade's midpoint lands on the beat. That gives 5-11 frames (harsh) and 24-44
  (soft) across one show's tempos.
- **A secondary phone can be not worth it.** A/B the same moments against the preferred phone before investing in
  it. One side-angle phone (behind keyboards and drums, stage lights into the lens, darker) lost 3 of 3 and became
  a last-resort fill only: no zoom, no pose extraction, no re-sync of its unverified clips.

- **The preferred phone has a ceiling.** "Phone first" plus "never cut away from it" produced a whole song as one
  3-minute phone take (and a 5-minute one). The phone keeps priority, but after 4 phrases (harsh) or 6 phrases
  (soft) a section start may cut away from it, to the fixed camera that does NOT show the operator filming. The
  cut-away only needs footage until the next section start; then the phone comes back. A cut-away required to
  cover the whole rest of the phone's take never found footage, so it never happened.

### Cache: shot bodies and transitions are separate fragments

The first dissolve version cached each shot WITH the extra frames its dissolve needed. Changing the dissolve length
(beat-based: harsh 0 -> 3 frames per side, soft 10 -> 14) changed every shot's range, so every key changed and the
whole show re-rendered for more than an hour. The user called it: "body and transitions are separated in the
cache; transitions are only cached when needed (on miss)".

```
cut at b, H = fixed margin (24 frames), h = dissolve frames per side (<= H)

A body:      [ a+H_before , b-H )              key: A's source + range       -> never depends on h
transition:              [ b-H , b+H )         key: A piece, B piece, h, H    -> rendered on a miss only:
                           A ... [blend 2h] ... B    A over [b-H, b+h), B over [b-h, b+H), xfade on the GPU
B body:                          [ b+H , c-H )
song = body + transition + body + ...  joined with concat -c copy (no full-song re-encode)
```

- **Margin per cut:** min(H, a third of either neighbouring shot), so a short shot keeps a body. A missing handle
  at a clip's end becomes frozen frames inside the transition. The A/B handle pieces are deleted once their
  transition exists.
- **Measured on one song:** first run 14 bodies + 13 transitions; a re-run had everything cached; changing the
  dissolve 6 -> 10 frames re-rendered only the 13 transitions (about 1.5 s of video each).
- **Pieces of a moving shot must share the whole shot's plan.** For moves, the keyframes are evaluated on the
  shot's own clock and shifted per piece. For the vertical zoom, detail and the shake path are measured over the
  whole shot (`portrait_zoom.fragment(ref=...)`). Measuring per piece crashed on a 0.3 s handle (no frames to
  sample), and would have framed and stabilised the body and its transition differently: a visible jump.
- **`xfade_vulkan` gotcha:** it passes untouched frames straight through, still in their own input's upload pool.
  With more inputs or longer dissolves, `hwdownload` failed with "Input frame is not in the configured hwframe
  context". A `scale_vulkan=w=iw:h=ih` before the download re-renders every frame into one pool.
- **Whole-song GPU assembly was CPU-bound.** NVENC sat at 5-24 % while one ffmpeg burned CPU, because every frame
  went CUDA decode -> system RAM -> Vulkan -> RAM -> NVENC. Re-encoding only the transitions removes that.
- **Still to do:** skip a song whose plan (shots + dissolve) matches its existing sidecar; and find the ~3.5 min
  per-song planning overhead (tempo and stage-motion recomputed every run) before calling the assembly
  "seconds".

### Unverified phone clips: re-run them with the trusted-clock rule, then let the user pick

Five clips of the preferred phone stayed unverified, one of them the user's favourite footage of a song ("really
fun videos ... discarded"). The timeline had placed each by an audio match 4-24 s away from what its clock
predicted ("far_from_clock"): a repeat of the same phrase. Searching ±1 s around clock + the phone's error
MEASURED on its verified clips of that song (here -0.1 to -0.8 s) gave one dominant peak per clip (correlation
0.53-0.80 against 0.29-0.43 for the runner-up). Show the user a short video per clip with the top 3 placements
back to back (a big number 1/2/3 on screen, final song audio at each placement) and let them pick by ear before
marking the clip verified. The rule was already in this skill. It just hadn't been re-run for this phone's
leftover clips: run it on every phone, every time.

### Lyric visuals: concert-style kinetic typography on the fill (reviewed song by song)

The blurred fill beside vertical shots (and, by choice, the edges of a full-frame shot) can carry lyric visuals the
way a stadium LED wall does. These are key phrases treated as design, appearing exactly when sung. They are NOT
karaoke.

**Text and timing.**
- **The words come from the user's lyrics, never from Whisper.** Whisper wrote "young and racist" for "young and
  restless".
- **Word times come from forced alignment** (MMS-FA, torchaudio, GPU) of those lyrics against the Demucs vocal stem.
  The stems are temporary and deleted after.
- **Whisper's own word times were 0.3-1 s off on live singing.** So was matching vocal attacks one by one.
- **Align in two steps.** One global forced alignment drifted 4-8 s on repeated hooks: the written lyrics have a
  fixed number of repeats and the live performance does not. 26 % of lines drifted more than 1 s.
  1. Run one global MMS-FA pass.
  2. Correct its drift with Whisper anchors: the lines Whisper heard clearly, shifted by the song's measured
     Whisper offset. Lines between anchors are interpolated.
  3. Re-time each line with MMS-FA inside a ±1.5 s window.
- **Order-only matching of the lyrics against Whisper text failed.** Whisper mishears too much: 50 of 62 lines found
  no match.
- **Sparse anchors flag good lines.** In a repeated chorus Whisper heard 4 sentences in 45 s, and its interpolated
  anchors landed 1.4 s late while the local alignment sat exactly on the 8-bar repeat. Accept a flagged line if it
  sits on singing at a plausible pace and within ~1.6 s of its anchor. The musical grid (identical repeats every N
  bars) is the better check for choruses.
- **Spell numbers out for the aligner** ("sixty-nine"), and merge them back to one written word.

**Where text can go.**
- Text goes only where the panel keeps ≥ ~450 px per side. Measure the zoom's REAL framing from its plan; a
  conservative estimate left text visibly off-centre.
- Lay each phrase out once, at the narrowest width it will have on screen. Re-wrapping while a zoom widens the
  picture read as the phrase repeating.
- A phrase ends where the room ends: at a cut to a full-frame camera.
- Match per-clip metadata by camera AND name. Two phones produced clips with the same name, and one landscape clip
  borrowed the other phone's "vertical" metadata.

**Two ways to choose the text.** The user found that the automatic mode "works best as long as we have the forced
alignment".
- **Automatic:** every reliable line inside the chosen moments, word by word, with the song's title words big. A
  curated version (2-3 phrases, from a visual-production review) lost to it.
- **Annotated:** the agent reads and understands the song, then writes an annotation per line: intent, key words,
  visual concept, keyword-only or full line, panel, group, escalation. A production-designer subagent turns the
  concepts into exact specs (fonts, sizes relative to the panel, colours, entrances in frames, effects quantised to
  the beat), and the renderer implements them.

  On a song about a dangerous beautiful man the hook "bello" became a gold stack with a heartbeat. "Peligroso" was
  red with an RGB split. The plea "¿por qué me haces esto? dímelo" sat on one panel and the "BELLO" answer on the
  other. On a song asking the wind to tie two lovers and time to stop, the chorus had wind streaks, ribbons weaving
  around "amárranos", and a tick ring around "tiempo". On "detente" EVERYTHING FREEZES, and the user noticed it
  ("the words stop vibrating"). Each of the three repeats grows: bigger, the other panel, then both panels.

  For the user, understanding the song is the whole point. Always annotate from its meaning.

**Rules that came out of review.**
- **Audio-reactive letters, without drawing an equalizer.** Use the doodle-video skill's 14-band spectrum (60 Hz-7 kHz,
  log spaced, per-band 10-95th percentile normalisation, ×0.8 decay). Each letter takes one band; the band lifts,
  scales and brightens it.
- **Treatment by song style.** Harsh songs: words slam in (2-frame 115 % overshoot plus a flash) and cut out. Soft
  songs: words rise from blur over 0.3 s and dissolve over 0.7 s.
- **Typography that is too straight reads as stiff.** A "TIEMPO" in a straight sans was "odd". Curved along its
  ring, with letters riding the audio, it worked.
- **Plain verse lines must not look typed.** Centre them in balanced rows, feature the longest content word (bigger,
  another font), and drop each word in on its beat.
- **Text over a full-frame picture is allowed, never on a face.** Use the clip's pose data for head boxes and pick a
  face-free height per phrase and panel. A panel with no free height gets no text. Big bold letters on a musician's
  face "may aggravate" them.
- **Call and response must not collide.** A plea holds through its answer but yields to the next line on its panel.
  A minimum hold never runs into the next line on the same panel.

**Engineering.**
- Text is a cached layer burned into the pieces that touch a moment, so other pieces are never touched. The cache
  key MUST include a hash of the drawing code and the annotation. Without it, a design fix silently served the old
  piece.
- Text frames depend only on their time. Draw each piece in up to 6 parallel chunks (consumer-GPU NVENC session
  limit), each with its own ffmpeg (frame-accurate seek on a re-encode), and join them with `-c copy`. Frames with
  nothing on screen are written blank without drawing.
- Never overwrite a version the user praised while iterating. Archive earlier renders (`old/<name> (<timestamp>)`)
  and give new variants another name.
- Cut review samples with the audio re-encoded, and check that both streams start at 0. A sample cut with `-ss`
  before `-i` and audio `-c copy` kept ~1 s of negative audio pre-roll. Players disagreed, and the user tuned a
  phantom "+1 s" lyric offset that was really this file bug.

**A "frame jump" on a phone shot can be real camera motion.** The frame timestamps were a perfect 30 fps, but the
content jumped over 2 frames, which a 2× zoom magnified. The slow-sway stabiliser deliberately leaves fast jitter.
It can be softened by motion-compensated interpolation of those frames (ffmpeg `minterpolate`, given 4 frames of
context).

Workflow that worked: 1-minute A/B/C... variants of ONE song, then 5-second clips with one transition
each (a harsh song with plain cuts, a soft one with dissolves) so every transition type is judged in
isolation, then the whole show. Keep the preferred phone's priority: the moves are for the fixed cams.

### Performer framing: a detector the user trains in two rounds

For a "fewer shots of X from behind" preference: crop X's head from the SKELETON (head keypoints, else
above the shoulders) -- not the top of the person box, which follows raised arms and scored hands as
"back of head". Score each crop with CLIP zero-shot (face-with-glasses vs back-of-head prompts) AND a
small logistic probe trained on CLIP embeddings of the user's labelled frames; a frame counts only if
both agree. Validate leave-one-moment-out before trusting it (an arms-raised veto looked good and
failed on a real back view with an arm up). Show a numbered contact sheet, one frame per moment; the
user labels "real / almost / wrong person" -- false alarms in close shots were a DIFFERENT person in
the performer's usual spot, so check identity before calling the classifier wrong. Fix confirmed
moments surgically in the scene script: leave the camera on the last beat before the turn, come back
on the first beat after, plain cuts, no leftover under 2 s, replacement = neighbouring shot's camera,
else preferred phone, else another fixed cam. Re-render only the songs that changed.

### Lessons from the second show (no master, GoPros + phones)

- **GCC-PHAT locked onto window edges** (lag = -(len(ref)-len(sig))): PHAT whitening turns a window's
  hard start/stop and sub-80 Hz rumble into the strongest feature. `audio_sync_utils.prep()` (DC
  removal, 80 Hz high-pass, Tukey taper) fixed it; use `gcc_phat_robust()` in new code.
- **Two agreeing windows can agree on the same wrong chorus repeat** when a clip is short and its
  windows overlap. For cameras with a trusted clock (phones with network time), reject an offset more
  than 2 s from that camera's median clock error (10 of 89 clips; a frame check sided with the clock).
- **A fixed camera's clock can be off by most of an hour** (54:39 here): two phones agreed within a
  second across ~50 clips, which pinned it. Chaptered cameras are compared to the anchor per song.
- **CuPy + an NVENC batch at the same time reset the GPU** ("invalid device"), killing the batch mid-clip
  and leaving truncated files. Sync now forces the CPU FFT; audit output durations after any batch.
- **Vertical phone video was squashed** to 1920x1080 for detection (~3x sideways), skewing detections
  and the phones' activity scores. `extract_people.py` now keeps the displayed shape (1080x1920).
- **Appearance matching (CLIP) confused two look-alike band members** (sunglasses, black tees) even with
  every member as a reference. Movement identifies people far better: the seated person is the
  drummer, a guitarist who never leaves the mic isn't the one walking around. Follow a performer's
  skeleton track from a moment that's clearly them; re-picking "whoever stands in their spot" each
  frame grabbed the wrong person as soon as they walked.
- **"Face not visible" is not "turned away"**: tight phone close-ups crop the head out entirely and
  read as a back view to a keypoint test. Require the head region in frame before calling a back view.

This skill is deliberately a thin layer over several existing skills, not a reimplementation of their
logic:
- **`bootlegcam`** -- per-clip technical metadata (`analyze_audio_quality.py`, `detect_blackouts.py`,
  `detect_zoom_glow.py`), the `clip_override` boost/only data shape (the eventual target shape for this
  skill's own automated director output), and `build_fullshow_video.py`'s per-song concat pattern
  (the architecture Stage 3 needs to avoid bootlegcam's own documented one-giant-filter-graph RAM
  blowup at whole-show scale).
- **`stage-movement-analysis`** -- band-member pose/activity extraction (`extract_pose.py`,
  `analyze.py`'s per-second activity math).
- **`crowd-scene-analysis`** -- track/appearance classification for people in frame, and
  `demo_multicam.py`'s GCC-PHAT audio-sync + camera-switch skeleton.
- **`doodle-video`**'s `pick_window()` -- precedent for combining a movement signal and an energy
  signal into one score.

Show-agnostic by design: every script here takes camera/folder paths as CLI arguments, nothing about a
specific show, venue or band roster is hardcoded.

## Stage 1 — camera metadata + unified timeline

### 1. Song windows (ground truth every other camera is checked/assigned against)

Any camera that already produces one file per song with a correct real-world clock (e.g. a GoPro set
to chapter-per-song, or any camera a human already split/labeled) can be turned into the reference
song-windows CSV:

```powershell
py scripts\make_song_windows.py <camera_dir> <out.csv>
```

Output columns: `song,creation_time,duration` -- `song` is the source file's own basename, so the next
step recovers both the song number and a human-readable title from it.

### 2. Masters/ folder (bootlegcam's own `NN-Title[-X].ext` convention)

```powershell
py scripts\build_masters.py --out <Masters dir> --song-windows windows.csv ^
  --labeled <name>=<dir>              # already "NN-Title...ext", any suffix normalized away
  --raw <name>=<dir>                  # no song label; assigned by real-timestamp overlap
  --offset-corrected "<name>=<dir>=<seconds>"   # like --raw, but the camera's clock is off by a
                                                 # known constant (see "Finding a clock offset" below)
```

- A `--labeled` clip's real timestamp is sanity-checked (not trusted blindly) against the song window
  its filename claims -- a mismatch prints a warning, doesn't stop the run.
- A `--raw`/`--offset-corrected` clip whose real `[start,end)` overlaps TWO songs becomes a bridge
  clip, named `NN-MM-Title to Title-X.ext`, same convention `bootlegcam` itself uses for a clip that
  bridges a song change.
- Letters (`-A`, `-B`, ...) are assigned **per song bucket**, not per contributor -- one contributor
  routinely has several clips for the same song (restarted recording, multi-clip coverage), so the
  letter must stay unique within that song's file listing, not just unique per source. (Real bug hit
  and fixed building this: assigning one fixed letter per contributor silently overwrote 9 of 91 clips
  the first time this ran, since two contributors' multi-clip songs collided on the same letter.)
- `--offset-corrected`'s correction is baked into the OUTPUT file's own `creation_time` metadata via a
  lossless remux (`-c copy`), never left as something only this script's in-memory state knows -- every
  later stage reads a self-consistent file.
- Writes `<out>\manifest.json`: every clip's final name, source contributor, and real `[start,end)`
  window -- the clip-level backbone of the unified timeline.

**Finding a camera's clock offset** (needed for `--offset-corrected`): compare the same song's
real-world start time from the reference camera (song-windows CSV) against the suspect camera's own
`creation_time` for that song's file, at 2-3 well-spread songs. A constant offset across all of them
(not drifting) confirms a one-time wrong clock setting, not real drift -- use that constant. First
hit at Microsoft Give 2026 (2026-10-23): one GoPro's clock read 19 days 16:19:18 behind, confirmed
identical across 3 anchor songs before trusting it as a flat correction.

**Clock metadata gets you to the nearest second; it does NOT get you frame-accurate sync --
verify with real audio cross-correlation, not just `creation_time` math.** Hit at Give 2026: after
the whole-day clock fix above, GoPro1/GoPro2 still LOOKED visibly out of sync in an actual render
(a cut to GoPro landed on the wrong beat/moment). GCC-PHAT audio cross-correlation of each GoPro's
own audio against the Nikon master (same technique `crowd-scene-analysis/scripts/demo_multicam.py`
already uses for its camera sync) found a consistent **~0.88s** residual error, present on BOTH
GoPro1 and GoPro2, confirmed via 8 independent measurements (z-scores 14.5-29.9, all comfortably
above a z>8 confidence floor) spread across the full ~90-minute show -- not a drift that grows over
time, a roughly CONSTANT sub-second offset on top of the whole-second clock correction. Likely
cause: GoPro's `creation_time` tag reflects something like when recording was triggered, not the
true timestamp of the first captured frame -- a known category of camera/encoder metadata quirk,
not something specific to this one correction. Fixed by shifting every GoPro1/GoPro2 clip's
`start`/`end` in `timeline.json` by the measured constant (not touching the underlying files,
since `timeline.json` is the one thing every downstream script reads). The `gcc_phat()` function
is ~10 lines (FFT-based phase correlation, see `demo_multicam.py`) -- cheap enough to re-run this
verification on any future show rather than trusting whole-second clock math alone. If a future
measurement shows the error growing with elapsed time instead of staying flat, that's real clock
drift, not this fixed-latency quirk -- fit a linear correction against elapsed time instead of a
flat constant in that case.

### 3. Per-clip technical metadata (reused as-is from `bootlegcam`)

No new code needed -- these already work against any Masters-convention folder:

```powershell
py <bootlegcam>\scripts\windows\analyze_audio_quality.py <Masters> <Metadata>
py <bootlegcam>\scripts\windows\detect_blackouts.py <Masters> <Metadata>
py <bootlegcam>\scripts\windows\batch_zoom_glow.py <Masters> <Metadata>
```

Each writes one `<clip>-<type>.json` sidecar per clip into the shared `Metadata` folder.
`analyze_audio_quality.py` deliberately skips clips whose name starts with `00-` (soundcheck/
pre-show, same convention `bootlegcam` itself uses) -- a `null` for those in the timeline is
expected, not a bug.

### 4. Per-clip audio energy, person detection/activity, and depth (new, generalized)

Three new scripts, all under `scripts\`, all needing the `.venv`/`.venv-depth` environments
already set up for `stage-movement-analysis` (reused, not duplicated -- see that skill's
SKILL.md "Setup" if they don't exist yet):

```powershell
# CPU-only, cheap. Needs stage-movement-analysis's .venv (has scipy):
<band-analysis>\.venv\Scripts\python.exe scripts\audio_energy.py <Masters> <AudioEnergy>

# GPU (YOLO11x-pose, same model/fps as stage-movement-analysis's extract_pose.py, but NO
# role-matching -- every detected person gets a per-clip-local track_id, nothing claims two
# tracks in different clips/cameras are the same real person):
<band-analysis>\.venv\Scripts\python.exe scripts\extract_people.py <Masters> <People>

# GPU (Video Depth Anything vits, reusing stage-movement-analysis's own vendored checkpoint by
# path -- not copied). Needs extract_people.py's output first (samples depth at its detections'
# locations) and the SEPARATE .venv-depth environment:
<band-analysis>\.venv-depth\Scripts\python.exe scripts\extract_depth.py <Masters> <People> <Depth>
```

Why these had to be new instead of reused directly: `stage-movement-analysis`'s
`extract_pose.py`/`extract_depth.py` assume a single fixed Nikon wide shot with exactly 4 band
roles at known stage-position anchors (`demo_labels.ROLES`). A phone/GoPro clip's framing is
arbitrary -- one person close-up, several people, a pan across the room -- so role-position
priors don't apply. `extract_people.py` detects however many people are actually in frame and
tracks them locally (greedy nearest-centroid matching across sampled frames, bridging a short
occlusion gap) purely to measure movement continuously; it makes no identity claim beyond "this
is probably the same person for the next few seconds within this one clip." Expect real
fragmentation (one real person showing up as several short-lived track_ids) on a shaky/zooming
phone clip -- this is a known, accepted limitation of a first pass, not something worth
over-engineering before Stage 2's design is settled; the per-second activity curve (mean and max
speed across whatever's currently tracked) is usable signal even with imperfect continuity.

`extract_depth.py` also writes a coarse per-clip "room depth profile" (a downsampled grid of the
clip's own median depth map) independent of any detected person -- useful on its own for
understanding a camera's framing of a small, boxy room (e.g. "this camera's left third of frame
is all near wall") when deciding how to crop/zoom later, not just how far a given performer is.
Depth is the slow step: unlike the 10fps-sampled pose/activity work, Video Depth Anything needs
every frame at the source's native fps for temporal consistency, so a 60fps clip costs ~6x a
10fps-equivalent one -- budget real time for this across a whole multicam Masters folder.

### 5. The unified real-world timeline

```powershell
py scripts\build_timeline.py --out timeline.json ^
  --masters-manifest <Masters>\manifest.json --song-windows windows.csv ^
  --canciones <concert-video-pipeline show folder>\Canciones.txt --nikon-songs <Songs-4K> ^
  --metadata-dir <Metadata> --audioenergy-dir <AudioEnergy> --people-dir <People> --depth-dir <Depth>
```

Every other script in this skill works in a camera's own local/internal time. This is where
everything moves onto ONE real-world UTC axis -- the question Stage 2's director logic actually
needs answered ("what was happening, for real, at 19:42:10 UTC, across every camera that was
rolling"), not "what's at frame 1234 of this one file."

The Nikon continuous master has no `creation_time` of its own -- `concert-video-pipeline`'s
`Songs-4K` output is split by a human reading the INTERNAL recording clock off `Canciones.txt`
(`"01-Summer Of 69,00:53:13"` = 53:13 into the raw recording). Real-world time for the Nikon is
recovered by an AFFINE FIT: for every song number in both `Canciones.txt` and the reference
`--song-windows` CSV, `real_utc_start - internal_seconds` should be one constant if the Nikon's
internal clock runs at a true, non-drifting rate. Confirmed for Give 2026 across all 22 songs:
offsets spread only 10 seconds show-to-show (well inside the slop of a human reading a
minute:second timestamp off a scrubber) -- the script takes the MEDIAN across every matched
song, not a single anchor, and prints the full spread so a future show with a worse spread is
obvious immediately rather than silently trusted.

The output JSON is an INDEX, not a data dump: large/binary per-clip signals (a zoom/glow trace,
a depth npz) stay as file-path pointers, not inlined. It's meant to be re-run as more Stage-1
scripts finish -- nothing requires every signal to exist yet; a sidecar that isn't there yet is
just `null`.

### Optional: per-camera lens/roll correction (GoPro wide-angle footage)

A GoPro's wide-angle lens shows real barrel/fisheye distortion, and a GoPro that's just hand-set
on a stand (not surveyed/leveled) usually has a few degrees of roll tilt too -- both need fixing
before that camera's footage is usable for cuts/crops later, and `bootlegcam`'s own `build_song.py`
has no lens-correction step of its own (its filter chain assumes already-rectilinear source).

Three scripts: `scripts\lens_calibrator.py` (interactive GUI), `scripts\apply_lens_correction.py`
(CPU batch render), and `scripts\apply_lens_correction_gpu.py` (GPU batch render -- **use this
one for any real batch**, see "GPU vs CPU" below; the CPU version exists mainly as a reference
implementation and a no-GPU fallback).

```powershell
# 1. find one camera's correction INTERACTIVELY (GUI, live preview) against one representative
#    frame -- there's no reliable way to recover the exact GoPro model/FOV-mode setting from
#    file metadata after it's passed through this skill's own remuxing, so this is calibrated by
#    eye against the real room (wall seams, mic stands, speaker edges), not guessed from specs:
py scripts\lens_calibrator.py <one_frame.png>

# resume known-good geometry on the ORIGINAL (not pre-corrected) frame -- sliders start at these
# values instead of the usual first-guess defaults, so re-opening the tool to dial in shadows/
# brightness/blacks doesn't mean re-dragging k1/k2/rotation/crop back to where they already were:
py scripts\lens_calibrator.py <one_frame.png> k1 k2 angle_deg crop_top crop_right crop_bottom crop_left

# 2. apply the locked-in values to every clip for THAT camera (one calibration per camera, not
#    per clip -- a GoPro mounted once for the whole show has one fixed lens/roll correction):
<band-analysis>\.venv\Scripts\python.exe scripts\apply_lens_correction_gpu.py <Masters> "<glob>" <out_dir> ^
  k1 k2 angle_deg crop_top crop_right crop_bottom crop_left [--parallel 3]
# add `--demo <start_sec> <duration_sec>` to render a short snippet of one clip first, before
# committing to a full-length batch run across every clip for that camera. `--shadow`/
# `--brightness`/`--blacks` pass through tone adjustments (see below); omit all three to leave
# tone untouched and apply geometry correction only.
```

`lens_calibrator.py`'s sliders: `k1`/`k2` (barrel correction, negative = un-fisheye), `rotation`
(degrees, **positive = clockwise** -- easy to get backwards when thinking in words like "rotate
right", since that's ambiguous between "turn the content clockwise" and "make the right edge
go up", which are opposite operations), four independent crop sliders (top/right/bottom/left)
with a "lock aspect ratio" toggle that applies one further CENTERED trim on whichever axis ends
up oversized so the output always lands back on the source's own aspect ratio without silently
overriding whichever sides were actually dialed in, and three independent TONE sliders --
`shadows` (0-1, local lift that fades to ~0 in bright areas, so stage lighting highlights don't
blow out), `brightness` (-1 to 1, flat global shift, affects highlights too -- unlike shadows
this CAN blow out or crush), `blacks` (-1 to 1, Lightroom-style: negative crushes toward more
contrast, positive lifts toward flatter/hazier). All three operate on the LAB L channel only
(hue/saturation of actual stage lighting untouched) and have NO ffmpeg-filter equivalent -- only
`apply_lens_correction.py`/`apply_lens_correction_gpu.py`'s own `--shadow`/`--brightness`/
`--blacks` flags reproduce them for a real render; the "Save" button's printed command line is
the authoritative source for the exact flags to use, including these.

Opening one of this tool's own earlier saves (filename contains `_k1...`) is detected
automatically and resets k1/k2/rotation/crop to neutral (0) instead of the usual starting
guesses, since the geometry is already baked into those pixels -- re-applying it would double-
distort. This auto-detection only fires when no explicit CLI values are passed; the resume-mode
CLI args above always win.

### GPU vs CPU -- use GPU, the CPU path cost real days on this project

`apply_lens_correction_gpu.py` runs the same k1/k2/rotation/crop math as the CPU version (same
`build_remap`/`crop_box`/`apply_tone` functions, imported directly -- not re-derived, so results
match what was approved in the GUI either way) but as `torch` CUDA `grid_sample` calls instead of
`cv2.remap`/`warpAffine`, encoding via NVENC (`h264_nvenc`) instead of libx264. On an RTX 4090
this was roughly **7x faster** than CPU on real full-length 4K60 GoPro footage (a ~46min/clip CPU
render became ~6min/clip) -- CPU software x264 encoding of 4K was the actual bottleneck, not the
per-frame remap math itself.

`--parallel N` runs N clips' pipelines concurrently (threads -- cv2 decode and torch CUDA calls
both release the GIL during the actual work, so this is real overlap, not just interleaving). A
single clip's pipeline barely dents the GPU on its own (`nvidia-smi dmon -s u` showed low single-
digit % on the `sm`/`enc` columns running one clip alone); `--parallel 3` pushed `sm` up to
60-100% and meaningfully cut total wall-clock time for a multi-clip batch. Start at `--parallel 3`
and watch `nvidia-smi dmon` while it runs -- back off if NVENC starts erroring (consumer GPUs
have historically capped concurrent encode sessions, though this wasn't hit at 3 on a 4090).

**NVDEC decode was tried and reverted -- don't re-attempt without re-proving it on a timed
before/after.** Decoding via `ffmpeg -hwaccel cuda` is fast in isolation (~36fps raw for this
footage), but wiring it into the script via a background reader thread + queue made the full
pipeline SLOWER end-to-end than plain `cv2.VideoCapture` decode (3m15s+ vs 56s for the same 30s
demo clip, abandoned before it even finished). Best guess: each raw 4K frame is ~25MB, and
shuttling that through Python twice per frame (decode pipe -> queue -> encode pipe) costs more in
Python/GIL/memory-copy overhead than NVDEC saves over cv2's already-reasonable decode speed. A
real fix would mean keeping frames in GPU memory end-to-end (NVIDIA's VPF/PyNvVideoCodec, not
plain ffmpeg subprocess pipes) -- a bigger lift, not done here. Batch size for the GPU transform
(`--batch`, default 16) also isn't worth tuning further: the transform itself is sub-millisecond
on a 4090 regardless of batch size, so it was never the bottleneck -- a quick batch=64 test came
out slower than batch=16, almost certainly from unrelated pipeline noise, not batch size itself.

Found building this for Give 2026's two GoPros: despite being similar camera hardware, GoPro1
and GoPro2 needed genuinely different k1/rotation/crop values -- mounting angle and exact framing
differ per camera, so a value found on one camera is a reasonable STARTING point for calibrating
the next one, never a value to copy over unchanged. GoPro's in-camera FOV modes (Linear/Wide/
SuperView) have very different distortion strength (roughly 90°/120-130°/170° by third-party
measurement, not an official spec); `ffmpeg`'s own `lenscorrection` filter is a simple two-
parameter radial model that matches a moderate ("Wide"/"Linear"-like) distortion reasonably but
visibly overcorrects into the opposite (pincushion) bend if pushed toward "SuperView"-strength
`k1`, so don't assume a stronger `k1` is always a better correction -- compare against real room
geometry (see below), not just "more correction = more correct."

`apply_lens_correction.py` deliberately reimplements the SAME remap/rotate/crop math as
`lens_calibrator.py` (imported directly, not re-derived) rather than handing the chosen k1/k2 to
ffmpeg's own `lenscorrection` filter for the production render -- that filter uses a different
distortion model, not guaranteed to reproduce the exact result a human approved in the GUI.

**Measuring the actual roll angle, not guessing it:** an automated Hough-line vertical-deviation
measurement (`cv2.HoughLinesP` + filter to near-vertical segments + weighted-mean/median angle)
is far more reliable than eyeballing a handful of rotated preview images, but only once the input
is restricted to real architectural lines:
- Ceiling LED light strips are the brightest, highest-contrast edges in a typical conference-room
  shot and get picked up as fake "lines" by Canny despite not being structural -- either crop them
  out of the detection region entirely, or suppress very bright pixels first (a `curves` tone
  limiter before edge detection) so they can't produce an edge at all.
- Floor carpet texture produces hundreds of short, high-contrast, near-random-angle edges that
  overwhelm real signal if the floor is left in the detection region -- crop it out too; restrict
  to a wall-only vertical band (excludes both the ceiling lights above and the floor below) for a
  clean result.
- Cross-check across multiple independent samples (a tight band-only ROI, the full-frame wall
  band, a different crop) rather than trusting one run -- genuine agreement across differently-
  cropped detections (this skill's build: three independent measurements landing within 3.0-4.1°
  of each other) is real confidence; a single run's number alone isn't.

### Required for every phone camera: handheld-shake detection + stabilization

`bootlegcam`'s own `build_song.py` already reads an optional `shakiness-report.csv`
(`steadiness_score()`/`load_shakiness_map()`) if one happens to exist beside a Masters folder, but
nothing in that skill ever generates one -- a real gap, filled here for phone clips (GoPros get fixed
mounts and lens correction instead; phones get handheld).

```powershell
<band-analysis>\.venv\Scripts\python.exe scripts\detect_shakiness.py <Masters> <out_shakiness.csv> ^
  [--sample-fps 5]
<band-analysis>\.venv\Scripts\python.exe scripts\apply_stabilization.py <Masters> <shakiness.csv> ^
  <out_dir> [--threshold 12.0]
```

`detect_shakiness.py`'s metric (`jitter_std_px`, the exact column name `build_song.py` expects):
sample frames at a low fixed rate, estimate frame-to-frame global motion via phase correlation
(`cv2.phaseCorrelate`), then take the STANDARD DEVIATION of that displacement across the clip -- not
its mean. A smooth intentional pan has large but CONSISTENT displacement (low std); handheld jitter
has small but ERRATIC displacement (high std) -- std, not raw motion magnitude, is what actually
separates "camera is moving on purpose" from "camera is shaking."

`apply_stabilization.py` is threshold-gated on purpose: only clips above `--threshold` get the
two-pass `vidstabdetect`+`vidstabtransform` re-encode; everything else is hardlinked through
unchanged. Stabilization has a real visual cost (a slight crop, occasional warping on fast content)
that isn't worth paying on footage that's already steady. The motion-estimation/warp itself
(`libvidstab`) is CPU-only -- no CUDA path available for that filter -- but the final encode uses
NVENC (`h264_nvenc`), same reasoning as the lens-correction GPU script below: software x264
encoding of 4K was the real cost, not the per-frame filter math.

Two layered ffmpeg filtergraph quoting gotchas hit building this, both from the same root cause (a
Windows path handed to an ffmpeg filter OPTION, where `:` is a key=value separator):
- A drive letter's literal `:` must be escaped as `\:`, and backslashes read more reliably as forward
  slashes in a filter string -- the same fix used everywhere else in this project.
- That alone isn't enough once the path also contains spaces (ordinary for a clip filename) -- the
  whole value then needs wrapping in single quotes, e.g. `result='D\:/path/with spaces.trf'`, or
  `vidstabdetect`/`vidstabtransform` fail to parse the filtergraph at all despite the colon being
  escaped correctly.
- And a literal apostrophe INSIDE a filename (e.g. `...Lovin' You...`) then terminates that wrapping
  single-quoted value early, breaking the parse the same way again, one layer up. ffmpeg's own
  escaping rule for a quote inside a quote is to close the quote, emit an escaped quote, then reopen
  it (`'` -> `'\''`) -- `quote_val()` in `apply_stabilization.py` handles all three layers together;
  use it rather than hand-building the filter string again.

### Known gotchas (hit building this for real footage)

- A long CPU-bound batch job left running unattended can get silently killed by low system memory
  pressure, well before it's actually done -- hit on a CPU `apply_lens_correction.py` run for
  GoPro2 that died mid-clip-15 with no error, leaving 20/23 clips actually written to disk (the
  rest just silently missing, not corrupt). Always check the OUTPUT FOLDER'S clip count against
  the source folder's, don't trust "the job looked like it was progressing" or an empty/stale log
  (see next point) as proof it finished -- diff the two directory listings before assuming a batch
  is complete.
- A batch script's own print()-based progress log can sit completely empty, or frozen at an early
  line, for the ENTIRE run, even though real work is happening -- Python stdout is block-buffered
  (not line-buffered) once it's redirected to a file instead of a terminal, so progress lines only
  actually land on disk in bursts, or all at once at exit. Don't diagnose a job as hung from an
  empty/stale log file alone; check the output folder's file sizes/timestamps and `ps`/`nvidia-smi`
  for real liveness instead.
- `apply_lens_correction_gpu.py`'s `--parallel N` run can leave the python process alive for a
  while (observed: still running several minutes later) even after every expected output file is
  already written and matches the source clip count -- don't assume "process still running" means
  "still working." Once the output folder's count matches the source count, it's safe to kill the
  process rather than wait on it.
- If the machine gets sluggish/unresponsive from several ffmpeg processes' RAM usage (seen running
  two camera's batches concurrently, ~3GB resident each), Windows' `EmptyWorkingSet` (psapi.dll,
  callable from PowerShell via `Add-Type`+P/Invoke) forcibly trims a process's resident RAM back to
  near-zero without killing it -- but trimming SEVERAL processes at once triggers a page-fault storm
  as they all reload pages simultaneously, which can itself freeze the machine for a bit. Trim only
  one or two at a time, or better, just kill any process whose work is already actually done (see
  above) instead of trimming a process still running.
- Apostrophe/quote characters in filenames render as mojibake in a terminal/Bash tool's captured
  output (`Lovin'` showing as `Lovin�`) -- this is a display artifact of that output path, not real
  file corruption. Verify the actual on-disk bytes with `ascii(filename)` in Python before concluding
  anything is actually broken.
- A genuinely different apostrophe character between two contributors' own files (e.g. a curly quote
  `'` vs an acute accent `´`) is a real source-data discrepancy, not a bug in this script -- harmless
  for the pipeline since only the `NN` prefix is load-bearing, not exact title spelling.
- Filenames with an apostrophe break Bash/MSYS glob expansion when shelling out to ffmpeg/ffprobe
  (same gotcha documented in `concert-video-pipeline` and `flac-album-art`) -- this skill's own
  scripts avoid it by using Python's `os.listdir`/`subprocess` directly rather than shell globbing.
- `ffprobe -of csv=p=0` with multiple `-show_entries` fields prints a TRAILING comma (e.g.
  `"3840,2160,60000/1001,"`), which silently produces one extra empty field on `.split(",")` --
  `extract_depth.py` hit this (`ValueError: too many values to unpack`); fixed with
  `.rstrip(",")` before splitting. Check for this on any new ffprobe csv parsing, not just here.
- A per-second-bucket slice built from a float fps (`int(s * fps)`) fails with `TypeError: slice
  indices must be integers` if the cast is forgotten on just one of the two bounds -- caught in
  `extract_people.py`'s `build_activity()`. Cast both bounds to `int(...)` explicitly rather than
  relying on one of them already being an int from elsewhere.
- `Canciones.txt` lines have NO leading line-number column in the real file -- that's an
  artifact the Read tool prints for display. A parser written against what Read shows (expecting
  `"<n>\t<label>,<time>"`) will match nothing against the real file's `"<label>,<time>"` lines.

## Stages 2-3 — the director, as first built for the master-camera show

The director lives in `scripts\build_highlights_edl.py`. This section describes how it was first
built for the show with a continuous master; with a `show.json` it also takes the anchor, the user's
preferred video sources, crowd moments and framing preferences from config (see the top sections).
It keeps every song in FULL (dead air trimmed only -- NOT a compressed ~30min highlights reel; that
was the original design, dropped per direct request: "we are no longer targeting a full video of 30
minutes, but the full show, without long pauses") and cuts between cameras throughout, as a
three-layer system, in order:

1. **Per-song window: real start to real end, not a compressed "highlight."** Opens at the song's
   actual first note (`find_song_onset()` -- fine-grained RMS-envelope analysis on the real audio,
   ~23ms resolution via librosa, NOT the coarse 1-second AudioEnergy trace, which is too coarse to
   tell an individual quiet pickup note from room silence around it -- defined as the first point
   where loudness jumps `ONSET_RISE_DB` dB above the local noise floor). Closes wherever SUSTAINED
   quiet begins afterward (`find_song_end()` -- same RMS technique, scanning for a sustained drop
   instead of a rise): applause fading, between-song banter, tuning. Verified on song 01 two
   independent ways (manual RMS-envelope reading AND librosa's own `onset_detect`, a different
   algorithm) before trusting it: both found the real first guitar pluck at ~1.49s, while the OLD
   loudest-contiguous-stretch approach had opened the clip at 18s, deep into the first verse,
   reading as "the video starts late, music already going." `LEAD_IN_SEC` is small (0.1s) on
   purpose -- enough to not clip the attack transient, not enough to skip past an instrumental
   intro to the vocals (an earlier `LEAD_IN_SEC=5.0` did exactly that, confirmed by ear: "you
   started it right when the lyrics start, but the song starts with just guitar first").
2. **Tempo-aware, beat-snapped cut timing** -- `librosa.beat.beat_track` runs on the song's own
   audio (extracted via a scratch ffmpeg WAV), giving a real per-song BPM and beat grid. Nominal
   shot length scales inversely from a 120bpm reference (`SEG_LEN_REF=11.0s`, clamped to
   `[MIN_SEG=6s, MAX_SEG=18s]`) so a fast song gets shorter shots and a slow one gets longer ones, and
   every cut point snaps to the NEAREST real beat (`nearest_beat()`) instead of an arbitrary
   fixed-clock boundary -- a cut landing exactly on a beat reads as intentional, same as real concert-
   video editing practice; one that doesn't reads as sloppy.
3. **Per-segment camera scoring** -- for each cut segment, every camera with a usable clip is scored
   (preferring a lens-corrected/stabilized version via `resolve_clip_path()`, falling back to the raw
   `Masters/` file if a corrected one isn't ready yet) on: mean person/pose activity
   (`extract_people.py`'s `activity.json`), a zoom/glow quality multiplier, blackout disqualification,
   a SMALL flat bonus for the camera carrying the chosen audio track (`NIKON_BONUS=0.05`), a small
   stickiness bonus for the currently-held camera (`STICKY_BONUS=0.04`, avoids flicker between
   near-tied cameras), a one-time-per-song novelty bonus for any camera not yet shown this song
   (`NOVELTY_BONUS=0.12` -- without this, a slightly lower-scoring but still decent angle never gets
   shown at all once Nikon is narrowly winning; real multicam editing samples every available angle
   at least once), a forced-variety cap (`MAX_DWELL_CHUNKS=2` -- never holds one camera long enough
   to get boring to watch), and an ANTICIPATION bonus (`ANTICIPATE_BONUS=0.20`): each candidate also
   samples its OWN activity a few seconds past the segment being scored (`ANTICIPATE_SEC=5.0`) and
   earns the bonus now if that future activity is meaningfully higher (`ANTICIPATE_RISE_RATIO=1.4`)
   than its current activity -- biasing the director to already be on a camera just before, say,
   someone walks over and grabs a mic, rather than reacting to it after the fact. This is a genuine
   first implementation of a real multicam-editing idea ("show anticipation, not just reaction,"
   confirmed via web research on live-music-video editing conventions), not something any of the
   reused skills already did.

   **The bonus values above were originally sized WITHOUT checking them against real measured
   activity deltas between cameras -- a mistake, caught and fixed.** Those deltas run ~0.02-0.08 on
   this footage, so the ORIGINAL `NIKON_BONUS=0.15`+`STICKY_BONUS=0.10` (plus up to
   `ANTICIPATE_BONUS=0.20`) easily outweighed the actual content signal, making Nikon win almost
   every segment regardless of which camera actually had the more active/interesting shot --
   confirmed on song 01: GoPro1 measured HIGHER raw activity than Nikon on one real segment (0.121
   vs 0.098) and still lost under the old weights. Rebalancing to the small values above (plus
   adding `NOVELTY_BONUS`, which didn't exist before) shifted Nikon from 20.9min to 11.5min of
   screen time across the show, with GoPro1 0.1min->5.3min and GoPro2 0.2min->4.2min -- i.e. the
   director actually started cutting to other cameras instead of defaulting to Nikon by construction.
   Lesson: ANY hand-picked scoring weight needs to be checked against the real scale of the signal
   it's supposed to compete with, not just picked to "feel reasonable."

Audio is locked to ONE camera's track (the Nikon master) for the whole output and never switched --
picture cuts between cameras freely, but the soundtrack stays one continuous recording throughout,
the way a real multicam mix works; switching audio sources mid-song is a tell that gives away a sloppy
edit.

Generalized after the second show: without a `show.json` the script keeps this first show's roster
and `CORRECTED_DIRS`; with one, the roster comes from config (proven by reproducing the second show's
scene script shot-for-shot). Still outstanding from the original plan: real crowd-member-becomes-
performer attribution (still an unsolved re-ID problem, not attempted here -- this director scores
existing per-camera activity/audio signal, it doesn't try to track a specific person across cameras or
across an occlusion gap) and the privacy/face-anonymization idea for non-performer attendees.

## A critical EDL bug: partial clip coverage was silently clamped, not excluded

**The single most damaging bug found building this show.** `build_highlights_edl.py`'s candidate loop
used to accept a camera clip for a segment window `[abs_lo, abs_hi)` as long as the clip's real
`[start,end)` OVERLAPPED that window at all, then CLAMPED `loc_lo`/`loc_hi` to whatever the clip
actually covered (`max(abs_lo, c_start)` / `min(abs_hi, c_end)`) -- while leaving the segment's own
`abs_start`/`abs_end` metadata at the ORIGINAL, unclamped nominal window. That silently created
segments where the real-world time span claimed didn't match the clip-local span actually rendered
(e.g. a clip starting 9s into a nominally-9s window got clamped to 0-9s of ITS OWN local time while
the segment still claimed to span the full original 9s of real time) -- and since `render_song.py`'s
audio timeline is built purely from cumulative rendered-segment durations, not re-synced per segment,
this silently shifted video ahead of (or behind) the continuous Nikon audio track from that point
onward, for the REST of the song. This is what caused a multi-hour "Iris's sync keeps being wrong
no matter how many times I fix the offset" debugging saga -- every individual clip offset fix was
correct, the bug was structural, in the EDL builder itself, not in any camera's calibration.

**Fix:** require FULL window coverage to even be a candidate (`c_start <= abs_lo and c_end >= abs_hi`)
-- no more partial-overlap clamping; a clip that doesn't fully cover a segment's window is simply not
considered for it, and the segment falls to the next-best fully-covering candidate instead.

**The real fix, long-term, is the automated QA assertion that now runs after every segment is picked:**
```python
real_span = (abs_hi - abs_lo).total_seconds()
clip_span = loc_hi - loc_lo
assert abs(real_span - clip_span) < 0.05, f"EDL span mismatch ..."
```
This is the direct answer to "when are you going to have a skill to do quality assurance" -- rather
than trusting a human to re-notice this bug class by eye in a future show, any future regression of
it now fails LOUDLY and immediately at EDL-build time, not after a multi-hour frame-by-frame hunt.
**Any new candidate-selection logic added to this script needs an invariant like this one** -- the
general lesson is: whenever a script CLAIMS a segment covers real-world time X, assert that what it
actually renders for that segment really is X seconds long, don't just trust the claim.

## Per-song rendering (`render_song.py`): 4K canvas, per-source treatments, provenance

`render_song.py` (the first show's renderer; for new shows prefer `render_cached.py`, which applies
the same treatments per camera from `show.json`, converts phone HDR to SDR inside each fragment, and
caches every shot) turns one song's EDL entry into a finished video:

- **True 4K throughout** (3840x2160) -- every camera at Give 2026 shoots native 4K, so nothing needs
  upscaling. Three per-source treatments, chosen by `treatment_for(seg)`:
  - **Landscape (Nikon)**: scale-to-fill + center crop.
  - **Portrait (Iris/Nova phones)**: blurred scaled-to-fill background + sharp foreground at
    full output height -- fills the frame without pillarboxing or reading as picture-in-picture.
  - **GoPro**: downscale to 2560x1440 ("2K") centered, same blurred-fill border technique as portrait
    -- GoPro's wide-angle glass is already soft after lens correction eats into the frame, so scaling
    it UP to fill 3840x2160 would visibly magnify that softness further. Centering it at a smaller,
    native-ish size instead avoids upscaling already-mediocre glass.
- **Audio is always the Nikon master**, trimmed to the highlight window, muxed in once at the end --
  never switched per-segment, same reasoning as the three-layer director system above.
- **Standing provenance JSON**: `write_provenance()` runs automatically after every render, writing
  `<output>.mp4.json` next to the video -- every transition's render-time position, source camera,
  exact clip file + trim range, real-world UTC window, and the director's own score for that pick.
  This is now a permanent feature of this script, not a one-off for this show -- it's what made
  answering "what is the provenance of that video at minute 2" a 10-second JSON lookup instead of
  another round of frame-scrubbing.
- **Final-output codec: `av1_nvenc`** (preset p7, cq 23, uncapped vbr, 8-bit `yuv420p`) -- matches
  `concert-video-pipeline`'s own established final-master standard (see its `04-Publish-Video.ps1`).
  Originally this script used `h264_nvenc` (cq 18, preset p5), inherited from the lens-correction/
  stabilization scripts rather than matched to the project's actual final-render convention --
  measured real difference re-encoding one already-rendered song: **42.9% smaller** at AV1 cq23 vs the
  original H.264 cq18 output, same visual quality tier. AV1 NVENC hardware encode needs an Ada-
  generation GPU (RTX 40-series+) -- same requirement `concert-video-pipeline` already documents.
  Deliberately kept 8-bit (not matching `concert-video-pipeline`'s 10-bit `p010le`) -- a 10-bit
  render earlier in this same project came out unplayable until forced back to 8-bit; 8-bit is also
  genuinely sufficient here since the real delivery target (YouTube) converts everything to 8-bit
  BT.709 limited-range on ingest regardless of what's uploaded (confirmed against YouTube's own
  published encoding guidance) -- true 10-bit only matters for genuine HDR mastering (a different,
  much bigger undertaking: BT.2020 primaries + PQ/HLG transfer function + mastering-display metadata,
  none of which this footage was shot or graded for), not just "upload more bits."
  **Switching codecs mid-show is dangerous**: all 22 songs must share identical encode parameters for
  the final `-c copy` concat to work -- changing the NVENC constant only takes effect for render
  invocations AFTER the edit; already-running render processes keep whatever was loaded in memory
  when they started. Never flip the codec while some songs are rendered and others aren't yet, without
  an explicit plan to re-render the already-done ones for consistency.

## Precise per-clip sync: two tools, and why ANY bounded audio search can still alias

Camera-clock metadata (even after a whole-second/whole-day fix) gets you to roughly the right
second, not frame-accurate sync -- see the GoPro ~0.88s residual-latency finding above. Two scripts
refine individual clips' real-world offsets further:

- **`precise_audio_sync.py`** -- GCC-PHAT search in a small margin (`SEARCH_MARGIN_SEC`, default 20s)
  around a clip's EXISTING metadata offset. Works well when that metadata is already roughly right
  (Iris, GoPro1/2 here) -- useless when the existing metadata is wrong by MORE than the margin,
  since a narrow search around a bad anchor just can't reach the true answer.
- **`sync_by_song_window.py`** -- for a camera whose metadata can be wrong by **far** more than any
  reasonable margin (an Android/OneDrive phone here, see below), anchor the search to the clip's
  SONG WINDOW instead of its own bad metadata: every clip's filename already encodes which song it
  belongs to, and that song's real-world window is independently validated (Nikon's own per-song
  split, cross-checked across all 22 songs). Search the clip's audio against that FULL SONG's Nikon
  audio -- still a bounded search (one song's length, minutes), nowhere near the original
  whole-show aliasing bug, but reaches far enough to recover a clip whose clock is off by minutes.

**Even this bounded, song-scoped search can still alias onto the WRONG repeat of a chorus/verse
within the SAME song.** Confirmed twice on real clips: a repeated 4-line chorus block appearing both
near a song's start and again in its outro got matched to the wrong occurrence despite a z-score of
29+ (high confidence does NOT mean correct when the content genuinely repeats) -- caught only by
pulling the actual matched frame on both sides and finding the lyric/scene content didn't agree with
what the user described having seen. **Don't trust a single automated measurement's z-score alone for
an irregular-content camera (open-mic guests, repeated hooks) -- cross-validate with a second,
INDEPENDENT signal before applying a correction**, in order of rigor:
1. A visual/lyric anchor from a human who actually watched the footage (fastest, catches gross errors).
2. The karaoke lyric screen's white->yellow transition, frame-matched between the two cameras (see
   below) -- the project's actual ground truth, accurate to source frame rate (~16.7ms at 59.94fps).
3. A SECOND, independently-scoped audio correlation (e.g. a tight window centered on the candidate
   point rather than the original full-song-window search) -- if two differently-scoped audio
   searches and a visual check all land within a few hundred ms of each other, that triangulation
   (not any single method alone) is what justifies calling an offset "verified," not just "measured."

**The karaoke lyric screen as ground truth**: find a frame where a specific lyric is in WHITE (not yet
sung) transitioning to YELLOW (currently singing), visible in both cameras -- the precise transition
timestamp in each gives a frame-verified offset, immune to audio-repetition aliasing since it's a
visual event, not a content-correlation guess. When a target camera's own framing doesn't show the
screen at the moment needed, scan several nearby seconds on the REFERENCE camera (which usually has
an unobstructed view) to map out the song's lyric structure, then infer where the target camera's
own content (a described gesture, a visible partial lyric through a gap between people, a distinctive
musical section like a guitar solo) should fall relative to that map -- a non-lyric visual/structural
anchor (someone visibly holding up a phone in the wide shot, a guitar solo instrumental section with
no lyrics active) is a perfectly valid cross-check too, just slightly less frame-precise than an exact
lyric transition.

**Per-clip, not per-camera-flat, is the right model for an unreliable camera.** Iris's and
GoPro1/2's corrections cluster TIGHTLY (Iris: -48.7 to -50.2s across 33 clips, essentially one flat
constant -- consistent with a single wrong clock SETTING, not drift) -- but an Android phone whose
app/OneDrive-upload pipeline rewrites `creation_time` per-file can have WILDLY inconsistent errors
even across clips of the same device (56-90s range just among 3 samples checked) -- this is NOT a
flat offset problem, don't try to find "the" constant for a camera like this; calibrate per clip.

**Root cause, if the camera is Android + OneDrive**: checked every timestamp location a video file
can carry (container-level `creation_time`, both stream-level `creation_time` tags, and the raw
`mvhd`/`tkhd`/`mdhd` boxes) -- all agreed exactly, which is itself the tell: a camera naturally
writing these wouldn't have every box agree with mechanical precision; something rewrote the whole
file's timestamp set at once, after the fact. OneDrive's handling of Android-camera-uploaded video
dates is a widely-reported issue (shows upload date instead of capture date for video specifically).
There's no better field to fall back to in the file itself once this has happened -- the fix has to
stay content-based (audio correlation + frame verification), there's nothing left to trust in the
metadata.

## Diagnosing a long GPU batch job: don't trust "no new output" alone

A `apply_lens_correction_gpu.py` batch run (fixing several truncated clips, see below) appeared to
produce ZERO output for over two hours and looked indistinguishable from hung -- it wasn't. Two
things resolved the ambiguity, neither of which is "wait and see":
- **CPU-time sampling, not wall-clock elapsed time.** `PowerShell Get-Process | Select Id,CPU,StartTime`
  -- if the worker's accumulated CPU time is CLIMBING between two checks a few minutes apart, it's
  genuinely computing regardless of what its log file shows; flat/unchanged CPU time across two checks
  is the real "it's actually stuck" signal.
- **The script's own stdout can be buffered for the ENTIRE multi-hour run**, then flush everything at
  once right as the job finishes or reaches a natural boundary -- confirmed directly: a task's output
  file that looked completely empty for 2+ hours suddenly contained the full, complete per-frame
  progress log for 3 whole clips the moment the first phase finished. This is the SAME buffering
  gotcha already documented above for `print()`-based progress logs, just confirmed again at a much
  larger scale (hours, not seconds) -- don't conclude "no output = no progress" from a task's captured
  stdout file; cross-check actual output file timestamps/sizes and CPU time instead.
- **Resource contention compounds this badly.** The same job that took 2+ hours to process its first
  (longest, 289s) clip sped up dramatically once two OTHER unrelated GPU-heavy processes (concurrent
  song renders) finished and stopped competing for the same CPU/NVENC resources -- always account for
  what ELSE is running on the GPU/CPU before concluding a job's slowness is abnormal.

**A batch lens-correction run can silently leave SOME clips truncated or missing, not just entirely
missing or entirely fine.** An interrupted `--parallel N` run (killed mid-batch, for whatever reason)
left the clips that were mid-flight at the moment of interruption as PARTIAL files -- `ffprobe`-valid,
playable, just much shorter than the source (one clip ended up at 5% of its real length) -- which
looks like "it finished successfully" to any check that only verifies the output file EXISTS, not
that its duration actually matches the source. **Always audit corrected-folder durations against
their raw-folder source durations** (`raw * 0.9` as a rough "is this suspiciously short" threshold)
before trusting a corrected folder is complete, especially after any run that could have been
interrupted (manual stop, crash, machine sleep) -- this exact gotcha caused 7 clips across two
cameras to sit silently broken for a full day before a render failure (`moov atom not found`, i.e.
genuinely corrupt/unfinalized, for the worst-affected one) finally surfaced it.

## Cancelling a background `xargs -P N` batch: TaskStop alone may not be enough

Stopping the harness's own tracked background task for an `xargs -P N | bash -c '...'` pipeline does
not reliably kill the underlying OS process tree on this platform -- confirmed directly: after
"stopping" such a task, the exact same worker PIDs were still alive and running, and killing just
those visible leaf `py`/`ffmpeg` workers didn't help either -- `xargs` itself (a separate, not
necessarily visible-at-a-glance process) immediately dispatched fresh replacement workers from its
remaining queue. **The actual `xargs` process has to be found and killed directly** (`ps aux | grep
xargs`, not just the leaf workers) to stop a batch like this for real; always re-check `ps aux` after
any stop/kill attempt on a multi-process batch rather than assuming the first kill worked.

## A second, more damaging sync bug: per-segment frame-rounding drift, not a camera problem at all

After the EDL full-coverage fix above, a user report of "the video looks a little off, not sure
where" (on the single continuous Nikon track, not any one camera) led to a MUCH more important find
than the GoPro-clip-offset chase that first looked promising: `render_song.py` built each song's
AUDIO track from the EDL's NOMINAL duration (`highlight_local_end - highlight_local_start`), but
every per-segment VIDEO cut uses frame-accurate seeking (`-ss` after `-i`), which still means
"nearest frame," not exact-to-the-millisecond. A segment length that isn't an exact multiple of one
frame (33.4ms at 29.97fps) rounds slightly on extraction, and those per-segment roundings do NOT
reliably cancel out over a whole song -- measured directly on song 03 (25 segments): a net **-43ms**
by the end (video running behind audio), confirmed again on song 01 at -167ms. Above the ~45ms
threshold where lip-sync drift starts reading as "off," and it hit EVERY song and EVERY camera,
since the bug is in the cut-and-concat step itself, not in any one camera's clock/offset -- this is
exactly why it was first mistaken for a GoPro sync problem (see below) before the real mechanism was
found.

**Fix:** after concatenating all of a song's video segments (`video_noaudio.mp4`), `probe_duration()`
measures that file's REAL, actual length via `ffprobe`, and the Nikon audio extraction is trimmed to
THAT measured number, not the EDL's nominal one. This doesn't eliminate the underlying per-segment
rounding, just stops it from compounding into a growing whole-song drift -- the right fix given the
rounding itself is an unavoidable consequence of frame-granular cuts, not a bug to "solve away."
**Lesson for any future duration-sensitive pipeline step here**: never trust a sum of nominal
per-piece durations to equal a concatenated file's real duration once each piece went through its own
independent extraction -- measure the real thing once assembled, don't just add up the plan.

## GoPro "sync bug" scare: mostly a repeat of the same aliasing risk, caught by rigor, not luck

`precise_audio_sync.py` flagged 8 of 23 GoPro2 clips and 7 of 23 GoPro1 clips as `SUSPICIOUS`
(10-40s "corrections"). Two of them (song 03 GoPro2, song 04 GoPro1) were individually re-verified
with a UNIQUE, non-repeating anchor (a song's one-time outro/title line, each song's outro visually
confirmed matching on BOTH cameras at the EXISTING metadata offset, not the tool's "measured" value)
and **both turned out to already be correct** -- the tool's own wide-window search had aliased onto a
repeated chorus phrase elsewhere in the song, the exact failure class this project has now hit three
separate times (`sync_by_song_window.py`'s own docstring, Nova's clips, now GoPro's). **A
physically-impossible "measured" value is a fast, free sanity check before even reaching for a frame
grab**: one of the two disproven clips' alternate offset would have placed its content past the end
of the Nikon reference file entirely -- if a correction implies a timestamp the source media
physically cannot contain, that alone rules it out. The remaining ~11 flagged clips were NOT
individually re-verified (the real bug turned out to be the duration-drift issue above, not camera
offsets) -- treat them as genuinely unverified, not as "probably fine" or "probably broken," if this
ever needs revisiting.

## Shared helper modules now exist -- use them, don't re-duplicate

`clip_utils.py` (`find_clips()`/`CLIP_GLOBS`) and `audio_sync_utils.py` (`SR`/`parse()`/`fmt()`/
`aud()`/`gcc_phat()`) were pulled out of 5 and 2 scripts respectively after a code review found both
byte-for-byte duplicated everywhere they were needed -- a real risk specifically for `gcc_phat()`,
since this project already found and fixed a sign-convention bug in it once, and a copy-pasted
function doesn't receive a fix made to its sibling. Any new script needing clip discovery or
GCC-PHAT audio correlation should import from these, not write a new copy.

`gcc_phat()` now runs on CuPy/cuFFT when available, falling back to NumPy automatically
(`cupy-cuda12x` + the matching `nvidia-cufft-cu12`/`nvidia-cuda-nvrtc-cu12`/`nvidia-cuda-nvcc-cu12`
packages, installed into the project's own venv since CuPy's JIT paths need real CUDA headers, not
just the runtime driver). Verified numerically identical to the NumPy path on both a synthetic
known-offset test and a full real-clip batch re-run (byte-identical z-scores and deltas).
**Honest caveat**: these clips' own FFTs are small (tens of thousands of samples) and most of
`precise_audio_sync.py`'s wall-clock time is actually ffmpeg audio extraction via subprocess, not the
correlation math -- don't expect a dramatic end-to-end speedup from this alone; the real win is
checking many clips back-to-back in one long-lived process, not a single isolated call.

## GPU-accelerating `render_song.py`'s per-segment pipeline: one real win, one real dead end

Probed this project's actual RTX 4090 before touching anything (`nvidia-smi`, `ffmpeg -hwaccels
-filters -buildconf`), per the standing rule to measure real hardware rather than assume generic
limits: compute capability 8.9 (Ada), `av1_nvenc`/`h264_nvenc`/`hevc_nvenc` encoders, `av1_cuvid`/
`h264_cuvid`/`hevc_cuvid` decoders, and `scale_cuda`/`overlay_cuda`/`pad_cuda`/`hwupload_cuda` filters
all available -- but **no `crop_cuda` and no GPU blur filter** in this ffmpeg build, so any treatment
needing those still needs a `hwdownload`/`hwupload_cuda` round-trip for just that step. Also
confirmed empirically (not assumed from "consumer cards cap NVENC sessions"): **6 concurrent real
`av1_nvenc` encode sessions at production settings all succeeded with zero failures** on this card --
Ada Lovelace lifted the old consumer session cap, so `render_song.py`'s current `-P 3` xargs
concurrency isn't GPU-session-limited and could go higher if CPU/decode throughput allows.

**The landscape (Nikon) treatment is now GPU-accelerated** (`LANDSCAPE_VF_GPU`: `-hwaccel cuda
-hwaccel_output_format cuda` decode + `scale_cuda` instead of CPU decode + `scale`), verified
**pixel-identical** to the old CPU path on a real segment (mean AND max absolute pixel difference
both exactly 0.0) while cutting wall-clock time by ~42% on an 8-second segment. `crop` still needs a
`hwdownload` first since no GPU crop filter exists, but everything before it -- decode and the
expensive scale -- now runs on NVDEC/the GPU instead of the CPU.

*(Superseded for new shows: `render_cached.py` takes the GPU path for these treatments too -- the user's
rule is GPU first, and a sub-1/255 background-blur difference isn't worth a 5-7x slower render. The
history below explains why `render_song.py` stayed on the CPU.)*

**The portrait/GoPro blur-fill treatments (`PORTRAIT_FC`/`GOPRO_FC`) deliberately were NOT converted**,
despite building and testing a working GPU version (`scale_cuda` for both bg/fg layers, `hwdownload`/
`hwupload_cuda` bracketing the `gblur` step since neither has a GPU equivalent, `overlay_cuda` for the
composite). It ran and encoded successfully, ~47% faster -- but came out with a small, real,
unexplained pixel drift versus the CPU reference (mean abs diff 0.96/255, max 27/255, ~2.1% of pixels
affected by >5/255). Two targeted fixes (forcing `yuv420p`/`nv12` pixel-layout consistency around the
blur step, forcing `scale_cuda`'s `interp_algo` to match the CPU `scale` filter's bicubic default)
made no difference at all to the diff, meaning the actual cause is still unknown -- possibly a
genuine (if small) numeric difference in how `gblur` or `overlay_cuda` compute versus their CPU
equivalents, not a parameter mismatch. **Do not ship this path without root-causing that
discrepancy first** -- it was deliberately left on the proven CPU implementation rather than trade an
unverified small error for a speed win, consistent with this project's own "if you know where it
matches, match it exactly" standard. `overlay_cuda` in this ffmpeg build also only accepts 8-bit
`nv12` as its main input, not `p010le` -- not a real quality loss for GoPro/phone sources (their own
source material is already 8-bit h264), but a hard constraint worth knowing before attempting this
again: the final `format=yuv420p10le` upconversion has to happen AFTER `hwdownload`, in its own
separate `format` step -- `hwdownload` itself must target the GPU frame's actual native format.

## Archiving incrementally while a batch is still running

For a long multi-song batch, don't wait for all songs to finish before moving anything -- a simple
polling loop (`while true; do mv any-newly-complete-pair; sleep 20; done`, launched detached via
`nohup ... & disown`) moves each song to its final destination within ~20s of completion, so an
OneDrive-synced archive starts uploading immediately rather than sitting idle for the whole batch's
multi-hour runtime. Same Win32-process-visibility gotcha as everything else backgrounded this way:
managing it (checking it's alive, stopping it) needs PowerShell `Get-Process`/`Stop-Process`, not
bash `ps`/`kill`. A cosmetic artifact hit running this: the move-log can show the SAME filename
"moved" several times in a row a few seconds apart -- confirmed harmless (the destination only ever
ends up with one copy, verified via direct `ls`), most likely the watcher's own glob re-matching a
file across loop iterations faster than the two `mv` calls complete; not worth chasing further since
the actual archived content was always correct, just don't be alarmed by it.

## A corrected clip can still have a small residual -- the big fix isn't automatically the whole fix

Even after a clip's LARGE aliasing-class error is found and fixed (Nova's song 02 clip: originally
off by -67.9s, fixed), a SMALLER leftover residual (-0.686s, confirmed via a tight GCC-PHAT check
plus two matching karaoke-lyric frames at the corrected timing) can still remain and be genuinely
perceptible as "video lags sound" once someone actually watches the rendered result -- a coarse fix
gets the right PART of the song, a fine one gets the right FRAME. **Spot-checking a "fixed" clip's
final rendered output once, by actually watching it, is still worth doing** -- the automated
pipeline's own EDL-consistency assertions can't catch a residual this small, since they check internal
arithmetic consistency, not real-world audio/video alignment.

## Known content signal not yet used: zoom/glow metadata already tracks aggressive zooming

`detect_zoom_glow.py`'s existing metadata (`bootlegcam`, reused as-is for Stage 1) already tracks
`box_area_frac` (a zoom-level proxy over time) and an explicit `zoom_transitions` list -- but that
list only flags a transition when it causes exposure blowout afterward, not zoom frequency/
aggressiveness on its own. A camera operator with a very zoom-happy style (confirmed on this show)
is therefore NOT currently penalized by the director's scoring even though the raw signal to detect
it already exists in this same sidecar -- a real, understood gap, not yet built.
