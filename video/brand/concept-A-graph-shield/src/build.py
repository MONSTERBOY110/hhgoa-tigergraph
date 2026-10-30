"""Build the concept A (graph shield) brand files from logo.svg.

Writes self-contained HTML pages into src/ and renders them to PNG with headless Edge:
  avatar-400.png             400 x 400, ink ground (X crops it to a circle)
  avatar-400-paper.png       400 x 400, paper ground (alternate)
  header-1500x500.png        1500 x 500 X banner, paper ground (pairs with the ink avatar)
  header-1500x500-ink.png    1500 x 500 X banner, ink ground (alternate; pairs with the paper avatar)
  preview-48.png             both avatars at 48 px in a circle on white and on black

Run: python build.py   (from any folder; paths are resolved from this file). Needs network for
Google Fonts (Oswald 500/700, IBM Plex Mono 500); KAVACH_PATTERN_SEED picks the banner pattern.
"""
import math
import os
import random
import re
import subprocess
from pathlib import Path

SRC = Path(__file__).resolve().parent
OUT = SRC.parent
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE = r"C:\Users\MONSTER\AppData\Local\Temp\edge-A-graph-shield"

INK, PAPER, ACCENT, GREY_ON_PAPER, GREY_ON_DARK = "#141A21", "#EEF0F3", "#C2491A", "#6B7684", "#9AA5B3"
FONTS = ('<link href="https://fonts.googleapis.com/css2?family=Oswald:wght@500;700'
         '&family=IBM+Plex+Mono:wght@500&display=swap" rel="stylesheet">')

# The mark's inner markup, read from logo.svg so every file uses the same drawing.
svg_text = (OUT / "logo.svg").read_text(encoding="utf-8")
MARK = re.search(r"<svg[^>]*>(.*)</svg>", svg_text, re.S).group(1)
MARK = re.sub(r"<title.*?</title>", "", MARK, flags=re.S)
MARK = re.sub(r"<!--.*?-->", "", MARK, flags=re.S).strip()

# Shield geometry in logo units: stroked extents x 4..116, y 2..138; the point that sits on the
# circle centre balances the clearance of the top corners and the tip (area centroid is y 65.9).
SHIELD_H = 136.0
CIRCLE_CY = 67.0
AVATAR_SHARE = 0.72          # shield height as a share of the avatar circle
PATTERN_SEED = int(os.environ.get("KAVACH_PATTERN_SEED", "5"))


def page(width, height, body, extra_css=""):
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width={width}">
<title>Kavach</title>
{FONTS}
<style>
html, body {{ margin: 0; padding: 0; width: {width}px; height: {height}px; overflow: hidden; }}
{extra_css}
</style></head>
<body>
{body}
</body></html>
"""


def avatar_html(ground, share=AVATAR_SHARE):
    """The mark centred in a 400 x 400 square; the shield's height is `share` of the circle."""
    units = SHIELD_H / share  # width of the square in logo units
    vb = f"{60 - units / 2:.3f} {CIRCLE_CY - units / 2:.3f} {units:.3f} {units:.3f}"
    body = (f'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="400" viewBox="{vb}" '
            f'style="display:block">\n<rect x="-200" y="-200" width="600" height="600" fill="{ground}"/>\n'
            f'{MARK}\n</svg>')
    return page(400, 400, body, f"body {{ background: {ground}; }}")


