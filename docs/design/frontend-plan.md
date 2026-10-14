# Frontend plan — making both products demo-able

Agreed 14 October 2026. Reference: [reference-donezo-dashboard.png](reference-donezo-dashboard.png),
a task-management dashboard shared as a visual target.

This comes **before** login and roles (§10). A demo needs screens, not
permissions. architecture.md §10 still lists roles among the immediate work and
should be read with that reordering in mind.

---

## The three decisions

| | Decision |
|---|---|
| **Colour** | Take the reference's *layout*, not its *colour*. Keep our validated series palette and the reserved status four. |
| **Typeface** | Vendor one subset `.woff2` locally, beside `echarts.min.js`. No CDN, no build step. |
| **Target** | A laptop at 1440–1920, viewed close up. The demo machine. |

### Why not the colour

The reference uses green as its brand accent — nav, buttons, KPI fill, bars,
gauge. Our palette already gives green a *meaning*: `--good` and `--good-ink`
are what `.pill.good` and `.tile.good` are drawn with, and on `/environment`
green means *this cascade is holding*. Chrome in the same colour as a safety
signal destroys the signal.

The reference is internally consistent because it does **not** use green for
status either — its "Completed" pill is `#e0e3e2`, near-neutral. It reserved
green for brand and moved state off it. We are reserving green for state. Both
are coherent; doing both at once is not.

Our series hues are blue / orange / aqua and were chosen to pass colourblind
adjacent-pair separation — `app.css` records that aqua measures 2.74:1 on the
light surface, which is why every chart already ships direct labels and a table
view. Repainting charts green would discard that for a resemblance.

---

## The principle that follows

> **Colour means data or state. Chrome is neutral.**

Primary buttons, the active nav item and focus rings are drawn in ink, not in a
hue. Nothing decorative is ever coloured. The consequence is that any colour on
the screen is information — which is what a monitoring product wants, and what
a task app has no need of.

This replaces the reference's green accent with a dark one and costs nothing
visually: the reference's own chrome is extremely dark green (`#164e34`,
`#092210`), which reads as near-black at a glance anyway.

---

## Tokens

### Change

| Token | Now | To | Why |
|---|---|---|---|
| `--plane` | `#f9f9f7` | cooler, ~`#f1f2f3` | Warm cast; and too close to `--surface` |
| `--surface` | `#fcfcfb` | `#ffffff` | **Cards are currently invisible as cards** — three values from the plane. This is the single biggest visual win and is independent of everything else |
| `--ink` | `#0b0b0b` | keep | Reference uses pure black; ours is kinder at small sizes |
| `--ink-2`, `--muted` | warm greys | cool equivalents | Match the neutral cast |
| `--grid`, `--axis` | warm | cool equivalents | Same |
| *new* `--chrome` | — | ink-derived | Buttons, active nav, focus ring |
| *new* spacing + radius scale | ad hoc | tokens | See below |

### Keep, deliberately

`--series-1/2/3` and the four status colours. Both are spoken for and both are
validated. Dark-mode tokens keep their existing relationships — every neutral
change gets mirrored, and dark mode is re-checked rather than assumed to follow.

**Any palette change is re-run through the data-viz validator before it lands.**
Eyeballing a contrast ratio is how a palette stops being validated.

---

## Type

One vendored family. Candidates, both SIL Open Font License — which satisfies
the CLAUDE.md invariant that every component permit commercial redistribution,
though the licence file gets read and committed alongside the font rather than
taken on trust:

- **Inter** — designed for UI at small sizes, excellent tabular figures. The
  better engineering choice for a screen that is mostly numbers.
- **Plus Jakarta Sans** — closer to the reference's slightly geometric feel.

Recommendation: **Inter**, subset to Latin, weights 400/500/600/700, with
`font-variant-numeric: tabular-nums` on every figure so columns of readings
align and a changing value does not make the row twitch. The system stack does
not reliably give us tabular figures on Windows, which is a real reason to
vendor rather than a cosmetic one.

Fetching the file needs an explicit go-ahead — it is a download, and it is the
only new third-party asset in this work.

Scale, matching the reference's rhythm rather than its exact pixels (those get
tuned in the browser against real content):

