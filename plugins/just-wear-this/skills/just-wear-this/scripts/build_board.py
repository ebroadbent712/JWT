#!/usr/bin/env python3
"""Just Wear This board builder.

Usage:
    python3 build_board.py plan.json [--out OUTPUT_DIR]

Reads a plan (which pieces each person wears, notes, palette, copy), pulls the
images from the library, and writes:
    <slug>-board.pdf          page 1 = the outfit board, page 2 = the details
    <slug>-board-preview.png  page 1 as an image (when a PDF renderer is available)
"""
import argparse
import itertools
import json
import os
import re
import subprocess
import sys

import numpy as np
from PIL import Image, ImageChops
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LIB = os.path.join(ROOT, "library")
FONTS = os.path.join(ROOT, "assets", "fonts")

INK = HexColor("#1B1B1B")
SOFT = HexColor("#5A5A5A")
RULE = HexColor("#CFCFCF")
PX_PER_PT = 4  # resolution of the composited outfit panels

# How big each kind of piece may appear, relative to the panel's item area.
# Keeps shoes and accessories in believable proportion to dresses and coats.
MAX_REL = {"hero": 1.0, "layer": 0.9, "bottom": 0.95, "shoes": 0.45, "accent": 0.3}
WEIGHT = {"hero": 1.7, "layer": 1.0, "bottom": 1.0, "shoes": 0.75, "accent": 0.45}
SMALL_ACCENTS = {"accessory"}
OWNED = set()  # item ids the client already owns (filled from the plan)
TIERS = [("everyday", "Everyday"), ("mid", "Mid"), ("splurge", "Splurge")]


def register_fonts():
    for name, file in [
        ("Inter", "Inter-Regular.ttf"), ("Inter-Medium", "Inter-Medium.ttf"),
        ("Inter-SemiBold", "Inter-SemiBold.ttf"), ("Inter-Black", "Inter-Black.ttf"),
        ("Hand", "Caveat-Medium.ttf"),
    ]:
        pdfmetrics.registerFont(TTFont(name, os.path.join(FONTS, file)))


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "board"


# ---------------------------------------------------------------- layouts

# Slot templates: (x, y, w, h) as fractions of a panel's item area, origin top-left.
# Slots may overlap a little; images are blended with "multiply", so white
# backgrounds disappear and overlapping pieces look like a real flat lay.
TEMPLATES = {
    1: [[(0.08, 0.0, 0.84, 1.0)]],
    2: [
        [(0.0, 0.0, 0.64, 1.0), (0.56, 0.48, 0.44, 0.52)],
        [(0.0, 0.0, 1.0, 0.66), (0.22, 0.62, 0.56, 0.38)],
    ],
    3: [
        [(0.0, 0.0, 1.0, 0.5), (0.0, 0.46, 0.62, 0.54), (0.58, 0.62, 0.42, 0.38)],
        [(0.0, 0.0, 0.6, 0.66), (0.48, 0.14, 0.52, 0.86), (0.0, 0.6, 0.5, 0.4)],
        [(0.0, 0.0, 0.62, 1.0), (0.58, 0.0, 0.42, 0.54), (0.58, 0.52, 0.42, 0.48)],
        [(0.0, 0.0, 1.0, 0.6), (0.0, 0.58, 0.52, 0.42), (0.5, 0.58, 0.5, 0.42)],
    ],
    4: [
        [(0.0, 0.0, 1.0, 0.45), (0.0, 0.42, 0.6, 0.58), (0.58, 0.44, 0.42, 0.3), (0.58, 0.72, 0.42, 0.28)],
        [(0.0, 0.0, 0.58, 0.62), (0.5, 0.02, 0.5, 0.62), (0.0, 0.6, 0.5, 0.4), (0.5, 0.62, 0.5, 0.38)],
        [(0.0, 0.0, 0.6, 0.64), (0.6, 0.0, 0.4, 0.4), (0.58, 0.36, 0.42, 0.64), (0.04, 0.62, 0.52, 0.38)],
        [(0.0, 0.0, 0.62, 0.7), (0.56, 0.12, 0.44, 0.88), (0.0, 0.68, 0.32, 0.32), (0.3, 0.7, 0.3, 0.3)],
    ],
    5: [
        [(0.0, 0.0, 0.56, 0.6), (0.5, 0.0, 0.5, 0.48), (0.5, 0.46, 0.5, 0.54), (0.0, 0.6, 0.32, 0.4), (0.28, 0.66, 0.24, 0.34)],
    ],
}


def fit(iw, ih, sw, sh, max_major):
    s = min(sw / iw, sh / ih)
    s = min(s, max_major / max(iw, ih))
    return iw * s, ih * s


def choose_layout(items, area_w, area_h):
    """Try every template and assignment; keep the one that shows the outfit best."""
    n = len(items)
    templates = TEMPLATES.get(n) or TEMPLATES[max(TEMPLATES)]
    best = None
    for t in templates:
        slots = t[:n]
        for perm in itertools.permutations(range(n)):
            score, placed, centers = 0.0, [], {}
            for item_i, slot_i in enumerate(perm):
                it = items[item_i]
                x, y, w, h = slots[slot_i]
                sw, sh = w * area_w, h * area_h
                role = it["layout_role"]
                max_major = MAX_REL.get(role, 0.6) * max(area_w, area_h)
                if role == "shoes" and it["_h"] > it["_w"] * 1.4:
                    max_major = 0.62 * max(area_w, area_h)  # tall boots
                if it["category"] in SMALL_ACCENTS:
                    max_major = min(max_major, 0.28 * max(area_w, area_h))
                dw, dh = fit(it["_w"], it["_h"], sw, sh, max_major)
                cy = y + h / 2
                # square root: rewards giving every piece a fair size over one huge piece
                s = ((dw * dh) / (area_w * area_h)) ** 0.5 * WEIGHT.get(role, 1.0)
                if role == "shoes" and cy < 0.45:
                    s *= 0.6  # shoes belong low in the panel
                if role == "hero" and cy > 0.6:
                    s *= 0.7  # the main piece belongs up top
                score += s
                centers.setdefault(role, []).append(cy)
                placed.append((it, x * area_w + (sw - dw) / 2, y * area_h + (sh - dh) / 2, dw, dh))
            # Real flat lays read top to bottom: the main piece sits above the bottoms.
            if "hero" in centers and "bottom" in centers and min(centers["hero"]) > min(centers["bottom"]) + 0.02:
                score *= 0.55
            if best is None or score > best[0]:
                best = (score, placed)
    return best[1]


