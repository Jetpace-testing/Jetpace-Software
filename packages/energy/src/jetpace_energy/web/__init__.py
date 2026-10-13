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
from jetpace_core.web.products import Product, Shell

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


def product() -> Product:
    return Product(
        slug="energy",
        label="Energy",
        templates=HERE / "templates",
        routes=routes,
    )