| Role | Size / weight |
|---|---|
| Page title | ~30px / 700 |
| Big figure (KPI) | ~34px / 700, tabular |
| Section title | ~15px / 600 |
| Body | ~14px / 400 |
| Caption, pill, axis | ~11–12px / 500 |

---

## Space and shape

From the reference, scaled to our target width:

- A spacing scale in tokens — 4 / 8 / 12 / 16 / 24 / 32 — rather than ad-hoc
  values, so density can be retuned in one place when the plant-PC question is
  answered.
- Card radius ~12px, card padding ~20px, grid gap ~16px.
- **Drop the inset "floating window" frame.** It costs ~32px on every edge and
  buys nothing on a maximised browser; the reference needs it because it is a
  marketing shot.
- Keep the reference's asymmetric grid: a wide primary card beside two narrower
  ones, rather than four equal columns. It is what stops the page reading as a
  spreadsheet.

---

## Navigation

Move from the current horizontal nav to a **left sidebar**, grouped as the
reference does. We have four items today and are about to add alarms, reports,
settings and users — a horizontal strip stops working well before that.

The product list is already dynamic (`products` in `_base.html`), so the
sidebar is a restyle of an existing mechanism, not a new one. The grouping maps
onto something real:

```
MONITORING     Overview · Devices · <the products this install serves>
SYSTEM         Settings · Help
```

The active item keeps the current `{% block nav %}` slug matching — unchanged.

---

## Screens in scope for the demo

| Screen | State | Work |
|---|---|---|
| Shell | exists | Sidebar, tokens, type, card treatment |
| `/` Overview | exists, thin | Needs to be worth looking at: the KPI row, a live table, something moving |
| `/devices` | exists | Restyle; decide what a 400-row version does |
| `/devices/{code}` | exists | Restyle; the trend chart is already generic |
| `/environment` | exists | The cleanroom story — rooms in spec, the pressure smalls, excursions |
| `/energy` | exists | The energy story — consumption, the `consumption()` statuses |

Every one of these already works. **This is a restyle, not a rebuild** — which
is the main reason it is achievable before a demo.

### What we are not taking

Avatars, the team-collaboration feed, the time tracker, "Add Member", the
mobile-app promo. They have no analogue here, and inventing users for a demo
means inventing them again when roles arrive.

---

## The thing most likely to let the demo down

**The data behind it.** Right now the energy product has two demo meters and
four days of history, and the environmental product has three rooms. A demo
that claims 400+ meters while showing two will not land, and an empty-looking
chart reads as a broken chart.

So seeding is part of this work, not an afterthought: enough meters, enough
rooms, and enough history that every chart on every screen has a convincing
shape — including the excursion bars, which need rooms that actually went out
of spec, and `consumption()`, which needs a counter reset somewhere so the
status pills have something to say.

`scripts/seed_demo.py` exists and is the place for it.

---

## Order of work

Each step leaves the suite green and all three dashboards serving, as before.

1. **Tokens and type.** Neutrals, the card/plane separation, the spacing scale,
   the vendored font. Nothing structural — and the card fix alone will change
   how finished the current screens look.
2. **The shell.** Sidebar, header, card component, button and pill treatment.
   Both products inherit it for free, which is the point of the split.
3. **Overview**, because it is the first screen in the demo.
4. **The two product screens**, one at a time.
5. **Devices and device detail.**
6. **Demo data**, seeded and checked against every chart.

Steps 1–2 are where the risk is: they touch `_base.html` and `app.css`, which
every page and both products depend on.

---

## Verification

The suite does not catch visual faults — three chart bugs have already got past
it on this project, every one of them a chart that drew nothing while the tests
passed. So each step ends by **looking at the running dashboards**, on all
three ports:

- 18850 admin (both products), 18851 energy, 18852 environment
- every chart draws, and the browser console is clean
- the nav shows only the products that instance serves
- dark mode is checked, not assumed — it is a separate set of tokens
- at 1440 and 1920, and narrower, to see what breaks first

`uv run pytest -q` stays green throughout; the structural tests already assert
the shell names no product, and restyling must not quietly reintroduce one.
