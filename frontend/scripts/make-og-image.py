# -*- coding: utf-8 -*-
"""
Generate frontend/public/og-image.png (1200x630) for AmpHive.

Everything on the canvas is either (a) a real shipped brand asset
(frontend/public/icon-512.png, the app icon), (b) a real brand colour from
frontend/src/styles/tokens.css, or (c) real copy from
frontend/src/utils/legal.js.  No invented screenshots, logos or statistics.

Fonts are the app's own self-hosted @fontsource families -- the same ones the
site ships, since the production CSP blocks font CDNs -- converted woff2 ->
ttf with fonttools because Pillow cannot rasterise woff2 directly.

Re-run it when the brand or the one-line description changes:

    cd frontend && npm install            # provides the @fontsource files
    pip install pillow fonttools brotli
    python frontend/scripts/make-og-image.py

The output is COMMITTED as frontend/public/og-image.png (referenced by the
og:image / twitter:image tags and the JSON-LD in frontend/index.html), so an
ordinary `npm run build` needs none of these dependencies. This script is
kept in the repo so the image is reproducible rather than a mystery binary.
"""
import os
import tempfile
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# Paths derive from this file's own location, so the script runs from anywhere
# and for anyone -- it lives at frontend/scripts/make-og-image.py.
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(REPO, "frontend", "public", "og-image.png")

# Pillow cannot rasterise woff2, so convert the app's own @fontsource files
# into a throwaway temp dir on each run rather than committing binary ttfs.
FONTS = tempfile.mkdtemp(prefix="amphive-og-fonts-")


def _build_fonts():
    """woff2 -> ttf for the three faces this poster uses, from node_modules."""
    from fontTools.ttLib import TTFont

    faces = {
        "bricolage-800.ttf": (
            "@fontsource/bricolage-grotesque",
            "bricolage-grotesque-latin-800-normal.woff2",
        ),
        "inter-400.ttf": ("@fontsource/inter", "inter-latin-400-normal.woff2"),
        "inter-600.ttf": ("@fontsource/inter", "inter-latin-600-normal.woff2"),
    }
    for out_name, (pkg, woff) in faces.items():
        src_path = os.path.join(REPO, "frontend", "node_modules", pkg, "files", woff)
        if not os.path.exists(src_path):
            raise SystemExit(
                "missing " + src_path + "\n"
                "Run `npm install` in frontend/ first -- the fonts come from the "
                "app's own @fontsource packages, never a font CDN."
            )
        font = TTFont(src_path)
        font.flavor = None
        font.save(os.path.join(FONTS, out_name))

W, H = 1200, 630
SS = 2                      # supersample factor for crisp vector-ish edges

# --- brand palette (tokens.css, [data-theme="day"]) -------------------------
PAPER      = (250, 247, 239)   # #FAF7EF  manifest background_color / theme-color
PAPER_DEEP = (243, 239, 227)   # --bg-deep-ish, used for the soft wash
INK        = (35, 37, 29)      # --ink        hsl(75 12% 13%)
INK_2      = (90, 94, 80)      # --ink-2      hsl(75 8% 34%)
BORDER     = (227, 225, 217)   # --border     hsl(48 16% 87%)
PRIMARY    = (245, 151, 10)    # --primary    hsl(36 92% 50%)  honey amber
BRAND      = (102, 127, 10)    # --brand      hsl(73 85% 27%)  volt lime (text-safe)
BRAND_GLOW = (202, 255, 10)    # --brand-glow hsl(73 100% 52%)

_build_fonts()

bricolage = lambda s: ImageFont.truetype(os.path.join(FONTS, "bricolage-800.ttf"), s)
inter     = lambda s: ImageFont.truetype(os.path.join(FONTS, "inter-400.ttf"), s)
inter_sb  = lambda s: ImageFont.truetype(os.path.join(FONTS, "inter-600.ttf"), s)


def tracked(draw, xy, text, font, fill, tracking):
    """Draw text with manual letter-spacing (Pillow has no tracking)."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking
    return x - tracking


def wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


img = Image.new("RGB", (W * SS, H * SS), PAPER)
d = ImageDraw.Draw(img)


def S(v):
    return int(round(v * SS))


# --- background: a soft warm wash in the lower-right, then the real app mark
# as a very low-opacity watermark bleeding off the right edge. -------------
wash = Image.new("RGB", (W * SS, H * SS), PAPER)
wd = ImageDraw.Draw(wash)
wd.ellipse([S(620), S(120), S(1420), S(760)], fill=PAPER_DEEP)
wash = wash.filter(ImageFilter.GaussianBlur(S(60)))
img = Image.blend(img, wash, 0.85)
d = ImageDraw.Draw(img)

mark = Image.open(os.path.join(REPO, "frontend", "public", "icon-512.png")).convert("RGBA")

# Watermark: the shipped bolt, large, faint, cropped by the right edge.
wm = mark.resize((S(880), S(880)), Image.LANCZOS)
wm.putalpha(wm.getchannel("A").point(lambda a: int(a * 0.11)))
img.paste(wm, (S(690), S(-60)), wm)

# --- brand lockup ----------------------------------------------------------
M = 84                                   # left margin

# eyebrow — the exact phrase from index.html's <title>
tracked(d, (S(M), S(78)), "SHARED EV CHARGING", inter_sb(S(25)), BRAND, S(4.2))

# app icon + wordmark, optically baseline-aligned
ICON = 116
icon = mark.resize((S(ICON), S(ICON)), Image.LANCZOS)
img.paste(icon, (S(M - 8), S(146)), icon)

word_font = bricolage(S(112))
d.text((S(M + ICON + 18), S(140)), "AmpHive", font=word_font, fill=INK)

# --- description: the real one-line copy from utils/legal.js ---------------
# (its first clause, "AmpHive is a shared EV charging platform.", is already
# said by the wordmark + eyebrow, so only the substantive half is drawn.)
DESC = ("Find a charger nearby, plug in, and pay from your charging credit "
        "\u2014 or host your own chargers and earn.")
body = inter(S(40))
y = 322
for line in wrap(d, DESC, body, S(920)):
    d.text((S(M), S(y)), line, font=body, fill=INK_2)
    y += 56

# --- footer: hairline rule, the real domain, brand chip -------------------
d.rectangle([S(M), S(500), S(W - M), S(500) + max(1, SS // 2)], fill=BORDER)

url_font = inter_sb(S(32))
d.text((S(M), S(528)), "amphive.app", font=url_font, fill=INK)

# honey chip: the primary action colour, carrying the one true call to action
chip = "Find a charger"
cf = inter_sb(S(26))
cw = d.textlength(chip, font=cf)
cx1, cy1 = S(W - M) - int(cw) - S(48), S(524)
cx2, cy2 = S(W - M), cy1 + S(52)
d.rounded_rectangle([cx1, cy1, cx2, cy2], radius=S(26), fill=PRIMARY)
d.text((cx1 + S(24), cy1 + S(12)), chip, font=cf, fill=(30, 20, 5))

# --- bottom brand band: honey -> volt-lime gradient ------------------------
BAND = 12
for i in range(S(W)):
    t = i / (S(W) - 1)
    c = tuple(int(PRIMARY[k] + (BRAND_GLOW[k] - PRIMARY[k]) * t) for k in range(3))
    d.rectangle([i, S(H - BAND), i, S(H)], fill=c)

img = img.resize((W, H), Image.LANCZOS)
img.save(OUT, "PNG", optimize=True)
print("wrote", OUT, os.path.getsize(OUT), "bytes", Image.open(OUT).size)
