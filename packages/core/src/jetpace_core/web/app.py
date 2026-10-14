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
import time
from collections.abc import Sequence
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import products as products_
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

# How old the newest reading may be before /healthz calls the system
# unwell.
#
# Above OFFLINE_AFTER, which is 90 seconds and is already this project's
# generous answer to "has this device gone quiet" - generous because a
# device in offline backoff is skipped for several scan passes and a
# tighter number would flap. This figure answers a bigger question: not
# one device going quiet but EVERY device going quiet, because `newest`
# is the newest reading from anything at all.
#
# It has to stay above the slowest scan interval in the configuration, or
# a site that polls a meter every three minutes would be reported unwell
# between readings. A site with a slow scan class raises it with
# JETPACE_HEALTH_AGE_LIMIT.
READING_AGE_LIMIT = 120.0

ENV_AGE_LIMIT = "JETPACE_HEALTH_AGE_LIMIT"


def reading_age_limit() -> float:
    """
    The limit, read at call time rather than captured at import.

    The same lesson as HEALTH_DEADLINE in the supervisor: a value frozen
    into a default argument cannot be changed by a test without changing
    how long the test takes, and a health threshold is exactly the thing
    a test needs to move.
    """
    raw = os.environ.get(ENV_AGE_LIMIT)
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 0:
            return value
        # A limit of zero or a typo would mark a healthy system unwell
        # for ever, which during an update means a rollback. Ignore it
        # and say so rather than obeying it.
        print(f"  ignoring {ENV_AGE_LIMIT}={raw!r}: not a positive number",
              flush=True)
    return READING_AGE_LIMIT


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




