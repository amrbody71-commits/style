"""Render the day's three looks as a single tall PNG.

An image opens on any phone with no login, no app and no artifact gallery -
Photos, Files, OneDrive and Mail all preview it natively. This is the fallback
that cannot break.

Usage:  python make_image.py
"""

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import build_lookbook as B

ROOT = Path(__file__).resolve().parent
W = 1080
BG = B.PAGE_BG
INK = (0, 0, 0)
MUTED = (139, 139, 139)
MID = (90, 90, 90)
LINE = (222, 222, 222)


def font(size, bold=False):
    for name in (("segoeuib.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main() -> None:
    closet = json.loads((ROOT / "closet.json").read_text(encoding="utf-8"))
    today = json.loads((ROOT / "today.json").read_text(encoding="utf-8"))
    by_id = {i["id"]: i for i in closet["items"]}
    w = today["weather"]

    f_mark = font(64, True)
    f_date = font(26)
    f_lab = font(20, True)
    f_val = font(40, True)
    f_tag = font(20, True)
    f_title = font(44, True)
    f_note = font(25)
    f_item = font(23)

    PAD = 56
    ROW = 300           # height of one garment row
    canvas_h = 4000
    img = Image.new("RGB", (W, canvas_h), BG)
    d = ImageDraw.Draw(img)
    y = PAD

    # masthead
    d.text((PAD, y), "style.", font=f_mark, fill=INK)
    d.text((W - PAD, y + 26), today["date"].upper(), font=f_date, fill=MUTED, anchor="ra")
    y += 104

    # weather strip
    d.line([(PAD, y), (W - PAD, y)], fill=LINE, width=2)
    cells = w["metrics"][:4]
    cw = (W - 2 * PAD) / len(cells)
    for i, m in enumerate(cells):
        cx = PAD + cw * i + 14
        label = m["l"].upper()
        val = (m["v"].replace("&deg;", "°").replace("<i>", " ")
                     .replace("</i>", "").replace("&ndash;", "-"))
        d.text((cx, y + 22), label, font=f_lab, fill=MUTED)
        d.text((cx, y + 52), val, font=f_val, fill=INK)
    y += 128
    d.line([(PAD, y), (W - PAD, y)], fill=LINE, width=2)
    y += 34

    # the call
    call = w["summary"]
    for line in wrap(call, f_note, W - 2 * PAD - 20, d):
        d.text((PAD + 14, y), line, font=f_note, fill=MID)
        y += 36
    y += 30

    for look in today["looks"]:
        items = [by_id[i] for i in look["items"]]
        worn = sorted((i for i in items if i["cat"] in B.BODY_ORDER),
                      key=lambda i: B.BODY_ORDER.index(i["cat"]))
        extras = [i for i in items if i["cat"] not in B.BODY_ORDER]
        row = worn + extras

        # tag pill
        tag = look["tag"].upper()
        tw = d.textlength(tag, font=f_tag)
        reserve = look.get("tone") == "reserve"
        d.rounded_rectangle([PAD, y, PAD + tw + 34, y + 40], radius=20,
                            fill=INK if reserve else BG, outline=INK, width=2)
        d.text((PAD + 17, y + 10), tag, font=f_tag, fill=BG if reserve else INK)
        y += 58

        d.text((PAD, y), look["title"], font=f_title, fill=INK)
        y += 62

        # garments in a row, each contained in its own cell
        n = len(row)
        cell = (W - 2 * PAD) / n
        for i, it in enumerate(row):
            g = Image.open(ROOT / it["file"])
            from PIL import ImageOps
            g = B.unify_bg(ImageOps.exif_transpose(g).convert("RGB"))
            g.thumbnail((int(cell) - 24, ROW - 60))
            gx = int(PAD + cell * i + (cell - g.width) / 2)
            img.paste(g, (gx, y + (ROW - 60 - g.height) // 2))
        y += ROW - 40

        for i, it in enumerate(row):
            name = it["name"].split(",")[0]
            cx = PAD + cell * i + cell / 2
            for j, line in enumerate(wrap(name, f_item, cell - 20, d)[:2]):
                d.text((cx, y + j * 28), line, font=f_item, fill=MID, anchor="ma")
        y += 76
        d.line([(PAD, y), (W - PAD, y)], fill=LINE, width=1)
        y += 40

    y += 10
    d.text((PAD, y), "Reply-worthy? Email yourself: subject  fit", font=f_item, fill=MUTED)
    y += 60

    img = img.crop((0, 0, W, y))
    out = ROOT / "today.png"
    img.save(out, "PNG", optimize=True)
    print(f"wrote {out}  ({out.stat().st_size/1024:.0f} KB)  {img.width}x{img.height}")


def wrap(text, f, maxw, d):
    words, lines, cur = text.split(), [], ""
    for word in words:
        trial = (cur + " " + word).strip()
        if d.textlength(trial, font=f) <= maxw:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


if __name__ == "__main__":
    main()
