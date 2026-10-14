# Brand assets

Jetpace's own logo, unlike `../vendor/`, which is third-party. Nothing
here needs a licence note; it needs provenance, because a logo that has
been resaved, recoloured or re-traced by somebody in a hurry is very hard
to spot afterwards and very annoying to discover on a customer's wall.

## What is here

| File | Source in the logo pack | Mark | Tagline |
|---|---|---|---|
| `jetpace-on-light.svg` | `01_JETPACE_Logo_with_Tagline/SVG/JETPACE_Logo_Transparent_for-light-bg.svg` | `#FA0303` | `#332F30` |
| `jetpace-on-dark.svg` | `01_JETPACE_Logo_with_Tagline/SVG/JETPACE_Logo_Transparent_for-dark-bg.svg` | `#FA0303` | `#FFFFFF` |

Both are **byte-for-byte copies**, renamed and nothing else. Verify:

```
476ad3ab6e0e8df4e0c1f1987d22e207b4a85ddbc1e17788b34eb374637f43e6  jetpace-on-light.svg
f1b139b0bd4b85c126166d5a9e8956bd48043d162e05dab79fd305cdf71471e3  jetpace-on-dark.svg
```

Pack as received: `JETPACE_Logo_Pack/JETPACE_Logo/`, 23 September 2025,
three sets — `01_..._with_Tagline`, `02_..._Wordmark_Only`,
`03_Both_Logos_Preview` — each in EPS, JPEG, JPG, PDF, PNG, SVG, TIFF and
WEBP.

## Why these two and not the other thirty

**With the tagline**, because the sidebar block it replaces carried both
the wordmark and "Test and Measurement" as separate text. The wordmark-only
set is for places too small to read a tagline — a favicon, a tab strip —
and there is no such place in this application yet.

**Transparent, not Light/Dark.** The Light and Dark files have a white or
black rectangle baked in, which is correct for a surface you do not
control and wrong for a panel whose colour is `--surface` and changes
with the theme. These two have no background: one has a near-black
tagline for light surfaces, the other a white tagline for dark ones, and
the stylesheet picks between them on `data-theme`.

**SVG, not PNG or WEBP.** It is a logo made of flat shapes: it is smaller
as paths than as pixels at any size worth shipping, and it is sharp on a
4K panel and at 125% Windows scaling without a second file. The red is
`#FA0303` at every zoom level rather than a resampled approximation of
it, which matters because a brand red that drifts is the kind of thing
nobody reports and everybody notices.

## Deliberately not touched

27 KB each, of which about 26 KB is the tagline's letterforms as outlines
at six decimal places. Rounding those to one decimal would cut it to
roughly 8 KB with an error of 0.005 px at the size this renders — but it
would mean shipping a logo file that is not the one the brand owner
signed off, to save 19 KB on a machine that loads it from its own disk.
If the size ever matters, minify then and record it here.

## No icon-only asset

There is no favicon, because the pack has no mark-only file: both sets
are roughly three times wider than tall and become illegible in a 32 px
square. The jet device could be lifted out of the wordmark, but cropping
a logo is the brand owner's call, not a developer's.
