"""STATS.html for edit_stats_dump.py: stat tiles, camera-time stacked bars per song (hover for values; exact numbers
in the table below as the accessible view), and the full per-song table (click a header to sort).
Palette: dataviz reference categorical slots 1-4, validated light and dark (CVD pass; two light slots under 3:1
contrast, so the table carries the values)."""
import html as H
import os

COLS = [("song", "#"), ("title", "Song"), ("style", "Style"), ("length_s", "Length s"), ("shots", "Shots"),
        ("cuts_per_min", "Cuts/min"), ("shot_median_s", "Median shot s"), ("shot_min_s", "Min s"), ("shot_max_s", "Max s"),
        ("longest_on", "Longest on"), ("cuts_on_phrase_pct", "Cuts on phrase %"), ("verticals", "Vert"), ("zooms", "Zoom"),
        ("short_pingpong", "Ping-pong"), ("dissolve_frames", "Dissolve fr"), ("lyric_lines_chosen", "Lines chosen"),
        ("lyric_lines_with_panel", "Lines w/ panel")]

CSS = """
:root{--bg:#fcfcfb;--ink:#1d1d1b;--ink2:#5c5b55;--line:#e4e3de;--card:#f4f3ef;--s0:#2a78d6;--s1:#eb6834;--s2:#1baf7a;--s3:#eda100;--s4:#e87ba4;--s5:#008300}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--line:#34332f;--card:#242422;--s0:#3987e5;--s1:#d95926;--s2:#199e70;--s3:#c98500}}
:root[data-theme=dark]{--bg:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--line:#34332f;--card:#242422;--s0:#3987e5;--s1:#d95926;--s2:#199e70;--s3:#c98500}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,"Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:32px 0 10px}
.sub{color:var(--ink2);margin:0 0 18px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.tile{background:var(--card);border-radius:10px;padding:12px 14px}
.tile .v{font-size:24px;font-weight:650;font-variant-numeric:tabular-nums} .tile .k{color:var(--ink2);font-size:12px}
.legend{display:flex;gap:16px;flex-wrap:wrap;color:var(--ink2);margin:0 0 8px}
.lg i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.row{display:grid;grid-template-columns:minmax(120px,260px) 1fr 96px;gap:10px;align-items:center;margin:3px 0}
.lab{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--ink2)}
.num{text-align:right;font-variant-numeric:tabular-nums} .num small{color:var(--ink2)}
.bar{display:flex;gap:2px;height:14px}
.seg{border-radius:2px;min-width:2px} .seg:first-child{border-radius:4px 2px 2px 4px} .seg:last-child{border-radius:2px 4px 4px 2px}
.seg:only-child{border-radius:4px}
.s0{background:var(--s0)} .s1{background:var(--s1)} .s2{background:var(--s2)} .s3{background:var(--s3)} .s4{background:var(--s4)} .s5{background:var(--s5)}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:6px 9px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{position:sticky;top:0;background:var(--card);cursor:pointer;font-weight:600}
td:nth-child(2),th:nth-child(2){text-align:left} tr:hover td{background:var(--card)}
#tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--bg);padding:5px 8px;border-radius:6px;font-size:12px;display:none}
.note{color:var(--ink2);font-size:12px;margin-top:10px}
@media (max-width:640px){.row{grid-template-columns:110px 1fr 70px}}
"""

JS = """
const tip=document.getElementById('tip');
document.querySelectorAll('.seg').forEach(s=>{
  s.addEventListener('mousemove',e=>{tip.textContent=s.dataset.tip;tip.style.display='block';
    tip.style.left=(e.clientX+12)+'px';tip.style.top=(e.clientY+12)+'px'});
  s.addEventListener('mouseleave',()=>tip.style.display='none')});
document.querySelectorAll('th').forEach((th,i)=>th.addEventListener('click',()=>{
  const tb=th.closest('table').tBodies[0], rs=[...tb.rows], d=th.dataset.dir==='a'?-1:1;
  th.dataset.dir=d>0?'a':'d';
  rs.sort((a,b)=>{const x=a.cells[i].textContent,y=b.cells[i].textContent,nx=parseFloat(x),ny=parseFloat(y);
    return (isNaN(nx)||isNaN(ny)?x.localeCompare(y):nx-ny)*d});
  rs.forEach(r=>tb.appendChild(r))}));
"""


def write(folder, rows, tiles, compare="", cams=(), title=""):
    CAMS = [(c, c) for c in cams][:6]          # categorical slots are fixed-order; more than 6 sources would need an "other" fold
    bars = []
    for r in rows:
        segs = "".join(
            f'<span class="seg s{i}" style="flex:{r[c + "_pct"]}" '
            f'data-tip="{H.escape(r["title"])} · {lab} {r[c + "_pct"]:.1f} %"></span>'
            for i, (c, lab) in enumerate(CAMS) if r.get(c + "_pct", 0) > 0)
        bars.append(f'<div class="row"><div class="lab">{r["song"]} {H.escape(r["title"])}</div>'
                    f'<div class="bar">{segs}</div><div class="num">{r["cuts_per_min"]:.1f}<small> cuts/min</small></div></div>')
    pacing = sorted({k for r in rows for k in r if k.startswith("pacing_")})
    cols = [c for c in COLS[:11] + [(k + "_pct", k + " %") for k, _ in CAMS] + COLS[11:-2]
            + [(k, k[7:].replace("_", " ")) for k in pacing] + COLS[-2:] if any(c[0] in r for r in rows)]
    thead = "".join(f"<th>{H.escape(v)}</th>" for _, v in cols)
    tbody = "".join("<tr>" + "".join(f"<td>{H.escape(str(r.get(k, '')))}</td>" for k, _ in cols) + "</tr>" for r in rows)
    tiles_h = "".join(f'<div class="tile"><div class="v">{H.escape(v)}</div><div class="k">{H.escape(k)}</div></div>'
                      for k, v in tiles)
    legend = "".join(f'<span class="lg"><i class="s{i}"></i>{lab}</span>' for i, (_, lab) in enumerate(CAMS))
    name = os.path.basename(os.path.normpath(folder))
    sub = (f"{H.escape(title)} · " if title else "") + f"{len(rows)} songs" + (f" · {H.escape(compare)}" if compare else "")
    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>Edit Stats {H.escape(name)}</title>'
           f"<style>{CSS}</style></head><body><main>"
           f"<h1>Edit statistics: {H.escape(name)}</h1><p class=\"sub\">{sub}</p>"
           f'<div class="tiles">{tiles_h}</div>'
           f'<h2>Camera time per song</h2><div class="legend">{legend}</div>{"".join(bars)}'
           f'<h2>Per song</h2><div class="wrap"><table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table></div>'
           '<p class="note">Click a column to sort. The pacing columns are the director\'s own counters from the render '
           'sidecars; a cut "on phrase" is within 0.15 s of a 4-bar '
           'phrase line; "Ping-pong" is A-B-A with B under 8 s; "Lines w/ panel" counts chosen lyric lines inside a vertical '
           'or zoom shot. Every field is also in stats.csv.</p>'
           f'</main><div id="tip"></div><script>{JS}</script></body></html>')
    open(os.path.join(folder, "STATS.html"), "w", encoding="utf-8").write(doc)