def create_app(dsn: str,
               products: Sequence[products_.Product] = ()) -> FastAPI:
    """
    The shell, plus whatever products were handed to it.

    `products` is a sequence rather than something this function looks up,
    so core never decides which products exist - see web/products.py. An
    app with none still serves the overview and the device pages, which is
    what makes those tests independent of either product.
    """
    app = FastAPI(title="Jetpace", docs_url="/api/docs")

    # When this process started, as a monotonic reading. /healthz asks
    # whether a reading has arrived since, which is how a release proves
    # it is logging rather than inheriting its predecessor's data.
    # Monotonic because it is only ever compared against another elapsed
    # time, and because a wall clock can step backwards.
    started = time.monotonic()

    # Product static BEFORE core's. Starlette matches mounts in the order
    # they are registered and "/static" is a prefix of
    # "/static/environment", so mounting core first would swallow every
    # product asset and serve a 404 from the wrong directory.
    for product in products:
        if product.static is not None:
            app.mount(f"/static/{product.slug}",
                      StaticFiles(directory=product.static),
                      name=f"static-{product.slug}")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    # One Jinja environment over several directories, so a product
    # template can `{% extends "_base.html" %}` and get the real shell.
    # Core first: FileSystemLoader returns the first match, so a product
    # cannot shadow a shell template - and because that resolution is
    # silent, test_web_shell.py asserts no basename appears twice.
    templates = Jinja2Templates(
        directory=[HERE / "templates", *(p.templates for p in products)])

    # The navigation follows what is loaded. It used to be four literal
    # links in _base.html, two of which named products, so a customer who
    # had bought one product had a tab for the other that led to a 404.
    templates.env.globals["products"] = [
        {"slug": p.slug, "label": p.label,
         "stylesheet": (f"/static/{p.slug}/{p.stylesheet}"
                        if p.stylesheet else None)}
        for p in products
    ]

    # --- cache busting -----------------------------------------------------
    #
    # Templates ask for an asset through `asset()`, which appends the file's
    # modification time, so the URL changes whenever the file does.
    #
    # This is not a nicety. StaticFiles sends an etag and a last-modified
    # and no Cache-Control, which leaves the browser to decide, and a
    # browser that decides to keep a stylesheet will render NEW markup with
    # OLD rules. That is not a hypothetical - it happened here the first
    # time the shell changed, and the page came back looking unstyled:
    # tabs across the top from a rule that had been deleted, an off-screen
    # skip link sitting in the middle of the page, and nothing to say why.
    #
    # The place it really matters is after an over-the-air update. The
    # supervisor swaps app/, restarts, and serves new HTML to an operator
    # whose browser has yesterday's CSS. Nobody at the site would guess
    # that is what they are looking at.
    #
    # A stat per asset per render, three of them. Cheap, and it means a
    # changed file is a changed URL without anyone having to remember.
    static_roots: dict[str, Path] = {"": HERE / "static"}
    for product in products:
        if product.static is not None:
            static_roots[product.slug] = product.static

    def asset(url_path: str) -> str:
        rest = url_path.removeprefix("/static/")
        head, _, tail = rest.partition("/")
        if tail and head in static_roots:
            root, relative = static_roots[head], tail
        else:
            root, relative = static_roots[""], rest
        try:
            stamp = int((root / relative).stat().st_mtime)
        except OSError:
            # Missing or unreadable: hand back the plain path and let the
            # 404 say so, rather than inventing a version for a file that
            # is not there.
            return url_path
        return f"{url_path}?v={stamp}"

    templates.env.globals["asset"] = asset

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

    # The product views used to be four routes here - /environment,
    # /energy and their two APIs - with the parameter keys 'dp' and
    # 'kwh_import' in the queries they called. They are now
    # packages/environment and packages/energy, mounted at the bottom of
    # this function, and nothing in this file names either of them.

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

    @app.get("/healthz")
    def healthz():
        """
        The supervisor's release gate: does the database answer, and is
        this version of the software actually logging?

        It used to ask whether any reading existed in the last 24 hours,
        and that was wrong twice over.

        ONE: it stayed true for a full day after logging stopped. On 13
        October a mis-registered service dropped the device simulator,
        readings stopped at 16:55, and this endpoint reported ok:true with
        67,000 readings still inside the window. Half an hour of no data,
        and the thing whose job is to notice said everything was well.

        TWO, and worse for the gate: freshness ALONE would not have fixed
        it. The gate runs just after a release is swapped in, and the
        readings written by the PREVIOUS version are still seconds old at
        that moment. A broken release that never logs anything would pass
        on its first poll, two seconds in, on the strength of data its
        predecessor wrote.

        So there are two questions, and both have to hold:

          - has a reading arrived SINCE THIS PROCESS STARTED? That is what
            the previous version's data cannot answer for, and what a
            release has to earn.
          - is the newest reading younger than the age limit? That is what
            catches logging stopping three hours later.

        The first is compared as DURATIONS - the database's measured age
        against our own uptime - never as two timestamps. Both sides are
        elapsed times, so a clock or timezone disagreement between the
        application and the database cancels out instead of deciding the
        answer.

        A system that has NEVER logged anything is well. It cannot have
        regressed from nothing, and two callers depend on this: the
        installer waits for ok:true on a database that is empty by
        definition, and a site that is installed but not yet commissioned
        must still be able to take an update.
        """
        try:
            with queries.connect(dsn) as conn:
                h = queries.health(conn)
        except Exception as exc:                       # noqa: BLE001
            return JSONResponse({"ok": False, "why": str(exc)},
                                status_code=503)

        uptime = time.monotonic() - started
        age = None if h.get("age_s") is None else float(h["age_s"])
        limit = reading_age_limit()

        if age is None:
            ok = True
            why = "nothing has ever been logged, so there is nothing to lose"
        elif age >= uptime:
            # The pre-swap case. Also true for a few seconds whenever the
            # web process restarts on its own, which resolves itself as
            # soon as the next reading lands.
            ok = False
            why = (f"no reading has arrived in the {uptime:.0f}s since this "
                   f"process started; the newest is {age:.0f}s old and was "
                   f"written before it")
        elif age > limit:
            ok = False
            why = (f"the newest reading is {age:.0f}s old, past the "
                   f"{limit:.0f}s limit; the system has stopped logging")
        else:
            ok = True
            why = "ok"

        return JSONResponse(
            {"ok": ok, "why": why,
             "newest": (h["newest"].isoformat()
                        if h.get("newest") else None),
             "age_s": None if age is None else round(age, 1),
             "uptime_s": round(uptime, 1),
             "age_limit_s": limit,
             "devices": h.get("devices")},
            status_code=200 if ok else 503)

    # Products last, so a product cannot shadow a shell route by
    # registering the same path first. /healthz in particular is the
    # supervisor's release gate and belongs to the platform.
    shell = products_.Shell(dsn=dsn, templates=templates)
    for product in products:
        app.include_router(product.routes(shell))

    return app


# find_repo_root() and dsn_from_env() used to live here and had no callers
# left: cmd_web builds the app with paths.postgres_dsn(). They were also a
# second, slightly different answer to "where is the root" - this one
# looked for .local and so could not resolve a frozen install, where the
# directory is called data. One root finder is now in jetpace_core.paths,
# which is where the lesson about not counting parents is written down.
