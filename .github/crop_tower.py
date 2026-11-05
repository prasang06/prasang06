#!/usr/bin/env python3
"""Post-process github-profile-3d-contrib output into a "Contribution skyline".

- crops the isometric tower grid to the most recent N weeks
- themes it:
    purple  -> smooth height-driven gradient, deep purple canvas,
               subtle peak bob with an animated gradient
    green / violet -> discrete level colors (no oscillation)
- lays out a title, legend and stats block beside the grid

Run in .github/workflows/profile-3d.yml right after
yoshi389111/github-profile-3d-contrib and before the commit step.

Usage:
    python3 crop_tower.py --weeks 27 --theme purple
"""
import argparse
import math
import re
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

# discrete level palettes (fallback + pattern-based styles like gitblock)
THEMES = {
    "green": {1: (35, 134, 54), 2: (46, 160, 67), 3: (63, 185, 80), 4: (86, 211, 100)},
    "violet": {1: (94, 64, 152), 2: (121, 85, 196), 3: (137, 87, 229), 4: (170, 127, 243)},
    "purple": {1: (85, 70, 127), 2: (122, 95, 192), 3: (157, 127, 224), 4: (211, 192, 247)},
}

# purple: continuous gradient stops (u, top-face rgb)
PURPLE_STOPS_DARK = [
    (0.00, (61, 53, 96)),
    (0.25, (85, 70, 127)),
    (0.50, (122, 95, 192)),
    (0.75, (157, 127, 224)),
    (1.00, (211, 192, 247)),
]
PURPLE_STOPS_LIGHT = [
    (0.00, (207, 197, 232)),
    (0.25, (179, 162, 221)),
    (0.50, (146, 117, 212)),
    (0.75, (122, 85, 200)),
    (1.00, (91, 53, 176)),
]

FACE_SHADE = {"top": 1.0, "left": 0.80, "right": 0.62}

SKYLINE = {
    "dark": {
        "bg": (23, 17, 48),
        "bg2": (38, 26, 78),
        "halo": (110, 76, 200),
        "title": (236, 231, 248),
        "muted": (139, 127, 184),
        "faint": (92, 79, 134),
        "line": (55, 48, 90),
    },
    "light": {
        "bg": (242, 237, 251),
        "bg2": (226, 214, 249),
        "halo": (196, 172, 244),
        "title": (42, 33, 69),
        "muted": (122, 111, 160),
        "faint": (162, 148, 200),
        "line": (213, 205, 236),
    },
}

L0_FALLBACK = {
    "dark": {"top": (27, 35, 48), "left": (20, 26, 37), "right": (14, 19, 28)},
    "light": {"top": (230, 235, 241), "left": (213, 219, 227), "right": (195, 202, 211)},
}

MARGIN = 14
MIN_CELLS = 200

CELL_RE = re.compile(r'<g transform="translate\(([\d.]+) ([\d.]+)\)">(.*?)</g>', re.S)
HEIGHT_RE = re.compile(r'height="([\d.]+)" transform="skewY\(30\)')
SIDESCALE_RE = re.compile(r'transform="skewY\(30\) scale\([\d.]+ ([\d.]+)\)"')
LEVEL_CLASS_RE = re.compile(r"\.([a-z0-9]+)-l([0-4])-(top|left|right)\s*\{[^}]*\}")
# cont-face-bg|fg|p<period>-level (gitblock, season) and cont-face-level (green)
CONT_CLASS_RE = re.compile(
    r"\.cont-(?P<face>top|left|right)-(?P<role>bg|fg|p[0-9]+)-(?P<level>[0-4])\s*\{[^}]*\}"
    r"|\.cont-(?P<face2>top|left|right)-(?P<level2>[0-4])\s*\{[^}]*\}"
)
DATES_RE = re.compile(r">(\d{4}-\d{2}-\d{2})\s*/\s*(\d{4}-\d{2}-\d{2})<")
TOTAL_RE = re.compile(r'class="fill-strong">(\d+)</text>')


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


def lerp_color(stops, u):
    u = min(1.0, max(0.0, u))
    for (u0, c0), (u1, c1) in zip(stops, stops[1:]):
        if u0 <= u <= u1:
            t = (u - u0) / (u1 - u0) if u1 > u0 else 0
            return tuple(round(a + (b - a) * t) for a, b in zip(c0, c1))
    return stops[-1][1]


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


