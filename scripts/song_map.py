r"""song_map.py -- what is happening in the music, per bar and per 4-bar phrase: metadata for the director.

Separates the song window into 4 stems (HTDemucs) IN MEMORY -- stems are disposable, never written; re-run if more
metadata is needed -- transcribes the vocals (Whisper large-v3, language given per song: per-30 s detection failed,
banter made a Spanish song "English"), and measures on the GPU (torch STFT / envelopes):
  vocal_intervals   singing on (vocal-stem level within 18 dB of its loud passages, 0.3 s smoothing) -- the
                    reliable signal; lyrics are only a HINT (missed lines, invented ones in quiet stretches)
  banter            voice while the band is silent
  instrumental      >= 2 bars without singing: intro / solo (guitars-keys loud) / break / outro
  fills             phrase boundaries preceded by a drum fill (onsets in the last bar >= 1.35x the usual bar)
  phrases[]         section letter (Ward clustering of chroma + 16 timbre bands + vocal share + drum density +
                    energy; chords alone put a whole live song in one section), chorus guess (repeating sung
                    section, loudest or with repeating lyrics), energy, vocal share, fill before, lyric lines,
                    crowd dancing
  grid_check        where stanza-first lines start in the 4-bar phrase -> a suggested phrase shift in bars (seen
                    -1 / 0 / +1 song by song on one show: check by ear before applying)
The grid is NOT re-tracked: bars are the phrase boundaries the edit already uses, divided by 4.
Whisper hallucination filter: drop a segment only if no_speech_prob > 0.6 AND avg_logprob < -1.0 (sung lines score
no_speech ~0.9 over music, so either alone drops real lyrics), compression_ratio > 2.4, or a stock phrase.

Needs one Python with torch (CUDA), demucs, openai-whisper, scipy, soundfile.
Usage: py song_map.py <audio> <start_s> <end_s> <phrases.json> <en|es|...> <out.json> [--crowd crowd.json]
  phrases.json: {"phrase_boundaries": [...]} in song time (a render sidecar works as is)
  crowd.json:   [[song_t, n_dancing], ...] (optional)"""
import difflib
import json
import subprocess
import sys

import numpy as np
import torch

HOP = 0.02
DEV = "cuda"
STOCK = ("thank you", "thanks for watching", "take care", "suscríbete", "subscribe", "gracias por ver",
         "subtítulos", "amara.org", "music", "música")


def separate(audio, start, dur):
    """4 HTDemucs stems of [start, start+dur), mono 22.05 kHz torch tensors on the GPU (never written to disk)."""
    from demucs.apply import apply_model
    from demucs.pretrained import get_model
    model = get_model("htdemucs").to(DEV).eval()
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", audio,
                          "-ac", "2", "-ar", str(model.samplerate), "-f", "f32le", "-"], capture_output=True).stdout
    x = torch.from_numpy(np.frombuffer(pcm, np.float32).reshape(-1, 2).T.copy())
    ref = x.mean(0)
    with torch.no_grad():
        y = apply_model(model, ((x - ref.mean()) / (ref.std() + 1e-8))[None].to(DEV), device=DEV, split=True,
                        overlap=0.25, progress=False)[0]
    y = (y * (ref.std() + 1e-8) + ref.mean()).mean(1)
    y = torch.nn.functional.avg_pool1d(y[:, None], 2)[:, 0]
    stems = {k: y[i] for i, k in enumerate(model.sources)}
    sr = model.samplerate // 2
    del model
    torch.cuda.empty_cache()
    return stems, sr


def transcribe(vocals, sr, lang):
    """Lyric lines with word times from the vocal stem; hallucinations filtered (see module doc)."""
    import whisper
    a = torch.nn.functional.interpolate(vocals[None, None], scale_factor=16000 / sr, mode="linear")[0, 0].cpu().numpy()
    m = whisper.load_model("large-v3", device=DEV)
    r = m.transcribe(a.astype(np.float32), language=lang, word_timestamps=True, condition_on_previous_text=False,
                     fp16=True)
    lines = []
    for sg in r["segments"]:
        t = sg["text"].strip().lower().strip("¡!¿?.,… ")
        if ((sg.get("no_speech_prob", 0) > 0.6 and sg.get("avg_logprob", 0) < -1.0) or
                sg.get("compression_ratio", 0) > 2.4 or any(b in t for b in STOCK) or len(t) < 2):
            continue
        ws = [[round(w["start"], 2), round(w["end"], 2), w["word"].strip()] for w in sg.get("words", [])]
        if ws:
            lines.append({"start": ws[0][0], "end": ws[-1][1], "text": sg["text"].strip(), "words": ws})
    del m
    torch.cuda.empty_cache()
    return lines