def tighten(placed, area_w, area_h, gap_frac=0.035, max_grow=1.45):
    """Tall panels (small families) can leave a big empty band between the top piece and the rest.
    Close any empty horizontal band down to a small gap, then grow the group a little and centre it."""
    if not placed:
        return placed
    gap = gap_frac * area_h
    items = sorted(placed, key=lambda p: p[2])
    # find empty bands: walk down the union of vertical extents
    shifts, cursor_bottom, total_shift = [], None, 0.0
    out = []
    for it, x, y, w, h in items:
        if cursor_bottom is not None and y - cursor_bottom > gap:
            total_shift += (y - cursor_bottom) - gap
        out.append([it, x, y - total_shift, w, h])
        cursor_bottom = max(cursor_bottom or 0, y + h) if cursor_bottom is not None else y + h
        cursor_bottom = max(cursor_bottom, y + h)
    # recompute properly with shifted coords
    top = min(p[2] for p in out); bottom = max(p[2] + p[4] for p in out)
    left = min(p[1] for p in out); right = max(p[1] + p[3] for p in out)
    cw, ch = right - left, bottom - top
    k = min(max_grow, area_h / ch if ch else 1, area_w / cw if cw else 1)
    k = max(1.0, k)
    nw, nh = cw * k, ch * k
    # sit near the top, under the name, rather than floating mid-panel
    ox, oy = (area_w - nw) / 2, min((area_h - nh) / 2, 0.06 * area_h)
    return [(it, ox + (x - left) * k, oy + (y - top) * k, w * k, h * k) for it, x, y, w, h in out]


def compose_panel(placed, area_w, area_h):
    W, H = int(area_w * PX_PER_PT), int(area_h * PX_PER_PT)
    canvas_img = Image.new("RGB", (W, H), "white")
    # Draw the main pieces first so smaller ones sit visually on top.
    order = sorted(placed, key=lambda p: -p[3] * p[4])
    for it, x, y, w, h in order:
        im = it["_img"].resize((max(1, int(w * PX_PER_PT)), max(1, int(h * PX_PER_PT))), Image.LANCZOS)
        layer = Image.new("RGB", (W, H), "white")
        layer.paste(im, (int(x * PX_PER_PT), int(y * PX_PER_PT)))
        canvas_img = ImageChops.multiply(canvas_img, layer)
    return canvas_img


# ---------------------------------------------------------------- drawing helpers

def spaced(c, x, y, text, font, size, spacing, color=INK, align="left"):
    w = sum(pdfmetrics.stringWidth(ch, font, size) for ch in text) + spacing * (len(text) - 1)
    if align == "right":
        x -= w
    c.setFillColor(color)
    t = c.beginText(x, y)
    t.setFont(font, size)
    t.setCharSpace(spacing)
    t.textOut(text)
    t.setCharSpace(0)
    c.drawText(t)
    return w


def hand_underline(c, x, y, w):
    c.setStrokeColor(INK)
    c.setLineWidth(0.9)
    p = c.beginPath()
    p.moveTo(x, y)
    p.curveTo(x + w * 0.3, y + 2.2, x + w * 0.7, y + 1.4, x + w, y + 2.6)
    c.drawPath(p, stroke=1, fill=0)


def hand_arrow(c, x0, y0, x1, y1):
    """A loose, hand-drawn style curved arrow from (x0, y0) to (x1, y1)."""
    c.setStrokeColor(INK)
    c.setLineWidth(0.75)
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    dx, dy = x1 - x0, y1 - y0
    cx, cy = mx - dy * 0.35, my + dx * 0.35
    p = c.beginPath()
    p.moveTo(x0, y0)
    p.curveTo(cx, cy, cx, cy, x1, y1)
    c.drawPath(p, stroke=1, fill=0)
    import math
    ang = math.atan2(y1 - cy, x1 - cx)
    for da in (2.6, -2.6):
        c.line(x1, y1, x1 + 5 * math.cos(ang + da), y1 + 5 * math.sin(ang + da))


def wrap(text, font, size, max_w):
    lines = []
    for para in text.split("\n"):
        words, line = para.split(), ""
        for w in words:
            trial = (line + " " + w).strip()
            if pdfmetrics.stringWidth(trial, font, size) <= max_w:
                line = trial
            else:
                if line:
                    lines.append(line)
                line = w
        lines.append(line)
    return lines


# ---------------------------------------------------------------- the board

def load_items(catalog):
    by_id = {}
    for it in catalog["items"]:
        it = dict(it)
        im = Image.open(os.path.join(LIB, "images", it["file"])).convert("RGB")
        it["_img"], it["_w"], it["_h"] = im, im.width, im.height
        by_id[it["id"]] = it
    return by_id


