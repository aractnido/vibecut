"""Edit statistics for a review folder: the geek stuff.
Per song: length, shots, cuts per minute, shot lengths, camera time shares, verticals and zooms, dissolve length,
how many cuts land on a phrase line, the director's pacing counters, and (for a lyrics version) how many chosen
lines fall where there is room. Writes <folder>/STATS.md and <folder>/stats.csv.
Usage: py edit_stats_report.py <review folder> [<folder to compare against>] [--annotations <dir>] [--title "<text>"]
Reads the per-song render sidecars (<song>.mp4.json: shots with source/mode/render_time_start/end, phrase_boundaries,
style, dissolve_frames, pacing counters). Camera names are taken from the sidecars."""
import csv, glob, json, os, statistics as st, sys

CAMS = []          # filled from the sidecars (every source that appears)
ANN = None
TITLE = ""


def song_stats(f):
    d = json.load(open(f, encoding="utf-8"))
    sh = d["shots"]
    T = sh[-1]["render_time_end"]
    lens = [s["render_time_end"] - s["render_time_start"] for s in sh]
    cuts = [s["render_time_start"] for s in sh[1:]]
    pb = d.get("phrase_boundaries", [])
    on_phrase = sum(1 for c in cuts if pb and min(abs(c - b) for b in pb) <= 0.15)
    share = {c: sum(l for s, l in zip(sh, lens) if s["source"] == c) / T * 100 for c in CAMS}
    camchg = sum(1 for a, b in zip(sh, sh[1:]) if a["source"] != b["source"])
    pingpong = sum(1 for a, b, c in zip(sh, sh[1:], sh[2:]) if a["source"] == c["source"] != b["source"]
                   and (b["render_time_end"] - b["render_time_start"]) < 8)
    longest = max(zip(lens, sh), key=lambda x: x[0])
    r = {"song": d["song_num"], "title": d["title"], "style": d.get("style", ""), "length_s": round(T, 1),
         "shots": len(sh), "cuts": len(cuts), "cuts_per_min": round(len(cuts) / (T / 60), 2),
         "camera_changes": camchg, "shot_median_s": round(st.median(lens), 1), "shot_mean_s": round(st.mean(lens), 1),
         "shot_min_s": round(min(lens), 1), "shot_max_s": round(longest[0], 1), "longest_on": longest[1]["source"],
         "cuts_on_phrase_pct": round(on_phrase / len(cuts) * 100) if cuts else 100,
         "verticals": sum(1 for s in sh if s["mode"] == "vertical"), "zooms": sum(1 for s in sh if s["mode"] == "zoom"),
         "dissolve_frames": d.get("dissolve_frames", 0), "short_pingpong": pingpong}
    for c in CAMS:
        r[c + "_pct"] = round(share[c], 1)
    for k, v in d.get("pacing", {}).items():
        r["pacing_" + k] = v
    af = os.path.join(ANN, d["song_num"] + ".json") if ANN else ""
    if af and os.path.exists(af):
        win = [(s["render_time_start"], s["render_time_end"]) for s in sh if s["mode"] in ("vertical", "zoom")]
        ls = json.load(open(af, encoding="utf-8"))["lines"]
        r["lyric_lines_chosen"] = len(ls)
        r["lyric_lines_with_panel"] = sum(1 for l in ls if any(a <= l.get("at", l["t"]) <= b for a, b in win))
    return r


def table(rows, cols, head):
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        out.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    return "\n".join(out)


