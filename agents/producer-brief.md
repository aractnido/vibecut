# Song producer brief (lyric visuals)

You prepare ONE song's lyrics version so the orchestrator can render it. You play three specialists in order and
write every result to disk. Do NOT render, do NOT publish, do NOT edit shared code (lyric_fx_base.py,
lyric_annot.py, lyric_layer.py, render_cinematic.py) or any other song's files. Approved songs
are never touched.

Work dir: <show folder>. Python with GPU libs: <venv python>
(-X utf8). System python `py` runs vocals_tmp.py. GPU always (no CPU decode loops).

## Inputs already there
metadata/song_research/NN.md (reading), metadata/room/NN.json (every sung line with room: "panel" + panel_w, or
"pic" + free corners), metadata/lyrics/NN.json (word times; ok:false = unreliable), metadata/lyricist/NN.json (old
picks), metadata/song_map/NN.json (bars, vocal_intervals), ReviewCine5/no-lyrics/NN-*.mp4.json (shots).

## 0. Copyright (a producer was stopped by the content filter for this)
Never write song lyrics into your messages or reports: at most 5 words of any lyric. In files, prefer word INDEXES
into the line's word list (e.g. "words": [0, 2], "key": [2]) or "ALL", and build files with small Python scripts that
read the words from metadata/lyrics/NN.json, instead of typing the lyrics yourself.

## 1. Lyricist (read agents/lyricist.md and follow it, plus these rules)
- Every sung line with room shows something; "none" only with why = repeat / echo / filler / unreliable / slur.
- Panel lines: the whole line shows when it fits; your keys are the fallback. Pic (corner) lines: at most 3 words.
- One entry per room-map line, in order: {t, show, words (exact spelling from the lyrics/fix file, or "ALL"), key,
  why}. Write scratch/lNN.json, then run: `py -X utf8 scratch/merge_lyricist.py NN scratch/lNN.json`.

- Distinctive screams / crowd shouts ("ooh ooh", "¡Afuera!", "¡Que poca madre!") are SIGNATURE moments, never
  filler; if one is missing from the lyrics file, find it in the audio (Whisper on the full mix + validated alignment).

## 2. Timing (validated only)
- Whisper is only for discovery (what is sung, its order, shouts missing from the lyrics file). Every time you draw
  with comes from MMS-FA (realign_window.py / realign_sections.py) and must pass the validator. Never use a Whisper
  timestamp directly as a line or word time.
- `py -X utf8 scripts/vocals_tmp.py NN` (vocal stem to scratch/fa/NN.wav).
- Re-align every collapsed / overlapping / ok:false section JOINTLY with
  `scripts/realign_window.py NN T0 T1 "line|line|..."` (windows from the song map's vocal_intervals; lines in sung
  order) or `scripts/realign_sections.py NN T0-T1 ...`. Only VALIDATED results are kept (the tool rejects otherwise:
  words on singing, no gaps, order, pace, no uncovered singing). Numbers: align them spelled out, then edit the fix
  file to show digits. If a chorus can't be validated, show only its meaning on reliable anchors (the "Me vale" rule)
  and say so. Delete scratch/fa/NN.wav when done (stems are disposable).

## 3. Designer (read agents/kinetic-typography.md and follow it)
Build ON the approved look (shared base lyric_fx_base.py: warm phrase colour + glow, title words emphasised,
features slam in on harsh songs / rise from blur on soft ones, lines out 0.27 s after the last word, room-adaptive
tiers; PIC tier for corners: cfg "pic", "feat_px", "lead_px", plate + shadow automatic). 2-4 signature effects with
MEANING for this song (examples the band leader loved: wedding rings on "got married", the shrugging "vale", the
Mercedes star, hearts cracking on "quería", letters snatched away on "quitaste"). Taste: nothing crude; no gun
imagery; never text on faces. Write the spec to metadata/specs/NN.md (look, palette, blocks, per-line table).

## 4. Implement
- scripts/lyric_fx_NN.py: `ST = B.style(...)`, `blocks(front, back, e, cfg, geo, pn, cx, Wp, t, num, ST_)`,
  `draw(t, evs, side_px, beat0, beat): return B.draw(t, evs, side_px, ST, blocks=blocks)`. Examples to copy from:
  scripts/lyric_fx_05.py, scripts/lyric_fx_02.py, scripts/lyric_fx_06.py. geo[word] = (x, y, size, width, ts) for
  feature words (custom words are not drawn by the base: your block draws them); geo["_box"] in corners. Every frame
  must be a pure function of t. Import everything you use (a missing import failed a render).
- metadata/annot_tables/NN.json (format: see metadata/annot_tables/05.json and scripts/build_annot_table.py): rows
  {t (fix time if re-aligned), words ("ALL" or list), keys, hero, room L/R/both/TL/TR/BL/BR, block, cfg, exit}.
  Two lines must never overlap in the same corner/panel; alternate.
- Run: `scripts/build_annot_table.py NN`, `scratch/smoke_fx.py NN` (must print 0 errors),
  `scripts/gap_check_room.py NN` (every chosen line covered; justify any miss).

## Report (under 200 words)
What the song's look is, the signature effects, lines shown / total, which sections were re-aligned (validated) or
left on meaning-only anchors, gap-check result, anything uncertain for the band leader.
