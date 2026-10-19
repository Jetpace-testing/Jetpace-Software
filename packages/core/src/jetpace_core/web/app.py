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
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import permissions
from ..poller import PointSpec
from . import auth_routes
from . import products as products_
from . import queries, writes

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


# How many rows and how long a window an export may ask for. A day of a
# 400-meter site is millions of readings, and an unbounded export is a
# way to take the dashboard down from a URL.
EXPORT_MAX_HOURS = 24 * 31
EXPORT_MAX_ROWS = 2_000_000

# The exports the reports panel offers. Windows rather than files,
# because nothing is pre-generated.
EXPORTS: tuple[tuple[str, str, str], ...] = (
    ("Readings, last 24 hours",
     "Every reading with its device, parameter, unit and quality flag",
     "/export/readings.csv?hours=24"),
    ("Readings, last 7 days",
     "The same, over a week - large, and streamed",
     "/export/readings.csv?hours=168"),
    ("Device inventory",
     "Every commissioned device, its gateway, model and register revision",
     "/export/devices.csv"),
)

# The sparkline's coordinate space: the viewBox the template declares.
SPARK_W, SPARK_H = 100.0, 24.0


# How many figures the shell contributes to the overview before any
# product adds its own: devices, online, offline, readings in 24 h.
# Pinned to _kpis.html by a test, because this number exists only to work
# out how to lay the row out and would otherwise drift the first time
# somebody adds a fifth.
# How long to wait for an instrument on the commissioning form.
#
# Shorter than the poller's own 2 s, deliberately: a browser is waiting,
# and the two failures this is distinguishing - the converter is not
# there, the slave is not answering - are both decided by a timeout. A
# device type with two points means a wrong slave address costs this once
# rather than twice, because the probe stops after the first point does
# not come back.
PROBE_TIMEOUT = 1.5

# What the form promises about how soon a commissioned device starts being
# read. It is `jetpace poller --reload`'s default, and the form says the
# number so that a device sitting offline for a few seconds reads as the
# truth about it rather than as a fault. A site that tunes --reload has to
# change this to match; nothing can discover it, because the poller is a
# different process and may not even be running.
DEVICE_RELOAD_SECONDS = 10

SHELL_TILES = 4

# The most figures that stay readable in one row, at three widths. Nine
# in a row measured 139px a card, which is what "a bit small" meant.
KPI_CAPS = {"": 5, "md": 4, "sm": 2}


def kpi_grid(tiles: int) -> str:
    """
    The custom properties the figures row carries, as a style attribute.

    One per breakpoint, because `repeat()` takes an integer and CSS has
    no way to cap one - so the caps are applied here, where the tile
    count is already known, and the stylesheet's media queries just
    choose which of the three to read.
    """
    return "; ".join(
        f"--kpi-cols{'-' + name if name else ''}: "
        f"{kpi_columns(tiles, cap)}"
        for name, cap in KPI_CAPS.items())


