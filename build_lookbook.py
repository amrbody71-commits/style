"""
Daily lookbook builder.

Reads closet.json + today.json, emits a self-contained lookbook.html with the
garment photos embedded as data URIs. Thumbnails are cached in .thumbs/ so the
expensive image work happens once per garment, ever.

The page is glassmorphic and weather-reactive: today.json's weather.mode
("clear" | "rain" | "overcast" | "night") drives the palette, the ambient
canvas layer and the glow temperature.

Usage:  python build_lookbook.py
"""

import base64
import json
import re
import statistics
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageOps

PAGE_BG = (245, 245, 245)   # must match --bg in the stylesheet


def unify_bg(img: Image.Image) -> Image.Image:
    """Flood the photo's own backdrop to the page colour so cut-outs sit on the
    page with no visible rectangle, then crop away the dead margin so the
    garment actually fills its tile. Flooding from the corners (rather than
    replacing all near-white pixels) keeps white garments intact - a white polo
    is not connected to the border, so it is never touched."""
    def edge_bg(im):
        ww, hh = im.size
        e = []
        for i in range(0, ww, max(1, ww // 40)):
            e.append(im.getpixel((i, 0)))
            e.append(im.getpixel((i, hh - 1)))
        for i in range(0, hh, max(1, hh // 40)):
            e.append(im.getpixel((0, i)))
            e.append(im.getpixel((ww - 1, i)))
        return tuple(int(statistics.median(c[k] for c in e)) for k in range(3))

    def shift_to_page(im):
        """Slide the whole image so its backdrop lands exactly on the page colour.

        Deliberately not a flood fill: white garments on a near-white backdrop
        (the Air Force 1s, the white trousers) are contiguous with it, so a
        flood eats the garment - measured at 31% and 19% survival. A global
        shift of a few levels is invisible on the clothes and destroys nothing."""
        bg = edge_bg(im)
        if sum(bg) / 3 > 170 and max(abs(bg[k] - PAGE_BG[k]) for k in range(3)) > 1:
            return Image.merge("RGB", [
                ch.point(lambda v, d=PAGE_BG[k] - bg[k]: max(0, min(255, v + d)))
                for k, ch in enumerate(im.split())
            ])
        return im

    def trim(im):
        flat = Image.new("RGB", im.size, PAGE_BG)
        bbox = ImageChops.difference(im, flat).convert("L").point(
            lambda v: 255 if v > 10 else 0).getbbox()
        return im.crop(bbox) if bbox else im

    # Twice, because several photos are two-tone: a white product panel sitting
    # inside a grey frame. One pass fixes the frame and leaves the panel as a
    # visible rectangle; cropping to the panel and shifting again clears it.
    img = trim(shift_to_page(img))
    img = trim(shift_to_page(img))
    w, h = img.size

    # Pad back out to a square. Every garment then occupies the same box, so a
    # wide shoe stops rendering larger than a tall pair of trousers, and each
    # piece sits centred in its row.
    cw, ch = img.size
    side = int(max(cw, ch) * 1.01)
    canvas = Image.new("RGB", (side, side), PAGE_BG)
    canvas.paste(img, ((side - cw) // 2, (side - ch) // 2))
    return canvas

ROOT = Path(__file__).resolve().parent
THUMBS = ROOT / ".thumbs"
THUMB_PX = 280      # shown at ~95px in the wear grid; 280 covers 2x DPR with room
QUALITY = 76
ZOOM_PX = 660      # lightbox: uncropped, so you see the whole garment
ZOOM_QUALITY = 64  # inlined as data URIs, so every KB is paid on first load

# Head-to-toe, the order you actually put clothes on and read them off a body.
BODY_ORDER = ["hat", "scarf", "jacket", "midlayer", "top", "bottom", "shoes"]
SLOT_LABEL = {
    "hat": "Head", "scarf": "Neck", "jacket": "Outer", "midlayer": "Layer",
    "top": "Torso", "bottom": "Legs", "shoes": "Feet", "accessory": "Detail",
}

# Garment colour names -> hex, for the 3D figure. Longest/most specific match wins,
# so "dark brown" is tested before "brown".
COLOR_HEX = [
    ("cornflower blue", "#7396c9"), ("washed navy", "#3d5670"), ("dark indigo", "#25344b"),
    ("grey overcheck", "#8e8d88"), ("light blue", "#a9c7de"), ("sage green", "#8b9481"),
    ("tobacco", "#7d5531"), ("dark brown", "#4e3826"), ("cream/ecru", "#ece2cf"),
    ("pale stone", "#cfc9bd"), ("oatmeal", "#ded3bf"), ("charcoal", "#3b3f44"),
    ("taupe", "#9c9184"), ("cognac", "#a86a3c"), ("khaki", "#a89372"), ("camel", "#bd9560"),
    ("greige", "#b6ab98"), ("beige", "#cbb894"), ("cream", "#eee4d2"), ("straw", "#b99a68"),
    ("navy", "#22314b"), ("olive", "#6f7355"), ("stone", "#c3bcae"), ("sand", "#cdbb9a"),
    ("white", "#f4f2ee"), ("black", "#22242a"), ("grey", "#9aa0a6"), ("tan", "#b78a5c"),
    ("brown", "#6b4a30"), ("blue", "#4d6d94"), ("green", "#6f7f6b"), ("silver", "#c3c8cd"),
]
SKIN = "#8a5f3f"   # figure-silhouette tone; set this to match the wearer


def color_of(item: dict) -> str:
    c = (item.get("color") or "").lower()
    for key, hexv in COLOR_HEX:
        if key in c:
            return hexv
    return "#9c9c9c"


ICON = {
    "temp": '<path d="M8 10.5V4a2 2 0 1 1 4 0v6.5a4 4 0 1 1-4 0Z"/><path d="M10 8v5"/>',
    "low": '<path d="M8 10.5V4a2 2 0 1 1 4 0v6.5a4 4 0 1 1-4 0Z"/><path d="M10 11v2"/>',
    "chance": '<path d="M10 2.5S4.5 8.6 4.5 12a5.5 5.5 0 0 0 11 0C15.5 8.6 10 2.5 10 2.5Z"/>',
    "amount": '<path d="M6 3v5M10 3v9M14 3v5"/><path d="M4.5 15.5c1.4 1 2.6 1 4 0s2.6-1 4 0"/>',
    "wind": '<path d="M2.5 7h9a2.5 2.5 0 1 0-2.5-2.5"/><path d="M2.5 11h12a2.5 2.5 0 1 1-2.5 2.5"/>',
    "uv": '<circle cx="10" cy="10" r="3.2"/><path d="M10 2.4v1.8M10 15.8v1.8M2.4 10h1.8M15.8 10h1.8'
          'M4.6 4.6l1.3 1.3M14.1 14.1l1.3 1.3M15.4 4.6l-1.3 1.3M5.9 14.1l-1.3 1.3"/>',
    "humid": '<path d="M10 2.5S5 8.2 5 11.6a5 5 0 0 0 10 0C15 8.2 10 2.5 10 2.5Z"/>'
             '<path d="M12.6 11.8a2.6 2.6 0 0 1-2.6 2.6"/>',
    "sun": '<path d="M2.6 14.5h14.8"/><path d="M5.6 14.5a4.4 4.4 0 0 1 8.8 0"/>'
           '<path d="M10 4.2v1.6M4.4 6.6l1.1 1.1M15.6 6.6l-1.1 1.1"/>',
    "cloud": '<path d="M6 14.5h7.6a3.1 3.1 0 0 0 .3-6.2 4.4 4.4 0 0 0-8.4 1.1A2.6 2.6 0 0 0 6 14.5Z"/>',
}


def thumb_data_uri(rel_path: str) -> str:
    """Return a base64 data URI for a garment photo, generating/caching a thumb."""
    src = ROOT / rel_path
    cache = THUMBS / (rel_path.replace("/", "__").replace("\\", "__").rsplit(".", 1)[0] + ".jpg")
    cache.parent.mkdir(parents=True, exist_ok=True)

    if not cache.exists() or cache.stat().st_mtime < src.stat().st_mtime:
        img = Image.open(src)
        img = unify_bg(ImageOps.exif_transpose(img).convert("RGB"))
        img.thumbnail((THUMB_PX, THUMB_PX), Image.LANCZOS)
        img.save(cache, "JPEG", quality=QUALITY, optimize=True)

    return "data:image/jpeg;base64," + base64.b64encode(cache.read_bytes()).decode()


PLAY_PX = 250
PLAY_Q = 70

try:
    import numpy as _np
    from scipy import ndimage as _ndi
    _CUT_OK = True
except ImportError:                      # no numpy/scipy: every tile falls back
    _CUT_OK = False


def cutout_rgba(img: Image.Image):
    """Cut the garment from its backdrop, safely.

    The naive cut (drop all near-white) ate white garments. This one removes
    only pixels that are BOTH background-flat AND reachable from the border
    without crossing the garment's own edge: a white tee's interior is sealed
    behind its shadowed outline, so it cannot leak away. Every result is
    validated - if the cut would cost the garment more than a sliver of its
    strong pixels, return None and the caller keeps the blend fallback."""
    if not _CUT_OK:
        return None
    arr = _np.asarray(img, dtype=_np.int16)
    d = _np.abs(arr - 245).max(axis=2)
    weak = d <= 12                        # could be backdrop
    strong = d >= 26                      # definitely garment
    lbl, n = _ndi.label(weak)
    if n == 0:
        return None
    border = _np.unique(_np.concatenate([lbl[0], lbl[-1], lbl[:, 0], lbl[:, -1]]))
    border = border[border != 0]
    bg = _np.isin(lbl, border)

    ink = int(strong.sum())
    if ink == 0:
        return None
    eaten = int((strong & bg).sum())
    kept_soft = int(((d >= 14) & ~bg).sum())
    total_soft = int((d >= 14).sum())
    if eaten > ink * 0.01 or (total_soft and kept_soft / total_soft < 0.92):
        return None                       # unsafe for this garment - fall back

    # Hollow-ghost check: fill the garment's outline into a solid hull; if the
    # flood claimed a big share of the INSIDE of that hull, the garment body
    # itself leaked away (white trousers, white shoes) - not a real cut.
    hull = _ndi.binary_fill_holes(_ndi.binary_dilation(strong, iterations=3))
    hullsum = int(hull.sum())
    if hullsum and int((bg & hull).sum()) > hullsum * 0.05:
        return None

    alpha = _np.where(bg, 0, 255).astype(_np.uint8)
    alpha = _ndi.grey_erosion(alpha, size=2)          # pull in a hair
    alpha = _ndi.gaussian_filter(alpha.astype(float), 1.1).clip(0, 255).astype(_np.uint8)
    out = img.convert("RGBA")
    out.putalpha(Image.fromarray(alpha, "L"))
    bbox = out.getbbox()
    if bbox:
        out = out.crop(bbox)
    return out


def play_data_uri(rel_path: str):
    """Playground tile: a true alpha cut-out when the garment survives the
    knife, else the white-lifted blend tile. Returns (data_uri, is_cut)."""
    src = ROOT / rel_path
    stem = "playf__" + rel_path.replace("/", "__").replace("\\", "__").rsplit(".", 1)[0]
    png = THUMBS / (stem + ".webp")
    jpg = THUMBS / (stem + ".jpg")
    png.parent.mkdir(parents=True, exist_ok=True)

    if not png.exists() and not jpg.exists():
        img = Image.open(src)
        img = unify_bg(ImageOps.exif_transpose(img).convert("RGB"))
        cut = cutout_rgba(img)
        if cut is not None:
            cut.thumbnail((PLAY_PX, PLAY_PX), Image.LANCZOS)
            cut.save(png, "WEBP", quality=82, method=6)
        else:
            lift = [min(255, round(i * 255 / 245)) for i in range(256)]
            img = img.point(lift * 3)
            img.thumbnail((PLAY_PX, PLAY_PX), Image.LANCZOS)
            img.save(jpg, "JPEG", quality=PLAY_Q, optimize=True)

    if png.exists():
        return ("data:image/webp;base64," + base64.b64encode(png.read_bytes()).decode(), 1)
    return ("data:image/jpeg;base64," + base64.b64encode(jpg.read_bytes()).decode(), 0)


def zoom_data_uri(rel_path: str) -> str:
    """Full-garment (uncropped) version for the lightbox, aspect preserved."""
    src = ROOT / rel_path
    cache = THUMBS / (
        "zoom__" + rel_path.replace("/", "__").replace("\\", "__").rsplit(".", 1)[0] + ".jpg"
    )
    cache.parent.mkdir(parents=True, exist_ok=True)

    if not cache.exists() or cache.stat().st_mtime < src.stat().st_mtime:
        img = Image.open(src)
        img = unify_bg(ImageOps.exif_transpose(img).convert("RGB"))
        img.thumbnail((ZOOM_PX, ZOOM_PX), Image.LANCZOS)
        img.save(cache, "JPEG", quality=ZOOM_QUALITY, optimize=True, progressive=True)

    return "data:image/jpeg;base64," + base64.b64encode(cache.read_bytes()).decode()


def short_date(today: dict) -> str:
    """Numerals only - '03.08.26' - to sit on the wordmark's line."""
    iso = today.get("iso")
    if iso:
        try:
            return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%y")
        except ValueError:
            pass
    return today.get("date", "").split(",")[-1].strip()


def stat(kind: str, label: str, value: str, qual: str) -> str:
    """One cell of the condition strip. The long-form reading lives in `title`
    so it stays available on hover without costing vertical space."""
    return (
        f'<div class="stat" title="{qual}">'
        f'<span class="shead">'
        f'<svg class="ic" viewBox="0 0 20 20" fill="none" stroke="currentColor" '
        f'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">{ICON[kind]}</svg>'
        f'<span class="slabel">{label}</span></span>'
        f'<span class="sval">{value}</span></div>'
    )


def look_fx(look: dict, fallback: str) -> str:
    """Which ambient effect belongs to THIS slot, not to the day as a whole.
    A dry morning under a wet afternoon should show a still page: rain falling
    behind a 10%-chance look is the same lie as dressing him for the day's max.
    Explicit "mode" on the look wins; otherwise read the chance out of "wx"."""
    if look.get("mode"):
        return look["mode"]
    m = re.search(r"(\d+)\s*%", look.get("wx", ""))
    if m:
        return "rain" if int(m.group(1)) >= 45 else "clear"
    return fallback


def main() -> None:
    closet = json.loads((ROOT / "closet.json").read_text(encoding="utf-8"))
    today = json.loads((ROOT / "today.json").read_text(encoding="utf-8"))
    by_id = {it["id"]: it for it in closet["items"]}
    w = today["weather"]

    deck = []
    zoomdata = {}          # item id -> full-size photo, emitted once and shared
    for idx, look in enumerate(today["looks"]):
        items = [by_id[i] for i in look["items"]]
        worn = [it for it in items if it["cat"] in BODY_ORDER]
        worn.sort(key=lambda it: BODY_ORDER.index(it["cat"]))
        details = [it for it in items if it["cat"] not in BODY_ORDER]

        # Head-to-toe down the middle, the order you actually put them on.
        wearline = "".join(
            f'<figure class="wear" style="--d:{i}">'
            f'<button type="button" data-zoom="{zoom_data_uri(it["file"])}" '
            f'data-name="{it["name"]}" data-slot="{SLOT_LABEL.get(it["cat"], "Detail")}" '
            f'aria-label="Enlarge {it["name"]}">'
            f'<img src="{thumb_data_uri(it["file"])}" alt="{it["name"]}" loading="lazy"></button>'
            f'</figure>'
            for i, it in enumerate(worn)
        )
        # Watch, belt and the like sit off to the side so they don't shrink the clothes.
        sidebar = "".join(
            f'<figure class="side" style="--d:{len(worn) + i}">'
            f'<button type="button" data-zoom="{zoom_data_uri(it["file"])}" '
            f'data-name="{it["name"]}" data-slot="Detail" aria-label="Enlarge {it["name"]}">'
            f'<img src="{thumb_data_uri(it["file"])}" alt="{it["name"]}" loading="lazy"></button>'
            f'</figure>'
            for i, it in enumerate(details)
        )
        side_block = f'<div class="siderail">{sidebar}</div>' if sidebar else ""

        n = idx + 1

        # Swipe-deck twin: same look, compact, judged one at a time.
        for it in worn + details:
            if it["id"] not in zoomdata:
                zoomdata[it["id"]] = {
                    "src": zoom_data_uri(it["file"]),
                    "name": it["name"],
                    "slot": SLOT_LABEL.get(it["cat"], "Detail"),
                }
        deck.append(
            f'<article class="swipe" data-n="{n}" data-title="{look["title"]}" '
            f'data-fx="{look_fx(look, w.get("mode", "clear"))}" style="--k:{idx}">'
            f'<span class="stamp keep">Keep</span><span class="stamp pass">Pass</span>'
            f'<div class="sfig"><div class="wearline">{wearline}</div>{side_block}</div>'
            f'<div class="sbot">'
            f'<span class="badge {look.get("tone", "daily")}">{look["tag"]}</span>'
            f'<h3>{look["title"]}</h3>'
            + (f'<p class="slotline">{look["when"]}'
               + (f' &middot; {look["wx"]}' if look.get("wx") else "")
               + '</p>' if look.get("when") else "")
            + f'</div></article>'
        )

    # Data-driven so the rail can grow without touching the template. Falls back
    # to the original four keys if a day's file predates the metrics list.
    metrics = w.get("metrics")
    if not metrics:
        metrics = [
            {"i": "temp", "l": "High", "v": f'{w["temp"]}&deg;',
             "t": f'Feels like {w["feels"]}&deg;'},
            {"i": "chance", "l": "Chance", "v": f'{w["rain_chance"]}%',
             "t": f'Chance of rain &mdash; {w["rain_chance_word"]}'},
            {"i": "amount", "l": "Amount", "v": w["rain_amount"].replace(" ", ""),
             "t": f'How much rain, if it comes &mdash; {w["rain_amount_word"]}'},
            {"i": "wind", "l": "Wind", "v": w["wind"].replace(" km/h", "<i>km/h</i>"),
             "t": w["wind_word"]},
        ]
    stats = "".join(stat(m["i"], m["l"], m["v"], m.get("t", "")) for m in metrics)

    tokens = {
        "{{MODE}}": w.get("mode", "clear"),
        "{{DATE}}": today["date"],
        "{{PLACE}}": today["place"],
        "{{SUMMARY}}": w["summary"],
        "{{STATS}}": stats,
        "{{CONFIDENCE}}": w["confidence"],
        "{{CONFCLASS}}": w["confidence_class"],
        "{{AGENDA}}": today["agenda"],
        "{{SHORTDATE}}": short_date(today),
        "{{ZOOMDATA}}": json.dumps(zoomdata, ensure_ascii=False),
        "{{PLAYCLOSET}}": json.dumps([
            {"i": it["id"], "c": it["cat"], "n": it["name"],
             "d": (lambda pr: pr[0])(play_data_uri(it["file"])),
             "a": play_data_uri(it["file"])[1]} for it in closet["items"]]),
        "{{DECK}}": "".join(reversed(deck)),   # last card painted first = bottom of pile
        "{{NLOOKS}}": str(len(deck)),
        # Where the "send my feedback" button drafts to. Read from the closet's
        # _meta so no address is baked into this file; unset means the button
        # falls back to copying the text to the clipboard.
        "{{FEEDBACK_EMAIL}}": (closet.get("_meta") or {}).get("owner", ""),
    }
    html = TEMPLATE
    for k, v in tokens.items():
        html = html.replace(k, str(v))

    # Escape every non-ASCII char to a numeric entity. The published page has no
    # charset meta of its own, so raw UTF-8 bytes can be decoded as latin-1 and
    # turn every em-dash into mojibake. Entities are charset-proof.
    html = html.encode("ascii", "xmlcharrefreplace").decode("ascii")

    out = ROOT / "lookbook.html"
    out.write_text(html, encoding="ascii")

    # Archive the day so tomorrow's run can see what was already shown and rotate away
    # from it. This is what stops the wardrobe collapsing onto a few favourites.
    hist = ROOT / "history"
    hist.mkdir(exist_ok=True)
    (hist / f"{today.get('iso', 'undated')}.json").write_text(
        json.dumps(today, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    shown = sorted({i for look in today["looks"] for i in look["items"]})

    # Rotation index: the daily task only needs "which ids ran on which day".
    # Reading 10 full archives to learn that costs ~9k tokens a run; this one
    # small file costs ~100. Written here so it can never drift from the archive.
    idx_path = hist / "index.json"
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        idx = {}
    idx[today.get("iso", "undated")] = shown
    idx = dict(sorted(idx.items())[-14:])          # a fortnight is plenty
    idx_path.write_text(json.dumps(idx, separators=(",", ":")), encoding="utf-8")

    # Styling brief: everything the daily task reasons over, minus the photo
    # paths and metadata only this script needs. Regenerated every build from
    # closet.json, so it can never drift from the source of truth.
    brief = [{k: v for k, v in it.items() if k != "file"} for it in closet["items"]]
    (ROOT / "closet-brief.json").write_text(
        json.dumps(brief, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # Keep the feedback log from growing without bound - the task reads it whole.
    fb_path = ROOT / "feedback.json"
    try:
        fb = json.loads(fb_path.read_text(encoding="utf-8"))
        if len(fb.get("entries", [])) > 40:
            fb["entries"] = fb["entries"][-40:]
            fb_path.write_text(json.dumps(fb, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError):
        pass
    print(f"wrote {out}  ({out.stat().st_size/1024:.0f} KB)  mode={w.get('mode','clear')}")
    print(f"archived {today.get('iso','undated')} · items shown: {' '.join(shown)}")


TEMPLATE = r"""<title>style.</title>
<!-- Deliberately NO viewport meta. The published page runs inside an iframe, where
     width=device-width resolves to the DEVICE screen, not the frame - laying the
     page out at ~1500px and clipping it to the frame. The host head supplies it. -->
<style>
  /* ---------- tokens ---------------------------------------------------- */
  :root {
    --sans: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text",
            "Segoe UI Variable Display", "Segoe UI", system-ui, Roboto, sans-serif;

    --r-card: 14px;
    --r-tile: 8px;

    /* Monochrome only. White ground, black ink, greys as structure.
       The design commits to white deliberately: weather changes the content,
       never the palette. Only the viewer's explicit dark toggle inverts it. */
    /* #f5f5f5 is the measured median background of the garment photos, so a
       cut-out sits on the page with no visible seam. */
    --bg:#f5f5f5;
    --ink:#000000; --ink-2:#5a5a5a; --ink-3:#8b8b8b;
    --glass:#f5f5f5; --glass-2:#f5f5f5;
    --edge:#dedede; --hair:#e7e7e7;
    --accent:#000000; --accent-2:#000000;
    --sky-3:#ffffff;                 /* text colour that sits on a black fill */
    --city:#f0f0f0; --city-far:#f7f7f7;
    --glow:rgba(0,0,0,.13);
    --shadow:0 1px 2px rgba(0,0,0,.04), 0 10px 30px -14px rgba(0,0,0,.16);
  }
  /* Weather modes keep their hooks but no longer tint the page - only the
     rain canvas reacts, and it draws in grey. */
  :root[data-mode="rain"], :root[data-mode="overcast"], :root[data-mode="night"] {
    --accent:#000000; --accent-2:#000000;
  }
  /* The page is white, always. The garment cut-outs are matted onto #f5f5f5,
     so inverting the ground put every photo on an island of light in a black
     field - it looked broken, not dark. No dark override, and color-scheme is
     pinned so the viewer's dark toggle can't restyle the scrollbars either. */
  :root { color-scheme: light; }

  * { box-sizing:border-box; }
  /* .decide sets display, which would otherwise beat the hidden attribute */
  [hidden] { display:none !important; }
  body {
    margin:0; color:var(--ink); font-family:var(--sans);
    line-height:1.5; -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
    background:var(--bg); min-height:100vh;
  }

  /* ---------- ambient layer -------------------------------------------- */
  #fx { position:fixed; inset:0; z-index:1; pointer-events:none; }
  /* Reactive dot field, BEHIND the content (z-index 0 vs .wrap's 2) so it can
     never obscure anything. Cleared each frame, never filled. */
  #grid { position:fixed; inset:0; z-index:0; pointer-events:none; }
  /* Magnetic cursor: a dot that trails the pointer and opens into a ring over
     anything interactive. Two elements, GPU-transformed only - no canvas, so it
     can never paint over the page. pointer-events:none keeps clicks reaching UI. */
  /* Above EVERYTHING. The deck gives cards z-index 50 while stacking, and the
     cursor disappearing behind a card reads as it freezing over the widget. */
  #cur, #curRing { position:fixed; top:0; left:0; z-index:2147483647; pointer-events:none;
                   border-radius:50%; opacity:0;
                   transition:opacity .25s ease, width .25s cubic-bezier(.22,1,.36,1),
                              height .25s cubic-bezier(.22,1,.36,1),
                              background .2s ease, border-color .2s ease; }
  #cur { width:7px; height:7px; background:var(--ink); margin:-3.5px 0 0 -3.5px; }
  #curRing { width:30px; height:30px; margin:-15px 0 0 -15px;
             border:1px solid var(--ink); opacity:0; }
  /* over the playground's night sky a black cursor vanishes - flip it white */
  html.pg-on #cur { background:#fff; }
  html.pg-on #curRing { border-color:#fff; }
  html.pg-on.cur-press #curRing { background:rgba(255,255,255,.14); }
  /* over something you can act on, the ring blooms and the dot recedes */
  html.cur-on #cur, html.cur-on #curRing { opacity:1; }
  html.cur-hot #curRing { width:46px; height:46px; margin:-23px 0 0 -23px; }
  html.cur-hot #cur { width:4px; height:4px; margin:-2px 0 0 -2px; }
  html.cur-press #curRing { width:24px; height:24px; margin:-12px 0 0 -12px;
                            background:rgba(0,0,0,.06); }
  /* driven by the class alone - JS only adds it after a real mouse moves */
  html.cur-on, html.cur-on * { cursor:none !important; }
  @media (prefers-reduced-motion:reduce) { #cur, #curRing { display:none; } }


  .wrap { position:relative; z-index:2; max-width:1120px; margin:0 auto;
          padding:clamp(24px,5vw,52px) clamp(16px,4vw,34px) 72px; }

  /* ---------- masthead: the wordmark ----------------------------------- */
  .mast { display:flex; align-items:baseline; justify-content:space-between; gap:14px;
          margin:0 0 24px; }
  .mark { font-size:clamp(1.9rem,5.4vw,2.5rem); font-weight:500; letter-spacing:-.045em;
          line-height:1; color:var(--ink); }
  .today { font-size:.82rem; font-weight:600; letter-spacing:-.01em; color:var(--ink);
           white-space:nowrap; font-variant-numeric:tabular-nums; }

  /* ---------- playground: door in the masthead, world behind it ---------- */
  .playlink { font:inherit; font-size:.68rem; font-weight:700; letter-spacing:.22em;
    text-transform:uppercase; color:var(--ink-3); background:none; border:none;
    cursor:pointer; padding:6px 10px; transition:color .2s ease, letter-spacing .3s ease; }
  .playlink:hover { color:var(--ink); letter-spacing:.3em; }
  .playlink:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }

  /* the transition: a black disc that swells from the tap point to swallow the page */
  #warp { position:fixed; left:0; top:0; width:60px; height:60px; margin:-30px 0 0 -30px;
    border-radius:50%; background:#000; transform:scale(0); z-index:58;
    pointer-events:none; }
  #warp.go { transition:transform .6s cubic-bezier(.55,0,.45,1); }

  #pg { position:fixed; inset:0; z-index:60; background:#000; display:none; }
  #pg.on { display:block; }
  #pg canvas { width:100%; height:100%; display:block; cursor:grab; touch-action:none; }
  #pg canvas:active { cursor:grabbing; }
  #pg .pgmark { position:absolute; left:22px; top:18px; z-index:2; font-size:1.15rem;
    font-weight:500; letter-spacing:-.04em; color:#fff; background:none; border:none;
    cursor:pointer; padding:6px 4px; font-family:var(--sans); opacity:0;
    transition:opacity .5s ease .35s; }
  #pg.on .pgmark { opacity:1; }
  #pg .pgmark:focus-visible { outline:2px solid #fff; outline-offset:2px; }
  #pg .pghint { position:absolute; right:22px; bottom:16px; z-index:2; font-size:.58rem;
    font-weight:700; letter-spacing:.2em; text-transform:uppercase; color:#777;
    pointer-events:none; opacity:0; transition:opacity .5s ease .5s; }
  #pg.on .pghint { opacity:1; }
  @media (prefers-reduced-motion:reduce) {
    #warp.go { transition:none; }
    #pg .pgmark, #pg .pghint { transition:none; opacity:1; }
  }

  /* ---------- the fitting rig: glass over the world ---------------------- */
  #rigToggle { position:absolute; right:20px; top:18px; z-index:3; font:inherit;
    font-size:.95rem; font-weight:600; letter-spacing:-.02em; color:#fff;
    background:rgba(255,255,255,.08); border:1px solid rgba(255,255,255,.22);
    border-radius:999px; padding:7px 16px; cursor:pointer; opacity:0;
    transition:opacity .5s ease .45s, background .2s ease; }
  #pg.on #rigToggle { opacity:1; }
  #rigToggle:hover { background:rgba(255,255,255,.16); }
  #rigToggle:focus-visible { outline:2px solid #fff; outline-offset:2px; }

  /* wardrobe shelf: pinned across the top, under the masthead row */
  #rigbar { position:absolute; left:0; right:0; top:62px; z-index:2;
    padding:6px 14px 10px;
    background:linear-gradient(180deg, rgba(0,0,0,.72) 0%, rgba(0,0,0,.38) 70%, transparent 100%);
    opacity:0; pointer-events:none; transition:opacity .45s ease .3s; }
  #pg.on #rigbar { opacity:1; pointer-events:auto; }
  #rigbar.hidden { opacity:0 !important; pointer-events:none !important;
    transition:opacity .3s ease; }

  /* the board: real frosted glass - the planet stays vaguely visible through it */
  #rig { position:absolute; left:50%; top:56%; transform:translate(-50%,-50%);
    z-index:2; width:min(460px, 93vw); height:min(66vh, 640px); display:flex;
    flex-direction:column; border-radius:22px; overflow:hidden;
    background:rgba(255,255,255,.07); border:1px solid rgba(255,255,255,.22);
    -webkit-backdrop-filter:blur(4px) saturate(1.15) brightness(1.1);
    backdrop-filter:blur(4px) saturate(1.15) brightness(1.1);
    box-shadow:0 30px 80px -20px rgba(0,0,0,.6), inset 0 1px 0 rgba(255,255,255,.18);
    opacity:0; pointer-events:none; transition:opacity .45s ease .3s,
    transform .45s cubic-bezier(.22,1,.36,1) .3s; }
  #pg.on #rig { opacity:1; pointer-events:auto; }
  #rig.hidden { opacity:0 !important; pointer-events:none !important;
    transform:translate(-50%,-46%); transition:opacity .3s ease, transform .3s ease; }

  .rigtabs { display:flex; gap:2px; overflow-x:auto; scrollbar-width:none;
    padding:6px 0; -webkit-overflow-scrolling:touch; }
  .rigtabs::-webkit-scrollbar { display:none; }
  .rigtab { flex:0 0 auto; font:inherit; font-size:.6rem; font-weight:700;
    letter-spacing:.16em; text-transform:uppercase; color:#9aa3ad; background:none;
    border:none; border-radius:999px; padding:7px 12px; cursor:pointer;
    transition:color .15s, background .15s; }
  .rigtab.on { color:#0b0f14; background:#fff; }
  .rigtab:not(.on):hover { color:#fff; }
  .rigtab:focus-visible { outline:2px solid #fff; outline-offset:2px; }

  /* the board: his pieces, stacked head-to-toe to play with */
  .rigstage { flex:1 1 auto; min-height:0; overflow-y:auto; padding:14px 16px;
    display:flex; flex-direction:column; gap:12px; align-items:center;
    scrollbar-width:none; }
  .rigstage::-webkit-scrollbar { display:none; }
  .rigempty { color:#cfd5db; font-size:.74rem; letter-spacing:.06em; margin:auto;
    text-align:center; text-shadow:0 1px 6px rgba(0,0,0,.5); }
  .rigslot { position:relative; flex:0 0 auto;
    animation:rigin .45s cubic-bezier(.22,1,.36,1) backwards; }
  @keyframes rigin { from { opacity:0; transform:translateY(10px) scale(.95); } }
  .rigtile { position:relative; width:min(150px, 36vw); aspect-ratio:1; }
  .rigslot.small .rigtile { width:min(74px, 19vw); }
  /* accessories sit shoulder to shoulder so a full board still has room */
  .rigextras { display:flex; flex-wrap:wrap; gap:10px; justify-content:center;
    flex:0 0 auto; }
  .rigtile img { width:100%; height:100%; object-fit:contain; display:block;
    border-radius:14px; background:rgba(245,245,245,.92);
    border:1px solid rgba(255,255,255,.35); }
  /* true cut-outs need no card at all - garment floats on the glass */
  .rigtile img.cut { background:none; border:none; border-radius:0;
    filter:drop-shadow(0 6px 14px rgba(0,0,0,.45)); }
  .rigx { position:absolute; top:-8px; right:-8px; width:26px; height:26px; padding:0;
    border-radius:50%; background:#0b0f14; color:#fff;
    border:1px solid rgba(255,255,255,.45); display:grid; place-items:center;
    cursor:pointer; z-index:2; transition:background .15s, color .15s; }
  .rigx svg { width:9px; height:9px; }
  .rigx:hover { background:#fff; color:#000; }
  .rigx:focus-visible { outline:2px solid #fff; outline-offset:2px; }
  /* the tray: the active category, scrolled sideways */
  .rigtray { display:flex; gap:8px; overflow-x:auto; padding:4px 0 6px;
    -webkit-overflow-scrolling:touch; scrollbar-width:none; }
  .rigtray::-webkit-scrollbar { display:none; }
  .rigtray button { flex:0 0 auto; width:66px; height:66px; padding:0; border:none;
    border-radius:10px; overflow:hidden; cursor:pointer; background:#f5f5f5;
    opacity:.88; transition:opacity .15s, transform .25s cubic-bezier(.22,1,.36,1); }
  .rigtray button:hover { opacity:1; transform:translateY(-3px); }
  .rigtray button.worn { outline:2px solid #fff; outline-offset:-2px; opacity:1; }
  .rigtray button img { width:100%; height:100%; object-fit:contain; display:block; }
  .rigtray button:focus-visible { outline:2px solid #fff; outline-offset:2px; }

  /* ---------- surfaces: flat, bordered, no blur ------------------------ */
  .glass { background:var(--glass); border:1px solid var(--edge); border-radius:var(--r-card); }

  /* ---------- stat bar -------------------------------------------------- */
  /* A draggable instrument rail. Deliberately NOT full-bleed: negative viewport
     margins made the whole page scroll sideways. A part-cut cell at the right
     edge is affordance enough that there is more to drag to. */
  .statwrap { position:relative; margin:0 0 32px; }
  .statbar { display:flex; overflow-x:auto; overscroll-behavior-x:contain;
             scroll-snap-type:x proximity; -webkit-overflow-scrolling:touch;
             border-top:1px solid var(--edge); border-bottom:1px solid var(--edge);
             cursor:grab; scrollbar-width:none; -ms-overflow-style:none;
             /* dragging a rail should never select its labels */
             user-select:none; -webkit-user-select:none; }
  .statbar::-webkit-scrollbar { display:none; }
  .statbar.dragging { cursor:grabbing; scroll-snap-type:none; }
  .stat { flex:0 0 auto; width:106px; display:flex; flex-direction:column; gap:7px;
          padding:12px 8px 13px; scroll-snap-align:start; text-align:center;
          align-items:center; border-right:1px solid var(--hair); }
  .stat:last-child { border-right:none; }
  /* fades at both ends so the rail reads as continuous, not clipped */
  .statwrap::before, .statwrap::after {
    content:""; position:absolute; top:1px; bottom:1px; width:26px; z-index:2;
    pointer-events:none; opacity:0; transition:opacity .2s ease; }
  .statwrap::before { left:0;
    background:linear-gradient(90deg,var(--bg),transparent); }
  .statwrap::after { right:0;
    background:linear-gradient(270deg,var(--bg),transparent); }
  .statwrap.more-l::before, .statwrap.more-r::after { opacity:1; }
  .shead { display:flex; align-items:center; justify-content:center; gap:5px; min-width:0; }
  .ic { width:13px; height:13px; flex:none; color:var(--ink-3); }
  .slabel { font-size:.55rem; font-weight:600; letter-spacing:.13em; text-transform:uppercase;
            color:var(--ink-3); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .sval { font-size:1.18rem; font-weight:600; letter-spacing:-.035em; line-height:1;
          font-variant-numeric:tabular-nums; white-space:nowrap; }
  .sval i { font-style:normal; font-size:.62em; font-weight:500; letter-spacing:0;
            color:var(--ink-3); margin-left:1px; }
  @media (max-width:360px) {
    .slabel { font-size:.5rem; letter-spacing:.07em; }
    .sval { font-size:1.02rem; }
    .ic { display:none; }
  }

  /* ---------- confidence ------------------------------------------------ */

  /* ---------- swipe deck ------------------------------------------------ */
  .decide { display:flex; flex-direction:column; align-items:center; }
  .dhead { font-size:.72rem; font-weight:600; letter-spacing:.1em; color:var(--ink-3);
    margin:16px 0 0; text-align:center; font-variant-numeric:tabular-nums; }
  .dhead #dcount { color:var(--ink); }
  /* Grows into whatever height the screen has, so the garments get as large as
     the viewport allows instead of leaving a void underneath. */
  .deck { position:relative; width:min(430px,100%); height:clamp(460px,74vh,780px); }
  .swipe { position:absolute; inset:0; padding:10px; display:flex; flex-direction:column;
    background:transparent; border:1px solid var(--edge); border-radius:var(--r-card);
    box-shadow:var(--shadow); cursor:grab; touch-action:none; overflow:hidden;
    transform:translateY(calc(var(--k) * 9px)) scale(calc(1 - var(--k) * .035));
    transition:transform .4s cubic-bezier(.22,1,.36,1), opacity .3s; }
  .swipe:active { cursor:grabbing; }
  /* Cards waiting behind are fully hidden. Transparent cards with visible
     borders stacked up as a nest of outlines, which just read as clutter. */
  .swipe.behind { opacity:0; pointer-events:none; }
  .swipe.gone { transition:transform .45s cubic-bezier(.4,0,.6,1), opacity .45s; opacity:0; }
  /* The garments themselves, laid out head-to-toe. The stage carries the same
     dot lattice as the page background so the pieces sit in that field. */
  /* No border, no background: the card is transparent, so the page's own
     reactive dot field shows through and stays magnetic under the clothes. */
  .sfig { flex:1 1 auto; min-height:0; padding:4px 2px; overflow:hidden;
    display:flex; gap:6px; }

  /* The rail floats over the right edge instead of taking a column, so the
     clothes stay centred on the card rather than being pushed off-centre. */
  .siderail { position:absolute; right:6px; top:50%; transform:translateY(-50%);
              width:66px; display:flex; flex-direction:column; gap:10px; }
  .side { margin:0; animation:wearin .55s cubic-bezier(.22,1,.36,1) backwards;
          animation-delay:calc(var(--d) * 80ms); }
  .side button { display:block; width:100%; padding:0; background:none; border:none;
    cursor:zoom-in; transition:transform .3s cubic-bezier(.22,1,.36,1); }
  .side button:hover { transform:scale(1.08); }
  .side button:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
  .side img { width:100%; aspect-ratio:1; object-fit:contain; display:block; }
  /* One garment per row, full width, in the order you put them on:
     head at the top, shoes at the bottom. Rows share the height evenly. */
  .wearline { display:flex; flex-direction:column; gap:6px; height:100%; }
  .wear { margin:0; position:relative; flex:1 1 0; min-height:0;
          animation:wearin .55s cubic-bezier(.22,1,.36,1) backwards;
          animation-delay:calc(var(--d) * 80ms); }
  @keyframes wearin { from { opacity:0; transform:translateY(18px) scale(.96); } }
  .wear button { display:block; width:100%; height:100%; padding:0; background:none;
    border:none; cursor:zoom-in; transition:transform .3s cubic-bezier(.22,1,.36,1); }
  .wear button:hover { transform:scale(1.04); }
  .wear button:focus-visible { outline:2px solid var(--accent); outline-offset:3px;
    border-radius:10px; }
  .wear button:focus:not(:focus-visible) { outline:none; }
  .side button:focus:not(:focus-visible) { outline:none; }
  .wear img { width:100%; height:100%; object-fit:contain; display:block; }
  .sbot { flex:0 0 auto; }
  .sbot h3 { font-size:1.15rem; font-weight:600; letter-spacing:-.032em; margin:8px 0 0; }
  /* which slice of the day this look is for, and that slice's own weather */
  .slotline { font-size:.62rem; font-weight:600; letter-spacing:.14em; text-transform:uppercase;
              color:var(--ink-3); margin:5px 0 0; font-variant-numeric:tabular-nums; }

  /* Verdict stamps: outline = pass, solid = keep. Weight carries the meaning, not hue. */
  .stamp { position:absolute; top:20px; z-index:3; font-size:.82rem; font-weight:700;
    letter-spacing:.2em; text-transform:uppercase; padding:8px 15px; border-radius:999px;
    opacity:0; pointer-events:none; border:1.5px solid var(--ink); }
  .stamp.keep { left:18px; background:var(--ink); color:var(--bg); transform:rotate(-9deg); }
  .stamp.pass { right:18px; background:var(--bg); color:var(--ink); transform:rotate(9deg); }

  .done { text-align:center; margin-top:26px; max-width:420px; }
  .done h3 { font-size:1.4rem; font-weight:700; letter-spacing:-.03em; margin:0 0 8px; }
  .done p { font-size:.86rem; color:var(--ink-2); margin:0 0 18px; white-space:pre-line; }
  .pill.wide { width:100%; padding:13px; font-size:.85rem; text-decoration:none;`n                box-sizing:border-box; }
  .linkish { font:inherit; font-size:.78rem; font-weight:600; background:none; border:none;
    color:var(--ink-3); cursor:pointer; padding:11px; text-decoration:underline;
    text-underline-offset:3px; }
  .linkish:hover { color:var(--ink); }

  /* ---------- reason sheet ---------------------------------------------- */
  /* No backdrop-filter anywhere: on a flat white page it buys nothing, and a
     full-viewport fixed element with one makes Chromium ghost the whole page. */
  #sheet { position:fixed; inset:0; z-index:45; display:none; align-items:flex-end;
    justify-content:center; background:rgba(0,0,0,.42); }
  #sheet.open { display:flex; }
  .sheetbody { width:min(460px,100%); border-radius:22px 22px 0 0; padding:22px 20px 26px;
    animation:slideup .3s cubic-bezier(.22,1,.36,1) both; }
  @keyframes slideup { from { transform:translateY(100%); } }
  @media (min-width:560px) {
    #sheet { align-items:center; }
    .sheetbody { border-radius:22px; }
    @keyframes slideup { from { transform:translateY(18px); opacity:0; } }
  }
  .sheetbody h3 { font-size:1.15rem; font-weight:700; letter-spacing:-.02em; margin:0 0 4px; }
  .shsub { font-size:.83rem; color:var(--ink-3); margin:0 0 16px; }
  .chips { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:14px; }
  .chip2 { font:inherit; font-size:.77rem; font-weight:600; cursor:pointer; padding:9px 14px;
    border-radius:999px; border:1px solid var(--hair); background:transparent; color:var(--ink-2);
    transition:color .16s, border-color .16s, background .16s; }
  .chip2:hover { color:var(--ink); border-color:var(--accent); }
  .chip2.on { background:var(--accent); border-color:var(--accent); color:var(--sky-3); }
  .chip2:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
  #shText { width:100%; font:inherit; font-size:.86rem; padding:12px 14px; border-radius:12px;
    border:1px solid var(--hair); background:var(--glass); color:var(--ink); }
  #shText:focus { outline:none; border-color:var(--accent); box-shadow:0 0 0 3px var(--glow); }
  .shbtns { display:flex; gap:10px; align-items:center; justify-content:flex-end; margin-top:16px; }

  /* ---------- looks ----------------------------------------------------- */
  .looks { display:grid; gap:20px; grid-template-columns:repeat(auto-fit,minmax(316px,1fr)); }
  .look { padding:22px; display:flex; flex-direction:column; position:relative;
          transition:transform .35s cubic-bezier(.22,1,.36,1), box-shadow .35s, border-color .35s;
          animation:rise .6s cubic-bezier(.22,1,.36,1) backwards;
          animation-delay:calc(var(--i) * 90ms); }
  @keyframes rise { from { opacity:0; transform:translateY(16px); } }
  .look:hover { transform:translateY(-4px); border-color:var(--accent);
                box-shadow:var(--shadow), 0 0 30px -6px var(--glow); }

  .badge { display:inline-flex; align-items:center; font-size:.6rem; font-weight:700;
    letter-spacing:.19em; text-transform:uppercase; padding:6px 13px; border-radius:999px;
    color:var(--accent); border:1px solid var(--accent); background:transparent; }
  .badge.reserve { background:var(--accent); color:var(--sky-3); border-color:var(--accent);
                   box-shadow:0 0 20px -4px var(--glow); }
  .lhead h2 { font-size:1.42rem; font-weight:700; letter-spacing:-.03em; margin:13px 0 7px;
              text-wrap:balance; }
  .why { color:var(--ink-2); font-size:.9rem; margin:0 0 18px; }

  /* ---------- rotatable colour study ----------------------------------- */
  .stage { position:relative; height:232px; margin:0 0 16px; border-radius:var(--r-tile);
    overflow:hidden; border:1px solid var(--hair);
    background:radial-gradient(120% 90% at 50% 8%,var(--glass-2),transparent 70%); }
  .mannequin { width:100%; height:100%; display:block; cursor:grab; touch-action:pan-y; }
  .mannequin:active { cursor:grabbing; }
  .mannequin:focus-visible { outline:2px solid var(--accent); outline-offset:-2px; }
  .spinhint { position:absolute; right:11px; bottom:9px; font-size:.55rem; font-weight:700;
    letter-spacing:.15em; text-transform:uppercase; color:var(--ink-3);
    pointer-events:none; transition:opacity .3s; }
  .stage.touched .spinhint { opacity:0; }

  /* ---------- the figure: head-to-toe, as worn ------------------------- */
  .figure { display:flex; gap:14px; margin-bottom:16px; align-items:flex-start; }
  .body { flex:1 1 auto; min-width:0; display:flex; flex-direction:column; gap:9px;
          position:relative; padding-left:17px; }
  /* spine: the line a body would hang on */
  .body::before { content:""; position:absolute; left:5px; top:14px; bottom:14px; width:1px;
    background:linear-gradient(180deg,transparent,var(--accent),transparent); opacity:.4; }

  .seg { margin:0; display:flex; align-items:center; gap:12px; position:relative; min-width:0; }
  /* node on the spine, marking each point down the body */
  .seg::before { content:""; position:absolute; left:-16px; top:50%; width:7px; height:7px;
    margin-top:-3.5px; border-radius:50%; background:var(--accent);
    box-shadow:0 0 0 3px var(--glow); }

  .frame { display:block; flex:none; width:82px; overflow:hidden; border-radius:var(--r-tile);
           border:1px solid var(--hair); box-shadow:0 4px 14px -6px rgba(0,0,0,.34);
           padding:0; background:none; cursor:zoom-in; }
  .frame:focus-visible, .lens:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
  .frame img { width:100%; aspect-ratio:1; object-fit:cover; display:block;
               transition:transform .5s cubic-bezier(.22,1,.36,1); }
  .seg:hover .frame img { transform:scale(1.13); }
  /* feet sit widest, head narrowest — reads as a body, not a list */
  .seg.hat .frame   { width:64px; }
  .seg.shoes .frame { width:96px; }

  figcaption { display:flex; flex-direction:column; min-width:0; }
  .slot { font-size:.56rem; font-weight:700; letter-spacing:.16em; text-transform:uppercase;
          color:var(--accent); }
  .nm { font-size:.76rem; color:var(--ink-2); line-height:1.34; }

  /* magnified detail chips — a watch dial is unreadable at tile size */
  .rail { flex:none; width:78px; display:flex; flex-direction:column; gap:11px;
          padding-left:13px; border-left:1px solid var(--hair); }
  .raillabel { font-size:.53rem; font-weight:700; letter-spacing:.16em; text-transform:uppercase;
               color:var(--ink-3); }
  .chip { margin:0; display:flex; flex-direction:column; gap:5px; align-items:center;
          text-align:center; }
  .lens { display:block; width:60px; height:60px; border-radius:50%; overflow:hidden;
          border:1px solid var(--edge); box-shadow:0 0 18px -6px var(--glow),
          0 4px 12px -5px rgba(0,0,0,.4); padding:0; background:none; cursor:zoom-in; }
  .lens img { width:100%; height:100%; object-fit:cover; display:block;
              transform:scale(1.55); transform-origin:center;
              transition:transform .5s cubic-bezier(.22,1,.36,1); }
  .chip:hover .lens img { transform:scale(2.1); }
  .chip .nm { font-size:.6rem; line-height:1.22; color:var(--ink-3); }

  @media (max-width:400px) {
    .rail { width:66px; }
    .lens { width:52px; height:52px; }
    .frame { width:70px; }
    .seg.shoes .frame { width:80px; }
  }

  .lnote { margin:0 0 16px; padding-top:15px; border-top:1px solid var(--hair);
           font-size:.83rem; color:var(--ink-2); }

  /* ---------- pill buttons ---------------------------------------------- */
  .rate { display:flex; gap:9px; margin-top:auto; }
  .pill { flex:1; display:inline-flex; align-items:center; justify-content:center; gap:7px;
    font:inherit; font-size:.78rem; font-weight:600; letter-spacing:.01em; cursor:pointer;
    padding:11px 12px; border-radius:999px; color:var(--ink-2);
    border:1px solid var(--hair); background:var(--glass-2);
    transition:transform .18s cubic-bezier(.22,1,.36,1), color .18s, border-color .18s, box-shadow .18s; }
  .pill svg { width:14px; height:14px; flex:none; }
  .pill:hover { transform:translateY(-2px); color:var(--ink); border-color:var(--accent);
                box-shadow:0 0 20px -5px var(--glow); }
  .pill:active { transform:translateY(0) scale(.975); }
  .pill:focus-visible { outline:2px solid var(--accent); outline-offset:3px; }
  .pill.done { color:var(--sky-3); background:var(--accent); border-color:var(--accent);
               box-shadow:0 0 26px -4px var(--glow); }


  /* ---------- lightbox --------------------------------------------------- */
  #lb { position:fixed; inset:0; z-index:40; display:none; place-items:center;
    padding:22px; background:rgba(0,0,0,.86); cursor:zoom-out; }
  #lb.open { display:grid; animation:fade .22s ease both; }
  @keyframes fade { from { opacity:0; } }
  #lb figure { margin:0; display:flex; flex-direction:column; align-items:center; gap:14px;
    max-width:min(720px,100%); max-height:100%; animation:pop .28s cubic-bezier(.22,1,.36,1) both; }
  @keyframes pop { from { opacity:0; transform:scale(.94); } }
  #lb img { max-width:100%; max-height:74vh; width:auto; height:auto; display:block;
    border-radius:16px; box-shadow:0 30px 70px -22px rgba(0,0,0,.85);
    border:1px solid rgba(255,255,255,.14); background:#0d1117; }
  #lb figcaption { text-align:center; color:#f2f5f8; }
  #lb .lslot { display:block; font-size:.6rem; font-weight:700; letter-spacing:.2em;
    text-transform:uppercase; color:var(--accent-2); margin-bottom:5px; }
  #lb .lname { font-size:1.02rem; font-weight:600; letter-spacing:-.01em; }
  #lbclose { position:absolute; top:16px; right:16px; width:42px; height:42px; border-radius:50%;
    display:grid; place-items:center; cursor:pointer; color:#f2f5f8;
    background:rgba(255,255,255,.10); border:1px solid rgba(255,255,255,.22); }
  #lbclose:hover { background:rgba(255,255,255,.2); }
  #lbclose:focus-visible { outline:2px solid var(--accent-2); outline-offset:2px; }
  .lbhint { font-size:.68rem; letter-spacing:.14em; text-transform:uppercase;
    color:rgba(242,245,248,.5); }

  #toast { position:fixed; left:50%; bottom:34px; transform:translateX(-50%) translateY(10px);
    z-index:9; padding:11px 20px; border-radius:999px; font-size:.8rem; font-weight:600;
    background:var(--ink); color:var(--bg); border:none; opacity:0; pointer-events:none;
    transition:opacity .24s, transform .24s; max-width:88vw; text-align:center; }
  #toast.show { opacity:1; transform:translateX(-50%) translateY(0); }

  @media (prefers-reduced-motion:reduce) {
    * { animation-duration:.01ms !important; transition-duration:.01ms !important; }
    .look:hover { transform:none; }
    .tile:hover .frame img { transform:none; }
  }
</style>

<canvas id="grid" aria-hidden="true"></canvas>
<canvas id="fx" aria-hidden="true"></canvas>
<div id="curRing" aria-hidden="true"></div>
<div id="cur" aria-hidden="true"></div>

<div class="wrap">
  <header class="mast">
    <span class="mark">style.</span>
    <button type="button" id="pgOpen" class="playlink">playground</button>
    <span class="today">{{SHORTDATE}}</span>
  </header>

  <div class="statwrap">
    <section class="statbar" aria-label="Today's conditions">{{STATS}}</section>
  </div>

  <section class="decide" id="decideView">
    <div class="deck" id="deck">{{DECK}}</div>
    <p class="dhead"><span id="dcount">1</span>/{{NLOOKS}}</p>
    <div class="done" id="done" hidden>
      <h3>That's the three.</h3>
      <p id="doneSum"></p>
      <button type="button" class="pill wide" id="sendAll">Send my feedback</button>
      <button type="button" class="linkish" id="copyAll">Copy it instead</button>
      <button type="button" class="linkish" id="redo">Start over</button>
    </div>
  </section>

</div>

<div id="warp" aria-hidden="true"></div>
<div id="pg" role="dialog" aria-modal="true" aria-label="Playground">
  <canvas id="pgcv"></canvas>
  <button type="button" class="pgmark" id="pgClose">style.</button>
  <button type="button" id="rigToggle" aria-expanded="true">fit.</button>
  <div id="rigbar">
    <div class="rigtabs" id="rigTabs" role="tablist" aria-label="Wardrobe"></div>
    <div class="rigtray" id="rigTray"></div>
  </div>
  <div id="rig">
    <div class="rigstage" id="rigStage"></div>
  </div>
</div>

<script type="application/json" id="playcloset">{{PLAYCLOSET}}</script>

<div id="sheet" role="dialog" aria-modal="true" aria-labelledby="shTitle">
  <div class="sheetbody glass">
    <h3 id="shTitle">Why?</h3>
    <p class="shsub" id="shSub"></p>
    <div class="chips" id="shChips"></div>
    <input type="text" id="shText" placeholder="Or say it in your own words&hellip;"
           autocomplete="off">
    <div class="shbtns">
      <button type="button" class="linkish" id="shSkip">Skip</button>
      <button type="button" class="pill" id="shSave">Save</button>
    </div>
  </div>
</div>

<script type="application/json" id="zoomdata">{{ZOOMDATA}}</script>

<div id="lb" role="dialog" aria-modal="true" aria-label="Enlarged garment">
  <button id="lbclose" type="button" aria-label="Close">
    <svg width="17" height="17" viewBox="0 0 16 16" fill="none" stroke="currentColor"
         stroke-width="1.8" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>
  </button>
  <figure>
    <img id="lbimg" src="" alt="">
    <figcaption>
      <span class="lslot" id="lbslot"></span>
      <span class="lname" id="lbname"></span>
    </figcaption>
    <span class="lbhint">Tap anywhere to close</span>
  </figure>
</div>

<div id="toast" class="glass" role="status" aria-live="polite"></div>

<script>
(function () {
  "use strict";
  var MODE = "{{MODE}}";
  var DATE = "{{DATE}}";
  document.documentElement.setAttribute("data-mode", MODE);

  /* Declared up here, not in the canvas block, because restack() reaches for it
     on first paint - long before that block has run. */
  var RAIN = false;

  /* Hoisted on purpose: restack() calls this on first paint, before the ambient
     canvas block below has run. Until then cv is undefined, so we just record
     the wanted state and let that block's own start() pick it up. */
  function setFx(mode) {
    var want = (mode === "rain");
    if (want === RAIN) return;
    RAIN = want;
    if (typeof cv !== "undefined" && cv) start();
  }

  /* Garment photos, keyed by item id and shared across all three looks so a
     piece worn twice is only embedded once. */
  var ZOOM = JSON.parse(document.getElementById("zoomdata").textContent);

  /* ---------- feedback ------------------------------------------------- */
  var toast = document.getElementById("toast"), timer;
  function flash(msg) {
    toast.textContent = msg;
    toast.classList.add("show");
    clearTimeout(timer);
    timer = setTimeout(function () { toast.classList.remove("show"); }, 2600);
  }
  function copy(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var ta = document.createElement("textarea");
      ta.value = text; ta.setAttribute("readonly", "");
      ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      document.body.removeChild(ta);
      ok ? resolve() : reject();
    });
  }
  document.querySelectorAll(".rate button").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var code = "#fit " + DATE + " " + btn.dataset.fb;
      var group = btn.closest(".rate");
      copy(code).then(function () {
        group.querySelectorAll("button").forEach(function (b) { b.classList.remove("done"); });
        btn.classList.add("done");
        flash("Copied - paste it to me in chat");
      }).catch(function () { flash(code); });
    });
  });

  /* ---------- swipe deck ------------------------------------------------ */
  var KEEP_CHIPS = ["Love the colours", "Right for the weather", "Comfortable",
                    "Feels like me", "Good for today's plans"];
  var PASS_CHIPS = ["Too warm", "Too cold", "Too formal", "Too casual",
                    "Not feeling the colours", "Wore something similar recently"];

  var deckEl = document.getElementById("deck");
  var cardsLeft = [].slice.call(deckEl.querySelectorAll(".swipe")).reverse(); // top first
  var verdicts = [], sheetPending = null;

  var sheet = document.getElementById("sheet");
  var shChips = document.getElementById("shChips"), shText = document.getElementById("shText");
  var shTitle = document.getElementById("shTitle"), shSub = document.getElementById("shSub");

  function topCard() { return cardsLeft[0]; }
  function restack() {
    cardsLeft.forEach(function (c, i) {
      c.style.setProperty("--k", i);
      c.style.zIndex = 50 - i;
      c.classList.toggle("behind", i > 0);
    });
    var f = cardsLeft[0];
    document.getElementById("dcount").textContent = f ? f.dataset.n : "-";
    /* The weather behind the page follows the look you're actually looking at.
       Rain falling behind a dry morning is a lie about the forecast. */
    setFx(f ? f.dataset.fx : MODE);
  }

  /* Swipe up = browse. The pile rotates so every look can be seen before any
     verdict is given; nothing is decided until you swipe sideways. */
  var cycling = false;
  function cycle() {
    if (cycling || cardsLeft.length < 2 || sheetPending) return;
    cycling = true;
    var c = cardsLeft[0];
    c.style.transition = "transform .26s cubic-bezier(.4,0,.6,1), opacity .26s";
    c.style.transform = "translateY(-78px) scale(.96)";
    c.style.opacity = "0";
    setTimeout(function () {
      cardsLeft.shift(); cardsLeft.push(c);
      c.style.transition = "none";
      c.style.transform = ""; c.style.opacity = "";
      restack();
      requestAnimationFrame(function () { c.style.transition = ""; cycling = false; });
    }, 260);
  }

  function openSheet(kind, card) {
    sheetPending = { kind: kind, card: card };
    shTitle.textContent = kind === "wore" ? "Nice - what worked?" : "Anything to add?";
    shSub.textContent = kind === "wore"
      ? "Tap what fits, or write your own. This is how I learn."
      : "Totally optional. Skip and I'll just note it wasn't for today.";
    shChips.innerHTML = "";
    (kind === "wore" ? KEEP_CHIPS : PASS_CHIPS).forEach(function (t) {
      var b = document.createElement("button");
      b.type = "button"; b.className = "chip2"; b.textContent = t;
      b.addEventListener("click", function () { b.classList.toggle("on"); });
      shChips.appendChild(b);
    });
    shText.value = "";
    sheet.classList.add("open");
  }
  function closeSheet(reason) {
    if (!sheetPending) return;
    var p = sheetPending; sheetPending = null;
    sheet.classList.remove("open");
    verdicts.push({ n: p.card.dataset.n, title: p.card.dataset.title,
                    kind: p.kind, reason: reason || "" });
    advance();
  }
  document.getElementById("shSkip").addEventListener("click", function () { closeSheet(""); });
  document.getElementById("shSave").addEventListener("click", function () {
    /* Read the lit chips straight from the DOM: what he SEES selected is
       exactly what saves. A shadow array can drift from the visuals - and on
       his phone it did, losing the Kept reasons. */
    var parts = [].slice.call(shChips.querySelectorAll(".on")).map(function (b) {
      return b.textContent;
    });
    if (shText.value.trim()) parts.push(shText.value.trim());
    closeSheet(parts.join(", "));
  });
  shText.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); document.getElementById("shSave").click(); }
  });

  function fling(card, dir) {
    if (sheetPending) return;   /* a second fling would rebuild the sheet and wipe the chips */
    card.classList.add("gone");
    card.style.transform = "translateX(" + (dir * 620) + "px) rotate(" + (dir * 26) + "deg)";
    cardsLeft.shift();
    setTimeout(function () { card.style.display = "none"; }, 460);
    openSheet(dir > 0 ? "wore" : "no", card);
  }

  function advance() {
    restack();
    if (cardsLeft.length === 0) finish();
  }

  function finish() {
    document.getElementById("deck").style.display = "none";
    document.querySelector(".dhead").style.display = "none";
    var d = document.getElementById("done");
    d.hidden = false;
    refreshSend();
    d.querySelector("#doneSum").textContent = verdicts.map(function (v) {
      /* \u escapes, not literal glyphs: the page is emitted as pure ASCII and
         HTML entities are NOT decoded inside a <script> block. */
      return (v.kind === "wore" ? "Kept: " : "Passed: ") + v.title +
             (v.reason ? " - " + v.reason : "");
    }).join("\n");
  }

  function feedbackCode() {
    return "#fit " + DATE + " | " + verdicts.map(function (v) {
      return "look" + v.n + " " + v.kind + (v.reason ? ": " + v.reason : "");
    }).join(" | ");
  }
  /* One tap opens Mail pre-filled. He sends it to himself; the 16:05 job reads
     his inbox next run and learns from it - so the loop closes without him ever
     opening this project again. */
  /* The one route that works everywhere: the artifact runtime calls his own
     Gmail connector and files the feedback as a draft, directly from the page.
     mailto: is swallowed by the sandbox, Gmail's compose URL ignores prefill
     on mobile, and navigator.share is not granted in this frame - all three
     were tried and all three died. This one runs inside the sandbox by design.
     The 16:05 job reads drafts, so a saved draft closes the loop by itself. */
  var GMAIL = "Gmail";
  function feedbackText() {
    return verdicts.map(function (v) {
      return (v.kind === "wore" ? "Kept: " : "Passed: ") + v.title +
             (v.reason ? " - " + v.reason : "");
    }).join("\n") + "\n";
  }
  function refreshSend() { /* built on tap, nothing to prepare */ }

  function fallbackCopy(text, note) {
    copy("fit " + DATE + "\n\n" + text).then(function () {
      flash(note + " - copied instead, paste it to me anywhere");
    }).catch(function () { flash(note); });
  }

  document.getElementById("sendAll").addEventListener("click", function () {
    var text = feedbackText();
    var btn = document.getElementById("sendAll");
    if (!(window.claude && window.claude.mcp)) {
      fallbackCopy(text, "Gmail link unavailable here");
      return;
    }
    btn.textContent = "Saving...";
    window.claude.mcp.callTool(GMAIL, "create_draft", {
      to: ["{{FEEDBACK_EMAIL}}"],
      subject: "fit " + DATE,
      body: text
    }).then(function () {
      btn.textContent = "Saved";
      flash("Saved to Gmail - I'll learn from it tonight");
    }).catch(function (err) {
      btn.textContent = "Send my feedback";
      var code = err && err.code;
      if (code === "needs_reauth") {
        fallbackCopy(text, "Reconnect Gmail in claude.ai Settings > Connectors");
      } else if (code === "server_not_connected" || code === "not_in_manifest") {
        fallbackCopy(text, "Gmail connector not reachable");
      } else {
        fallbackCopy(text, "Could not save");
      }
    });
  });
  document.getElementById("copyAll").addEventListener("click", function () {
    var code = feedbackCode();
    copy(code).then(function () { flash("Copied"); }).catch(function () { flash(code); });
  });
  document.getElementById("redo").addEventListener("click", function () { location.reload(); });

  /* drag: axis locks on first real movement - sideways decides, upward browses */
  var dx = 0, dy = 0, axis = null, dragging = false;
  var startX = 0, startY = 0, active = null;

  function press(e) {
    var card = topCard();
    if (!card || sheetPending || cycling) return;
    active = card; dragging = true; axis = null;
    startX = e.clientX; startY = e.clientY; dx = 0; dy = 0;
    card.style.transition = "none";
    card.setPointerCapture && card.setPointerCapture(e.pointerId);
  }
  function moveC(e) {
    if (!dragging || !active) return;
    dx = e.clientX - startX;
    dy = e.clientY - startY;
    if (!axis && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) {
      axis = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
    }
    if (axis === "x") {
      active.style.transform = "translateX(" + dx + "px) rotate(" + (dx / 22) + "deg)";
      var t = Math.min(1, Math.abs(dx) / 105);
      active.querySelector(".stamp.keep").style.opacity = dx > 0 ? t : 0;
      active.querySelector(".stamp.pass").style.opacity = dx < 0 ? t : 0;
    } else if (axis === "y") {
      var up = Math.min(0, dy);                       // only upward travels
      active.style.transform = "translateY(" + up + "px) scale(" +
        (1 - Math.min(0.05, Math.abs(up) / 1600)) + ")";
    }
  }
  function release() {
    if (!dragging || !active) return;
    dragging = false;
    var card = active; active = null;
    card.style.transition = "";
    card.querySelector(".stamp.keep").style.opacity = 0;
    card.querySelector(".stamp.pass").style.opacity = 0;
    if (axis === "x" && Math.abs(dx) > 105) { fling(card, dx > 0 ? 1 : -1); }
    else if (axis === "y" && dy < -62) { card.style.transform = ""; cycle(); }
    else { card.style.transform = ""; }
    axis = null;
  }
  deckEl.addEventListener("pointerdown", press);
  deckEl.addEventListener("pointermove", function (e) {
    if (dragging) { e.preventDefault(); moveC(e); }
  });
  deckEl.addEventListener("pointerup", release);
  deckEl.addEventListener("pointercancel", release);

  /* No on-screen controls: the gestures are the interface. Keyboard still works. */
  document.addEventListener("keydown", function (e) {
    if (sheetPending || lb.classList.contains("open")) return;
    if (document.getElementById("pg").classList.contains("on")) return;
    if (e.target && e.target.classList && e.target.classList.contains("mannequin")) return;
    if (e.key === "ArrowRight" && topCard()) fling(topCard(), 1);
    if (e.key === "ArrowLeft" && topCard()) fling(topCard(), -1);
    if (e.key === "ArrowUp") { e.preventDefault(); cycle(); }
  });

  restack();

  /* ---------- weather rail ---------------------------------------------- */
  /* Drag left/right to reach the rest of the readings. Touch already scrolls it
     natively; this adds click-and-drag for a mouse, and fades the ends to show
     there is more in that direction. */
  (function () {
    var rail = document.querySelector(".statbar");
    var wrap = document.querySelector(".statwrap");
    if (!rail) return;
    var down = false, sx = 0, sl = 0;
    var vel = 0, lastX = 0, lastT = 0, raf = null;
    var over = 0;                       // rubber-band overshoot, in px

    function max() { return rail.scrollWidth - rail.clientWidth; }
    function ends() {
      wrap.classList.toggle("more-l", rail.scrollLeft > 4);
      wrap.classList.toggle("more-r", rail.scrollLeft < max() - 4);
    }
    function paint() {
      /* The overshoot can't live in scrollLeft - the browser clamps it - so the
         rail is translated instead, which is what gives the bump at each end. */
      rail.style.transform = over ? "translateX(" + (-over).toFixed(2) + "px)" : "";
    }
    rail.addEventListener("scroll", ends, { passive: true });
    addEventListener("resize", ends, { passive: true });

    /* Glide on release, then rebound if it ran past an end. */
    function glide() {
      if (down) { raf = null; return; }
      if (Math.abs(vel) > 0.06) {
        var next = rail.scrollLeft + vel;
        if (next < 0)            { over += vel * 0.55; rail.scrollLeft = 0; vel *= 0.82; }
        else if (next > max())   { over += vel * 0.55; rail.scrollLeft = max(); vel *= 0.82; }
        else                     { rail.scrollLeft = next; }
        vel *= 0.94;                                   // friction
      } else { vel = 0; }
      if (over) { over *= 0.80; if (Math.abs(over) < 0.3) over = 0; }   // spring back
      paint();
      raf = (Math.abs(vel) > 0.06 || over) ? requestAnimationFrame(glide) : null;
      if (!raf) ends();
    }
    function run() { if (!raf) raf = requestAnimationFrame(glide); }

    rail.addEventListener("pointerdown", function (e) {
      if (e.pointerType !== "mouse") return;
      down = true; sx = e.clientX; sl = rail.scrollLeft;
      lastX = e.clientX; lastT = performance.now(); vel = 0;
      rail.classList.add("dragging");
      rail.setPointerCapture && rail.setPointerCapture(e.pointerId);
    });
    rail.addEventListener("pointermove", function (e) {
      if (!down) return;
      e.preventDefault();
      var want = sl - (e.clientX - sx);
      if (want < 0)           { over = (want) * 0.42; rail.scrollLeft = 0; }
      else if (want > max())  { over = (want - max()) * 0.42; rail.scrollLeft = max(); }
      else                    { over = 0; rail.scrollLeft = want; }
      paint();
      var now = performance.now(), dt = now - lastT;
      if (dt > 0) vel = -(e.clientX - lastX) / dt * 15;   // px per frame
      lastX = e.clientX; lastT = now;
    });
    function release() {
      if (!down) return;
      down = false; rail.classList.remove("dragging");
      if (performance.now() - lastT > 90) vel = 0;        // paused before letting go
      run();
    }
    rail.addEventListener("pointerup", release);
    rail.addEventListener("pointercancel", release);
    rail.addEventListener("pointerleave", release);
    ends();
  })();

  /* ---------- reactive dot field ---------------------------------------- */
  /* A lattice of faint dots. Near the pointer they are pushed outward, grow and
     darken, so the field visibly parts around you and settles back when you
     leave. Runs only while it is actually disturbed - idle costs nothing. */
  (function () {
    var reduceMo = matchMedia("(prefers-reduced-motion: reduce)");
    var cv = document.getElementById("grid");
    var ctx = cv.getContext("2d");
    var GAP = 34, RADIUS = 150, PUSH = 26;
    var pts = [], W = 0, H = 0, mx = -9999, my = -9999, raf = null;

    /* A full screen is ~1900 dots. Filling each one separately meant 1900
       fillStyle strings and 1900 fill() calls every frame, which pinned the
       main thread and made the whole page - the cursor most of all - feel
       laggy. Alpha is quantised into a handful of buckets instead, so a frame
       costs NB fills regardless of screen size. Visually indistinguishable. */
    var NB = 14, AMAX = 0.44, ASCALE = NB / AMAX;
    var buckets = [], ASTR = [], COL = "0,0,0";
    for (var b0 = 0; b0 < NB; b0++) buckets.push([]);

    function rgb() {
      var c = getComputedStyle(document.documentElement)
                .getPropertyValue("--ink").trim() || "#000";
      if (c.charAt(0) !== "#") return "0,0,0";
      if (c.length === 4) c = "#" + c[1] + c[1] + c[2] + c[2] + c[3] + c[3];
      var n = parseInt(c.slice(1), 16);
      return [(n >> 16) & 255, (n >> 8) & 255, n & 255].join(",");
    }

    function build() {
      var dpr = Math.min(devicePixelRatio || 1, 2);
      W = innerWidth; H = innerHeight;
      cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
      cv.style.width = W + "px"; cv.style.height = H + "px";
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      /* getComputedStyle forces a style flush; doing it per frame was pure
         waste on a page whose ink colour never changes. Resolve it here. */
      COL = rgb();
      ASTR = [];
      for (var b = 0; b < NB; b++) {
        ASTR.push("rgba(" + COL + "," + ((b + 0.5) / ASCALE).toFixed(3) + ")");
      }
      pts = [];
      for (var y = GAP / 2; y < H + GAP; y += GAP) {
        for (var x = GAP / 2; x < W + GAP; x += GAP) {
          pts.push({ ox: x, oy: y, x: x, y: y, ph: (x * 0.021 + y * 0.017) });
        }
      }
      draw();
    }

    /* The field always breathes: every dot drifts on its own slow sine, and the
       pointer pushes on top of that. Two phases per dot, seeded from position,
       so neighbours never move in lockstep. */
    function draw(now) {
      /* the globe owns the frame budget while the playground is open */
      if (document.documentElement.classList.contains("pg-on")) { raf = null; return; }
      var t = (now || performance.now()) / 1000;
      ctx.clearRect(0, 0, W, H);
      var b, k;
      for (b = 0; b < NB; b++) buckets[b].length = 0;
      var R2 = RADIUS * RADIUS;
      for (var i = 0; i < pts.length; i++) {
        var p = pts[i];
        var ax = Math.sin(t * 0.42 + p.ph) * 2.6;
        var ay = Math.cos(t * 0.35 + p.ph * 1.3) * 2.6;
        var dx = p.ox - mx, dy = p.oy - my;
        var d2 = dx * dx + dy * dy, f = 0, tx, ty;
        /* Almost every dot is outside the pointer's reach, and for those the
           sqrt and the normalise are wasted work - compare squares first. */
        if (d2 < R2) {
          var dist = Math.sqrt(d2);
          f = 1 - dist / RADIUS;
          f = f * f;                                 // tighter falloff
          var inv = dist ? PUSH * f / dist : 0;
          tx = p.ox + ax + dx * inv;
          ty = p.oy + ay + dy * inv;
        } else {
          tx = p.ox + ax;
          ty = p.oy + ay;
        }
        p.x += (tx - p.x) * 0.14;                    // ease toward target
        p.y += (ty - p.y) * 0.14;
        var a = (0.085 + f * 0.34) * (0.82 + 0.18 * Math.sin(t * 0.7 + p.ph));
        b = a * ASCALE | 0;
        if (b < 0) b = 0; else if (b >= NB) b = NB - 1;
        buckets[b].push(p.x, p.y, 0.9 + f * 1.9);
      }
      for (b = 0; b < NB; b++) {
        var arr = buckets[b];
        if (!arr.length) continue;
        ctx.fillStyle = ASTR[b];
        ctx.beginPath();
        for (k = 0; k < arr.length; k += 3) {
          /* moveTo first, or each arc joins the last one with a stray line. */
          ctx.moveTo(arr[k] + arr[k + 2], arr[k + 1]);
          ctx.arc(arr[k], arr[k + 1], arr[k + 2], 0, 6.2832);
        }
        ctx.fill();
      }
      raf = requestAnimationFrame(draw);
    }
    function run() { if (!raf) raf = requestAnimationFrame(draw); }
    window.__gridRun = run;

    if (reduceMo.matches) { cv.style.display = "none"; return; }

    addEventListener("pointermove", function (e) {
      mx = e.clientX; my = e.clientY; run();
    }, { passive: true });
    addEventListener("pointerleave", function () {
      mx = -9999; my = -9999; run();
    }, { passive: true });
    addEventListener("resize", build, { passive: true });
    build();
  })();

  /* ---------- playground: the particle globe ---------------------------- */
  /* Ported from the Toolcraft session's particle-globe (his build, his world):
     a planet of sampled points - continents from coarse Earth outlines frayed
     by noise, thinned ocean, drifting cloud shell, polar aurora, city lights
     after dark, route arcs out of the marker, cursor-steered rotation. */
  (function () {
    var pg = document.getElementById("pg");
    var warp = document.getElementById("warp");
    var openBtn = document.getElementById("pgOpen");
    var closeBtn = document.getElementById("pgClose");
    var cv = document.getElementById("pgcv");
    if (!pg || !cv) return;
    var ctx2 = cv.getContext("2d");
    var reduceM = matchMedia("(prefers-reduced-motion: reduce)").matches;

    /* ---- deterministic world ---- */
    function mulberry32(seed) {
      var a = seed >>> 0;
      return function () {
        a |= 0; a = (a + 0x6d2b79f5) | 0;
        var t = Math.imul(a ^ (a >>> 15), 1 | a);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
      };
    }
    function hash3(x, y, z) {
      var h = Math.imul(x | 0, 374761393) + Math.imul(y | 0, 668265263) +
              Math.imul(z | 0, 1274126177);
      h = Math.imul(h ^ (h >>> 13), 1274126177);
      return ((h ^ (h >>> 16)) >>> 0) / 4294967295;
    }
    function lerp(a, b, t) { return a + (b - a) * t; }
    function smooth(t) { return t * t * (3 - 2 * t); }
    function noise3(x, y, z) {
      var xi = Math.floor(x), yi = Math.floor(y), zi = Math.floor(z);
      var u = smooth(x - xi), v = smooth(y - yi), w = smooth(z - zi);
      function c(dx, dy, dz) { return hash3(xi + dx, yi + dy, zi + dz); }
      var x00 = lerp(c(0,0,0), c(1,0,0), u);
      var x10 = lerp(c(0,1,0), c(1,1,0), u);
      var x01 = lerp(c(0,0,1), c(1,0,1), u);
      var x11 = lerp(c(0,1,1), c(1,1,1), u);
      return lerp(lerp(x00, x10, v), lerp(x01, x11, v), w);
    }
    function fbm(x, y, z) {
      return noise3(x, y, z) * 0.55 + noise3(x*2.1, y*2.1, z*2.1) * 0.3 +
             noise3(x*4.3, y*4.3, z*4.3) * 0.15;
    }
    var EARTH = [
      [[-17,15],[-6,5],[5,4],[9,4],[13,-5],[12,-18],[15,-29],[18,-34],[27,-34],[33,-26],[40,-16],[41,-2],[51,11],[43,12],[37,22],[34,28],[32,31],[25,32],[10,34],[0,36],[-6,36],[-10,30],[-17,21]],
      [[-10,36],[-9,43],[-2,48],[3,51],[8,54],[12,56],[10,58],[5,62],[12,65],[16,69],[28,71],[45,68],[60,70],[75,73],[100,76],[130,73],[160,70],[170,66],[180,65],[170,60],[160,55],[145,50],[140,45],[130,42],[127,35],[122,30],[118,24],[108,21],[105,10],[100,6],[98,14],[94,16],[90,22],[87,21],[80,15],[77,8],[73,15],[70,22],[62,25],[57,25],[50,29],[48,30],[44,37],[36,36],[35,31],[30,40],[26,40],[20,42],[14,45],[12,44],[8,44],[3,42],[-2,43]],
      [[-168,65],[-160,71],[-140,70],[-125,70],[-100,70],[-85,70],[-80,62],[-65,60],[-56,52],[-66,45],[-74,40],[-76,35],[-81,25],[-90,29],[-97,26],[-97,16],[-92,15],[-84,10],[-79,9],[-83,15],[-95,18],[-106,23],[-114,31],[-117,33],[-122,38],[-124,46],[-131,55],[-140,60],[-150,60],[-165,60]],
      [[-81,0],[-79,-6],[-76,-14],[-71,-18],[-70,-25],[-72,-37],[-74,-45],[-75,-52],[-68,-55],[-62,-40],[-57,-35],[-48,-25],[-40,-20],[-39,-13],[-35,-8],[-44,-2],[-51,4],[-60,8],[-72,12],[-77,8],[-79,2]],
      [[113,-22],[114,-27],[118,-34],[129,-32],[137,-35],[140,-38],[147,-39],[150,-37],[154,-27],[146,-19],[142,-11],[136,-12],[130,-12],[125,-14],[117,-20]],
      [[-45,60],[-52,65],[-55,70],[-45,78],[-30,82],[-20,78],[-22,70],[-40,62]],
      [[43,-12],[50,-15],[50,-25],[45,-25],[43,-18]],
      [[130,31],[141,41],[145,44],[140,36],[135,33]],
      [[-11,51],[-8,58],[-2,59],[2,53],[-1,50],[-6,50]],
      [[166,-46],[172,-34],[178,-38],[174,-41],[168,-47]]
    ];
    var CITIES = [
      [-0.1,51.5],[-74,40.7],[139.7,35.7],[151.2,-33.9],[-46.6,-23.6],
      [18.4,-33.9],[77.2,28.6],[37.6,55.8],[-118.2,34.1],[103.8,1.4]
    ];
    function latLonToVec(lat, lon) {
      var p = lat * Math.PI / 180, t = lon * Math.PI / 180, cp = Math.cos(p);
      return [cp * Math.cos(t), Math.sin(p), cp * Math.sin(t)];
    }
    function inPolygon(lon, lat, poly) {
      var inside = false;
      for (var i = 0, j = poly.length - 1; i < poly.length; j = i++) {
        var a = poly[i], b = poly[j];
        if ((a[1] > lat) !== (b[1] > lat) &&
            lon < (b[0] - a[0]) * (lat - a[1]) / (b[1] - a[1]) + a[0]) inside = !inside;
      }
      return inside;
    }
    var DETAIL = 1.9, COAST = 0.5;
    function isLand(dx, dy, dz) {
      var jitter = COAST * 6;
      var nx = (fbm(dx*DETAIL*3+4.2, dy*DETAIL*3+9.7, dz*DETAIL*3+1.4) - 0.5) * jitter;
      var ny = (fbm(dx*DETAIL*3+51.3, dy*DETAIL*3+17.1, dz*DETAIL*3+88.6) - 0.5) * jitter;
      var lat = Math.asin(Math.max(-1, Math.min(1, dy))) * 180 / Math.PI + ny;
      var lon = Math.atan2(dz, dx) * 180 / Math.PI + nx;
      if (lon > 180) lon -= 360;
      if (lon < -180) lon += 360;
      if (lat < -68) return true;
      for (var i = 0; i < EARTH.length; i++) if (inPolygon(lon, lat, EARTH[i])) return true;
      return false;
    }

    /* his defaults, from the Toolcraft schema */
    var P = { scale:0.78, seed:23, mLat:26.8, mLon:30.8, mSpread:7, mCol:[255,45,45],
      ocean:0.22, relief:0.05, clouds:0.45, cloudDrift:0.5, aurora:0.65, routes:0.7,
      mouse:0.55, bulge:0.4, cluster:0.6, spin:0.28, exposure:1.3, depth:0.55,
      glow:0.45, atmosphere:0.5, sun:55, night:0.82, city:0.6, stars:0.5,
      accent:[95,141,255] };

    var world = null;
    function buildGlobe(points) {
      var rnd = mulberry32(P.seed * 2654435761);
      var cloudN = Math.round(points * 0.34 * P.clouds);
      var auroraN = Math.round(5200 * P.aurora);
      var cap = points + cloudN + auroraN + 9000;
      var xs = new Float32Array(cap), ys = new Float32Array(cap), zs = new Float32Array(cap);
      var ws = new Float32Array(cap), ms = new Uint8Array(cap), ks = new Uint8Array(cap);
      var n = 0;
      var mv = latLonToVec(P.mLat, P.mLon);
      var markCos = Math.cos(P.mSpread * Math.PI / 180);
      function push(x, y, z, w, kind) {
        if (n >= cap) return;
        xs[n] = x; ys[n] = y; zs[n] = z; ws[n] = w; ks[n] = kind || 0;
        if ((kind || 0) === 0) {
          var len = Math.sqrt(x*x + y*y + z*z) || 1;
          ms[n] = (x*mv[0] + y*mv[1] + z*mv[2]) / len > markCos ? 1 : 0;
        }
        n++;
      }
      var budget = points * 4, i, theta, phi, sp, dx, dy, dz;
      for (i = 0; i < budget && n < points; i++) {
        theta = rnd() * Math.PI * 2;
        phi = Math.acos(2 * rnd() - 1);
        sp = Math.sin(phi);
        dx = sp * Math.cos(theta); dy = Math.cos(phi); dz = sp * Math.sin(theta);
        var land = isLand(dx, dy, dz);
        if (!land && rnd() > P.ocean * 0.55) continue;
        var r = land ? 1 + P.relief : 1;
        push(dx*r, dy*r, dz*r, land ? 0.95 : 0.16 + P.ocean * 0.12, 0);
      }
      var made = 0;
      for (i = 0; i < cloudN * 5 && made < cloudN; i++) {
        theta = rnd() * Math.PI * 2; phi = Math.acos(2 * rnd() - 1); sp = Math.sin(phi);
        dx = sp * Math.cos(theta); dy = Math.cos(phi); dz = sp * Math.sin(theta);
        var v = fbm(dx*2.6+71.2, dy*5.4+3.9, dz*2.6+44.8);
        if (v < 0.52) continue;
        push(dx*1.035, dy*1.035, dz*1.035, 0.5 + (v-0.52)*1.8, 1);
        made++;
      }
      for (i = 0; i < auroraN; i++) {
        var north = rnd() < 0.5;
        var band = 64 + rnd() * 12;
        var alat = (north ? band : -band) * Math.PI / 180;
        var alon = rnd() * Math.PI * 2;
        var cl = Math.cos(alat);
        dx = cl * Math.cos(alon); dy = Math.sin(alat); dz = cl * Math.sin(alon);
        var av = fbm(dx*4.1+12.4, dy*4.1+62.7, dz*4.1+30.2);
        if (av < 0.44) continue;
        var ar2 = 1.05 + rnd() * 0.12 * (av - 0.44) * 6;
        push(dx*ar2, dy*ar2, dz*ar2, 0.35 + (av-0.44)*1.5, 2);
      }
      var lat2, lon2, s2, t2, p2, cy2, yy;
      for (lat2 = -75; lat2 <= 75; lat2 += 15) {
        p2 = lat2 * Math.PI / 180; cy2 = Math.cos(p2); yy = Math.sin(p2);
        var steps = Math.max(60, Math.round(220 * cy2));
        for (s2 = 0; s2 < steps; s2++) {
          t2 = s2 / steps * Math.PI * 2;
          push(cy2 * Math.cos(t2), yy, cy2 * Math.sin(t2), 0.13, 0);
        }
      }
      for (lon2 = 0; lon2 < 360; lon2 += 15) {
        t2 = lon2 * Math.PI / 180;
        for (s2 = 0; s2 < 150; s2++) {
          p2 = s2 / 150 * Math.PI - Math.PI / 2; cy2 = Math.cos(p2);
          push(cy2 * Math.cos(t2), Math.sin(p2), cy2 * Math.sin(t2), 0.13, 0);
        }
      }
      return { ks: ks, ms: ms, n: n, ws: ws, xs: xs, ys: ys, zs: zs };
    }

    /* ---- view + loop ---- */
    var W = 0, H = 0, img = null, buf = null, glowCv = null, gctx = null;
    var starX = null, starY = null, starB = null, STARN = 1400;
    var view = null, overLand = false, raf = null, t0 = 0;

    function seedView() {
      var mvec = latLonToVec(P.mLat, P.mLon);
      var yaw = -Math.PI / 2 - Math.atan2(mvec[2], mvec[0]);
      var pitch = -Math.atan2(Math.sin(P.mLat * Math.PI / 180), Math.hypot(mvec[0], mvec[2]));
      view = { dragPitch: pitch, dragYaw: yaw, dragging: false, inside: false,
               lx: 0, ly: 0, pitch: pitch, px: -1, py: -1, tPitch: 0, tYaw: 0, yaw: yaw };
    }
    function size() {
      W = cv.clientWidth; H = cv.clientHeight;
      cv.width = W; cv.height = H;
      img = ctx2.createImageData(W, H);
      buf = img.data;
      glowCv = document.createElement("canvas");
      glowCv.width = W; glowCv.height = H;
      gctx = glowCv.getContext("2d");
      var srnd = mulberry32(9176);
      starX = new Float32Array(STARN); starY = new Float32Array(STARN); starB = new Float32Array(STARN);
      for (var i = 0; i < STARN; i++) {
        starX[i] = srnd() * W; starY[i] = srnd() * H;
        var t = srnd(); starB[i] = 30 + t * t * t * 210;
      }
    }

    function frame(now) {
      raf = requestAnimationFrame(frame);
      var dt = Math.min(64, now - t0); t0 = now;
      var c = world, v = view;
      if (!c || !v) return;

      if (v.inside && P.mouse > 0) {
        v.tYaw = (v.px / W - 0.5) * 2.2 * P.mouse;
        v.tPitch = (v.py / H - 0.5) * 1.3 * P.mouse;
      } else { v.tYaw = 0; v.tPitch = 0; }
      var k = 1 - Math.pow(0.0025, dt / 1000);
      v.yaw += (v.tYaw + v.dragYaw - v.yaw) * k;
      v.pitch += (v.tPitch + v.dragPitch - v.pitch) * k;
      if (!v.dragging) v.dragYaw += P.spin * dt * 0.0004;

      buf.fill(0);
      for (var ii = 3; ii < buf.length; ii += 4) buf[ii] = 255;

      var cy = Math.cos(v.yaw), sy = Math.sin(v.yaw);
      var cp = Math.cos(v.pitch), sp = Math.sin(v.pitch);
      var S = Math.min(W, H) * 0.42 * P.scale * 1.35;
      var ox = W / 2, oy = H / 2, focal = 3.1;
      var sa = P.sun * Math.PI / 180;
      var sunX = Math.cos(sa), sunY = 0.3, sunZ = -Math.sin(sa);
      var sunLen = Math.sqrt(sunX*sunX + sunY*sunY + sunZ*sunZ);

      if (P.stars > 0.01) {
        var discR = S * 1.03;
        for (var si = 0; si < STARN; si++) {
          var dxs = starX[si] - ox, dys = starY[si] - oy;
          if (dxs*dxs + dys*dys < discR*discR) continue;
          var k3 = ((starY[si] | 0) * W + (starX[si] | 0)) * 4;
          var sv = starB[si] * P.stars;
          buf[k3] = sv; buf[k3+1] = sv; buf[k3+2] = sv;
        }
      }

      var pushR = Math.min(W, H) * 0.22;
      var pushOn = v.inside && P.bulge > 0.01;
      var detectR = Math.min(W, H) * 0.05;
      var clusterR = Math.min(W, H) * 0.17;
      var clusterOn = v.inside && P.cluster > 0.01 && overLand;
      var landNear = 0;
      var cloudPhase = now * 0.00004 * P.cloudDrift;
      var ccy = Math.cos(cloudPhase), csy = Math.sin(cloudPhase);

      for (var i = 0; i < c.n; i++) {
        var kind = c.ks[i];
        var x0 = c.xs[i], y0 = c.ys[i], z0 = c.zs[i];
        if (kind === 1) {
          var rx = x0 * ccy - z0 * csy;
          z0 = x0 * csy + z0 * ccy; x0 = rx;
        }
        var x1 = x0 * cy - z0 * sy;
        var z1 = x0 * sy + z0 * cy;
        var y2 = y0 * cp - z1 * sp;
        var z2 = y0 * sp + z1 * cp;
        if (z2 > 0.02) continue;

        var persp = focal / (focal + z2);
        var sxf = x1 * persp * S + ox;
        var syf = y2 * persp * S + oy;
        var isLandPt = kind === 0 && c.ws[i] > 0.6;
        var cursorD = Infinity;
        if (v.inside) {
          var dd0 = sxf - v.px, dd1 = syf - v.py;
          cursorD = Math.sqrt(dd0*dd0 + dd1*dd1);
          if (isLandPt && cursorD < detectR) landNear++;
        }
        var boost = 0;
        if (pushOn && cursorD < pushR && cursorD > 0.001) {
          var fall = 1 - cursorD / pushR;
          var lift = fall * fall * P.bulge * 26;
          sxf += (sxf - v.px) / cursorD * lift;
          syf += (syf - v.py) / cursorD * lift;
          boost = fall * fall * P.bulge * 120;
        }
        var sx = sxf | 0, sy2 = syf | 0;
        if (sx < 0 || sy2 < 0 || sx >= W || sy2 >= H) continue;

        var dfade = 1 - P.depth * Math.min(1, Math.max(0, (z2 + 1) * 0.5));
        var plen = Math.sqrt(x1*x1 + y2*y2 + z2*z2) || 1;
        var lambert = (x1*sunX + y2*sunY + z2*sunZ) / (plen * sunLen);
        var daylight = Math.min(1, Math.max(0, lambert * 2.1 + 0.32));
        var lit = 1 - P.night * (1 - daylight);
        var val = 255 * P.exposure * c.ws[i] * dfade * lit + boost;
        if (val <= 0 && kind !== 2) continue;
        if (val > 255) val = 255;

        var isNight = daylight < 0.34;
        var cityLit = false;
        if (P.city > 0.01 && isLandPt && isNight) {
          var h = (Math.imul(i + 1, 2654435761) >>> 0) / 4294967296;
          if (h < P.city * 0.16) {
            cityLit = true;
            val = Math.max(val, 90 + h * 900 * P.city);
            if (val > 255) val = 255;
          }
        }

        var rr, gg, bb;
        if (c.ms[i] === 1) {
          var bv = Math.min(255, val * 1.25);
          rr = bv * P.mCol[0] / 255; gg = bv * P.mCol[1] / 255; bb = bv * P.mCol[2] / 255;
        } else if (kind === 2) {
          var strength = Math.max(0, 1 - daylight * 2.4);
          if (strength <= 0.01) continue;
          var shimmer = 0.72 + 0.28 * Math.sin(now * 0.0011 + c.zs[i] * 9);
          var av2 = Math.min(255, 255 * P.exposure * c.ws[i] * dfade * strength * shimmer * 3.4);
          rr = av2 * 0.25; gg = av2; bb = av2 * 0.62;
        } else if (kind === 1) {
          rr = val; gg = val; bb = val;
        } else if (cityLit) {
          rr = val; gg = val * 0.72; bb = val * 0.38;
        } else {
          var rad = Math.sqrt(x1*x1 + y2*y2);
          var limb = Math.min(1, Math.max(0, (rad - 0.55) / 0.5)) * P.atmosphere * daylight;
          rr = val + (P.accent[0] - 255) * limb * 0.7;
          gg = val + (P.accent[1] - 255) * limb * 0.7;
          bb = val + (P.accent[2] - 255) * limb * 0.4;
        }

        var k2 = (sy2 * W + sx) * 4;
        var nr = buf[k2] + rr, ng = buf[k2+1] + gg, nb = buf[k2+2] + bb;
        buf[k2] = nr > 255 ? 255 : nr;
        buf[k2+1] = ng > 255 ? 255 : ng;
        buf[k2+2] = nb > 255 ? 255 : nb;

        if (clusterOn && isLandPt && cursorD < clusterR) {
          var fall2 = 1 - cursorD / clusterR;
          var copies = (fall2 * fall2 * P.cluster * 7) | 0;
          var spread = 10 + fall2 * 30;
          for (var ci2 = 0; ci2 < copies; ci2++) {
            var jx = (sxf + (Math.random() - 0.5) * spread) | 0;
            var jy = (syf + (Math.random() - 0.5) * spread) | 0;
            if (jx < 0 || jy < 0 || jx >= W || jy >= H) continue;
            var kk = (jy * W + jx) * 4;
            var cvv = 150 + fall2 * 105;
            var q0 = buf[kk] + cvv, q1 = buf[kk+1] + cvv, q2v = buf[kk+2] + cvv;
            buf[kk] = q0 > 255 ? 255 : q0;
            buf[kk+1] = q1 > 255 ? 255 : q1;
            buf[kk+2] = q2v > 255 ? 255 : q2v;
          }
        }
      }
      overLand = landNear > 4;

      if (P.routes > 0.01) {
        var A = latLonToVec(P.mLat, P.mLon);
        for (var ri = 0; ri < CITIES.length; ri++) {
          var B = latLonToVec(CITIES[ri][1], CITIES[ri][0]);
          var dotAB = Math.max(-1, Math.min(1, A[0]*B[0] + A[1]*B[1] + A[2]*B[2]));
          var omega = Math.acos(dotAB);
          if (omega < 0.02) continue;
          var so = Math.sin(omega);
          var head = ((now * 0.00022 + ri * 0.17) % 1.4) - 0.2;
          for (var s3 = 0; s3 <= 90; s3++) {
            var tt = s3 / 90;
            var k1b = Math.sin((1 - tt) * omega) / so;
            var k2b = Math.sin(tt * omega) / so;
            var liftb = 1 + Math.sin(tt * Math.PI) * (0.06 + omega * 0.14);
            var gx0 = (A[0]*k1b + B[0]*k2b) * liftb;
            var gy0 = (A[1]*k1b + B[1]*k2b) * liftb;
            var gz0 = (A[2]*k1b + B[2]*k2b) * liftb;
            var gx1 = gx0 * cy - gz0 * sy;
            var gz1 = gx0 * sy + gz0 * cy;
            var gy2 = gy0 * cp - gz1 * sp;
            var gz2 = gy0 * sp + gz1 * cp;
            if (gz2 > 0.02) continue;
            var gp = focal / (focal + gz2);
            var px3 = (gx1 * gp * S + ox) | 0;
            var py3 = (gy2 * gp * S + oy) | 0;
            if (px3 < 0 || py3 < 0 || px3 >= W || py3 >= H) continue;
            var near = Math.max(0, 1 - Math.abs(tt - head) * 7);
            var amt = (105 + near * near * 330) * P.routes;
            var kk2 = (py3 * W + px3) * 4;
            var r3 = buf[kk2] + amt * P.mCol[0] / 255;
            var g3 = buf[kk2+1] + amt * P.mCol[1] / 255;
            var b3 = buf[kk2+2] + amt * P.mCol[2] / 255;
            buf[kk2] = r3 > 255 ? 255 : r3;
            buf[kk2+1] = g3 > 255 ? 255 : g3;
            buf[kk2+2] = b3 > 255 ? 255 : b3;
          }
        }
      }

      ctx2.putImageData(img, 0, 0);

      var M = latLonToVec(P.mLat, P.mLon);
      var mx1 = M[0] * cy - M[2] * sy;
      var mz1 = M[0] * sy + M[2] * cy;
      var my2 = M[1] * cp - mz1 * sp;
      var mz2 = M[1] * sp + mz1 * cp;
      if (mz2 <= 0.02) {
        var mp = focal / (focal + mz2);
        var cxp = mx1 * mp * S + ox;
        var cyp = my2 * mp * S + oy;
        ctx2.globalCompositeOperation = "lighter";
        for (var ring = 0; ring < 2; ring++) {
          var phase = (now * 0.0004 + ring * 0.5) % 1;
          ctx2.strokeStyle = "rgba(" + P.mCol[0] + "," + P.mCol[1] + "," + P.mCol[2] + "," +
                             ((1 - phase) * 0.5).toFixed(3) + ")";
          ctx2.lineWidth = 1.4;
          ctx2.beginPath();
          ctx2.arc(cxp, cyp, phase * S * 0.34, 0, Math.PI * 2);
          ctx2.stroke();
        }
        ctx2.globalCompositeOperation = "source-over";
      }

      if (P.atmosphere > 0.01) {
        var R2 = S * 1.02;
        var g2 = ctx2.createRadialGradient(ox, oy, R2 * 0.82, ox, oy, R2 * 1.22);
        g2.addColorStop(0, "rgba(95,141,255,0)");
        g2.addColorStop(0.45, "rgba(95,141,255," + (0.16 * P.atmosphere).toFixed(3) + ")");
        g2.addColorStop(1, "rgba(95,141,255,0)");
        ctx2.globalCompositeOperation = "lighter";
        ctx2.fillStyle = g2;
        ctx2.fillRect(0, 0, W, H);
        ctx2.globalCompositeOperation = "source-over";
      }

      if (P.glow > 0.01 && gctx && typeof ctx2.filter === "string") {
        gctx.clearRect(0, 0, W, H);
        gctx.drawImage(cv, 0, 0);
        ctx2.globalCompositeOperation = "lighter";
        ctx2.filter = "blur(" + (4 * (0.4 + P.glow)).toFixed(1) + "px)";
        ctx2.globalAlpha = 0.45 * P.glow;
        ctx2.drawImage(glowCv, 0, 0);
        ctx2.filter = "blur(" + (15 * (0.4 + P.glow)).toFixed(1) + "px)";
        ctx2.globalAlpha = 0.3 * P.glow;
        ctx2.drawImage(glowCv, 0, 0);
        ctx2.filter = "none";
        ctx2.globalAlpha = 1;
        ctx2.globalCompositeOperation = "source-over";
      }
    }

    /* ---- pointer steering + drag ---- */
    cv.addEventListener("pointermove", function (e) {
      if (!view) return;
      var r = cv.getBoundingClientRect();
      view.px = (e.clientX - r.left) * W / r.width;
      view.py = (e.clientY - r.top) * H / r.height;
      view.inside = true;
      if (view.dragging) {
        view.dragYaw += (e.clientX - view.lx) * 0.006;
        view.dragPitch += (e.clientY - view.ly) * 0.006;
        view.dragPitch = Math.max(-1.3, Math.min(1.3, view.dragPitch));
        view.lx = e.clientX; view.ly = e.clientY;
      }
    });
    cv.addEventListener("pointerdown", function (e) {
      if (!view) return;
      view.dragging = true; view.lx = e.clientX; view.ly = e.clientY;
      cv.setPointerCapture && cv.setPointerCapture(e.pointerId);
    });
    function pgUp(e) {
      if (!view) return;
      view.dragging = false;
      if (cv.hasPointerCapture && cv.hasPointerCapture(e.pointerId)) cv.releasePointerCapture(e.pointerId);
    }
    cv.addEventListener("pointerup", pgUp);
    cv.addEventListener("pointercancel", pgUp);
    cv.addEventListener("pointerleave", function () { if (view) view.inside = false; });

    /* ---- open / close, through the warp ---- */
    function coverScale(x, y) {
      var far = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
      return far * 2 / 60 * 1.08;
    }
    function openPg(e) {
      var x = innerWidth / 2, y = 40;
      if (e && e.clientX) { x = e.clientX; y = e.clientY; }
      function reveal() {
        pg.classList.add("on");
        document.documentElement.classList.add("pg-on");
        size();
        if (!world) {
          var area = W * H;
          var pts = Math.max(26000, Math.min(90000, Math.round(90000 * area / (1280 * 800))));
          world = buildGlobe(pts);
        }
        if (!view) seedView();
        t0 = performance.now();
        /* paint the first frame synchronously - a throttled rAF must never
           leave the door open onto a black room */
        if (!raf) frame(performance.now());
        closeBtn.focus();
        warp.classList.remove("go");
        warp.style.transform = "scale(0)";
      }
      if (reduceM) { reveal(); return; }
      warp.style.left = x + "px";
      warp.style.top = y + "px";
      warp.classList.remove("go");
      warp.style.transform = "scale(0)";
      /* forced reflow commits the start state - no rAF, which can be throttled */
      void warp.offsetWidth;
      warp.classList.add("go");
      warp.style.transform = "scale(" + coverScale(x, y).toFixed(2) + ")";
      setTimeout(reveal, 610);
    }
    function closePg() {
      if (raf) { cancelAnimationFrame(raf); raf = null; }
      pg.classList.remove("on");
      document.documentElement.classList.remove("pg-on");
      if (!reduceM) {
        var x = innerWidth / 2, y = innerHeight / 2;
        warp.style.left = x + "px";
        warp.style.top = y + "px";
        warp.classList.remove("go");
        warp.style.transform = "scale(" + coverScale(x, y).toFixed(2) + ")";
        void warp.offsetWidth;
        warp.classList.add("go");
        warp.style.transform = "scale(0)";
      }
      if (window.__gridRun) window.__gridRun();
      openBtn.focus();
    }
    openBtn.addEventListener("click", openPg);
    closeBtn.addEventListener("click", closePg);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && pg.classList.contains("on")) closePg();
    });
    addEventListener("resize", function () { if (pg.classList.contains("on")) size(); });
  })();

  /* ---------- fitting rig: dress the board over the globe ---------------- */
  (function () {
    var rig = document.getElementById("rig");
    var tabsEl = document.getElementById("rigTabs");
    var trayEl = document.getElementById("rigTray");
    var stageEl = document.getElementById("rigStage");
    var toggle = document.getElementById("rigToggle");
    if (!rig || !tabsEl) return;
    var CLOSET;
    try { CLOSET = JSON.parse(document.getElementById("playcloset").textContent); }
    catch (e) { return; }

    var LABEL = { top: "Tops", midlayer: "Zips", jacket: "Jackets", bottom: "Pants",
                  shoes: "Shoes", hat: "Hats", scarf: "Scarf", accessory: "Extras" };
    var ORDER = ["top", "midlayer", "jacket", "bottom", "shoes", "hat", "scarf", "accessory"];
    var STAGE = ["hat", "scarf", "jacket", "midlayer", "top", "bottom", "shoes"];

    var byCat = {};
    CLOSET.forEach(function (it) { (byCat[it.c] = byCat[it.c] || []).push(it); });
    var cats = ORDER.filter(function (c) { return byCat[c] && byCat[c].length; });

    var lastAdded = null; // so a newly added piece can be scrolled into view
    var worn = {};        // slot -> item, one per category
    var extras = [];      // accessories, a few at once
    var active = cats[0];

    function isWorn(it) {
      if (it.c === "accessory") {
        return extras.some(function (e) { return e.i === it.i; });
      }
      return worn[it.c] && worn[it.c].i === it.i;
    }

    function renderTabs() {
      tabsEl.innerHTML = "";
      cats.forEach(function (c) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "rigtab" + (c === active ? " on" : "");
        b.setAttribute("role", "tab");
        b.setAttribute("aria-selected", c === active ? "true" : "false");
        b.textContent = LABEL[c] || c;
        b.addEventListener("click", function () { active = c; renderTabs(); renderTray(); });
        tabsEl.appendChild(b);
      });
    }

    function renderTray() {
      trayEl.innerHTML = "";
      (byCat[active] || []).forEach(function (it) {
        var b = document.createElement("button");
        b.type = "button";
        if (isWorn(it)) b.className = "worn";
        b.setAttribute("aria-label", it.n);
        var im = document.createElement("img");
        im.src = it.d; im.alt = it.n; im.loading = "lazy";
        b.appendChild(im);
        b.addEventListener("click", function () { wear(it); });
        trayEl.appendChild(b);
      });
    }

    function wear(it) {
      lastAdded = isWorn(it) ? null : it.i;
      if (it.c === "accessory") {
        var idx = extras.findIndex(function (e) { return e.i === it.i; });
        if (idx >= 0) extras.splice(idx, 1);
        else { if (extras.length >= 4) extras.shift(); extras.push(it); }
      } else if (worn[it.c] && worn[it.c].i === it.i) {
        delete worn[it.c];       // tapping the worn piece takes it off
      } else {
        worn[it.c] = it;         // one per slot: a new top replaces the top
      }
      renderStage(); renderTray();
    }

    var XSVG = '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor"' +
      ' stroke-width="1.8" stroke-linecap="round"><path d="M2.5 2.5l7 7M9.5 2.5l-7 7"/></svg>';
    var STAGE = ["hat", "scarf", "jacket", "midlayer", "top", "bottom", "shoes"];

    function slotRow(it, small, d) {
      var row = document.createElement("div");
      row.className = "rigslot" + (small ? " small" : "");
      row.setAttribute("data-id", it.i);
      row.style.animationDelay = (d * 50) + "ms";
      var tile = document.createElement("div");
      tile.className = "rigtile";
      var im = document.createElement("img");
      im.src = it.d; im.alt = it.n;
      if (it.a) im.className = "cut";
      var x = document.createElement("button");
      x.type = "button"; x.className = "rigx";
      x.setAttribute("aria-label", "Remove " + it.n);
      x.innerHTML = XSVG;
      x.addEventListener("click", function () { wear(it); });
      tile.appendChild(im); tile.appendChild(x);
      row.appendChild(tile);
      return row;
    }

    function renderStage() {
      stageEl.innerHTML = "";
      var d = 0, any = false;
      STAGE.forEach(function (slot) {
        if (worn[slot]) { stageEl.appendChild(slotRow(worn[slot], false, d++)); any = true; }
      });
      if (extras.length) {
        var xr = document.createElement("div");
        xr.className = "rigextras";
        extras.forEach(function (it) { xr.appendChild(slotRow(it, true, d++)); });
        stageEl.appendChild(xr);
        any = true;
      }

      /* A full board scrolls, so a piece added off-screen looks like nothing
         happened. Bring whatever was just added into view. */
      if (lastAdded) {
        var el = stageEl.querySelector('[data-id="' + lastAdded + '"]');
        if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
        lastAdded = null;
      }
    }
    toggle.addEventListener("click", function () {
      var hidden = rig.classList.toggle("hidden");
      bar.classList.toggle("hidden", hidden);
      toggle.setAttribute("aria-expanded", hidden ? "false" : "true");
    });

    renderTabs(); renderTray(); renderStage();
  })();

  /* ---------- magnetic cursor ------------------------------------------- */
  /* The dot pins to the pointer; the ring eases toward it, so the pair stretches
     apart when you move fast and settles when you stop. Over anything
     interactive the ring blooms and the dot shrinks. */
  (function () {
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    var dot = document.getElementById("cur");
    var ring = document.getElementById("curRing");
    var root = document.documentElement;
    var HOT = "a,button,input,textarea,select,summary,[role=button]," +
              ".mannequin,.swipe,.orb,.pill,.chip2,.linkish,[data-zoom]";

    var mx = 0, my = 0, rx = 0, ry = 0, on = false, raf = null;

    /* The ring is the only part that eases, so it is the only part that needs a
       frame. The dot is written straight from the pointer event - waiting for
       rAF put it a frame behind the real cursor, and any busy frame turned that
       into visible drag. */
    function tick() {
      rx += (mx - rx) * 0.18;               // ring eases toward the dot
      ry += (my - ry) * 0.18;
      ring.style.transform = "translate3d(" + rx + "px," + ry + "px,0)";
      if (Math.abs(mx - rx) > 0.1 || Math.abs(my - ry) > 0.1) {
        raf = requestAnimationFrame(tick);
      } else { raf = null; }
    }
    function run() { if (!raf) raf = requestAnimationFrame(tick); }

    addEventListener("pointermove", function (e) {
      if (e.pointerType !== "mouse") return;
      mx = e.clientX; my = e.clientY;
      if (!on) {
        on = true; rx = mx; ry = my;
        ring.style.transform = "translate3d(" + rx + "px," + ry + "px,0)";
        root.classList.add("cur-on");
      }
      dot.style.transform = "translate3d(" + mx + "px," + my + "px,0)";
      run();
    }, { passive: true });

    /* Hover state on pointerover, not pointermove: closest() against that
       selector list plus a classList write, sixty times a second, for an answer
       that only changes when the element under the pointer does. */
    addEventListener("pointerover", function (e) {
      if (e.pointerType && e.pointerType !== "mouse") return;
      root.classList.toggle("cur-hot", !!(e.target && e.target.closest && e.target.closest(HOT)));
    }, { passive: true });

    addEventListener("pointerdown", function (e) {
      if (e.pointerType === "mouse") root.classList.add("cur-press");
    }, { passive: true });
    addEventListener("pointerup", function () { root.classList.remove("cur-press"); },
                     { passive: true });
    addEventListener("pointerleave", function () {
      on = false; root.classList.remove("cur-on", "cur-hot", "cur-press");
    }, { passive: true });
    addEventListener("blur", function () {
      on = false; root.classList.remove("cur-on", "cur-hot", "cur-press");
    });
  })();

  /* ---------- lightbox -------------------------------------------------- */
  var lb = document.getElementById("lb"), lbimg = document.getElementById("lbimg");
  var lbname = document.getElementById("lbname"), lbslot = document.getElementById("lbslot");
  var lastFocus = null;

  function openLB(src, name, slot, origin) {
    lastFocus = origin || null;
    lbimg.src = src;
    lbimg.alt = name;
    lbname.textContent = name;
    lbslot.textContent = slot || "";
    lb.classList.add("open");
    document.body.style.overflow = "hidden";
    document.getElementById("lbclose").focus();
  }
  function closeLB() {
    lb.classList.remove("open");
    document.body.style.overflow = "";
    lbimg.src = "";
    if (lastFocus) { lastFocus.focus(); lastFocus = null; }
  }
  /* Every garment opens full screen on click. This binding went missing when the
     mannequin was removed - it used to be the only caller of openLB. */
  document.querySelectorAll("[data-zoom]").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      openLB(btn.dataset.zoom, btn.dataset.name, btn.dataset.slot, btn);
    });
  });

  lb.addEventListener("click", closeLB);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && lb.classList.contains("open")) closeLB();
  });

  /* ---------- ambient canvas ------------------------------------------- */
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  var cv = document.getElementById("fx"), ctx = cv.getContext("2d");
  var parts = [], w = 0, h = 0, raf = null;

  function accent() {
    var v = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
    return v || "#ffffff";
  }
  function size() {
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = cv.width = Math.floor(innerWidth * dpr);
    h = cv.height = Math.floor(innerHeight * dpr);
    cv.style.width = innerWidth + "px";
    cv.style.height = innerHeight + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    seed();
  }
  function seed() {
    var area = innerWidth * innerHeight;
    var n = RAIN ? Math.min(220, Math.round(area / 5200))
                 : Math.min(60, Math.round(area / 24000));
    parts = [];
    for (var i = 0; i < n; i++) {
      parts.push(RAIN ? {
        x: Math.random() * innerWidth, y: Math.random() * innerHeight,
        len: 9 + Math.random() * 17, vy: 4.6 + Math.random() * 5.2,
        vx: -0.7 - Math.random() * 0.8, a: 0.10 + Math.random() * 0.24
      } : {
        x: Math.random() * innerWidth, y: Math.random() * innerHeight,
        r: 0.7 + Math.random() * 1.7, vy: -0.10 - Math.random() * 0.22,
        vx: 0.05 + Math.random() * 0.16, a: 0.10 + Math.random() * 0.26,
        ph: Math.random() * 6.28
      });
    }
  }
  function draw(t) {
    ctx.clearRect(0, 0, innerWidth, innerHeight);
    if (RAIN) {
      ctx.strokeStyle = accent(); ctx.lineWidth = 1.05; ctx.lineCap = "round";
      for (var i = 0; i < parts.length; i++) {
        var p = parts[i];
        ctx.globalAlpha = p.a;
        ctx.beginPath();
        ctx.moveTo(p.x, p.y);
        ctx.lineTo(p.x + p.vx * 2.4, p.y + p.len);
        ctx.stroke();
        p.x += p.vx; p.y += p.vy;
        if (p.y > innerHeight + 24) { p.y = -24; p.x = Math.random() * innerWidth; }
        if (p.x < -24) p.x = innerWidth + 24;
      }
    } else {
      ctx.fillStyle = accent();
      for (var j = 0; j < parts.length; j++) {
        var q = parts[j];
        ctx.globalAlpha = q.a * (0.62 + 0.38 * Math.sin(t / 1350 + q.ph));
        ctx.beginPath();
        ctx.arc(q.x, q.y, q.r, 0, 6.2832);
        ctx.fill();
        q.x += q.vx; q.y += q.vy;
        if (q.y < -12) { q.y = innerHeight + 12; q.x = Math.random() * innerWidth; }
        if (q.x > innerWidth + 12) q.x = -12;
      }
    }
    ctx.globalAlpha = 1;
    raf = requestAnimationFrame(draw);
  }
  function start() {
    /* Only rain earns an ambient layer. Drifting dots on a white ground read as
       dust, not atmosphere, so every other mode stays perfectly still. */
    if (reduce.matches || !RAIN) {
      if (raf) { cancelAnimationFrame(raf); raf = null; }
      ctx.clearRect(0, 0, innerWidth, innerHeight);
      cv.style.display = "none";
      return;
    }
    cv.style.display = "";
    size();
    if (raf) cancelAnimationFrame(raf);
    raf = requestAnimationFrame(draw);
  }
  addEventListener("resize", function () { if (!reduce.matches) size(); }, { passive: true });
  reduce.addEventListener ? reduce.addEventListener("change", start) : null;
  start();

})();
</script>
"""


if __name__ == "__main__":
    main()
