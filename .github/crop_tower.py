#!/usr/bin/env python3
"""Post-process github-profile-3d-contrib output.

- crops the isometric tower grid to the most recent N weeks
- replaces the animated rainbow palette with a static color theme
- keeps the one-shot load growth animation, drops the radar/stats widget

Run in .github/workflows/profile-3d.yml right after
yoshi389111/github-profile-3d-contrib and before the commit step.

Usage:
    python3 crop_tower.py --weeks 27 --theme green
"""
import argparse
import re
import sys
from datetime import date, timedelta
from pathlib import Path

# level 1..4 -> base (top face) color; left/right faces are shaded versions
THEMES = {
    "green": {1: (35, 134, 54), 2: (46, 160, 67), 3: (63, 185, 80), 4: (86, 211, 100)},
    "violet": {1: (94, 64, 152), 2: (121, 85, 196), 3: (137, 87, 229), 4: (170, 127, 243)},
    "purple": {1: (91, 33, 182), 2: (124, 58, 237), 3: (147, 51, 234), 4: (192, 132, 252)},
}

# empty-day (level 0) neutral colors, per background type
L0_DARK = {"top": (27, 35, 48), "left": (20, 26, 37), "right": (14, 19, 28)}
L0_LIGHT = {"top": (230, 235, 241), "left": (213, 219, 227), "right": (195, 202, 211)}

FACE_SHADE = {"top": 1.0, "left": 0.80, "right": 0.64}
MARGIN = 14
MIN_CELLS = 200

CELL_RE = re.compile(r'<g transform="translate\(([\d.]+) ([\d.]+)\)">(.*?)</g>', re.S)
# side face is the rect right-skewed 30 deg (height before scale varies per style)
HEIGHT_RE = re.compile(r'height="([\d.]+)" transform="skewY\(30\)')
SIDESCALE_RE = re.compile(r'transform="skewY\(30\) scale\([\d.]+ ([\d.]+)\)"')
LEVEL_CLASS_RE = re.compile(r"\.([a-z0-9]+)-l([0-4])-(top|left|right)\s*\{[^}]*\}")
# gitblock: cont-face-bg|fg-level; season: cont-face-p<period>-level
CONT_CLASS_RE = re.compile(r"\.cont-(top|left|right)-(bg|fg|p[0-9]+)-([0-4])\s*\{[^}]*\}")


def shade(rgb, f):
    return tuple(max(0, min(255, round(c * f))) for c in rgb)


def rgb_css(rgb):
    return "rgb(%d,%d,%d)" % rgb


def luminance(color):
    m = re.match(r"#([0-9a-fA-F]{6})$", color)
    if m:
        h = m.group(1)
        return (int(h[0:2], 16) + int(h[2:4], 16) + int(h[4:6], 16)) / 3
    nums = [int(v) for v in re.findall(r"\d+", color)]
    return sum(nums[:3]) / 3 if nums else 0


def fit_lattice(cells, s, h_flat):
    """Locate the floor lattice and assign (a=day, b=week) to grid cells.

    A cell's static origin sits on the floor lattice only when flat:
    contributed cubes are bottom-aligned, so their origin is raised by
    s*(h_flat - h). Flat cells anchor the lattice.

    cells: list of (x, y, h, start_offset, html).
    Returns a list of (b, a, x, y, h, start_offset, html) with b
    normalized so the earliest week is 0, or None when the layout
    is not recognized.
    """
    from collections import defaultdict

    x0 = cells[0][0]
    flat = [c for c in cells if abs(c[2] - h_flat) < 0.05]
    if len(flat) < 50:
        return None

    # dy (floor row step, ~11.55): within one column, consecutive flat
    # rows differ by 2 in (a+b) (parity), so the smallest within-column
    # y gap is 2*dy
    cols = defaultdict(list)
    for x, y, h, _s, _html in flat:
        ba = (x - x0) / 20
        if abs(ba - round(ba)) < 0.1:
            cols[round(ba)].append(y)
    gaps = [b - a for ys in cols.values() for a, b in zip(sorted(ys), sorted(ys)[1:])]
    if not gaps:
        return None
    dy = min(gaps) / 2
    if not (8 < dy < 16):
        return None
    y0 = min(y for _x, y, _h, _s, _html in flat)

    kept = []
    for x, y, h, start, html in cells:
        ba = (x - x0) / 20
        k = (y - y0 - s * (h_flat - h)) / dy  # a + b on the floor lattice
        if abs(ba - round(ba)) > 0.1 or abs(k - round(k)) > 0.15:
            continue
        a = (round(k) - round(ba)) / 2
        b = (round(k) + round(ba)) / 2
        if a % 1 or b % 1:
            continue
        a, b = int(a), int(b)
        if not (-2 <= a <= 8 and -2 <= b <= 56):
            continue
        kept.append((b, a, x, y, h, start, html))
    if len(kept) < MIN_CELLS:
        return None

    b_min = min(b for b, *_ in kept)
    return [(b - b_min, a, x, y, h, start, html) for b, a, x, y, h, start, html in kept]


def strip_keyframes(s):
    out = []
    i = 0
    while True:
        j = s.find("@keyframes", i)
        if j < 0:
            out.append(s[i:])
            break
        k = s.find("{", j)
        if k < 0:
            out.append(s[i:j])
            break
        depth = 0
        m = k
        while m < len(s):
            if s[m] == "{":
                depth += 1
            elif s[m] == "}":
                depth -= 1
                if depth == 0:
                    break
            m += 1
        out.append(s[i:j])
        i = m + 1
    return "".join(out)


