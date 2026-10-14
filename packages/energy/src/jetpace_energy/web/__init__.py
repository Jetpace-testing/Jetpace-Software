"""
The energy product's pages.

Exactly one public name, `product()` - see the note in
jetpace_environment.web, which has the same shape for the same reasons.

The routes were `/energy` and `/api/energy-daily` in core's app.py.

No stylesheet: this product's chart code is inline in its template and it
uses no CSS core does not already define. If that changes, it gets a
`static/energy.css` and names it in `product()` below; it must not add a
rule to core's app.css.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jetpace_core.web import queries as core_queries
from jetpace_core.web.products import Event, Product, Shell, Tile

from .. import queries

HERE = Path(__file__).resolve().parent


def routes(shell: Shell) -> APIRouter:
    router = APIRouter()

    @router.get("/energy", response_class=HTMLResponse)
    def energy(request: Request):
        with core_queries.connect(shell.dsn) as conn:
            return shell.templates.TemplateResponse(request, "energy.html", {
                "energy": queries.energy_period(conn, days=7),
            })

    @router.get("/api/energy-daily")
    def api_energy_daily(days: int = 7):
        with core_queries.connect(shell.dsn) as conn:
            rows = queries.energy_daily(conn, days=days)
        series: dict[str, dict] = defaultdict(
            lambda: {"code": "", "name": "", "points": []})
        for r in rows:
            s = series[r["code"]]
            s["code"], s["name"] = r["code"], r["name"]
            s["points"].append([r["day"].isoformat(), float(r["used"] or 0)])
        return JSONResponse({"days": days,
                             "series": sorted(series.values(),
                                              key=lambda s: s["code"])})

    return router


def tiles(conn) -> list[Tile]:
    """
    What this product adds to the shared overview.

    Both figures come through consumption(), so a meter whose period the
    system cannot defend is counted as such rather than quietly summed
    into the total - architecture.md §7. A tile that adds up numbers it
    does not trust is worse than no tile.
    """
    rows = queries.energy_period(conn, days=7)
    defensible = [r for r in rows if r.get("status") == "ok"]
    total = sum(float(r["used"] or 0) for r in defensible)
    top = max(defensible, key=lambda r: float(r["used"] or 0), default=None)

    return [
        Tile(label="Consumption, 7 days", icon="trend", tone="teal",
             value=f"{total:,.0f}" if defensible else "—",
             small="kWh" if defensible else "",
             # Anything the system would not stand behind is the thing to
             # say out loud, not to average away.
             state="" if not rows else
                   ("good" if len(defensible) == len(rows) else "alarm")),
        # THE FIGURE SLOT GETS THE FIGURE. This tile used to put the
        # device code there and the kilowatt-hours in the unit slot,
        # which is the wrong way round twice over: the card is 125px
        # wide and sized for a number at 18px, so a ten-character code
        # either forced its grid track two and a half times wider than
        # the eight cards beside it or broke across five lines. The
        # identity belongs on the label line, where every other tile
        # puts what it is about.
        Tile(label=f"Highest consumer{' · ' + top['code'] if top else ''}",
             icon="gauge", tone="blue",
             value=f"{float(top['used'] or 0):,.0f}" if top else "—",
             small="kWh" if top else ""),
    ]


#: What consumption() returns when the period is billable. Anything else
#: is a figure this system will not stand behind, and §7 requires saying
#: so out loud rather than substituting a plausible guess.
DEFENSIBLE = "ok"

#: What each status means to somebody who has to act on it. The status
#: strings are consumption()'s, in migrations/0002_rollups.sql; these
#: sentences are this product's, because the shell has no business
#: knowing what a counter reset is.
WHY = {
    "counter_reset": "the counter went backwards, so the period cannot "
                     "be closed by subtraction",
    "partial_new_meter": "commissioned mid-period, so this covers only "
                         "what the meter recorded",
    "stale_open": "the opening reading is too old to close this period "
                  "against",
    "not_a_counter": "this parameter is not a counter, so consumption "
                     "is not defined for it",
}


def events(conn) -> list[Event]:
    """
    The exceptions only this product can see.

    A billing period the system cannot defend. `consumption()` already
    decides this - it returns a status, and architecture.md §7 is
    explicit that a blank always carries one - so this reads that
    decision out rather than making a second one. The tile beside it
    counts these; this names them, which is what somebody who has to fix
    a meter before the month closes actually needs.

    Not "critical" or "warning": there is no alarm model to carry a
    severity (see Event), and an unbillable meter is a suspect figure
    rather than a plant emergency.
    """
    found = []
    for r in queries.energy_period(conn, days=7):
        status = r.get("status")
        if status == DEFENSIBLE:
            continue
        found.append(Event(
            when=r.get("period_end"),
            kind="suspect",
            device=r["code"],
            where=r["name"],
            parameter="Consumption, 7 days",
            value="needs review",
            limit="—",
            # Still unbillable until somebody intervenes at the meter.
            ongoing=True,
            detail=WHY.get(status, f"consumption() returned {status!r}"),
        ))
    return found


def product() -> Product:
    return Product(
        slug="energy",
        label="Energy",
        templates=HERE / "templates",
        routes=routes,
        tiles=tiles,
        events=events,
    )