def client_photo(path, crop=None):
    """A client's own piece, from the photo the photographer sent.
    crop = [left, top, right, bottom] as fractions of the photo, framed tightly on the garment (no faces).
    Photos on a white background sit on the board like the flat lays; anything else gets a clean photo frame."""
    from PIL import ImageOps
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    if crop:
        W, H = im.size
        l, t, r, b = crop
        im = im.crop((int(l * W), int(t * H), int(r * W), int(b * H)))
    im.thumbnail((1000, 1000), Image.LANCZOS)
    a = np.asarray(im.convert("L"))
    edge = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    if edge.mean() < 240:  # not a white-background product shot: frame it as a photo
        pad = max(12, im.width // 40)
        framed = Image.new("RGB", (im.width + 2 * pad, im.height + 2 * pad), "white")
        framed.paste(im, (pad, pad))
        from PIL import ImageDraw
        d = ImageDraw.Draw(framed)
        d.rectangle([pad - 1, pad - 1, pad + im.width, pad + im.height], outline=(190, 186, 178), width=max(2, im.width // 160))
        im = framed
    return im


def grid_rows(n):
    # big families: spread people evenly over three rows instead of leaving a near-empty last row
    fixed = {1: [1], 2: [2], 3: [3], 4: [2, 2], 5: [2, 3], 6: [3, 3], 7: [3, 4], 8: [4, 4],
             9: [3, 3, 3], 10: [4, 3, 3], 11: [4, 4, 3], 12: [4, 4, 4]}
    if n in fixed:
        return fixed[n]
    rows = -(-n // 4)
    base, extra = divmod(n, rows)
    return [base + 1] * extra + [base] * (rows - extra)


def _lab(hexs):
    h = hexs.lstrip("#")
    rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    rgb = [((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92 for c in rgb]
    x = (0.4124 * rgb[0] + 0.3576 * rgb[1] + 0.1805 * rgb[2]) / 0.9505
    y = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
    z = (0.0193 * rgb[0] + 0.1192 * rgb[1] + 0.9505 * rgb[2]) / 1.089
    f = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def tonal_clashes(plan, items_by_id, limit=16):
    """Pieces in one person's outfit that are so close in color they read as one block on camera
    (a camel cardigan over stone chinos). Returns readable warnings."""
    out, blazer_jeans = [], []
    for p in plan["people"]:
        its = [items_by_id[i] for i in p["items"] if i in items_by_id]
        uppers = [i for i in its if i["category"] in ("top", "outerwear")]
        lowers = [i for i in its if i["category"] == "bottom"]
        dresses = [i for i in its if i["category"] == "dress"]
        pairs = [(u, l) for u in uppers for l in lowers] + [(o, d) for o in uppers if o["category"] == "outerwear" for d in dresses]
        for a, b in pairs:
            if not a.get("hex") or not b.get("hex") or a.get("pattern") or b.get("pattern"):
                continue
            la, lb = _lab(a["hex"]), _lab(b["hex"])
            d = sum((x - y) ** 2 for x, y in zip(la, lb)) ** 0.5
            if d < limit:
                names = (a["name"] + " " + b["name"]).lower()
                if "blazer" in names and ("jeans" in names or "denim" in names):
                    blazer_jeans.append(p["label"])
                    continue
                out.append(f"{p['label']}: {a['name']} ({a['id']}) and {b['name']} ({b['id']}) are too close in color (difference {d:.0f}); they will read as one block. Swap one for a lighter or darker piece.")
    if len(set(blazer_jeans)) > 1:
        out.append(f"Navy blazer over dark jeans on {', '.join(sorted(set(blazer_jeans)))}: fine for one person, not more. Change all but one.")
    return out


def board_palette(plan, items_by_id, catalog, limit=12):
    """The swatches at the bottom must be colors people are actually wearing.
    This story's swatches come first (only the ones someone wears), then worn colors from the other
    stocked stories; near-duplicates are skipped. 3 to 5 swatches."""
    stories = catalog["color_stories"]
    story = stories.get(plan.get("color_story"), {})
    own = list(story.get("palette", []))
    others = [sw for k, st in stories.items() if st is not story and st.get("stocked", True) for sw in st.get("palette", [])]
    others += [{"name": "Stone", "hex": "#BFB09A"}, {"name": "Charcoal", "hex": "#3E4044"}, {"name": "Oatmeal", "hex": "#D9CDB8"},
               {"name": "Denim", "hex": "#2B3A5C"}, {"name": "Olive", "hex": "#5B5A3A"}, {"name": "Mustard", "hex": "#C9962E"}]
    weight = {"hero": 3, "layer": 2, "bottom": 1.5}
    worn = []
    for n, p in enumerate(plan["people"]):
        for i in p["items"]:
            it = items_by_id.get(i)
            if it and it.get("hex") and it["category"] not in ("shoes", "accessory"):
                w = weight.get(it.get("layout_role"), 1) + (2 if it["category"] == "dress" else 0)
                if n == 0 and (it["category"] == "dress" or it.get("layout_role") == "hero"):
                    w += 10  # the anchor's color always earns a swatch
                worn.append((_lab(it["hex"]), w))
    dist = lambda x, y: sum((a - b) ** 2 for a, b in zip(x, y)) ** 0.5
    score = lambda sw: sum(w for l, w in worn if dist(_lab(sw["hex"]), l) < limit)
    pal = []
    for group in (own, others):
        for sc, sw in sorted(((score(sw), sw) for sw in group), key=lambda x: -x[0]):
            if sc <= 0 or len(pal) >= 5:
                continue
            if any(dist(_lab(sw["hex"]), _lab(q["hex"])) < 14 or sw["name"].lower() == q["name"].lower() for q in pal):
                continue
            pal.append(sw)
    for sw in own:  # never fewer than three
        if len(pal) >= 3:
            break
        if sw not in pal:
            pal.append(sw)
    # keep the story's own order for its swatches, extras after
    return sorted(pal, key=lambda sw: own.index(sw) if sw in own else 10 + pal.index(sw))


def draw_board_page(c, plan, settings, items_by_id, numbering):
    PW, PH = letter
    M = 30
    # Header
    c.setFillColor(INK)
    c.setFont("Inter-Black", 30)
    c.drawString(M, PH - M - 26, "JUST WEAR THIS.")
    sub = f"{plan['family_name']}  ·  OUTFIT PLAN".upper()
    spaced(c, M + 1, PH - M - 44, sub, "Inter-Medium", 6.8, 2.1)
    c.setFont("Inter", 17)
    c.drawRightString(PW - M, PH - M - 22, plan.get("title_right", ""))
    spaced(c, PW - M, PH - M - 40, plan.get("subtitle_right", "").upper(), "Inter-Medium", 6.3, 1.9, align="right")
    if plan.get("dress_level"):
        spaced(c, PW - M, PH - M - 52, f"DRESS LEVEL  ·  {plan['dress_level'].upper()}", "Inter-SemiBold", 6.3, 1.9, align="right")

    top = PH - M - 62
    footer_h = 100
    bottom = M + footer_h
    people = plan["people"]
    rows = grid_rows(len(people))
    weights = [1.15 if i == 0 and len(rows) > 1 and rows[0] < rows[-1] else 1.0 for i in range(len(rows))]
    total_h = top - bottom
    row_hs = [total_h * w / sum(weights) for w in weights]

    c.setStrokeColor(RULE)
    c.setLineWidth(0.6)
    idx, y_cursor = 0, top
    for r, count in enumerate(rows):
        rh = row_hs[r]
        pw_ = (PW - 2 * M) / count
        if r > 0:
            c.line(M, y_cursor, PW - M, y_cursor)
        for col in range(count):
            person = people[idx]
            px = M + col * pw_
            if col > 0:
                c.setStrokeColor(RULE)
                c.line(px, y_cursor - 8, px, y_cursor - rh + 8)
            draw_person_panel(c, person, items_by_id, numbering, px + 10, y_cursor - rh + 6, pw_ - 20, rh - 12, count)
            idx += 1
        y_cursor -= rh
    c.setStrokeColor(RULE)
    c.line(M, bottom, PW - M, bottom)

    # Footer: palette, headline, story
    story = plan["_story"]
    c.setFillColor(INK)
    c.setFont("Inter", 12)
    c.drawString(M + 4, bottom - 24, "Color palette")
    sw, gap = 38, 7
    for i, sw_ in enumerate(story["palette"][:5]):
        c.setFillColor(HexColor(sw_["hex"]))
        c.rect(M + 4 + i * (sw + gap), M + 15, sw, sw, stroke=0, fill=1)
        c.setFillColor(SOFT)
        c.setFont("Inter", 5.6)
        for j, nl in enumerate(wrap(sw_["name"], "Inter", 5.6, sw)[:2]):
            c.drawString(M + 4 + i * (sw + gap), M + 6 - j * 7, nl)
    divx = M + 4 + 5 * (sw + gap) + 14
    c.setStrokeColor(RULE)
    c.line(divx, bottom - 14, divx, M + 6)
    c.setFillColor(INK)
    c.setFont("Inter", 13.5)
    for i, line in enumerate(plan.get("headline", [])[:3]):
        c.drawString(divx + 18, bottom - 30 - i * 19, line)
    hw = max([pdfmetrics.stringWidth(l, "Inter", 13.5) for l in plan.get("headline", [])[:3]] or [100])
    tx = divx + 18 + hw + 22
    c.setFont("Inter", 7.6)
    for i, line in enumerate(wrap(plan.get("story", ""), "Inter", 7.6, PW - M - tx)[:7]):
        c.drawString(tx, bottom - 28 - i * 10.5, line)
    studio = settings.get("studio_name")
    if studio:
        c.setFillColor(SOFT)
        spaced(c, PW - M, M - 14, f"STYLED BY {studio.upper()}", "Inter-Medium", 5.6, 1.6, color=SOFT, align="right")


def occupancy(panel_img, area_w, area_h, band):
    """Boolean grid (1 cell = 1 pt, origin top-left) of where pieces are drawn."""
    a = np.asarray(panel_img.convert("L").resize((int(area_w), int(area_h - band)), Image.BOX))
    occ = np.zeros((int(area_h) + 1, int(area_w) + 1), dtype=bool)
    occ[: a.shape[0], : a.shape[1]] = a < 246
    # small safety margin around every piece
    pad = 3
    grown = occ.copy()
    for dy in range(-pad, pad + 1):
        for dx in range(-pad, pad + 1):
            grown |= np.roll(np.roll(occ, dy, 0), dx, 1)
    return grown


def is_free(occ, taken, x, y, w, h, W, H):
    if x < 0 or y < 0 or x + w > W or y + h > H:
        return False
    if occ[int(y): int(y + h) + 1, int(x): int(x + w) + 1].any():
        return False
    for tx, ty, tw, th in taken:
        if x < tx + tw + 2 and tx < x + w + 2 and y < ty + th + 2 and ty < y + h + 2:
            return False
    return True


def rect_gap(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    dx = max(bx - (ax + aw), ax - (bx + bw), 0)
    dy = max(by - (ay + ah), ay - (by + bh), 0)
    return (dx * dx + dy * dy) ** 0.5


def draw_person_panel(c, person, items_by_id, numbering, x, y, w, h, per_row):
    label_size = 21 if per_row <= 2 else 17
    c.setFillColor(INK)
    c.setFont("Inter", label_size)
    label = person["label"]
    c.drawString(x, y + h - label_size, label)
    lw = pdfmetrics.stringWidth(label, "Inter", label_size)
    hand_underline(c, x - 2, y + h - label_size - 7, lw + 6)

    items = [dict(items_by_id[i]) for i in person["items"]]
    # A piece the client owns and loves is the point of their look: keep it big.
    owned_main = [it for it in items if it["id"] in OWNED and it["category"] in ("dress", "top")]
    if owned_main:
        for it in items:
            if it is owned_main[0]:
                it["layout_role"] = "hero"
            elif it["layout_role"] == "hero":
                it["layout_role"] = "layer"
    # A statement jacket is the point of the look: show it big, and let a plain top under it step back.
    elif any(it.get("statement") and it["category"] == "outerwear" for it in items) and not any(it["category"] == "dress" for it in items):
        for it in items:
            if it.get("statement") and it["category"] == "outerwear":
                it["layout_role"] = "hero"
            elif it["layout_role"] == "hero" and it["category"] == "top" and not it.get("statement"):
                it["layout_role"] = "layer"
    if not any(it["layout_role"] == "hero" for it in items):
        for cat in ("dress", "outerwear", "top"):
            lead = next((it for it in items if it["category"] == cat), None)
            if lead:
                lead["layout_role"] = "hero"
                break
    header = label_size + 14
    band = 12  # room under the pieces for labels
    area_w, area_h = w, h - header
    lay_h = area_h - band
    placed = choose_layout(items, area_w, lay_h)
    # breathing room: shrink every piece a little around its own centre
    placed = [(it, px + pw_ * 0.06, py + ph * 0.06, pw_ * 0.88, ph * 0.88) for it, px, py, pw_, ph in placed]
    placed = tighten(placed, area_w, lay_h)
    panel = compose_panel(placed, area_w, lay_h)
    top_y = y + area_h  # PDF y of the area's top edge
    c.drawImage(ImageReader(panel), x, top_y - lay_h, area_w, lay_h)

    def to_pdf(ax, ay):
        return x + ax, top_y - ay

    occ = occupancy(panel, area_w, area_h, band)
    W, H = occ.shape[1] - 1, occ.shape[0] - 1
    taken = []

    def free_spot(xx, yy, tw, bh):
        """Free on the panel; a label may also sit in the header row, to the right of the name."""
        if yy >= 0:
            # labels may dip a few points into the gutter under the panel
            return is_free(occ, taken, xx, yy, tw, min(bh, H - yy), W, H) if yy + bh <= H + 4 else False
        if yy < -(header - 6) or xx < lw + 12 or xx + tw > W:
            return False
        if yy + bh > 0 and occ[0: int(yy + bh) + 1, int(xx): int(xx + tw) + 1].any():
            return False
        return not any(xx < tx + tw_ + 2 and tx < xx + tw + 2 and yy < ty + th_ + 2 and ty < yy + bh + 2
                       for tx, ty, tw_, th_ in taken)

    # Number labels: as close as possible under (or over) each piece, never on top of anything.
    fs = 6.2 if per_row <= 2 else (5.6 if per_row == 3 else 5.2)
    for it, px, py, pw_, ph in sorted(placed, key=lambda p: -p[3] * p[4]):
        num = f"{numbering[it['id']]:02d}"
        own = "  · you have this" if it["id"] in OWNED else ""
        words = it["name"].split()
        # Try the full label first; in tight panels fall back to a shorter name, then just the number
        # (page 2 lists every number in full), so labels never sit on top of each other.
        MATERIAL = {"leather", "suede", "wool", "cotton", "linen", "silk", "velvet", "knit", "cable-knit", "corduroy", "tweed", "cashmere", "patent"}
        trimmed = [w for w in words if w.lower() not in MATERIAL]
        versions = [f"{num} {it['name']}{own}"]
        if len(trimmed) < len(words):
            versions.append(f"{num} {' '.join(trimmed)}{own}")
        # color + garment ("Chocolate moccasins") reads better than the last few words alone
        lead = words[:2] if words[0].lower() in ("dark", "light", "deep", "soft", "pale") and len(words) > 2 else words[:1]
        if len(words) > len(lead) + 1:
            versions.append(f"{num} {' '.join(lead + words[-1:])}{own}")
        versions += [f"{num} {' '.join(words[-k:])}{own}" for k in (2,)] + [num + own]
        th = fs + 1
        cx = px + pw_ / 2
        spot = None
        # each try: the lines to draw; a full name may wrap onto two short lines
        tries = []
        for vi, text in enumerate(versions):
            tries.append([text])
            if vi <= 1 and len(words) >= 2:
                ws = text[len(num) + 1:].replace(own, "").split() if own else text[len(num) + 1:].split()
                if len(ws) >= 2:
                    half = (len(ws) + 1) // 2
                    tries.append([f"{num} {' '.join(ws[:half])}", ' '.join(ws[half:]) + own])
            if vi == 0 and len(words) >= 3:
                k = -(-len(words) // 3)  # three balanced lines
                chunks = [words[i:i + k] for i in range(0, len(words), k)]
                tries.append([f"{num} {' '.join(chunks[0])}"] + [' '.join(c) for c in chunks[1:-1]] + [' '.join(chunks[-1]) + own])
        for lines_ in tries:
            tw = max(pdfmetrics.stringWidth(t, "Inter", fs) for t in lines_)
            bh = th * len(lines_)
            cands = []
            for yy in [py + ph + 1, py + ph + 4, py - bh - 2, py + ph - bh - 2, py + ph * 0.7]:
                for xx in [cx - tw / 2, px, px + pw_ - tw, cx - tw / 2 - 30, cx - tw / 2 + 30]:
                    cands.append((xx, yy))
            # just under / over the garment itself (the image box often has white margin)
            cols = occ[:, int(max(0, px)): int(min(W, px + pw_)) + 1]
            rows_hit = np.where(cols[int(max(0, py)): int(min(H, py + ph)) + 1].any(axis=1))[0]
            if len(rows_hit):
                gb, gt = py + rows_hit[-1] + 1, py + rows_hit[0]
                for xx in [cx - tw / 2, px, px + pw_ - tw]:
                    cands += [(xx, gb), (xx, gt - bh - 1)]
            # beside the piece, level with its lower part
            for yy in [py + ph - bh, py + ph * 0.75 - bh / 2, py + ph * 0.5 - bh / 2]:
                cands += [(px - tw - 3, yy), (px + pw_ + 3, yy)]
            def lab_score(p):
                sc = rect_gap((p[0], p[1], tw, bh), (px, py, pw_, ph)) + 0.15 * abs(p[1] - (py + ph + 2)) + 0.05 * abs(p[0] + tw / 2 - cx)
                # a label that sits off to the side of its piece reads as belonging to the neighbour
                if p[0] + tw < px + 4 or p[0] > px + pw_ - 4:
                    sc += 14
                return sc
            cands.sort(key=lab_score)
            spot = next(((xx, yy) for xx, yy in cands if free_spot(xx, yy, tw, bh)), None)
            if spot:
                text = lines_
                break
        if spot is None:  # last resort: anywhere free, nearest to the piece
            text = versions[-1]
            tw = pdfmetrics.stringWidth(text, "Inter", fs)
            best = None
            for yy in range(0, int(H - th), 3):
                for xx in range(0, int(W - tw), 4):
                    if is_free(occ, taken, xx, yy, tw, th, W, H):
                        d = rect_gap((xx, yy, tw, th), (px, py, pw_, ph))
                        if best is None or d < best[0]:
                            best = (d, xx, yy)
            spot = (best[1], best[2]) if best else (cx - tw / 2, py + ph + 1)
            text = [text] if isinstance(text, str) else text
            text = text[:1] if len(text) > 1 else text
            tw = pdfmetrics.stringWidth(text[0], "Inter", fs)
        taken.append((spot[0], spot[1], tw, th * len(text)))
        c.setFillColor(INK)
        c.setFont("Inter", fs)
        for li, t in enumerate(text):
            bx, by = to_pdf(spot[0], spot[1] + fs + li * th)
            c.drawString(bx, by, t)

    # Handwritten note: in empty space near the piece it talks about, with a short arrow.
    note = person.get("note")
    if not note:
        return
    target = next((p for p in placed if p[0]["id"] == note.get("points_to")), placed[0])
    tbox = target[1:]
    hs0 = 13 if per_row <= 2 else 11.5
    tx0, ty0, tw0, th0 = tbox

    def arrow_ends(nx, ny, nw, nh):
        ncx, ncy = nx + nw / 2, ny + nh / 2
        tx = min(max(ncx, tx0 + tw0 * 0.15), tx0 + tw0 * 0.85)
        ty = min(max(ncy, ty0 + th0 * 0.1), ty0 + th0 * 0.85)
        return min(max(tx, nx), nx + nw), min(max(ty, ny), ny + nh), tx, ty

    def crossing(nx, ny, nw, nh):
        """How far (pt) the arrow would run over other pieces on its way to the target."""
        sx, sy, tx, ty = arrow_ends(nx, ny, nw, nh)
        n = int(max(abs(tx - sx), abs(ty - sy)) / 2) + 1
        hit = 0
        for k in range(1, n):
            qx, qy = sx + (tx - sx) * k / n, sy + (ty - sy) * k / n
            if tx0 - 4 <= qx <= tx0 + tw0 + 4 and ty0 - 4 <= qy <= ty0 + th0 + 4:
                break  # reached the target
            if 0 <= qy < occ.shape[0] and 0 <= qx < occ.shape[1] and occ[int(qy), int(qx)]:
                hit += 2
            elif any(tx_ - 1 <= qx <= tx_ + tw_ + 1 and ty_ - 1 <= qy <= ty_ + th_ + 1 for tx_, ty_, tw_, th_ in taken):
                hit += 2
        return hit

    # Try the note as written, then on one line, then a little smaller; keep the spot with the best score
    # (close to the piece, above it if possible, and an arrow that doesn't run across other pieces).
    variants = []
    raw = note["text"].split("\n")
    for lines in (raw, [" ".join(raw)] if len(raw) > 1 else None):
        if not lines:
            continue
        for k in (1.0, 0.88):
            variants.append((lines, hs0 * k, k))
    best = None
    for lines, hs, k in variants:
        nw = max(pdfmetrics.stringWidth(l, "Hand", hs) for l in lines)
        nh = hs * 0.95 * len(lines) + 2
        size_pen = 0 if k == 1.0 else 8
        for yy in range(0, int(H - nh), 3):
            for xx in range(0, int(W - nw), 3):
                if not is_free(occ, taken, xx, yy, nw, nh, W, H):
                    continue
                d = rect_gap((xx, yy, nw, nh), tbox)
                sc = abs(d - 16) + (0 if yy < ty0 + th0 * 0.6 else 6) + size_pen
                if best is not None and sc >= best[0]:
                    continue
                sc += 3 * crossing(xx, yy, nw, nh)
                if best is None or sc < best[0]:
                    best = (sc, xx, yy, lines, hs, nw, nh)
        # header option: right of the person's name
        hx, hy = w - nw - 2, -header + 2
        if hx > lw + 14 and not any(hx < tx + tw_ + 2 and tx < hx + nw + 2 and hy < ty + th_ + 2 and ty < hy + nh + 2
                                    for tx, ty, tw_, th_ in taken):
            hx = max(hx, lw + 10)
            sc = 40 + size_pen + 3 * crossing(hx, hy, nw, nh)
            if best is None or sc < best[0]:
                best = (sc, hx, hy, lines, hs, nw, nh)
    if best is None:
        lines, hs = raw, hs0 * 0.85
        nw = max(pdfmetrics.stringWidth(l, "Hand", hs) for l in lines)
        nh = hs * 0.95 * len(lines) + 2
        nx, ny = max(w - nw - 2, lw + 10), -header + 2
    else:
        _, nx, ny, lines, hs, nw, nh = best
    c.setFillColor(INK)
    c.setFont("Hand", hs)
    for i, l in enumerate(lines):
        px_, py_ = to_pdf(nx, ny + hs * 0.8 + i * hs * 0.95)
        c.drawString(px_, py_, l)
    # arrow from the note edge nearest the target to just outside the target
    sx, sy, tx, ty = arrow_ends(nx, ny, nw, nh)
    vx, vy = tx - sx, ty - sy
    L = (vx * vx + vy * vy) ** 0.5
    # an arrow that would run across other clothes or labels is left off; the note sits by its piece
    if L > 14 and crossing(nx, ny, nw, nh) <= 4:
        ux, uy = vx / L, vy / L
        a0 = to_pdf(sx + ux * 3, sy + uy * 3)
        a1 = to_pdf(tx - ux * 5, ty - uy * 5)
        hand_arrow(c, a0[0], a0[1], a1[0], a1[1])


def draw_details_page(c, plan, settings, items_by_id, numbering, uses):
    PW, PH = letter
    M = 42
    y = PH - M - 10
    c.setFillColor(INK)
    c.setFont("Inter-Black", 20)
    c.drawString(M, y, "JUST WEAR THIS.")
    spaced(c, PW - M, y + 4, f"{plan['family_name']}  ·  THE DETAILS".upper(), "Inter-Medium", 6.5, 1.9, align="right")
    y -= 40

    def heading(t):
        nonlocal y
        c.setFillColor(INK)
        c.setFont("Inter-SemiBold", 11.5)
        c.drawString(M, y, t)
        hand_underline(c, M - 1, y - 5, pdfmetrics.stringWidth(t, "Inter-SemiBold", 11.5) + 4)
        y -= 22

    heading("Why it works")
    c.setFont("Inter", 9.3)
    for line in wrap(plan.get("why_it_works", ""), "Inter", 9.3, PW - 2 * M):
        c.drawString(M, y, line)
        y -= 13.5
    y -= 14

    heading("Shop the look")
    c.setFont("Inter", 7.6)
    c.setFillColor(SOFT)
    c.drawString(M, y, "Every piece comes at three price points. Mix and match: splurge on the favorite, save on the rest.")
    y -= 18
    col_x = [PW - M - 150, PW - M - 92, PW - M - 38]  # left edges of the tier columns

    def table_header():
        nonlocal y
        c.setFillColor(SOFT)
        c.setFont("Inter-Medium", 7)
        c.drawString(M, y, "NO.")
        c.drawString(M + 30, y, "PIECE")
        c.drawString(M + 220, y, "FOR")
        for (key, lab), cx in zip(TIERS, col_x):
            c.drawString(cx, y, lab.upper())
        y -= 6
        c.setStrokeColor(RULE)
        c.line(M, y, PW - M, y)
        y -= 13

    def new_page():
        """Big families: the table carries on to another page at full size instead of shrinking."""
        nonlocal y
        c.showPage()
        y = PH - M - 10
        c.setFillColor(INK)
        c.setFont("Inter-Black", 20)
        c.drawString(M, y, "JUST WEAR THIS.")
        spaced(c, PW - M, y + 4, f"{plan['family_name']}  ·  THE DETAILS, CONTINUED".upper(), "Inter-Medium", 6.5, 1.9, align="right")
        y -= 40

    table_header()
    checklist_h = 40 + sum(13.5 * len(wrap(t, "Inter", 9.3, PW - 2 * M - 18)) + 4 for t in plan.get("prep_checklist", []))
    footer_h = 50
    rows = sorted(numbering.items(), key=lambda kv: kv[1])
    n = max(1, len(rows))
    # 1) table + checklist on this page with rows at least 16pt apart; 2) else the whole table here and the
    # checklist on the next page; 3) else full-size rows carried across pages. Never shrink below readable.
    with_list = (y - checklist_h - footer_h - M - 24) / n
    table_only = (y - M - 40 - 18) / n
    if with_list >= 16:
        step = min(20.0, with_list)
    elif table_only >= 16:
        step = min(20.0, table_only)
    else:
        step = 19.0
    fs = 8.8 if step >= 18 else 8.4
    for idx, (item_id, num) in enumerate(rows):
        if y - step < M + 40:
            c.setFont("Inter", 6.8)
            c.setFillColor(SOFT)
            c.drawString(M, y + 2, "Continued on the next page.")
            new_page()
            heading("Shop the look (continued)")
            table_header()
        it = items_by_id[item_id]
        c.setFillColor(INK)
        c.setFont("Inter", fs)
        c.drawString(M, y, f"{num:02d}")
        c.drawString(M + 30, y, it["name"])
        c.setFillColor(SOFT)
        for_text = ", ".join(uses[item_id])
        for_lines = wrap(for_text, "Inter", fs, col_x[0] - (M + 220) - 10)
        c.drawString(M + 220, y, for_lines[0] + ("…" if len(for_lines) > 1 else ""))
        if item_id in OWNED:
            c.setFillColor(INK)
            c.setFont("Hand", 13)
            c.drawString(col_x[0], y - 1, "You have this!")
        else:
            links = it.get("shop_links") or ({"mid": it["shop_url"]} if it.get("shop_url") else {})
            for (key, lab), cx in zip(TIERS, col_x):
                url = links.get(key)
                if url:
                    c.setFillColor(INK)
                    c.setFont("Inter-SemiBold", min(8.4, fs))
                    c.drawString(cx, y, "Shop >")
                    lw = pdfmetrics.stringWidth("Shop >", "Inter-SemiBold", min(8.4, fs))
                    c.linkURL(url, (cx - 2, y - 3, cx + lw + 2, y + 9), relative=0)
                else:
                    c.setFillColor(HexColor("#B5B5B5"))
                    c.setFont("Inter", 8.4)
                    c.drawString(cx + 6, y, "–")
        y -= step * 0.35
        c.setStrokeColor(HexColor("#EDEDED"))
        c.line(M, y, PW - M, y)
        y -= step * 0.65
    c.setFont("Inter", 6.8)
    c.setFillColor(SOFT)
    c.drawString(M, y + 2, "Links show the look, not always the exact piece. A dash means there's no great match at that price.")
    y -= 18
    if y - checklist_h < M + footer_h:
        new_page()

    heading("The week before")
    c.setFont("Inter", 9.3)
    for item in plan.get("prep_checklist", []):
        c.setStrokeColor(INK)
        c.setLineWidth(0.7)
        c.rect(M, y - 1.5, 8, 8, stroke=1, fill=0)
        for i, line in enumerate(wrap(item, "Inter", 9.3, PW - 2 * M - 18)):
            c.setFillColor(INK)
            c.drawString(M + 16, y, line)
            y -= 13.5
        y -= 4

    # Footer
    c.setFillColor(SOFT)
    c.setFont("Inter", 6.8)
    disclosure = settings.get("disclosure", "")
    fy = M
    for line in reversed(wrap(disclosure, "Inter", 6.8, PW - 2 * M)):
        c.drawString(M, fy, line)
        fy += 9.5
    studio = settings.get("studio_name")
    if studio:
        site = settings.get("website", "")
        c.setFillColor(INK)
        c.setFont("Hand", 15)
        c.drawString(M, fy + 10, f"Can't wait to see you! — {studio}")
        if site:
            c.setFont("Inter", 7.5)
            c.setFillColor(SOFT)
            c.drawRightString(PW - M, fy + 12, site)


def build(plan_path, out_dir):
    register_fonts()
    with open(plan_path) as f:
        plan = json.load(f)
    with open(os.path.join(LIB, "catalog.json")) as f:
        catalog = json.load(f)
    settings_path = os.path.join(ROOT, "settings.json")
    settings = json.load(open(settings_path)) if os.path.exists(settings_path) else {}
    settings.update(plan.get("settings_override", {}))

    items_by_id = load_items(catalog)
    # Smart links: print justwearthis.co/s/<id>-<tier> instead of the raw store link,
    # so a sold-out item can be swapped later without re-sending boards.
    smart = catalog.get("smart_links", {})
    if smart.get("enabled"):
        base = smart.get("base", "https://justwearthis.co/s/")
        for iid, it in items_by_id.items():
            it["shop_links"] = {t: f"{base}{iid}-{t}" for t in (it.get("shop_links") or {})}
    missing = [i for p in plan["people"] for i in p["items"] if i not in items_by_id]
    if missing:
        sys.exit(f"Unknown item ids (not in the library): {missing}")
    for w in tonal_clashes(plan, items_by_id):
        print("CHECK (fix before delivering):", w)
    story = catalog["color_stories"].get(plan.get("color_story"), {})
    if plan.get("palette"):
        pal = plan["palette"]
        weight_hex = [_lab(items_by_id[i]["hex"]) for p in plan["people"] for i in p["items"]
                      if items_by_id[i].get("hex") and items_by_id[i]["category"] not in ("shoes", "accessory")]
        for sw in pal:
            if not any(sum((a - b) ** 2 for a, b in zip(_lab(sw["hex"]), l)) ** 0.5 < 20 for l in weight_hex):
                print(f"CHECK (fix before delivering): palette swatch {sw['name']} isn't worn by anyone on the board; drop it or swap it for a color they wear.")
    else:
        pal = board_palette(plan, items_by_id, catalog)
    plan["_story"] = {"palette": pal}

    OWNED.clear()
    for o in plan.get("owned", []):
        # an owned piece can carry its own label ("Her mustard knit dress") when a library piece stands in for it
        if isinstance(o, dict):
            OWNED.add(o["id"])
            if o.get("label") and o["id"] in items_by_id:
                items_by_id[o["id"]] = dict(items_by_id[o["id"]], name=o["label"])
            if o.get("photo") and o["id"] in items_by_id:
                try:
                    im = client_photo(o["photo"], o.get("crop"))
                    items_by_id[o["id"]] = dict(items_by_id[o["id"]], _img=im, _w=im.width, _h=im.height)
                except Exception as e:
                    print(f"CHECK (fix before delivering): couldn't use the photo {o['photo']} ({e}); the library picture is shown instead.")
        else:
            OWNED.add(o)
    numbering, uses = {}, {}
    for p in plan["people"]:
        for i in p["items"]:
            if i not in numbering:
                numbering[i] = len(numbering) + 1
            uses.setdefault(i, []).append(p["label"])

    os.makedirs(out_dir, exist_ok=True)
    slug = slugify(plan["family_name"])
    pdf_path = os.path.join(out_dir, f"{slug}-board.pdf")
    c = canvas.Canvas(pdf_path, pagesize=letter)
    c.setTitle(f"Just Wear This — {plan['family_name']}")
    c.setAuthor(settings.get("studio_name", "Just Wear This"))
    draw_board_page(c, plan, settings, items_by_id, numbering)
    c.showPage()
    draw_details_page(c, plan, settings, items_by_id, numbering, uses)
    c.showPage()
    c.save()
    print(f"Board PDF: {pdf_path}")

    preview = os.path.join(out_dir, f"{slug}-board-preview")
    try:
        subprocess.run(["pdftoppm", "-png", "-r", "150", "-f", "1", "-l", "1", "-singlefile", pdf_path, preview],
                       check=True, capture_output=True)
        print(f"Preview PNG: {preview}.png")
    except Exception:
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(pdf_path)
            doc[0].get_pixmap(dpi=150).save(preview + ".png")
            print(f"Preview PNG: {preview}.png")
        except Exception:
            print("Preview PNG skipped (no PDF renderer available).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("--out", default="output")
    a = ap.parse_args()
    build(a.plan, a.out)