def process(path, weeks, theme):
    """Returns an error string, or None on success."""
    svg = path.read_text(encoding="utf-8")

    style_m = re.search(r"<style>.*?</style>", svg, re.S)
    if not style_m:
        return "no <style> block found"
    style = style_m.group(0)

    bg_m = re.search(r"\.fill-bg\s*\{\s*fill:\s*([^;}]+)", style)
    dark = (not bg_m) or luminance(bg_m.group(1).strip()) < 128

    # side-face y-scale (1.15 for most styles, 0.65 for gitblock)
    s_m = SIDESCALE_RE.search(svg)
    if not s_m:
        return "no side face found"
    s = float(s_m.group(1))

    cells = []
    for g in CELL_RE.finditer(svg):
        hm = HEIGHT_RE.search(g.group(3))
        if not hm:
            return "cell without a side face"
        cells.append((float(g.group(1)), float(g.group(2)), float(hm.group(1)), g.start(), g.group(0)))

    # flat (level-0) height: the modal height across cells
    from collections import Counter
    h_flat = Counter(h for _x, _y, h, _s, _html in cells).most_common(1)[0][0]

    kept = fit_lattice(cells, s, h_flat)
    if kept is None:
        return "lattice fit failed (%d cell-like groups)" % len(cells)

    b_max = max(b for b, *_ in kept)
    b_min = max(0, b_max - weeks + 1)
    window = [c for c in kept if c[0] >= b_min]
    if len(window) < 20:
        return "empty window (kept %d of %d cells)" % (len(window), len(kept))

    # bounding box of the window; a cell spans x..x+36 and
    # y-10.4 .. y+max(21, 10.39+s*h) (top face diamond plus side faces)
    L = min(x for _, _, x, *_ in window) - MARGIN
    R = max(x + 36.1 for _, _, x, *_ in window) + MARGIN
    T = min(y - 10.4 for _, _, _, y, *_ in window) - MARGIN
    B = max(y + max(21.0, 10.39 + s * h) for _, _, _, y, h, *_ in window) + MARGIN
    w, hgt = R - L, B - T

    # themed style: static fills for level/face classes, keyframes removed.
    # Two class schemes exist: "xx-lN-face" (most styles) and
    # "cont-face-bg|fg-N" (gitblock's pattern fills).
    l0 = L0_DARK if dark else L0_LIGHT

    def repl(cm):
        prefix, level, face = cm.group(1), int(cm.group(2)), cm.group(3)
        col = l0[face] if level == 0 else shade(theme[level], FACE_SHADE[face])
        return ".%s-l%d-%s{fill:%s;}" % (prefix, level, face, rgb_css(col))

    def repl_cont(cm):
        face, role, level = cm.group(1), cm.group(2), int(cm.group(3))
        bg = l0[face] if level == 0 else shade(theme[level], FACE_SHADE[face])
        col = shade(bg, 0.62) if role == "fg" else bg
        return ".cont-%s-%s-%d{fill:%s;}" % (face, role, level, rgb_css(col))

    new_style = strip_keyframes(
        CONT_CLASS_RE.sub(repl_cont, LEVEL_CLASS_RE.sub(repl, style))
    )

    cells_html = [
        re.sub(r' style="animation-delay:[\d.]+s"', "", html)
        for *_, start, html in sorted(window, key=lambda c: c[5])
    ]

    # date range + total, parsed from the original header/footer
    dm = re.search(r">(\d{4}-\d{2}-\d{2})\s*/\s*(\d{4}-\d{2}-\d{2})<", svg)
    total_m = re.search(r'class="fill-strong">(\d+)</text>', svg)
    label = ""
    if dm:
        try:
            yy, mm, dd = (int(v) for v in dm.group(2).split("-"))
            end = date(yy, mm, dd)
            sunday = end - timedelta(days=(end.weekday() + 1) % 7)
            start_sun = sunday - timedelta(days=7 * (weeks - 1))
            label = start_sun.isoformat() + " \u2192 " + end.isoformat()
            if total_m:
                label += "  \u00b7  " + total_m.group(1) + " contributions"
        except ValueError:
            label = ""
    label_html = ""
    if label:
        label_html = (
            '<text x="%.1f" y="%.1f" text-anchor="end" font-size="15" fill="%s">%s</text>'
            % (R - 8, T + 26, "#8b949e" if dark else "#57606a", label)
        )

    defs_m = re.search(r"<defs>.*?</defs>", svg, re.S)
    defs = defs_m.group(0) if defs_m else ""

    out = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="%d" '
        'viewBox="%.2f %.2f %.2f %.2f">%s%s'
        '<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" class="fill-bg"></rect>%s%s</svg>'
        % (
            round(1280 * hgt / w), L, T, w, hgt,
            new_style, defs, L, T, w, hgt,
            "".join(cells_html), label_html,
        )
    )
    path.write_text(out, encoding="utf-8")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=27)
    ap.add_argument("--theme", choices=sorted(THEMES), default="green")
    args = ap.parse_args()

    theme = THEMES[args.theme]
    failed = False
    for path in sorted(Path("profile-3d-contrib").glob("*.svg")):
        try:
            err = process(path, args.weeks, theme)
        except Exception as exc:  # keep going, but fail loudly at the end
            err = "exception: %s" % exc
        if err:
            failed = True
            print("  ! %s: %s (left untouched)" % (path.name, err))
        else:
            print("  + %s" % path.name)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