def kpi_columns(tiles: int, cap: int) -> int:
    """
    How many columns the figures row should use, at most `cap` wide.

    The design writes `repeat(7, ...)` because it has exactly seven
    figures. We have four plus whatever the loaded products contribute:
    four on a bare install, seven with one product, nine with both. A
    fixed count is wrong for two of those three, and letting the browser
    fill the row gave nine cards at 139px - then, one breakpoint down,
    eight with a ninth stranded underneath.

    SPREAD EVENLY OVER THE FEWEST ROWS THAT FIT UNDER THE CAP. Take the
    minimum number of rows, then divide the figures across them rather
    than filling each row to the cap. Nine under a cap of five is two
    rows of five and four, not five and four by accident; nine under a
    cap of four is three rows of three, not four, four and one.

    THEN, IF THAT STILL STRANDS ONE, STEP DOWN. Nine under a cap of four
    spreads to three rows of three; thirteen under the same cap spreads
    to four columns and leaves a single card, so it tries three, then
    two. Some counts cannot avoid it - thirteen leaves a remainder of one
    at four, three and two columns alike - and those keep the widest
    layout rather than being made narrow for nothing.

    The browser cannot work this out, because the rule is about what the
    row MEANS - a lone card under a full row reads as a mistake - rather
    than about what fits.
    """
    tiles = max(tiles, 1)
    rows = -(-tiles // cap)                 # ceil: fewest rows under the cap
    ideal = min(cap, -(-tiles // rows))     # ceil: spread evenly over them

    for columns in range(ideal, 1, -1):
        if tiles <= columns or tiles % columns != 1:
            return columns
    return ideal


def sparkline(values: Sequence[float]) -> str:
    """
    A list of numbers as SVG polyline coordinates.

    Normalised to its own range, which is the right choice for a mark
    this small: a 24-pixel line scaled to an absolute axis would be flat
    for most parameters and meaningless for the rest. It shows the SHAPE
    of the day and nothing else, which is why the template marks it
    presentational and prints the real range underneath.

    A flat series draws down the middle rather than along the floor -
    pinning it to y=0 would read as "it fell to zero", which is the one
    thing a flat line definitely does not mean.
    """
    points = [float(v) for v in values if v is not None]
    if not points:
        return ""
    if len(points) == 1:
        return f"0,{SPARK_H / 2:.2f} {SPARK_W:.2f},{SPARK_H / 2:.2f}"

    lo, hi = min(points), max(points)
    span = hi - lo
    step = SPARK_W / (len(points) - 1)
    out = []
    for i, v in enumerate(points):
        # SVG y grows downward, so the subtraction is what puts a high
        # reading at the top.
        y = SPARK_H / 2 if span == 0 else SPARK_H * (1 - (v - lo) / span)
        out.append(f"{i * step:.2f},{y:.2f}")
    return " ".join(out)




async def set_point_fields(request: Request) -> dict[str, str]:
    """
    The `low_<param>` / `high_<param>` fields, whatever they turned out to be.

    A DEPENDENCY RATHER THAN DECLARED Form PARAMETERS, because how many
    there are depends on which device type was chosen, and FastAPI can
    only declare a fixed list. The names stay unique per parameter so
    `WriteRejected.field` keeps addressing one input: the modal reddens a
    field with `querySelector("[name=...]")`, and a repeated name would
    redden whichever came first - a humidity rejection pointing at the
    temperature box.

    AND IT IS ASYNC WHILE THE ROUTE STAYS SYNC, deliberately. Reading the
    form needs an await; making the route itself `async def` to get one
    would put a blocking Modbus probe - held under `probe_lock`, up to a
    timeout per point - and a blocking database transaction on the event
    loop, with every five-second panel poll queued behind it. FastAPI
    awaits the dependency on the loop and runs the route in the
    threadpool, which is the arrangement the rest of this file assumes.
    """
    form = await request.form()
    return {key: str(value) for key, value in form.multi_items()
            if key.startswith(("low_", "high_"))}


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
        directory=[HERE / "templates",
                   *(p.templates for p in products
                     if p.templates is not None)])

    # The navigation follows what is loaded. It used to be four literal
    # links in _base.html, two of which named products, so a customer who
    # had bought one product had a tab for the other that led to a 404.
    #
    # `linked` rather than the routes callable itself: the template needs
    # to know whether there is a page to link TO, and handing a view a
    # callable it could invoke is how a template grows a side effect.
    templates.env.globals["products"] = [
        {"slug": p.slug, "label": p.label,
         "linked": p.routes is not None,
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

    # --- who is asking -----------------------------------------------
    #
    # Installed BEFORE the routes below, and as a middleware rather than
    # a dependency: a dependency is opt-in, so a route that forgets one
    # is open and looks exactly like a route that did not. This is
    # opt-out - everything is checked and the few public paths are named
    # in one list. See web/auth_routes.py.
    auth_routes.install(app, dsn, templates)
    auth_routes.install_users(app, dsn, templates)

    # Every template can ask who is signed in, so the shell can show a
    # name and hide what a role cannot reach. HIDING IS NOT ENFORCING:
    # the server refuses regardless, and the page only avoids offering a
    # button that would be refused.
    def signed_in(request):
        return auth_routes.principal_of(request)

    def may(request, permission):
        person = auth_routes.principal_of(request)
        return bool(person and person.can(permission))

    templates.env.globals["signed_in"] = signed_in
    templates.env.globals["may"] = may
    templates.env.globals["PERM"] = permissions

    @app.exception_handler(auth_routes.Forbidden)
    def forbidden(request: Request, exc: auth_routes.Forbidden):
        """
        403 as a page for a person, as a short body for a panel.

        It says WHICH roles may do the thing, because "forbidden" alone
        sends somebody to the administrator without knowing what to ask
        for.
        """
        if auth_routes.wants_html(request):
            return templates.TemplateResponse(
                request, "forbidden.html",
                {"message": exc.message,
                 "principal": auth_routes.principal_of(request)},
                status_code=403)
        return HTMLResponse(f"<p class=\"panel-note\">{exc.message}</p>",
                            status_code=403)

    def product_tiles(conn) -> list[dict]:
        """
        Each loaded product's own figures for the overview.

        ISOLATED PER PRODUCT. One product's query failing must not blank
        the whole first screen - but it must not vanish quietly either,
        or a site runs for a week with a tile missing and nobody wonders
        why. A product that raises gets a tile saying so, in its own
        place, and the rest of the page is unaffected.
        """
        collected: list[dict] = []
        for product in products:
            if product.tiles is None:
                continue
            try:
                found = product.tiles(conn)
            except Exception as exc:                   # noqa: BLE001
                print(f"  {product.slug} tiles failed: {exc}", flush=True)
                collected.append({"label": product.label, "state": "alarm",
                                  "value": "unavailable", "small": "",
                                  "icon": "alarm", "tone": "red"})
                continue
            collected.extend(
                {"label": t.label, "value": t.value, "small": t.small,
                 "state": t.state, "icon": t.icon, "tone": t.tone}
                for t in found)
        return collected

    def product_events(conn) -> list[products_.Event]:
        """
        Each loaded product's own exceptions, for the panel on the
        overview.

        ISOLATED PER PRODUCT, for the same reason the tiles are: one
        product's query failing must not blank the panel, and must not
        vanish quietly either. A product that raises contributes one row
        saying so, which is an exception in its own right - a product
        that cannot tell you whether the plant is in spec is a fault
        worth seeing on the first screen.
        """
        collected: list[products_.Event] = []
        for product in products:
            if product.events is None:
                continue
            try:
                collected.extend(product.events(conn))
            except Exception as exc:                   # noqa: BLE001
                print(f"  {product.slug} events failed: {exc}", flush=True)
                collected.append(products_.Event(
                    when=None, kind="suspect", device=product.label,
                    where="this product's own check",
                    parameter="Exceptions", value="unavailable", limit="—",
                    ongoing=True, detail=str(exc)[:120]))
        return collected

    def exceptions(conn, limit: int = 12) -> list[dict]:
        """
        Everything the overview can honestly call an exception.

        THE SHELL'S TWO KINDS, plus whatever the loaded products
        contribute. Core's are both facts about data arriving - a device
        that has gone quiet, a sample the poller itself flagged - which
        is the only kind of judgement the shared layer is entitled to
        make. Anything measured against a specification needs the
        specification, and that belongs to a product.

        NEWEST FIRST, then capped. A site with two hundred silent devices
        would otherwise push a live pressure excursion off the bottom of
        a panel designed to show four rows, and the excursion is the one
        somebody has to act on. Ongoing before recovered, for the same
        reason.
        """
        rows: list[dict] = []

        for s in queries.silence(conn, limit=limit):
            age = s["age_s"]
            rows.append({
                "when": s["newest"], "kind": "silent",
                "device": s["code"], "where": s["gateway"],
                "parameter": "Connectivity",
                "value": "no data",
                # The threshold is the shell's, so the shell may name it.
                "limit": "< 90 s",
                "ongoing": True,
                "detail": ("never reported" if age is None else
                           f"silent for {age // 3600} h" if age >= 3600 else
                           f"silent for {age // 60} min"),
            })

        for s in queries.suspect(conn, limit=limit):
            value = s["value"]
            rows.append({
                "when": s["newest"], "kind": "suspect",
                "device": s["code"], "where": s["gateway"],
                "parameter": s["parameter"],
                "value": ("—" if value is None else
                          f"{float(value):,.{s['decimals']}f} {s['unit']}"),
                "limit": "quality 0",
                # A flagged sample is a thing that happened, not a state
                # that persists; the newest one is as current as it gets.
                "ongoing": False,
                "detail": (f"{s['samples']} "
                           + ("substituted" if s["worst"] == 1 else "suspect")
                           + f" sample{'' if s['samples'] == 1 else 's'}"),
            })

        rows.extend({
            "when": e.when, "kind": e.kind, "device": e.device,
            "where": e.where, "parameter": e.parameter, "value": e.value,
            "limit": e.limit, "ongoing": e.ongoing, "detail": e.detail,
        } for e in product_events(conn))

        # None sorts before any datetime, and a device that has NEVER
        # reported is the oldest news there is - so an absent timestamp
        # is treated as the beginning of time rather than crashing the
        # comparison or floating to the top.
        rows.sort(key=lambda r: (r["ongoing"],
                                 r["when"].timestamp() if r["when"] else 0.0),
                  reverse=True)
        return rows[:limit]

    def facility(conn) -> tuple[list[dict], dict]:
        """
        Devices grouped by the gateway they hang off, with a count per
        state.

        The gateway IS the grouping, not a stand-in for one: it is a
        serial bus or a converter, which on a plant floor is an area. The
        design's floor plan needs room coordinates, and there is no room
        in this schema - see the note on FACILITY.
        """
        zones: dict[str, dict] = {}
        counts = {"online": 0, "silent": 0, "never": 0}

        for r in queries.facility(conn):
            z = zones.setdefault(r["gateway"], {
                "gateway": r["gateway"], "host": r["host"], "port": r["port"],
                "enabled": r["enabled"], "site": r["site"], "devices": [],
            })
            if r["online"]:
                marker, state, key = "normal", "online", "online"
            elif r["newest"] is None:
                marker, state, key = "warning", "never reported", "never"
            else:
                marker, state, key = "offline", "silent", "silent"
            counts[key] += 1
            z["devices"].append({"code": r["code"], "name": r["name"],
                                 "marker": marker, "state": state})

        return list(zones.values()), counts

    def device_card(conn, code: str | None) -> dict:
        """
        One device's current values, with the shape of its day.

        Two queries: what it reports now, and its hourly rollup for the
        last 24 hours. The second is what draws the sparklines and gives
        each card its observed range - from the rollup rather than from
        raw readings, because a day of one device at a 2-second scan is
        43,200 rows to draw a 24-pixel line.
        """
        if not code:
            return {}
        device = queries.device(conn, code)
        if device is None:
            return {}

        shape = queries.device_shape(conn, code)
        points = []
        newest = None
        online = False
        suspect = False
        for p in queries.device_points(conn, code):
            s = shape.get(p["key"], {})
            points.append({**p,
                           "spark": sparkline(s.get("points", [])),
                           "lo": s.get("lo"), "hi": s.get("hi")})
            if newest is None or p["ts"] > newest:
                newest = p["ts"]
            online = online or bool(p["fresh"])
            suspect = suspect or bool(p["quality"])

        return {"device": device, "points": points, "newest": newest,
                "online": online, "suspect": suspect}

    def trend_points(rows) -> dict[str, list[dict]]:
        """
        Which parameters each device reports, for the trend panel's tabs.

        Built from the rows the live table already has, so the tabs cost
        no extra query and cannot disagree with the table above them.
        """
        return {
            d["code"]: [{"key": p["param"], "label": p["param_label"],
                         "unit": p["unit"], "decimals": p["decimals"],
                         "aggregation": p["aggregation"]}
                        for p in d["points"]]
            for d in rows
        }

    def initial_device(rows) -> str | None:
        """
        Which device the trend panel and the device card start on.

        A DEVICE THAT IS ANSWERING, if there is one. The obvious choice
        is the first row, and on this prototype the first row is a demo
        device that stopped reporting four days ago - so the first screen
        opens on an empty chart saying "nothing recorded", which reads as
        a broken panel rather than as a quiet instrument. Any real site
        with a decommissioned device at the top of the alphabet has the
        same problem.

        Falling back to the first row when nothing is online, because an
        empty chart is still better than an empty select.
        """
        for r in rows:
            if r["online"]:
                return r["code"]
        return rows[0]["code"] if rows else None

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
        auth_routes.require(request, permissions.VIEW)
        with queries.connect(dsn) as conn:
            rows = rows_for_live(conn)
            zones, counts = facility(conn)
            tiles = product_tiles(conn)
            first = initial_device(rows)
            catalogue = device_types()
            default = (next(iter(catalogue.values()))[1]
                       if catalogue else None)
            return templates.TemplateResponse(request, "index.html", {
                "summary": queries.summary(conn),
                # The commissioning form's furniture. Computed on the
                # overview rather than fetched when the modal opens,
                # because a suggested code has to be read off the device
                # table and the modal is already in the page.
                "device_types": {code: entry[1]
                                 for code, entry in catalogue.items()},
                "type_groups": type_groups(),
                "device_type": default,
                "planned_types": planned_types(),
                "default_port": default.default_port if default else 502,
                "suggested_code": (
                    writes.suggest_code(conn, queries.tenant_id(conn),
                                        default.code_prefix)
                    if default else ""),
                "devices": rows,
                "product_tiles": tiles,
                "kpi_columns": kpi_grid(SHELL_TILES + len(tiles)),
                "distribution": queries.distribution(conn),
                "zones": zones,
                "counts": counts,
                "site_label": zones[0]["site"] if zones else None,
                "card": device_card(conn, first),
                "selected": first,
                "trend_points": trend_points(rows),
                "events": exceptions(conn),
                "ranges": RANGES,
                "exports": [{"label": a, "note": b, "href": c}
                            for a, b, c in EXPORTS],
            })

    # --- commissioning a device ----------------------------------------
    #
    # The first write path this application has had. Three routes: one
    # that describes the selected device type, one that reads the
    # instrument, and one that creates the rows.
    #
    # ONE PROBE AT A TIME per process. A probe holds a TCP connection to
    # a converter for up to a couple of seconds and lands a request on a
    # shared serial bus that the poller is also walking. Converters
    # serialise onto the serial side, so an overlapping probe comes back
    # empty rather than corrupting anything - but an operator clicking
    # the button five times should queue, not pile five sessions onto one
    # bus. A single lock rather than one per bus because probes are rare
    # and short, and a dict of locks would be a second thing to get
    # wrong.
    probe_lock = threading.Lock()

    def device_types() -> dict:
        """
        Every instrument the loaded products sell, keyed by model code.

        Computed per request rather than once, because it is a dict
        comprehension over a tuple the products already hold - and
        because caching it would make a reload of this process the only
        way to notice a product being enabled.
        """
        return products_.device_types(products)

    def type_groups() -> list:
        """
        The catalogue as the dropdown shows it: families, then variants.

        Several models can be one instrument with different sensors
        fitted - a cleanroom display comes as pressure, as temperature
        and humidity, or as all three - and an operator looks for the
        product before the variant.

        GROUPED IN DECLARATION ORDER, not with Jinja's `groupby`, which
        sorts by the attribute. Sorting would move whichever entry the
        browser selects by default away from the one `index` computed
        the fields for, and a form showing one instrument selected
        beside another's set points is the fault this whole partial
        exists to prevent.
        """
        groups: list[tuple[str, list]] = []
        for _code, (_slug, device_type) in device_types().items():
            if groups and groups[-1][0] == device_type.family:
                groups[-1][1].append(device_type)
            else:
                groups.append((device_type.family, [device_type]))
        return groups

    def planned_types() -> list:
        """
        Instruments the loaded products will sell and cannot yet.

        Shown greyed in the type list. They carry no register map, so
        there is nothing to commission against them - which is why they
        are a separate list and not entries with a missing field.
        """
        return [p for product in products for p in product.planned]

    def point_specs(device_type) -> list:
        """
        The device type's points as the poller's own PointSpec.

        Built here so the probe reads EXACTLY what the poller will read,
        from the same register map, with the same addresses and the same
        decoding. A probe that read something else would be confirming a
        device the poller then cannot talk to.
        """
        return [
            PointSpec(param=p.param,
                      address=device_type.register_for(p).address,
                      kind=device_type.register_for(p).kind,
                      scale=p.scale)
            for p in device_type.points
        ]

    def probe_detail(found, device_type) -> str:
        """
        One line recording what the instrument said, for the audit row.

        On success this is the reading the device was accepted on, which
        is the one measurement nobody can reconstruct later. On failure
        it is the probe's own explanation, which is what somebody reading
        the audit log in a year needs in order to judge the override.
        """
        if not found.answered:
            return found.detail
        units = {p.param: p.unit for p in device_type.points}
        decimals = {p.param: p.decimals for p in device_type.points}
        return ", ".join(
            f"{v.param}={v.value:.{decimals.get(v.param, 2)}f}"
            f"{' ' + units[v.param] if units.get(v.param) else ''}"
            for v in found.values) or found.detail

    def limit_text(limit: dict | None, unit: str) -> str:
        """"LSP -20 Pa, HSP -5 Pa", or one side, or nothing."""
        if not limit:
            return ""
        parts = []
        if limit["low"] is not None:
            parts.append(f"LSP {float(limit['low']):g}")
        if limit["high"] is not None:
            parts.append(f"HSP {float(limit['high']):g}")
        text = ", ".join(parts)
        return f"{text} {unit}".strip() if text else ""

    def limit_lines(limits: dict, device_type) -> list[dict]:
        """
        One line per parameter that ended up with a specification.

        A list and not one string: a cleanroom transmitter is held to a
        temperature band AND a humidity band, and running them together
        on one line is how somebody reads a humidity set point in
        degrees.
        """
        return [{"label": point.label,
                 "text": limit_text(limits.get(point.param), point.unit)}
                for point in device_type.limit_points()
                if limits.get(point.param)]

    def set_points_from(posted: dict, device_type):
        """
        Turn `low_<param>` / `high_<param>` fields into set points.

        TWO RULES, and the second is the one worth stating. A blank field
        for a parameter this type does not read is IGNORED: the operator
        never saw that field, it is left over from another type the
        dropdown offered. A field with something in it is REJECTED,
        naming itself - somebody typed a specification against a
        parameter this instrument does not measure, and dropping that
        silently loses a specification.
        """
        mine = {p.param for p in device_type.limit_points()}
        for field, value in posted.items():
            param = field.split("_", 1)[1]
            if param not in mine and str(value).strip():
                raise writes.WriteRejected(
                    f"{device_type.code} does not measure {param!r}, so it "
                    f"has no set point for it.", field)
        return [
            writes.SetPoint(param=point.param,
                            low=posted.get(f"low_{point.param}", ""),
                            high=posted.get(f"high_{point.param}", ""),
                            low_field=f"low_{point.param}",
                            high_field=f"high_{point.param}")
            for point in device_type.limit_points()
        ]

    def selected_type(model: str | None):
        """
        The DeviceType the form is talking about, or the first one.

        A model code that no loaded product sells is a rejection and not
        a fallback: silently commissioning a device as something else is
        the worst outcome this form has available, because the register
        map decides which addresses are read and nothing downstream would
        ever question it.
        """
        catalogue = device_types()
        if not catalogue:
            raise writes.WriteRejected(
                "No product in this install carries a register map, so "
                "there is no device type to commission.", "model")
        if not model:
            return next(iter(catalogue.values()))[1]
        entry = catalogue.get(model)
        if entry is None:
            raise writes.WriteRejected(
                f"{model!r} is not a device type this install carries. It "
                f"has {', '.join(sorted(catalogue))}.", "model")
        return entry[1]

    # `/partials/device-type` stood here once rendering a note about
    # which registers the chosen instrument would be read at. That note
    # went with the rest of the form's explanatory text, and so did the
    # route - but the route is back, with a different job, because the
    # form now has FIELDS that depend on which type is selected and not
    # just prose.
    #
    # Three things on the form belong to the chosen type: the set point
    # fields (one pair per parameter it carries a specification on, each
    # labelled with that parameter's own unit), the default port, and the
    # suggested device code. Until this route existed they were all
    # computed once, from the FIRST type in the catalogue, and nothing
    # updated them when the dropdown changed. With one type that was
    # invisible; with two it was a wrong unit; with per-parameter set
    # points it would be the wrong parameters entirely.
    #
    # A request rather than JavaScript because the code suggestion has to
    # be read off the device table - "the lowest free RH-nnn" is not
    # something a browser can work out.

    @app.get("/partials/device-type", response_class=HTMLResponse)
    def partial_device_type(request: Request, model: str | None = None):
        """The parts of the form that belong to the selected device type."""
        auth_routes.require(request, permissions.DEVICE_ADD)
        try:
            device_type = selected_type(model)
        except writes.WriteRejected as exc:
            return templates.TemplateResponse(
                request, "_commissioned.html", {"rejected": exc})

        with queries.connect(dsn) as conn:
            suggested = writes.suggest_code(
                conn, queries.tenant_id(conn), device_type.code_prefix)
        return templates.TemplateResponse(request, "_device_type.html", {
            "device_type": device_type,
            "default_port": device_type.default_port,
            "suggested_code": suggested,
            # The port and the code live in other fieldsets, so they
            # travel back as out-of-band swaps - but only in a response.
            # Included in the form itself they would be a second input
            # with the same name.
            "oob": True,
        })

    @app.post("/partials/probe", response_class=HTMLResponse)
    def partial_probe(request: Request,
                      host: str = Form(""),
                      port: str = Form("502"),
                      unit_id: str = Form("1"),
                      model: str | None = Form(None)):
        """
        Read the instrument once and say what came back.

        READ ONLY, and that is a build-time property rather than a
        promise: `modbus_client.probe` calls `read` and there is no write
        function in the module to call. Modbus writes are disabled at
        build time (CLAUDE.md): an instrument's slave address and baud
        rate are writable registers, and a device knocked off the bus
        from a browser is a site visit.
        """
        auth_routes.require(request, permissions.DEVICE_ADD)
        from ..modbus_client import probe as modbus_probe

        try:
            device_type = selected_type(model)
            port_n = writes._as_int(port, "port")
            unit_n = writes._as_int(unit_id, "slave id")
            if not host.strip():
                raise writes.WriteRejected(
                    "Give the converter's address first.", "host")
            if not 1 <= unit_n <= writes.MAX_UNIT_ID:
                raise writes.WriteRejected(
                    f"A Modbus slave address is 1 to "
                    f"{writes.MAX_UNIT_ID}.", "unit_id")
        except writes.WriteRejected as exc:
            return templates.TemplateResponse(
                request, "_commissioned.html", {"rejected": exc})

        with probe_lock:
            result = modbus_probe(host.strip(), port_n, unit_n,
                                  point_specs(device_type),
                                  timeout_s=PROBE_TIMEOUT)

        decimals = {p.param: p.decimals for p in device_type.points}
        return templates.TemplateResponse(request, "_probe.html", {
            "probe": {
                "answered": result.answered,
                "reachable": result.reachable,
                "detail": result.detail,
                # NOT "values". Jinja resolves `probe.values` to the
                # dict's own .values METHOD before it looks for a key of
                # that name, so the template silently iterated a bound
                # method and raised. Any dict handed to a template has
                # `keys`, `values`, `items`, `get` and `update` already
                # taken.
                #
                # Each reading is rounded to the decimals the PRODUCT
                # declared for that parameter. dp is 0 - a cleanroom
                # cascade is read in whole pascals - and "12.00 Pa" on a
                # commissioning screen implies a precision the
                # instrument does not have.
                "readings": [
                    {"param": v.param,
                     "shown": f"{v.value:.{decimals.get(v.param, 2)}f}"}
                    for v in result.values
                ],
            },
            "host": host.strip(), "port": port_n, "unit_id": unit_n,
            "labels": {p.param: p.label for p in device_type.points},
            "units": {p.param: p.unit for p in device_type.points},
        })

    @app.post("/partials/commission", response_class=HTMLResponse)
    def partial_commission(request: Request,
                           host: str = Form(""),
                           port: str = Form("502"),
                           unit_id: str = Form("1"),
                           model: str | None = Form(None),
                           code: str = Form(""),
                           name: str = Form(""),
                           gateway_label: str = Form(""),
                           reason: str = Form(""),
                           posted: dict = Depends(set_point_fields)):
        """
        Create the device, its parameters, its limits and the audit row.

        One transaction, owned by the `with` below: `writes` never
        commits, so either all of it lands or none of it does. A device
        row without its device_parameter rows would be polled and store
        nothing, and a device without its audit row is a configuration
        change at a validated site that nobody can account for.

        THE DEVICE IS PROBED AGAIN HERE, and not trusted from the earlier
        click. What the form posts about whether it answered is a hidden
        field in a browser, and the probe result goes into the audit
        record as the value the instrument was accepted on - so it has to
        be something this process observed, at the moment it wrote the
        row, rather than something it was told.
        """
        auth_routes.require(request, permissions.DEVICE_ADD)
        from ..modbus_client import probe as modbus_probe

        try:
            device_type = selected_type(model)
            port_n = writes._as_int(port, "port")
            unit_n = writes._as_int(unit_id, "slave id")

            with probe_lock:
                found = modbus_probe(host.strip(), port_n, unit_n,
                                     point_specs(device_type),
                                     timeout_s=PROBE_TIMEOUT)

            with queries.connect(dsn) as conn:
                device = writes.commission_device(
                    conn, device_type=device_type,
                    code=code, name=name, host=host, port=port_n,
                    unit_id=unit_n,
                    set_points=set_points_from(posted, device_type),
                    gateway_label=gateway_label,
                    # FULLY, not partly. `probe` reports answered=True
                    # when some registers read and a later one does not,
                    # which with a one-parameter instrument could never
                    # happen. With two it can, and a transmitter whose
                    # humidity register is silent would otherwise be
                    # commissioned as answering, with no reason asked
                    # for and only the temperature in the audit record -
                    # a device accepted against a half-wrong map with
                    # nothing saying so.
                    answered=(found.answered
                              and len(found.values) == len(device_type.points)),
                    detail=probe_detail(found, device_type),
                    reason=reason,
                    # NO LONGER NULL. Every device commissioned from here
                    # now carries the name of whoever did it, into the
                    # audit row and into the set point's `set_by` - which
                    # is the whole reason the user model existed to be
                    # built. architecture.md debt #20 closes here.
                    actor=auth_routes.principal_of(request).username)
                limits = queries.device_limits(conn, device["code"])
                next_code = writes.suggest_code(
                    conn, queries.tenant_id(conn), device_type.code_prefix)
        except writes.WriteRejected as exc:
            return templates.TemplateResponse(
                request, "_commissioned.html", {"rejected": exc})

        return templates.TemplateResponse(request, "_commissioned.html", {
            "device": device,
            "unit_id": device["unit_id"],
            "parameters": ", ".join(p.label for p in device_type.points),
            "limit_lines": limit_lines(limits, device_type),
            "reload_seconds": DEVICE_RELOAD_SECONDS,
            "next_code": next_code,
        })

    # The product views used to be four routes here - /environment,
    # /energy and their two APIs - with the parameter keys 'dp' and
    # 'kwh_import' in the queries they called. They are now
    # packages/environment and packages/energy, mounted at the bottom of
    # this function, and nothing in this file names either of them.

    @app.get("/partials/live", response_class=HTMLResponse)
    def partial_live(request: Request):
        """
        Polled by HTMX. Refreshes four panels on one request.

        The device table is the swap target; the figures, the facility
        panel and the distribution donut come back as out-of-band swaps,
        because the design puts them in cells that are not siblings and
        there is a chart between them that must not be replaced. One
        request rather than four - see _live_push.html.
        """
        auth_routes.require(request, permissions.VIEW)
        with queries.connect(dsn) as conn:
            zones, counts = facility(conn)
            tiles = product_tiles(conn)
            return templates.TemplateResponse(request, "_live_push.html", {
                "summary": queries.summary(conn),
                "devices": rows_for_live(conn),
                "product_tiles": tiles,
                "kpi_columns": kpi_grid(SHELL_TILES + len(tiles)),
                "distribution": queries.distribution(conn),
                "zones": zones,
                "counts": counts,
                "site_label": zones[0]["site"] if zones else None,
            })

    @app.get("/partials/device-card", response_class=HTMLResponse)
    def partial_device_card(request: Request, code: str | None = None):
        """
        The selected-device panel.

        `code` arrives from the trend panel's select - on change, and on
        this panel's own poll through hx-include, so a refresh keeps the
        selection rather than snapping back to the first device. An
        unknown or absent code renders the panel's empty state rather
        than a 404, because this is a fragment inside a page that is
        otherwise fine.
        """
        auth_routes.require(request, permissions.VIEW)
        with queries.connect(dsn) as conn:
            if not code:
                code = initial_device(rows_for_live(conn))
            return templates.TemplateResponse(request, "_device_card.html", {
                "card": device_card(conn, code),
            })

    @app.get("/partials/exceptions", response_class=HTMLResponse)
    def partial_exceptions(request: Request):
        """
        The exceptions panel.

        Polled more slowly than the live table - every 30 seconds rather
        than every 5 - because the suspect-sample query is the only scan
        on this page. See the note on queries.SUSPECT.
        """
        auth_routes.require(request, permissions.VIEW)
        with queries.connect(dsn) as conn:
            return templates.TemplateResponse(request, "_exceptions.html", {
                "events": exceptions(conn),
            })

    # --- exports -------------------------------------------------------
    #
    # The reports panel's rows. Nothing is pre-generated and nothing is
    # stored: each of these runs a query and streams the answer out.
    #
    # NO AUTHENTICATION, because this application has none yet - these
    # are exactly as reachable as every page beside them, which is to say
    # reachable by anyone who can reach the port. That is why the web
    # process binds 127.0.0.1 by default. Login is §10 and these routes
    # go behind it with the rest.

    def csv_line(values) -> str:
        """
        One CSV row.

        Quoting by hand rather than through the csv module because the
        output is streamed: csv.writer wants a file object, and wrapping
        a generator in StringIO to satisfy it would buffer the whole
        export - which is the one thing streaming exists to avoid.
        """
        out = []
        for v in values:
            if v is None:
                out.append("")
                continue
            s = str(v)
            if any(c in s for c in ',"\n\r'):
                s = '"' + s.replace('"', '""') + '"'
            out.append(s)
        return ",".join(out) + "\r\n"

    @app.get("/export/readings.csv")
    def export_readings(request: Request, hours: float = 24.0):
        """
        Readings for a window, streamed.

        BOUNDED IN BOTH DIRECTIONS. The window is clamped and the row
        count is capped, because a day of a 400-meter site is millions of
        rows and an unbounded export is a way to take the dashboard down
        from a URL. When the cap bites, the file says so in a trailing
        comment - a truncated export that looks complete is worse than
        one that refuses.
        """
        auth_routes.require(request, permissions.EXPORT)
        window = max(MIN_HOURS, min(float(hours), EXPORT_MAX_HOURS))

        def rows():
            # The connection is opened INSIDE the generator and closed by
            # it. A server-side cursor only lives as long as its
            # transaction, so a connection opened in the handler and
            # closed on return would hand the browser an empty file.
            with queries.connect(dsn) as conn:
                yield csv_line(["# jetpace readings export",
                                f"window={window:g}h",
                                f"cap={EXPORT_MAX_ROWS}",
                                "timestamps are UTC as stored"])
                yield csv_line(["ts", "device", "device_name", "gateway",
                                "parameter", "unit", "value", "quality"])
                n = 0
                with queries.export_readings(conn, window,
                                             EXPORT_MAX_ROWS) as cur:
                    for r in cur:
                        n += 1
                        yield csv_line([r["ts"].isoformat(), r["device"],
                                        r["device_name"], r["gateway"],
                                        r["parameter"], r["unit"],
                                        r["value"], r["quality"]])
                if n >= EXPORT_MAX_ROWS:
                    yield csv_line([f"# TRUNCATED at {n} rows; "
                                    f"ask for a shorter window"])
                else:
                    yield csv_line([f"# {n} rows"])

        return StreamingResponse(
            rows(), media_type="text/csv",
            headers={"Content-Disposition":
                     f'attachment; filename="readings-{window:g}h.csv"'})

    @app.get("/export/devices.csv")
    def export_devices(request: Request):
        """The commissioned fleet. Small enough to build in one pass."""
        auth_routes.require(request, permissions.EXPORT)
        with queries.connect(dsn) as conn:
            rows = queries.export_devices(conn)

        head = ["code", "name", "gateway", "host", "port", "unit_id",
                "model", "model_revision", "vendor", "kind", "site",
                "scan_class", "commissioned_at"]
        body = [csv_line(["# jetpace device inventory",
                          f"devices={len(rows)}"]), csv_line(head)]
        for r in rows:
            when = r["commissioned_at"]
            body.append(csv_line(
                [r[k] if k != "commissioned_at" else
                 (when.isoformat() if when else "") for k in head]))

        return StreamingResponse(
            iter(body), media_type="text/csv",
            headers={"Content-Disposition":
                     'attachment; filename="devices.csv"'})

    # --- the device list and one device --------------------------------

    @app.get("/devices", response_class=HTMLResponse)
    def devices_page(request: Request):
        auth_routes.require(request, permissions.VIEW)
        with queries.connect(dsn) as conn:
            return templates.TemplateResponse(request, "devices.html", {
                "devices": queries.devices(conn),
            })

    @app.get("/partials/devices", response_class=HTMLResponse)
    def partial_devices(request: Request):
        auth_routes.require(request, permissions.VIEW)
        with queries.connect(dsn) as conn:
            return templates.TemplateResponse(request, "_devices.html", {
                "devices": queries.devices(conn),
            })

    @app.get("/devices/{code}", response_class=HTMLResponse)
    def device_page(request: Request, code: str):
        auth_routes.require(request, permissions.VIEW)
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
                # The set points in force, per parameter. Rendered onto
                # the chips rather than into the chart, so the chart's
                # own code reads them off whichever parameter is
                # selected without a second request - and so a parameter
                # that has no limit simply carries none.
                "limits": queries.device_limits(conn, code),
            })

    # --- taking a device out of service -------------------------------
    #
    # ON THE DEVICE'S OWN PAGE, and deliberately nowhere else. The device
    # list is swapped wholesale by HTMX every ten seconds, so a confirm
    # affordance there can be re-rendered out from under somebody
    # mid-interaction - and a destructive control repeated across four
    # hundred rows, next to nothing but a code, is how the wrong
    # transmitter gets retired. The page has the model, the datasheet
    # reference, the gateway, the slave address and the last reading on
    # screen, which is the context the decision needs.

    @app.post("/devices/{code}/retire", response_class=HTMLResponse)
    def retire(request: Request, code: str, reason: str = Form("")):
        """Retire one device, with a reason, and say what that did."""
        auth_routes.require(request, permissions.DEVICE_RETIRE)
        try:
            with queries.connect(dsn) as conn:
                retired = writes.retire_device(
                    conn, tenant_id=queries.tenant_id(conn),
                    device_code=code, reason=reason,
                    actor=auth_routes.principal_of(request).username)
        except writes.WriteRejected as exc:
            return templates.TemplateResponse(
                request, "_retired.html", {"rejected": exc, "code": code})
        return templates.TemplateResponse(request, "_retired.html", {
            "device": retired,
            "reload_seconds": DEVICE_RELOAD_SECONDS,
        })

    @app.get("/partials/device-points/{code}", response_class=HTMLResponse)
    def partial_device_points(request: Request, code: str):
        auth_routes.require(request, permissions.VIEW)
        with queries.connect(dsn) as conn:
            return templates.TemplateResponse(request, "_device_points.html", {
                "points": queries.device_points(conn, code),
                # Polled every few seconds alongside the values. A set
                # point is append-only and changes about once a year, so
                # re-reading it here is one index lookup per parameter -
                # and it means a limit changed in another tab is on this
                # one within the poll interval, rather than until
                # somebody reloads.
                "limits": queries.device_limits(conn, code),
            })

    @app.get("/api/trend")
    def api_trend(request: Request, device: str, param: str,
                  hours: float = 24.0):
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
        auth_routes.require(request, permissions.VIEW)
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
        # A product with no pages contributes no routes. It is still
        # loaded, because it still carries its instruments to the
        # commissioning form.
        if product.routes is not None:
            app.include_router(product.routes(shell))

    return app


# find_repo_root() and dsn_from_env() used to live here and had no callers
# left: cmd_web builds the app with paths.postgres_dsn(). They were also a
# second, slightly different answer to "where is the root" - this one
# looked for .local and so could not resolve a frozen install, where the
# directory is called data. One root finder is now in jetpace_core.paths,
# which is where the lesson about not counting parents is written down.
