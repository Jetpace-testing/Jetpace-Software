"""
The dashboard.

FastAPI serving Jinja templates, with HTMX for the live table and ECharts
for the trends. Both libraries are served from `static/vendor/` rather than
a CDN, because the product has to work on a plant network with no route to
the internet - which is also why there is no build step.

Endpoint functions are synchronous. FastAPI runs them in a thread pool, so
nothing here needs async/await, and the same decision that keeps asyncio
out of the poller keeps it out of the web layer.

    uv run python scripts/run_web.py
"""

from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import queries

HERE = Path(__file__).resolve().parent

# Pressure thresholds for the status pill. A cleanroom cascade is healthy
# positive; approaching zero is worth a warning before it reverses.
DP_WARN_BELOW = 5.0


def classify_dp(value: float) -> tuple[str, str]:
    """(status, label). Status colours are reserved and always carry a label."""
    if value <= 0:
        return "critical", "reversed"
    if value < DP_WARN_BELOW:
        return "warning", "low"
    return "good", "in spec"


def create_app(dsn: str) -> FastAPI:
    app = FastAPI(title="Jetpace", docs_url="/api/docs")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals["classify_dp"] = classify_dp

    def rows_for_live(conn):
        """Group the flat live query into one entry per device."""
        by_device: dict[str, dict] = {}
        for r in queries.live(conn):
            d = by_device.setdefault(r["code"], {
                "code": r["code"], "name": r["name"], "gateway": r["gateway"],
                "unit_id": r["unit_id"], "online": False, "age_s": None,
                "points": [],
            })
            d["points"].append(r)
            # a device is online if any of its points is fresh
            if r["online"]:
                d["online"] = True
            if d["age_s"] is None or r["age_s"] < d["age_s"]:
                d["age_s"] = r["age_s"]
        return sorted(by_device.values(), key=lambda d: d["code"])

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        with queries.connect(dsn) as conn:
            return templates.TemplateResponse(request, "index.html", {
                "summary": queries.summary(conn),
                "devices": rows_for_live(conn),
                "excursions": queries.excursions(conn, days=7),
                "energy": queries.energy_period(conn, days=7),
            })

    @app.get("/partials/live", response_class=HTMLResponse)
    def partial_live(request: Request):
        """Polled by HTMX. Returns only the table body and the summary row."""
        with queries.connect(dsn) as conn:
            return templates.TemplateResponse(request, "_live.html", {
                "summary": queries.summary(conn),
                "devices": rows_for_live(conn),
            })

    @app.get("/api/dp-trend")
    def api_dp_trend(hours: int = 24):
        """
        ECharts wants series, so shape it here rather than in JavaScript.

        One series per room, each a list of [epoch_ms, avg]. min and max
        travel alongside so the tooltip can show the range the hour
        actually covered - an average alone hides the excursion that
        matters.
        """
        with queries.connect(dsn) as conn:
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

    @app.get("/api/energy-daily")
    def api_energy_daily(days: int = 7):
        with queries.connect(dsn) as conn:
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

    @app.get("/healthz")
    def healthz():
        """
        What the supervisor's health gate will call. It asserts the
        database answers AND that data is arriving - a release that starts
        cleanly but silently stops logging must not pass.
        """
        try:
            with queries.connect(dsn) as conn:
                s = queries.summary(conn)
            fresh = bool(s.get("readings_24h"))
            return JSONResponse(
                {"ok": fresh, "readings_24h": s.get("readings_24h"),
                 "newest": s.get("newest").isoformat() if s.get("newest") else None},
                status_code=200 if fresh else 503)
        except Exception as exc:                       # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)},
                                status_code=503)

    return app


def find_repo_root() -> Path | None:
    """
    Walk up looking for the workspace root, rather than counting parents.

    Counting was the first version and it was wrong - this file sits five
    levels down, not four, and the error only showed up as "No database"
    when the server refused to start. A marker file cannot be miscounted,
    and it keeps working if the package ever moves.
    """
    for directory in Path(__file__).resolve().parents:
        if (directory / "uv.lock").exists() or (directory / ".local").is_dir():
            return directory
    return None


def dsn_from_env() -> str:
    if env := os.environ.get("JETPACE_DSN"):
        return env
    root = find_repo_root()
    pw = root / ".local" / "app_password" if root else None
    if pw is None or not pw.exists():
        raise SystemExit(
            "No database password found.\n"
            "  Run: python scripts/setup_postgres.py\n"
            "  Or set JETPACE_DSN to a connection string.")
    return (f"postgresql://jetpace_app:{pw.read_text().strip()}"
            f"@127.0.0.1:5433/jetpace")