def main():
    global ANN, TITLE
    args = sys.argv[1:]
    if "--annotations" in args:
        i = args.index("--annotations"); ANN = args[i + 1]; del args[i:i + 2]
    if "--title" in args:
        i = args.index("--title"); TITLE = args[i + 1]; del args[i:i + 2]
    folder = args[0]
    cmp_ = args[1] if len(args) > 1 else None
    for f in glob.glob(os.path.join(folder, "[0-9][0-9]-*.mp4.json")):
        for s in json.load(open(f, encoding="utf-8"))["shots"]:
            if s["source"] not in CAMS:
                CAMS.append(s["source"])
    rows = [song_stats(f) for f in sorted(glob.glob(os.path.join(folder, "[0-9][0-9]-*.mp4.json")))]
    if not rows:
        print("no song json in", folder)
        return
    keys = sorted({k for r in rows for k in r}, key=lambda k: list(rows[0]).index(k) if k in rows[0] else 99)
    with open(os.path.join(folder, "stats.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    T = sum(r["length_s"] for r in rows)
    allcuts = sum(r["cuts"] for r in rows)
    tot = {c: sum(r[c + "_pct"] * r["length_s"] for r in rows) / T for c in CAMS}
    md = [f"# Edit statistics: {os.path.basename(os.path.normpath(folder))}", "",
          "## Whole show", "",
          f"- **Songs:** {len(rows)}, {T / 60:.1f} min",
          f"- **Shots:** {sum(r['shots'] for r in rows)}, {allcuts} cuts ({allcuts / (T / 60):.2f} per minute)",
          f"- **Median shot:** {st.median([r['shot_median_s'] for r in rows]):.1f} s (median of song medians)",
          f"- **Camera time:** " + ", ".join(f"{c} {tot[c]:.1f} %" for c in CAMS),
          f"- **Verticals:** {sum(r['verticals'] for r in rows)}; **zooms:** {sum(r['zooms'] for r in rows)}",
          f"- **Cuts on a phrase line:** {sum(r['cuts_on_phrase_pct'] * r['cuts'] for r in rows) / max(1, allcuts):.0f} %",
          f"- **Short ping-pongs (A-B-A, B < 8 s):** {sum(r['short_pingpong'] for r in rows)}"]
    if any("lyric_lines_chosen" in r for r in rows):
        md.append(f"- **Lyric lines chosen:** {sum(r.get('lyric_lines_chosen', 0) for r in rows)}, with a panel "
                  f"(vertical or zoom) {sum(r.get('lyric_lines_with_panel', 0) for r in rows)}")
    if cmp_:
        old = {r["song"]: r for r in (song_stats(f) for f in sorted(glob.glob(os.path.join(cmp_, "[0-9][0-9]-*.mp4.json"))))}
        oc = sum(o["cuts"] for o in old.values())
        md += ["", f"Compared with {os.path.basename(os.path.normpath(cmp_))}: {oc} cuts then, {allcuts} now; "
               f"{CAMS[0]} {sum(o.get(CAMS[0] + '_pct', 0) * o['length_s'] for o in old.values()) / max(1, sum(o['length_s'] for o in old.values())):.1f} % "
               f"then, {tot[CAMS[0]]:.1f} % now."]
    import edit_stats_html  # noqa: E402
    tiles = [("Length", f"{T / 60:.1f} min"), ("Cuts per minute", f"{allcuts / (T / 60):.2f}"),
             ("Median shot", f"{st.median([r['shot_median_s'] for r in rows]):.1f} s"),
             (f"{CAMS[0]} time", f"{tot[CAMS[0]]:.1f} %"),
             ("Verticals / zooms", f"{sum(r['verticals'] for r in rows)} / {sum(r['zooms'] for r in rows)}"),
             ("Cuts on a phrase line", f"{sum(r['cuts_on_phrase_pct'] * r['cuts'] for r in rows) / max(1, allcuts):.0f} %")]
    if any("lyric_lines_chosen" in r for r in rows):
        tiles.append(("Lyric lines with panel", f"{sum(r.get('lyric_lines_with_panel', 0) for r in rows)} / "
                                                f"{sum(r.get('lyric_lines_chosen', 0) for r in rows)}"))
    edit_stats_html.write(folder, rows, tiles, md[-1].strip() if cmp_ else "", CAMS, TITLE)
    md += ["", "## Per song", "",
           table(rows, ["song", "title", "style", "length_s", "shots", "cuts_per_min", "shot_median_s", "shot_min_s",
                        "shot_max_s", "longest_on", "cuts_on_phrase_pct", "dissolve_frames"],
                 ["#", "Song", "Style", "Length s", "Shots", "Cuts/min", "Median s", "Min s", "Max s", "Longest on",
                  "Cuts on phrase %", "Dissolve fr"]),
           "", "## Cameras and director", "",
           table(rows, ["song"] + [c + "_pct" for c in CAMS] + ["verticals", "zooms",
                        "short_pingpong"] + sorted({k for r in rows for k in r if k.startswith("pacing_")}) + [
                        "lyric_lines_chosen", "lyric_lines_with_panel"],
                 ["#"] + [c + " %" for c in CAMS] + ["Vert", "Zoom", "Ping-pong",
                  ] + [k[7:].replace("_", " ") for k in sorted({k for r in rows for k in r if k.startswith("pacing_")})] + [ "Lines chosen", "Lines w/ panel"]),
           "", "Columns: `stats.csv` has every field. the pacing columns are the director's own counters from the sidecars; a cut 'on a phrase line' is "
           "within 0.15 s of a 4-bar phrase boundary."]
    open(os.path.join(folder, "STATS.md"), "w", encoding="utf-8").write("\n".join(md) + "\n")
    print("\n".join(md[:12]))


if __name__ == "__main__":
    main()
