"""
The environmental product's pages.

Exactly one public name, `product()`, which is what core's shell calls to
find out what this package contributes. Core discovers this module by a
slug read from configuration, so core contains no import of it - see
jetpace_core.web.products for why that is done by name rather than by
entry point.

The routes were `/environment` and `/api/dp-trend` in core's app.py. They
are unchanged apart from taking their connection string and template
environment from the Shell rather than from a closure.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jetpace_core.web import queries as core_queries
from jetpace_core.web.products import Product, Shell

from .. import queries

HERE = Path(__file__).resolve().parent


def routes(shell: Shell) -> APIRouter:
    """
    Build this product's routes against the shell it is being mounted in.

    A factory rather than a module-level router because both handlers need
    the connection string and the shared Jinja environment, and neither
    exists at import time. Taking them as arguments keeps them visible
    instead of reaching into application state.
    """
    router = APIRouter()

    @router.get("/environment", response_class=HTMLResponse)
    def environment(request: Request):
        with core_queries.connect(shell.dsn) as conn:
            return shell.templates.TemplateResponse(
                request, "environment.html", {
                    "summary": queries.environment_summary(conn),
                    "excursions": queries.excursions(conn, days=7),
                })

    @router.get("/api/dp-trend")
    def api_dp_trend(hours: int = 24):
        """
        ECharts wants series, so shape it here rather than in JavaScript.

        One series per room, each a list of [epoch_ms, avg]. min and max
        travel alongside so the tooltip can show the range the hour
        actually covered - an average alone hides the excursion that
        matters.
        """
        with core_queries.connect(shell.dsn) as conn:
            rows = queries.dp_trend(conn, hours=hours)

        series: dict[str, dict] = {}
        for r in rows:
            s = series.setdefault(r["code"], {
                "code": r["code"], "name": r["name"],
                "avg": [], "min": [], "max": [],
            })
            ms = int(r["bucket"].timestamp() * 1000)
            s["avg"].append([ms, float(r["avg"])])
            s["min"].append([ms, float(r["min"])])
            s["max"].append([ms, float(r["max"])])
        return JSONResponse({"hours": hours,
                             "series": sorted(series.values(),
                                              key=lambda s: s["code"])})

    return router


def product() -> Product:
    return Product(
        slug="environment",
        label="Environment",
        templates=HERE / "templates",
        static=HERE / "static",
        stylesheet="environment.css",
        routes=routes,
    )