def fit_lattice(cells, s, h_flat):
    """Locate the floor lattice and assign (a=day, b=week) to grid cells.

    A cell's static origin sits on the floor lattice only when flat:
    contributed cubes are bottom-aligned, so their origin is raised by
    s*(h_flat - h). Flat cells anchor the lattice.

    cells: list of (x, y, h, start_offset, html).
    Returns a list of (b, a, x, y, h, start_offset, html) with the raw
    week index b, or None when the layout is not recognized.
    """
    x0 = cells[0][0]
    flat = [c for c in cells if abs(c[2] - h_flat) < 0.05]
    if len(flat) < 50:
        return None

    # dy (floor row step, ~11.55): within one column, consecutive flat
    # rows differ by 2 in (a+b) (parity), so the smallest within-column
    # y gap is 2*dy
    cols = {}
    for x, y, h, _s, _html in flat:
        ba = (x - x0) / 20
        if abs(ba - round(ba)) < 0.1:
            cols.setdefault(round(ba), []).append(y)
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
    return kept


OSC_DUR, OSC_BEGIN = 2.6, 0.0


def mix(c0, c1, t):
    return tuple(round(a + (b - a) * t) for a, b in zip(c0, c1))


def osc_anim(attr, values, kt):
    """Infinite animation on the shared bob clock."""
    return ('<animate attributeName="%s" values="%s" keyTimes="%s" dur="%.1fs" '
            'begin="%.1fs" repeatCount="indefinite"/>' % (attr, values, kt, OSC_DUR, OSC_BEGIN))


def osc_values(amp, phase, n=16):
    """Sampled sine (0 -> amp -> 0) starting at the given phase (0..1),
    so every tower shares one clock but sits at its own point in it."""
    vals = [amp * (1 - math.cos(2 * math.pi * (k / n + phase))) / 2 for k in range(n + 1)]
    return vals, ";".join("%.4f" % (k / n) for k in range(n + 1))


def rebuild_cell(html, rgb_faces, grads, osc, top_anim=""):
    """Swap a cell's level classes for inline fills / gradients and add a
    subtle peak bob: the top face rises by amp while the side faces grow
    to match, so the base stays planted. The generator's growth
    animation is dropped: towers render at full height from the start."""
    head_m = re.match(r'<g transform="translate\([^"]*\)">', html)
    head = head_m.group(0)
    inner = html[len(head): -len("</g>")]
    inner = re.sub(r"<animateTransform\b[^>]*/>|<animateTransform\b[^>]*>\s*</animateTransform>", "", inner)
    inner = re.sub(r"<animate\b[^>]*/>|<animate\b[^>]*>\s*</animate>", "", inner)
    rects = re.findall(r"<rect\b[^>]*>", inner)
    if rgb_faces is not None:
        for i, (old, col) in enumerate(zip(rects[:3], rgb_faces)):
            base = re.sub(r'\sclass="[^"]*"|\sstyle="[^"]*"', "", old)
            fill = "url(#%s)" % grads[i - 1] if grads and i > 0 else rgb_css(col)
            new = base[:-1] + ' fill="%s">' % fill
            if i == 0 and top_anim:
                new += top_anim
            inner = inner.replace(old, new, 1)
    if osc:
        amp, s, phase = osc
        ty, kt = osc_values(amp, phase)

        def side(m):
            h = float(m.group(1))
            vals = ";".join("%.3f" % (h + v / s) for v in ty)
            anim = osc_anim("height", vals, kt)
            return m.group(0).replace("</rect>", anim + "</rect>")

        inner = re.sub(r'<rect\b[^>]*height="([\d.]+)" transform="(?:translate\([^)]*\) )?skewY\((?:30|-30)\) scale[^>]*>.*?</rect>',
                       side, inner, flags=re.S)
        bob = ('<animateTransform attributeName="transform" type="translate" values="%s" keyTimes="%s" '
               'dur="%.1fs" begin="%.1fs" repeatCount="indefinite"/>'
               % (";".join("0 %.3f" % -v for v in ty), kt, OSC_DUR, OSC_BEGIN))
        inner = "<g>" + bob + inner + "</g>"
    return head + inner + "</g>"


def cell_level(html):
    """Contribution level of a cell from its top-face class or pattern id."""
    m = re.search(r'fill="url\(#pattern_([0-4])_', html)
    if m:
        return int(m.group(1))
    m = re.search(r'class="[^"]*-l([0-4])-top"', html)
    if m:
        return int(m.group(1))
    m = re.search(r'class="cont-top-(?:p[0-9]+-)?([0-4])"', html)
    if m:
        return int(m.group(1))
    return 0