def env_db(x, sr):
    h = int(sr * HOP)
    fr = x[: len(x) // h * h].reshape(-1, h)
    return 20 * torch.log10(fr.pow(2).mean(1).sqrt() + 1e-6)


def onset_strength(x, sr):
    S = torch.stft(x, n_fft=1024, hop_length=int(sr * HOP), window=torch.hann_window(1024, device=DEV),
                   return_complex=True).abs()
    S = torch.log1p(100 * S)
    return torch.relu(S[:, 1:] - S[:, :-1]).sum(0)


def chroma(x, sr):
    n = 4096
    S = torch.stft(x, n_fft=n, hop_length=int(sr * HOP), window=torch.hann_window(n, device=DEV),
                   return_complex=True).abs() ** 2
    f = torch.fft.rfftfreq(n, 1 / sr).to(DEV)
    ok = (f > 55) & (f < 2000)
    pc = torch.round(12 * torch.log2(f[ok] / 440.0)).long() % 12
    C = torch.zeros(12, S.shape[1], device=DEV)
    C.index_add_(0, pc, S[ok])
    return C / (C.sum(0, keepdim=True) + 1e-9)


def bands(x, sr, nb=16):
    S = torch.stft(x, n_fft=2048, hop_length=int(sr * HOP), window=torch.hann_window(2048, device=DEV),
                   return_complex=True).abs() ** 2
    f = torch.fft.rfftfreq(2048, 1 / sr).to(DEV)
    edges = torch.logspace(np.log10(60), np.log10(sr / 2 - 1), nb + 1, device=DEV)
    idx = torch.bucketize(f, edges).clamp(1, nb) - 1
    B = torch.zeros(nb, S.shape[1], device=DEV)
    B.index_add_(0, idx, S)
    return torch.log10(B + 1e-9)


def intervals(mask, t, min_on=0.4, max_gap=0.5):
    iv, on = [], None
    for i, m in enumerate(mask):
        if m and on is None:
            on = t[i]
        if not m and on is not None:
            iv.append([on, t[i]]); on = None
    if on is not None:
        iv.append([on, t[-1]])
    merged = []
    for a, b in iv:
        if merged and a - merged[-1][1] < max_gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [[round(a, 2), round(b, 2)] for a, b in merged if b - a >= min_on]


def frac_in(iv, a, b):
    return sum(max(0, min(b, y) - max(a, x)) for x, y in iv) / max(b - a, 1e-6)


def norm_text(s):
    return "".join(c for c in s.lower() if c.isalnum() or c == " ").strip()




def analyze(stems, sr, pb, lines, cr, title=""):
    """stems: dict vocals/drums/bass/other -> mono torch tensors on the GPU; pb: phrase boundaries (song time);
    lines: lyric lines [{start, end, text}] (hint, may be empty); cr: [(song_t, n_dancing)] (may be empty)."""
    voc, drm, bas, oth = stems["vocals"], stems["drums"], stems["bass"], stems["other"]
    n = min(len(voc), len(drm), len(bas), len(oth))
    voc, drm, bas, oth = voc[:n], drm[:n], bas[:n], oth[:n]
    dur = n / sr
    v_db, d_db, b_db, o_db = (env_db(x, sr) for x in (voc, drm, bas, oth))
    m_db = env_db(voc + drm + bas + oth, sr)
    band_db = env_db(drm + bas + oth, sr)
    d_on = onset_strength(drm, sr)
    C = chroma(voc + oth, sr)
    Bm = bands(voc + drm + bas + oth, sr)
    T = min(len(v_db), len(d_on), C.shape[1])
    t = np.arange(T) * HOP
    v, band = v_db[:T].cpu().numpy(), band_db[:T].cpu().numpy()
    # singing: vocal level within 18 dB of its loud passages, smoothed 0.3 s
    thr = np.percentile(v, 95) - 18
    k = int(0.3 / HOP)
    act = np.convolve((v > thr).astype(float), np.ones(k) / k, mode="same") > 0.5
    vocal_iv = intervals(act, t)
    quiet_band = np.convolve((band < np.percentile(band, 50) - 15).astype(float), np.ones(k) / k, mode="same") > 0.5
    banter_iv = intervals(act & quiet_band, t, min_on=1.0)
    # grid: published phrase boundaries, bars = quarters; extend one phrase before/after with the median length
    P = float(np.median(np.diff(pb)))
    grid = list(pb)
    while grid[0] - P > 0:
        grid.insert(0, grid[0] - P)
    while grid[-1] + P < dur:
        grid.append(grid[-1] + P)
    phrases = [(a, b) for a, b in zip(grid, grid[1:])]
    bars = [a + j * (b - a) / 4 for a, b in phrases for j in range(4)] + [grid[-1]]
    don = d_on[:T].cpu().numpy()
    don = don / (np.percentile(don, 99) + 1e-9)

    def onsets(a, b):          # drum onset peaks in [a, b)
        i0, i1 = int(a / HOP), int(b / HOP)
        x = don[i0:i1]
        if len(x) < 3:
            return 0
        pk = (x[1:-1] > x[:-2]) & (x[1:-1] >= x[2:]) & (x[1:-1] > 0.25)
        return int(pk.sum())
    bar_on = [onsets(a, b) for a, b in zip(bars, bars[1:])]
    usual = float(np.median([x for x in bar_on if x > 0])) if any(bar_on) else 0
    # lyrics (hint)
    texts = [norm_text(l["text"]) for l in lines]
    rep = [sum(1 for j, u in enumerate(texts) if j != i and len(u) > 6 and
               difflib.SequenceMatcher(None, u, ti).ratio() > 0.75) for i, ti in enumerate(texts)]
    Cn = C[:, :T].cpu().numpy()
    Bn = Bm[:, :T].cpu().numpy()
    md = m_db[:T].cpu().numpy()
    rows = []
    for i, (a, b) in enumerate(phrases):
        i0, i1 = int(a / HOP), max(int(a / HOP) + 1, int(b / HOP))
        lb = [j for j, l in enumerate(lines) if a <= l["start"] < b]
        last_bar = bar_on[4 * i - 1] if i > 0 and 4 * i - 1 < len(bar_on) else 0
        dn = [vv for tt, vv in cr if a <= tt < b]
        rows.append({"t0": round(a, 2), "t1": round(b, 2),
                     "energy_db": round(float(np.mean(md[i0:i1])), 1),
                     "drum_onsets_per_bar": round(float(np.mean(bar_on[4 * i:4 * i + 4])) if 4 * i < len(bar_on) else 0, 1),
                     "vocal_share": round(frac_in(vocal_iv, a, b), 2),
                     "fill_before": bool(i > 0 and usual > 0 and last_bar >= 1.35 * usual and last_bar >= usual + 1.5),
                     "lyrics": [lines[j]["text"] for j in lb],
                     "lyric_repeats": int(sum(rep[j] for j in lb)),
                     "crowd_dancing": round(float(np.mean(dn)), 2) if dn else None,
                     "_chroma": Cn[:, i0:i1].mean(1), "_bands": Bn[:, i0:i1].mean(1)})
    # sections: agglomerative clustering of z-scored phrase features -- harmony (chroma), timbre (16 bands),
    # vocal share, drum density, energy. A live mix barely changes chords/level between verse and chorus,
    # so harmony alone put a whole song in one section.
    from scipy.cluster.hierarchy import fcluster, linkage
    F = np.array([np.concatenate([r["_chroma"] * 3, r["_bands"], [r["vocal_share"] * 2, r["drum_onsets_per_bar"] / 4,
                                                                 r["energy_db"] / 3]]) for r in rows])
    F = (F - F.mean(0)) / (F.std(0) + 1e-6)
    k_target = int(np.clip(round(len(rows) / 6), 3, 6))
    lab = fcluster(linkage(F, "ward"), k_target, "maxclust") if len(rows) > k_target else np.arange(1, len(rows) + 1)
    order = {}
    for L in lab:                                   # letters in order of first appearance
        order.setdefault(L, chr(64 + len(order) + 1))
    for r, L in zip(rows, lab):
        r["section"] = order[L]
    # chorus guess: a repeating sung section that is loudest, or whose lyrics repeat
    e_mean = np.mean([r["energy_db"] for r in rows])
    by = {}
    for r in rows:
        by.setdefault(r["section"], []).append(r)
    cand = {s: rs for s, rs in by.items() if len(rs) >= 2 and np.mean([x["vocal_share"] for x in rs]) > 0.3}
    score = {s: np.mean([x["energy_db"] for x in rs]) - e_mean + 2.0 * np.mean([x["lyric_repeats"] > 0 for x in rs])
             for s, rs in cand.items()}
    chorus = max(score, key=score.get) if score else None
    for r in rows:
        r["chorus_guess"] = r["section"] == chorus
        r.pop("_chroma"); r.pop("_bands")
    # instrumental stretches (>= 2 bars without singing)
    instr, cur = [], None
    for j, (a, b) in enumerate(zip(bars, bars[1:])):
        quiet = frac_in(vocal_iv, a, b) < 0.1
        if quiet and cur is None:
            cur = [a, b]
        elif quiet:
            cur[1] = b
        elif cur is not None:
            instr.append(cur); cur = None
    if cur:
        instr.append(cur)
    o = o_db[:T].cpu().numpy()
    o_ref = np.percentile(o, 60)
    out_instr = []
    for a, b in instr:
        if b - a < 2 * P / 4 - 0.05:
            continue
        lvl = float(np.mean(o[int(a / HOP):int(b / HOP)]))
        kind = "intro" if a <= bars[1] else "outro" if b >= bars[-2] else ("solo" if lvl >= o_ref else "break")
        out_instr.append({"t0": round(a, 2), "t1": round(b, 2), "kind": kind, "guitar_keys_db_vs_song": round(lvl - o_ref, 1)})
    # grid check: sung line starts relative to the nearest bar line (in bars, -0.5..0.5) and to the phrase (0..4 bars)
    bar_len = P / 4
    firsts = [l for j, l in enumerate(lines) if j == 0 or l["start"] - lines[j - 1]["end"] >= 1.0]
    rel_bar = [((l["start"] - grid[0]) / bar_len + 0.5) % 1 - 0.5 for l in firsts]
    rel_phr = [int(round((l["start"] - grid[0]) / bar_len) % 4) for l in firsts]
    hist = np.bincount(rel_phr, minlength=4).tolist() if rel_phr else [0, 0, 0, 0]
    return {"title": title, "duration": round(dur, 2), "bar_s": round(bar_len, 3),
            "bars": [round(x, 2) for x in bars], "phrases": rows, "vocal_intervals": vocal_iv, "banter": banter_iv,
            "instrumental": out_instr, "fills": [r["t0"] for r in rows if r["fill_before"]],
            "chorus_section": chorus,
            "grid_check": {"stanza_first_lines": len(rel_phr), "line_start_bar_in_phrase_hist": hist,
                           "median_offset_from_bar_line_bars": round(float(np.median(rel_bar)), 2) if rel_bar else None,
                           "suggested_phrase_shift_bars": (int(np.argmax(hist)) - 4 if np.argmax(hist) > 2 else int(np.argmax(hist)))
                           if sum(hist) >= 5 and max(hist) >= 0.45 * sum(hist) else 0}}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    audio, a, b, phr, lang, out = args[0], float(args[1]), float(args[2]), args[3], args[4], args[5]
    crowd_f = next((sys.argv[i + 1] for i, x in enumerate(sys.argv) if x == "--crowd"), None)
    if crowd_f in args:
        args.remove(crowd_f)
    pb = json.load(open(phr, encoding="utf-8"))["phrase_boundaries"]
    stems, sr = separate(audio, a, b - a)
    lines = transcribe(stems["vocals"], sr, lang)
    cr = [tuple(x) for x in json.load(open(crowd_f, encoding="utf-8"))] if crowd_f else []
    m = analyze(stems, sr, pb, lines, cr)
    m["lyrics"] = lines
    json.dump(m, open(out, "w", encoding="utf-8"), indent=1, ensure_ascii=False,
              default=lambda x: x.item() if hasattr(x, "item") else str(x))
    secs = "".join(r["section"] for r in m["phrases"])
    print(f"sections {secs} chorus={m['chorus_section']} instrumental {[(i['kind'], i['t0']) for i in m['instrumental']]} "
          f"fills {len(m['fills'])} grid shift {m['grid_check']['suggested_phrase_shift_bars']} -> {out}")


if __name__ == "__main__":
    main()
