"""show.json -- one file per show that names the cameras and their roles, read by the scripts that used to hardcode
a roster (sync_to_anchor.py, build_soundtrack.py, floor_energy.py, build_highlights_edl.py, render_cached.py,
link_signals.py). Lives in the show working folder ($VIVECUT_SHOW_ROOT/show.json); see examples/show.example.json.

Every path in show.json may be absolute or relative to the show folder. Missing optional keys fall back to the
defaults below. With NO show.json, build_highlights_edl.py keeps its original Nikon-anchor behaviour.
"""
import json
import os

ROOT = os.environ.get("VIVECUT_SHOW_ROOT", os.getcwd())

DEFAULTS = {
    "anchor": {"camera": "nikon", "clock_fix_s": 0.0, "audio": "self"},
    "cameras": {},
    "director": {"priority": [], "dwell": {}, "anchor_bonus": 0.05, "crowd": None, "back_of_head": None},
    "soundtrack": {"priority": [], "bed": None},
    "floor": {},
    "render": {"out_dir": "Preview/highlights", "cache_dir": "cache/frag", "title_hold_s": 4.0},
}


def path(p):
    """Resolve a show.json path against the show folder."""
    if p is None:
        return None
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def load(required=True):
    f = os.path.join(ROOT, "show.json")
    if not os.path.exists(f):
        if required:
            raise SystemExit(f"no show.json in {ROOT} (set VIVECUT_SHOW_ROOT; see examples/show.example.json)")
        return None
    cfg = json.load(open(f, encoding="utf-8"))
    for k, v in DEFAULTS.items():
        if isinstance(v, dict):
            merged = dict(v)
            merged.update(cfg.get(k) or {})
            cfg[k] = merged
        else:
            cfg.setdefault(k, v)
    return cfg
