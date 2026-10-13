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

# classify_dp lived here, judging a pressure value as "in spec", "low" or
# "reversed", and was registered as a template global so any page could
# call it. The shared live table did, which is how the overview came to
# have an opinion about cleanrooms.
#
# It is gone rather than kept unused: a product-specific helper available
# to every template is an invitation to re-couple the shell to one
# product, and the next person to need it will not know it was supposed to
# be out of bounds. When /environment needs server-rendered status pills
# it comes back there, with the limits that belong to it.

# The time ranges offered on a trend. Labels are what a person says out
# loud; the hours are what the query gets.
RANGES: tuple[tuple[str, float], ...] = (
    ("1 h", 1),
    ("6 h", 6),
    ("24 h", 24),
    ("3 d", 72),
    ("7 d", 168),
    ("30 d", 720),
)

# A window from a query string is not to be trusted. These are the bounds.
MIN_HOURS = 0.1
MAX_HOURS = 24 * 400


def window_label(hours: float) -> str:
    """How the chart footer says the window, in words rather than hours."""
    for label, h in RANGES:
        if abs(h - hours) < 1e-9:
            return f"last {label}"
    if hours < 1:
        return f"last {int(hours * 60)} min"
    if hours < 48:
        return f"last {hours:g} h"
    return f"last {hours / 24:g} days"




def create_app(dsn: str) -> FastAPI:
    app = FastAPI(title="Jetpace", docs_url="/api/docs")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")

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
        """
        The overview, and only what is true of every product.

        It used to carry the pressure panels and the energy panels on the
        same screen, which is a page that is wrong for every customer -
        each of them has bought one product and is being shown another.
        Those moved to /environment and /energy.
        """
        with queries.connect(dsn) as conn:
            return templates.TemplateResponse(request, "index.html", {
                "summary": queries.summary(conn),
                "devices": rows_for_live(conn),
            })

    # --- the product views ----------------------------------------------
    #
    # Separate routes, separate templates, and the parameter keys they are
    # about ('dp', 'kwh_import') appear only here and in the queries these
    # call. The shell above mentions neither.
    #
    # architecture.md §3 has these becoming packages/energy and
    # packages/environment, with environment pinning a validated core
    # version while energy tracks head. This is the seam that makes that a
    # file move rather than a redesign.

    @app.get("/environment", response_class=HTMLResponse)
    def environment(request: Request):
        with queries.connect(dsn) as conn:
            return templates.TemplateResponse(request, "environment.html", {
                "summary": queries.environment_summary(conn),
                "excursions": queries.excursions(conn, days=7),
            })

    @app.get("/energy", response_class=HTMLResponse)
    def energy(request: Request):
        with queries.connect(dsn) as conn:
            return templates.TemplateResponse(request, "energy.html", {
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

    # --- the device list and one device --------------------------------

    @app.get("/devices", response_class=HTMLResponse)
    def devices_page(request: Request):
        with queries.connect(dsn) as conn:
            return templates.TemplateResponse(request, "devices.html", {
                "devices": queries.devices(conn),
            })

    @app.get("/partials/devices", response_class=HTMLResponse)
    def partial_devices(request: Request):
        with queries.connect(dsn) as conn:
            return templates.TemplateResponse(request, "_devices.html", {
                "devices": queries.devices(conn),
            })

    @app.get("/devices/{code}", response_class=HTMLResponse)
    def device_page(request: Request, code: str):
        with queries.connect(dsn) as conn:
            found = queries.device(conn, code)
            if found is None:
                # 404 as a page, not as JSON: this URL is reached by
                # clicking or by a stale bookmark, and a person is reading.
                return templates.TemplateResponse(
                    request, "not_found.html", {"code": code},
                    status_code=404)
            return templates.TemplateResponse(request, "device.html", {
                "device": found,
                "points": queries.device_points(conn, code),
                "ranges": RANGES,
            })

    @app.get("/partials/device-points/{code}", response_class=HTMLResponse)
    def partial_device_points(request: Request, code: str):
        with queries.connect(dsn) as conn:
            return templates.TemplateResponse(request, "_device_points.html", {
                "points": queries.device_points(conn, code),
            })

    @app.get("/api/trend")
    def api_trend(device: str, param: str, hours: float = 24.0):
        """
        One device, one parameter, any window.

        The window is clamped rather than trusted. hours=100000 would ask
        the database to bucket the entire history into one chart, and the
        only thing that achieves is a timeout on a site - from a URL
        anybody can type.

        Points are arrays rather than objects: [ms, avg, min, max,
        samples]. Thirty days is a few hundred rows, and five keys repeated
        a few hundred times is most of the response body.
        """
        hours = max(MIN_HOURS, min(float(hours), MAX_HOURS))
        with queries.connect(dsn) as conn:
            rows, width, source = queries.trend(conn, device, param, hours)

        points = [
            [int(r["bucket"].timestamp() * 1000),
             round(float(r["avg"]), 4),
             round(float(r["min"]), 4),
             round(float(r["max"]), 4),
             int(r["samples"] or 0)]
            for r in rows
        ]
        return JSONResponse({
            "device": device, "param": param, "hours": hours,
            "window": window_label(hours),
            "width": width, "source": source,
            "rows": len(points), "points": points,
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