def pattern(exclude, seed=PATTERN_SEED, W=1500, H=500):
    """A sparse, faint graph: Poisson-disc nodes, each joined to its nearest neighbours.

    Deterministic (seeded). Nodes stay out of the lockup box, edges never cross it, and edges
    that would run along a border are dropped so the pattern does not read as a frame.
    """
    rng = random.Random(seed)
    lock_left = min(x0 for x0, _, _, _ in exclude)

    def keep(x):  # density thins from the open left side toward the lockup, none beyond it
        if x < lock_left - 260:
            return 1.0
        if x < lock_left:
            return 1.0 - 0.8 * (x - (lock_left - 260)) / 260
        return 0.0

    pts = []
    tries = 0
    while tries < 8000 and len(pts) < 60:
        tries += 1
        p = (rng.uniform(-10, W + 10), rng.uniform(-10, H + 10))
        if any(x0 <= p[0] <= x1 and y0 <= p[1] <= y1 for x0, y0, x1, y1 in exclude):
            continue
        if rng.random() > keep(p[0]):
            continue
        if all(math.dist(p, q) > 128 for q in pts):
            pts.append(p)

    def crosses(a, b):
        for x0, y0, x1, y1 in exclude:
            for t in (i / 24 for i in range(25)):
                x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
                if x0 <= x <= x1 and y0 <= y <= y1:
                    return True
        return False

    def hugs_border(a, b, m=40):
        return ((a[1] < m and b[1] < m) or (a[1] > H - m and b[1] > H - m)
                or (a[0] < m and b[0] < m) or (a[0] > W - m and b[0] > W - m))

    edges = set()
    for i, p in enumerate(pts):
        near = sorted((math.dist(p, q), j) for j, q in enumerate(pts) if j != i)
        for d, j in near[:2]:
            if d < 200 and not crosses(p, pts[j]) and not hugs_border(p, pts[j]):
                edges.add(tuple(sorted((i, j))))
    degree = [0] * len(pts)
    for i, j in edges:
        degree[i] += 1
        degree[j] += 1
    lines = "\n".join(
        f'<line x1="{pts[i][0]:.1f}" y1="{pts[i][1]:.1f}" x2="{pts[j][0]:.1f}" y2="{pts[j][1]:.1f}"/>'
        for i, j in sorted(edges))
    dots = "\n".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{7 if degree[k] >= 3 else 5}"/>'
                     for k, (x, y) in enumerate(pts) if degree[k] > 0)
    return lines, dots


HEADER_COLOURS = {
    # ground, pattern edge, pattern node, wordmark, tagline, built-on line
    "paper": (PAPER, "#DCE0E5", "#CED4DB", INK, INK, ACCENT),
    "ink": (INK, "#212932", "#2B343E", PAPER, GREY_ON_DARK, "#F08A52"),
}


def header_html(ground="paper"):
    bg, edge_c, node_c, word_c, tag_c, built_c = HEADER_COLOURS[ground]
    W, H = 1500, 500
    F = 210                      # KAVACH font size
    cap = 0.81 * F               # Oswald cap height
    ink_w = 3.177 * F            # KAVACH ink width at letter-spacing 0
    lsb = 0.05 * F               # left side bearing of K
    s = 0.8 * cap / SHIELD_H     # shield scale: 80 percent of the cap height
    gap = 0.12 * F
    lock_w = ink_w + gap + 112 * s
    x0 = 450 + (1050 - lock_w) / 2   # centred in the area right of the avatar
    tag_size = lock_w / 17.68        # tagline spans the full lockup (Oswald 500 advance 17.68 em)
    tag_cap = 0.81 * tag_size
    mono = 26
    mono_cap = 0.70 * mono
    g1, g2 = 42, 34
    total = cap + g1 + tag_cap + g2 + mono_cap
    top = (H - total) / 2
    base = top + cap
    tag_base = base + g1 + tag_cap
    mono_base = tag_base + g2 + mono_cap
    shield_x = x0 + ink_w + gap - 4 * s          # stroked left edge sits at the gap
    shield_y = base + 4 - 138 * s                # tip overshoots the baseline by 4 px
    pad = 56
    exclude = [(x0 - pad, top - pad, x0 + lock_w + pad, mono_base + pad)]
    lines, dots = pattern(exclude)
    body = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" style="display:block">