def process(path, weeks, theme):
    """Returns an error string, or None on success."""
    svg = path.read_text(encoding="utf-8")

    style_m = re.search(r"<style>.*?</style>", svg, re.S)
    if not style_m:
        return "no <style> block found"
    style = style_m.group(0)

    bg_m = re.search(r"\.fill-bg\s*\{\s*fill:\s*([^;}]+)", style)
    dark = (not bg_m) or luminance(bg_m.group(1).strip()) < 128
    variant = "dark" if dark else "light"

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

    h_flat = Counter(h for _x, _y, h, _s, _html in cells).most_common(1)[0][0]
    kept = fit_lattice(cells, s, h_flat)
    if kept is None:
        return "lattice fit failed (%d cell-like groups)" % len(cells)

    b_max = max(b for b, *_ in kept)
    b_min = max(min(b for b, *_ in kept), b_max - weeks + 1)
    window = [c for c in kept if c[0] >= b_min]
    if len(window) < 20:
        return "empty window (kept %d of %d cells)" % (len(window), len(kept))

    is_pattern = 'fill="url(#pattern' in window[0][6]

    # colors
    if theme == "purple":
        palette = SKYLINE[variant]
        stops = PURPLE_STOPS_DARK if dark else PURPLE_STOPS_LIGHT
        bg = palette["bg"]
        h_max_win = max(h for *_x, _y, h, _s, _html in [(c[0], c[1], c[2], c[3], c[4], c[5], c[6]) for c in window])
        h_range = max(0.05, h_max_win - h_flat)
        oscillate = True
    else:
        palette = None
        stops = None
        bg = None
        h_max_win = None
        h_range = 1.0
        oscillate = False

    def faces_for(level, h):
        if theme == "purple" and not is_pattern:
            u = 0.0 if abs(h - h_flat) < 0.05 else math.sqrt(min(1.0, (h - h_flat) / h_range))
            base = lerp_color(stops, u)
            return tuple(shade(base, FACE_SHADE[f]) for f in ("top", "left", "right")), u
        l0 = L0_FALLBACK[variant]
        base = l0["top"] if level == 0 else THEMES[theme][level]
        return tuple(shade(base, FACE_SHADE[f]) for f in ("top", "left", "right")), 0.0

    # rebuild cells
    cells_html = []
    grad_defs = []
    floor = lerp_color(stops, 0.0) if stops else None
    glow = (228, 212, 255) if dark else (stops[-1][1] if stops else None)
    for b, a, x, y, h, _start, html in sorted(window, key=lambda c: c[5]):
        level = cell_level(html)
        u = 0.0
        rgb_faces = None
        grads = None
        top_anim = ""
        osc = None
        if oscillate and h > h_flat + 0.05:
            # small relative to the tower: ~6% of its height, never < 1.8 units
            amp = max(1.8, 0.06 * s * h)
            phase = ((a + b) * 0.09) % 1.0
            osc = (amp, s, phase)
        if theme == "purple" and not is_pattern:
            rgb_faces, u = faces_for(level, h)
            if osc:
                # animated gradient on the bob clock: as the peak rises it
                # brightens toward `glow` and the floor color climbs the
                # sides, so the light seems to well up through the tower
                wave, kt = osc_values(1.0, osc[2])
                peak = lerp_color(stops, u)
                lit = mix(peak, glow, 0.4)
                top_anim = osc_anim("fill", ";".join(
                    rgb_css(shade(mix(peak, lit, w), FACE_SHADE["top"])) for w in wave), kt)
                grads = []
                for face in ("left", "right"):
                    gid = "tw-%d-%d-%s" % (b, a, face[0])
                    grads.append(gid)
                    f = FACE_SHADE[face]
                    grad_defs.append(
                        '<linearGradient id="%s" x1="0" y1="1" x2="0" y2="0">'
                        '<stop offset="0" stop-color="%s">%s</stop>'
                        '<stop offset="1" stop-color="%s">%s</stop></linearGradient>'
                        % (gid, rgb_css(shade(floor, f)),
                           osc_anim("offset", ";".join("%.3f" % (0.4 * w) for w in wave), kt),
                           rgb_css(shade(peak, f)),
                           osc_anim("stop-color", ";".join(rgb_css(shade(mix(peak, lit, w), f)) for w in wave), kt))
                    )
        cells_html.append(rebuild_cell(html, rgb_faces, grads, osc, top_anim))

    # style
    new_style = strip_keyframes(style)
    if theme == "purple":
        new_style = re.sub(r"(\.fill-bg\s*\{\s*fill:\s*)[^;}]+", r"\g<1>%s;" % rgb_css(bg), new_style)
        if is_pattern:
            l0 = L0_FALLBACK[variant]

            def repl_cont(cm):
                face = cm.group("face") or cm.group("face2")
                level = int(cm.group("level") or cm.group("level2"))
                role = cm.group("role") or "bg"
                base = l0[face] if level == 0 else shade(THEMES["purple"][level], FACE_SHADE[face])
                col = shade(base, 0.62) if role == "fg" else base
                return re.sub(r"\{[^}]*\}", "{fill:%s;}" % rgb_css(col), cm.group(0))

            new_style = CONT_CLASS_RE.sub(repl_cont, new_style)
        else:
            new_style = CONT_CLASS_RE.sub("", LEVEL_CLASS_RE.sub("", new_style))
    else:
        l0 = L0_FALLBACK[variant]

        def repl(cm):
            prefix, level, face = cm.group(1), int(cm.group(2)), cm.group(3)
            col = l0[face] if level == 0 else shade(THEMES[theme][level], FACE_SHADE[face])
            return ".%s-l%d-%s{fill:%s;}" % (prefix, level, face, rgb_css(col))

        def repl_cont(cm):
            face = cm.group("face") or cm.group("face2")
            level = int(cm.group("level") or cm.group("level2"))
            role = cm.group("role") or "bg"
            base = l0[face] if level == 0 else shade(THEMES[theme][level], FACE_SHADE[face])
            col = shade(base, 0.62) if role == "fg" else base
            return re.sub(r"\{[^}]*\}", "{fill:%s;}" % rgb_css(col), cm.group(0))

        new_style = CONT_CLASS_RE.sub(repl_cont, LEVEL_CLASS_RE.sub(repl, new_style))

    # grid bounding box
    L = min(x for _, _, x, *_ in window) - MARGIN
    R = max(x + 36.1 for _, _, x, *_ in window) + MARGIN
    T = min(y - 10.4 for _, _, _, y, *_ in window) - MARGIN
    B = max(y + max(21.0, 10.39 + s * h) for _, _, _, y, h, *_ in window) + MARGIN
    grid_w, grid_h = R - L, B - T

    # canvas layout: text column left, grid right
    canvas_w = 1104
    grid_x0 = 400
    scale = (canvas_w - grid_x0 - 24) / grid_w
    canvas_h = max(500, grid_h * scale + 56)
    gy = (canvas_h - grid_h * scale) / 2
    tx = grid_x0 - L * scale
    ty = gy - T * scale

    # stats (all exact)
    total_m = TOTAL_RE.search(svg)
    total = int(total_m.group(1)) if total_m else 0
    active = sum(1 for c in window if c[4] > h_flat + 0.05)
    pos = set(7 * b + a for b, a, _x, _y, h, _s, _html in kept if h > h_flat + 0.05)
    streak = 0
    if pos:
        d = max(pos)
        while d in pos:
            streak += 1
            d -= 1

    dates_m = DATES_RE.search(svg)
    year, date_label = "—", ""
    if dates_m:
        try:
            sy, sm, sd = (int(v) for v in dates_m.group(1).split("-"))
            ey, em, ed = (int(v) for v in dates_m.group(2).split("-"))
            end = date(ey, em, ed)
            year = str(ey)
            sunday = end - timedelta(days=(end.weekday() + 1) % 7)
            start_sun = sunday - timedelta(days=7 * (weeks - 1))
            date_label = start_sun.isoformat() + " \u2192 " + end.isoformat()
        except ValueError:
            pass

    # text block
    tc = palette or SKYLINE[variant]
    t = tc["title"]
    mu = tc["muted"]
    fa = tc["faint"]
    ln = tc["line"]
    font = 'font-family="Ubuntu, Helvetica, Arial, sans-serif"'
    text_block = (
        '<text x="64" y="88" %s font-size="34" font-weight="bold" fill="%s">Contribution skyline</text>'
        '<text x="64" y="116" %s font-size="12" letter-spacing="2.5" fill="%s">%s \u00b7 RENDERED NIGHTLY</text>'
        '<text x="64" y="138" %s font-size="12" fill="%s">%s</text>'
        '<line x1="64" y1="162" x2="310" y2="162" stroke="%s" stroke-width="1"/>'
        '<text x="64" y="190" %s font-size="11" letter-spacing="2" fill="%s">COMMITS PER DAY</text>'
        '<rect x="64" y="202" width="150" height="12" rx="2" fill="url(#lg-grad)"/>'
        '<text x="64" y="230" %s font-size="11" fill="%s">0</text>'
        '<text x="214" y="230" %s font-size="11" text-anchor="end" fill="%s">peak</text>'
        '<line x1="64" y1="254" x2="310" y2="254" stroke="%s" stroke-width="1"/>'
        '<text x="64" y="284" %s font-size="11" letter-spacing="2" fill="%s">TOTAL</text>'
        '<text x="160" y="284" %s font-size="11" letter-spacing="2" fill="%s">ACTIVE</text>'
        '<text x="252" y="284" %s font-size="11" letter-spacing="2" fill="%s">STREAK</text>'
        '<text x="64" y="314" %s font-size="22" font-weight="bold" fill="%s">%s</text>'
        '<text x="160" y="314" %s font-size="22" font-weight="bold" fill="%s">%s</text>'
        '<text x="252" y="314" %s font-size="22" font-weight="bold" fill="%s">%sd</text>'
    ) % (
        font, rgb_css(t), font, rgb_css(mu), year, font, rgb_css(fa), date_label, rgb_css(ln),
        font, rgb_css(mu), font, rgb_css(fa), font, rgb_css(fa), rgb_css(ln),
        font, rgb_css(mu), font, rgb_css(mu), font, rgb_css(mu),
        font, rgb_css(t), format(total, ","), font, rgb_css(t), active, font, rgb_css(t), streak,
    )
    # legend gradient
    if stops:
        grad_stops = "".join(
            '<stop offset="%d%%" stop-color="%s"/>' % (round(u * 100), rgb_css(c)) for u, c in stops
        )
    else:
        l0 = L0_FALLBACK[variant]
        grad_stops = "".join(
            '<stop offset="%d%%" stop-color="%s"/>'
            % (i * 25, rgb_css(l0["top"] if i == 0 else THEMES[theme][i]))
            for i in range(5)
        )
    defs_m = re.search(r"<defs>.*?</defs>", svg, re.S)
    defs = defs_m.group(0) if defs_m else ""
    defs += (
        '<defs><linearGradient id="lg-grad" x1="0" y1="0" x2="1" y2="0">%s</linearGradient>%s</defs>'
        % (grad_stops, "".join(grad_defs))
    )

    if bg:
        # diagonal wash plus a soft halo behind the grid
        defs += (
            '<defs><linearGradient id="bg-grad" x1="0" y1="0" x2="1" y2="1">'
            '<stop offset="0" stop-color="%s"/><stop offset="1" stop-color="%s"/></linearGradient>'
            '<radialGradient id="bg-halo" cx="0.66" cy="0.55" r="0.55">'
            '<stop offset="0" stop-color="%s" stop-opacity="0.22"/>'
            '<stop offset="1" stop-color="%s" stop-opacity="0"/></radialGradient></defs>'
            % (rgb_css(bg), rgb_css(palette["bg2"]), rgb_css(palette["halo"]), rgb_css(palette["halo"]))
        )
        bg_rect = (
            '<rect x="0" y="0" width="%.0f" height="%.0f" fill="url(#bg-grad)"></rect>'
            '<rect x="0" y="0" width="%.0f" height="%.0f" fill="url(#bg-halo)"></rect>'
        ) % (canvas_w, canvas_h, canvas_w, canvas_h)
    else:
        bg_rect = '<rect x="0" y="0" width="%.0f" height="%.0f" class="fill-bg"></rect>' % (canvas_w, canvas_h)

    out = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="%d" viewBox="0 0 %.0f %.0f">%s%s%s%s'
        '<g transform="translate(%.2f %.2f) scale(%.4f)">%s</g></svg>'
        % (
            round(1280 * canvas_h / canvas_w), canvas_w, canvas_h,
            new_style, defs, bg_rect, text_block,
            tx, ty, scale, "".join(cells_html),
        )
    )
    path.write_text(out, encoding="utf-8")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=27)
    ap.add_argument("--theme", choices=sorted(THEMES), default="purple")
    args = ap.parse_args()

    failed = False
    for path in sorted(Path("profile-3d-contrib").glob("*.svg")):
        try:
            err = process(path, args.weeks, args.theme)
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
