# style.

I photographed my wardrobe, and now something builds me an outfit every morning
before I wake up.

https://github.com/amrbody71-commits/style/raw/main/docs/style-reel.mp4

It reads tomorrow's weather, picks looks out of the actual clothes I own, and
renders a single self-contained page with the real garments cut out and laid on
it. I tell it which looks I wore and which I passed on, and it writes what it
learned into a rules file that constrains every run after that.

## The wardrobe is not in this repository

The photographs, the real `closet.json`, the learned style rules and the
feedback log are personal and stay private — a closet file carries a home
location and a physical description, and the feedback log is a record of what I
actually wore.

What ships is **the engine and the schema**:

| File | What it is |
|---|---|
| `build_lookbook.py` | The whole builder: garment cut-outs, look assembly, and the page |
| `closet.schema.json` | The wardrobe format, documented field by field |
| `closet.sample.json` | Twelve placeholder garments so a clone runs |
| `make_image.py` | Renders the day's pick as a single flat image for phone previews |

```bash
cp closet.sample.json closet.json     # then point the `file` paths at your own photos
python build_lookbook.py              # writes lookbook.html
```

Garment photos are the one thing you must supply. Everything else runs as-is.

## How it works

**Photograph → cut out → embed.** Every garment is shot on a plain backdrop.
`unify_bg()` floods that backdrop from the corners out to the page colour, so a
cut-out sits on the page with no visible rectangle around it, then crops the
dead margin so the garment actually fills its tile. Flooding from the corners
rather than by colour-keying is what stops it eating the garment itself when a
shirt happens to be near-white.

**Thumbnails are cached in `.thumbs/`**, so the expensive image work happens
once per garment ever, not once per morning.

**The page is one file.** Garments go in as data URIs, so `lookbook.html` opens
from a phone with no server, no assets folder and no network. Every non-ASCII
character is escaped to a numeric entity on the way out — the published page
carries no charset declaration of its own, and raw UTF-8 read as latin-1 turns
every em-dash into mojibake.

**It reacts to the weather.** `today.json`'s `weather.mode` — `clear`, `rain`,
`overcast` or `night` — drives the palette, the ambient canvas layer and the
glow temperature, so a wet morning and a bright one do not look alike.

**It learns.** The feedback button drafts a note listing what was kept and what
was passed, with reasons. Those become dated one-line rules — *always a white
tee under any quarter-zip*, *never stack two long-sleeve layers in summer*, *cap
a look at one warmth-3-or-above piece unless it is genuinely cold* — and the
rules file is read on every subsequent run. It is capped at twenty lines: at the
cap, the two most similar merge before a new one is added, so it stays a rulebook
rather than a diary.

The address that button drafts to is read from `_meta.owner` in your closet file.
Leave it empty and the button copies to the clipboard instead. There is no
address baked into this repository.

## Licence

MIT — see [LICENSE](LICENSE). The code only. The wardrobe it was written for is
not part of this repository.