<rect width="{W}" height="{H}" fill="{bg}"/>
<g stroke="{edge_c}" stroke-width="2.5">
{lines}
</g>
<g fill="{node_c}">
{dots}
</g>
<text x="{x0 - lsb:.1f}" y="{base:.1f}" font-family="Oswald" font-weight="700" font-size="{F}" fill="{word_c}">KAVACH</text>
<g transform="translate({shield_x:.2f} {shield_y:.2f}) scale({s:.4f})">
{MARK}
</g>
<text x="{x0 - 0.01 * tag_size:.1f}" y="{tag_base:.1f}" font-family="Oswald" font-weight="500" font-size="{tag_size:.2f}" fill="{tag_c}">AN AI FRAUD INVESTIGATOR A BANK CAN RUN</text>
<text x="{x0:.1f}" y="{mono_base:.1f}" font-family="IBM Plex Mono" font-weight="500" font-size="{mono}" letter-spacing="{0.14 * mono:.2f}" fill="{built_c}">BUILT ON TIGERGRAPH</text>
</svg>"""
    return page(W, H, body, f"body {{ background: {bg}; }}")


def preview_html():
    css = f"""
body {{ background: #ffffff; font-family: 'IBM Plex Mono', monospace; }}
.sheet {{ display: flex; width: 480px; height: 160px; }}
.panel {{ flex: 1; display: flex; flex-direction: column; justify-content: center; gap: 14px; padding: 0 22px; }}
.light {{ background: #ffffff; color: #0f1419; }}
.dark {{ background: #000000; color: #e7e9ea; }}
.row {{ display: flex; align-items: center; gap: 10px; }}
.av {{ width: 48px; height: 48px; border-radius: 50%; display: block; flex: none; }}
.name {{ font: 700 15px/1.2 system-ui, 'Segoe UI', sans-serif; }}
.handle {{ font: 400 13px/1.2 system-ui, 'Segoe UI', sans-serif; opacity: 0.6; }}
.label {{ font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; opacity: 0.55; }}
"""
    def panel(cls, label):
        return (f'<div class="panel {cls}"><div class="label">{label}</div>'
                f'<div class="row"><img class="av" src="../avatar-400.png" alt="">'
                f'<div><div class="name">Kavach</div><div class="handle">@kavachhhgoa</div></div></div>'
                f'<div class="row"><img class="av" src="../avatar-400-paper.png" alt="">'
                f'<div><div class="name">Kavach</div><div class="handle">paper ground (alternate)</div></div></div></div>')
    body = f'<div class="sheet">{panel("light", "48 px on white")}{panel("dark", "48 px on black")}</div>'
    return page(480, 160, body, css)


def render(html_name, png_name, w, h):
    html = SRC / html_name
    png = OUT / png_name
    # grayscale text antialiasing (no LCD colour fringes) and exact sRGB brand colours
    subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--disable-lcd-text", "--force-color-profile=srgb",
                    f"--user-data-dir={PROFILE}", f"--window-size={w},{h}",
                    "--virtual-time-budget=4000", f"--screenshot={png}", html.as_uri()],
                   check=True, capture_output=True)
    print("rendered", png)


def main():
    (SRC / "avatar.html").write_text(avatar_html(INK), encoding="utf-8")
    (SRC / "avatar-paper.html").write_text(avatar_html(PAPER), encoding="utf-8")
    (SRC / "header.html").write_text(header_html("paper"), encoding="utf-8")
    (SRC / "header-ink.html").write_text(header_html("ink"), encoding="utf-8")
    (SRC / "preview.html").write_text(preview_html(), encoding="utf-8")
    render("avatar.html", "avatar-400.png", 400, 400)
    render("avatar-paper.html", "avatar-400-paper.png", 400, 400)
    render("header.html", "header-1500x500.png", 1500, 500)
    render("header-ink.html", "header-1500x500-ink.png", 1500, 500)
    render("preview.html", "preview-48.png", 480, 160)


if __name__ == "__main__":
    main()
