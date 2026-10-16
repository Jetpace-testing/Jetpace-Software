"""
The gate: what is reachable without signing in, and what is not.

The most important test in this file is the one that walks every route
the application declares and asserts each is either on the public list or
guarded. A dependency-based gate is opt-in and the failure mode of
forgetting one is an endpoint that is open and looks exactly like one
that is not; a middleware is opt-out, and this test is what keeps the
opt-out list honest.

Runs against a scripted database. Sign-in itself needs a real scrypt
verification, which is a second, so the tests that need one say so.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jetpace_core import permissions as perms
from jetpace_core import security
from jetpace_core.web import auth_routes, identity
from jetpace_core.web import products as P
from jetpace_core.web import queries as q
from jetpace_core.web.app import HERE, create_app

DSN = "postgresql://unused:unused@127.0.0.1:1/unused"
SOURCE = Path(auth_routes.__file__).read_text(encoding="utf-8")


class Conn:
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def cursor(self): return Cur()


class Cur:
    rows = []
    def execute(self, sql, params=None): pass
    def fetchone(self): return {"id": 1, "name": "Prototype", "n": 0}
    def fetchall(self): return []
    def __enter__(self): return self
    def __exit__(self, *exc): return False


@pytest.fixture
def anonymous(monkeypatch):
    """An install with accounts, and a visitor with no session."""
    monkeypatch.setattr(q, "connect", lambda dsn: Conn())
    monkeypatch.setattr(identity, "count_users", lambda conn: 1)
    monkeypatch.setattr(identity, "resolve", lambda conn, t, slide=True: None)
    return TestClient(create_app(DSN, P.load(["environment"])),
                      raise_server_exceptions=False, follow_redirects=False)


@pytest.fixture
def fresh(monkeypatch):
    """An install with no accounts at all."""
    monkeypatch.setattr(q, "connect", lambda dsn: Conn())
    monkeypatch.setattr(identity, "count_users", lambda conn: 0)
    return TestClient(create_app(DSN, P.load(["environment"])),
                      raise_server_exceptions=False, follow_redirects=False)


def signed_in_as(monkeypatch, *roles, must_change=False):
    person = identity.Principal(
        user_id=1, username="rshah", full_name="Rishi Shah", tenant_id=1,
        site_roles={1: frozenset(roles)}, must_change_password=must_change)
    monkeypatch.setattr(q, "connect", lambda dsn: Conn())
    monkeypatch.setattr(identity, "count_users", lambda conn: 1)
    monkeypatch.setattr(identity, "resolve",
                        lambda conn, t, slide=True: person)
    client = TestClient(create_app(DSN, P.load(["environment"])),
                        raise_server_exceptions=False,
                        follow_redirects=False)
    client.cookies.set(identity.COOKIE, "pretend")
    return client


# --- the one that matters -------------------------------------------------

def test_every_route_is_public_on_purpose_or_guarded():
    """
    THE TEST THIS FILE EXISTS FOR.

    Walks every route the application declares and asserts it is either
    on the public list - a decision somebody made and can be asked about
    - or carries a permission check. A new route is open by accident
    exactly once, and this is where that stops.
    """
    app = create_app(DSN, P.load_default())
    app_source = Path(
        __import__("jetpace_core.web.app", fromlist=["x"]).__file__
    ).read_text(encoding="utf-8")
    for pkg in ("environment", "energy"):
        app_source += (Path(__import__(f"jetpace_{pkg}.web", fromlist=["x"])
                            .__file__).read_text(encoding="utf-8"))

    unguarded = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not hasattr(route, "methods") or not path:
            continue
        if auth_routes.is_public(path) or path in auth_routes.PASSWORD_PATHS:
            continue
        if path in ("/users", "/users/{username}/state"):
            continue            # guarded inside auth_routes, checked below
        if path.startswith(("/api/docs", "/openapi", "/docs", "/redoc")):
            continue
        handler = getattr(route, "endpoint", None)
        name = getattr(handler, "__name__", "")
        # The handler's body must contain a require() call.
        body = _body_of(app_source, name)
        if "auth_routes.require" not in body and "require(request" not in body:
            unguarded.append(f"{path} -> {name}")
    assert not unguarded, (
        "these routes neither require a permission nor appear in "
        f"PUBLIC_PATHS: {unguarded}")


def _body_of(source: str, name: str) -> str:
    match = re.search(rf"\n(\s*)def {re.escape(name)}\(", source)
    if not match:
        return ""
    start = match.start()
    indent = len(match.group(1))
    lines = source[start:].splitlines()[1:]
    out = []
    for line in lines[1:]:
        if line.strip() and (len(line) - len(line.lstrip())) <= indent \
                and not line.lstrip().startswith(("#", '"')):
            break
        out.append(line)
    return "\n".join(out)


def test_the_public_list_is_short_and_deliberate():
    """
    Four paths and one prefix. Anything added here is a decision worth
    arguing about, which is the point of asserting the whole set.
    """
    assert auth_routes.PUBLIC_PATHS == {
        "/login", "/first-run", "/logout", "/healthz"}
    assert auth_routes.PUBLIC_PREFIXES == ("/static/",)


def test_health_stays_open_because_the_supervisor_has_no_account():
    """
    Polled every two seconds while a release is on trial, by a process
    with no browser. Behind the login, an update could never be promoted.
    """
    assert auth_routes.is_public("/healthz")


# --- what an anonymous visitor gets --------------------------------------

@pytest.mark.parametrize("path", [
    "/", "/devices", "/devices/DP-001", "/environment", "/energy",
])
def test_a_page_redirects_to_the_login(anonymous, path):
    r = anonymous.get(path, headers={"accept": "text/html"})
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


@pytest.mark.parametrize("path", [
    "/partials/live", "/partials/exceptions", "/api/trend?device=x&param=y",
    "/export/devices.csv", "/export/readings.csv",
])
def test_data_is_refused_rather_than_redirected(anonymous, path):
    """
    A 303 to an export is a CSV containing a login page. A 401 says what
    happened, and HX-Redirect tells HTMX to move the whole window rather
    than render the login form inside a dashboard panel.
    """
    r = anonymous.get(path)
    assert r.status_code == 401
    assert r.headers.get("HX-Redirect") == "/login"


def test_an_htmx_request_is_never_redirected(anonymous):
    r = anonymous.get("/", headers={"accept": "text/html",
                                    "HX-Request": "true"})
    assert r.status_code == 401
    assert r.headers.get("HX-Redirect") == "/login"


def test_the_commissioning_post_is_refused_too(anonymous):
    r = anonymous.post("/partials/commission", data={"code": "DP-009"})
    assert r.status_code == 401


def test_static_files_stay_public(anonymous):
    """The login page needs the stylesheet and the logo to render."""
    assert anonymous.get("/static/app.css").status_code == 200


# --- the first run --------------------------------------------------------

def test_an_install_with_no_accounts_offers_to_create_one(fresh):
    body = fresh.get("/login").text
    assert "Create the administrator" in body
    assert 'action="/first-run"' in body


def test_nothing_is_readable_before_the_first_account_exists(fresh):
    """
    Including the API and the exports. An installation nobody has
    claimed must not be readable either.
    """
    assert fresh.get("/", headers={"accept": "text/html"}).status_code == 303
    assert fresh.get("/export/devices.csv").status_code == 401


def test_first_run_refuses_once_an_account_exists(anonymous):
    """
    The whole security of a public endpoint that creates an
    administrator. Without this it is a way for anybody who can reach
    the port to mint one.
    """
    r = anonymous.post("/first-run", data={
        "username": "mallory", "full_name": "M", "password": "x" * 14,
        "confirm": "x" * 14})
    assert r.status_code == 409
    assert "already has accounts" in r.text


def test_there_is_no_default_password_anywhere():
    """
    Meril RTD ships `admin / Meril-LifeScience` in its source. A known
    credential left enabled is the most common finding there is, so this
    product has none: the first account is created by whoever reaches
    the page first, on an installation nobody can read until they do.
    """
    for path in (Path(identity.__file__), Path(auth_routes.__file__),
                 Path(security.__file__)):
        text = path.read_text(encoding="utf-8")
        assert "INITIAL_USERS" not in text
        assert not re.search(r'password\s*=\s*"[A-Za-z0-9@!]{6,}"', text)


# --- permissions ----------------------------------------------------------

def test_a_viewer_cannot_export(monkeypatch):
    client = signed_in_as(monkeypatch, "viewer")
    assert client.get("/export/devices.csv").status_code == 403


def test_a_manager_can_export(monkeypatch):
    client = signed_in_as(monkeypatch, "manager")
    assert client.get("/export/devices.csv").status_code != 403


def test_a_manager_cannot_commission_a_device(monkeypatch):
    client = signed_in_as(monkeypatch, "manager")
    assert client.post("/partials/commission",
                       data={"code": "DP-009"}).status_code == 403


def test_an_engineer_can(monkeypatch):
    client = signed_in_as(monkeypatch, "engineer")
    assert client.post("/partials/commission",
                       data={"code": "DP-009"}).status_code != 403


def test_only_an_administrator_reaches_the_users_page(monkeypatch):
    assert signed_in_as(monkeypatch, "engineer").get(
        "/users").status_code == 403
    assert signed_in_as(monkeypatch, "administrator").get(
        "/users").status_code != 403


def test_support_cannot_see_the_dashboard(monkeypatch):
    """
    §10: support cannot read instrument data by default. It is a role
    for somebody looking at whether the system works.
    """
    client = signed_in_as(monkeypatch, "support")
    assert client.get("/", headers={"accept": "text/html"}).status_code == 403


def test_a_refusal_says_which_roles_may_do_it(monkeypatch):
    """
    "Forbidden" alone sends somebody to their administrator without
    knowing what to ask for.
    """
    client = signed_in_as(monkeypatch, "viewer")
    body = client.get("/export/devices.csv").text
    assert "Manager" in body and "Engineer" in body


# --- the password that must be changed ------------------------------------

def test_an_issued_password_must_be_replaced_before_anything_else(
        monkeypatch):
    """
    Otherwise an administrator knows a working password for somebody
    else's account, and nothing that account does is attributable to the
    person whose name is on it.
    """
    client = signed_in_as(monkeypatch, "administrator", must_change=True)
    r = client.get("/", headers={"accept": "text/html"})
    assert r.status_code == 303 and r.headers["location"] == "/password"
    assert client.get("/password").status_code == 200


# --- the idle timer -------------------------------------------------------

def test_a_polling_partial_does_not_count_as_being_present():
    """
    THE ONE THAT WOULD FAIL AN INSPECTION.

    Four panels refresh every 5 to 30 seconds. If that extended the
    session, the automatic logoff required by 21 CFR 11.10(d) and Annex
    11 §12 would never fire on a machine with a browser open - which is
    every machine this runs on.
    """
    class R:
        def __init__(self, method, path):
            self.method = method
            self.url = type("U", (), {"path": path})()

    for path in ("/partials/live", "/partials/exceptions",
                 "/partials/device-card", "/partials/devices"):
        assert not auth_routes.counts_as_activity(R("GET", path)), path

    # A person, on the other hand.
    for method, path in (("GET", "/"), ("GET", "/devices"),
                         ("GET", "/api/trend"),
                         ("POST", "/partials/commission")):
        assert auth_routes.counts_as_activity(R(method, path)), path


def test_the_idle_limit_is_short_enough_to_mean_something():
    assert identity.IDLE_MINUTES <= 30
    assert identity.ABSOLUTE_HOURS <= 24, (
        "a session outliving a shift attributes the next shift's actions "
        "to the person who signed in")


# --- redirect safety ------------------------------------------------------

@pytest.mark.parametrize("target", [
    "https://evil.example/", "//evil.example/", "http://evil.example",
    "javascript:alert(1)", "/\\evil.example",
])
def test_a_login_redirect_cannot_leave_this_site(target):
    """
    A login page that redirects wherever a query string says is the
    standard way one becomes part of somebody else's phishing page: the
    link looks like yours, the password is typed into yours, and the
    browser lands on theirs.
    """
    assert auth_routes.safe_next(target) == "/"


def test_an_ordinary_path_survives():
    assert auth_routes.safe_next("/devices/DP-001") == "/devices/DP-001"


# --- the cookie -----------------------------------------------------------

def test_the_cookie_cannot_be_read_by_script_or_sent_cross_site():
    assert "httponly=True" in SOURCE
    assert 'samesite="lax"' in SOURCE


def test_the_cookie_is_only_marked_secure_over_https():
    """
    A Secure cookie on http is not stored at all, so the sign-in would
    appear to succeed and then not. These dashboards are http on
    127.0.0.1.
    """
    assert 'secure=request.url.scheme == "https"' in SOURCE


def test_signing_out_clears_the_cookie_even_if_the_database_is_down():
    assert "delete_cookie" in SOURCE
    out = SOURCE.split("def logout")[1].split("@app")[0]
    assert "except Exception" in out, (
        "a sign-out that cannot reach the database must still take the "
        "browser's credential away")


# --- failing closed -------------------------------------------------------

def test_the_gate_refuses_when_the_database_is_unreachable(monkeypatch):
    """
    An authentication check that cannot run has not passed. Falling open
    here would mean a database outage is also an access-control outage.
    """
    def explode(dsn):
        raise RuntimeError("no database")

    monkeypatch.setattr(q, "connect", explode)
    client = TestClient(create_app(DSN, P.load(["environment"])),
                        raise_server_exceptions=False)
    assert client.get("/").status_code == 503


# --- the account menu -----------------------------------------------------

def test_the_header_shows_who_is_signed_in(monkeypatch):
    client = signed_in_as(monkeypatch, "administrator")
    body = client.get("/", headers={"accept": "text/html"}).text
    assert "Rishi Shah" in body
    assert "rshah" in body
    assert "Administrator" in body


def test_the_menu_offers_user_management_only_to_an_administrator(
        monkeypatch):
    """
    Hiding is not enforcing - /users refuses an engineer either way.
    This is about not offering a button whose only outcome is a 403.
    """
    admin = signed_in_as(monkeypatch, "administrator").get(
        "/", headers={"accept": "text/html"}).text
    engineer = signed_in_as(monkeypatch, "engineer").get(
        "/", headers={"accept": "text/html"}).text
    assert 'href="/users"' in admin
    menu = engineer.split('id="account-menu"', 1)[1].split("</div>", 20)[0]
    assert 'href="/users"' not in menu


def test_signing_out_from_the_menu_is_a_post(monkeypatch):
    """
    A GET that ends a session can be fired by an <img> on another page.
    SameSite=lax already blocks that, but a form costs nothing and does
    not depend on a browser getting lax right.
    """
    body = signed_in_as(monkeypatch, "viewer").get(
        "/", headers={"accept": "text/html"}).text
    out = body.split('class="account-out"', 1)[1][:200]
    assert 'method="post"' in body.split('class="account-out"')[0][-120:] \
        or 'action="/logout"' in out


def test_the_menu_says_when_the_session_ends(monkeypatch):
    """
    Automatic logoff is fifteen minutes and a poll deliberately does not
    postpone it, so a dashboard sitting open signs itself out while
    apparently being watched. Saying when turns a surprise into
    something somebody can plan around.
    """
    from datetime import datetime, timedelta, timezone

    person = identity.Principal(
        user_id=1, username="rshah", full_name="Rishi Shah", tenant_id=1,
        site_roles={1: frozenset({"viewer"})},
        expires_at=datetime(2026, 10, 16, 17, 45, tzinfo=timezone.utc))
    monkeypatch.setattr(q, "connect", lambda dsn: Conn())
    monkeypatch.setattr(identity, "count_users", lambda conn: 1)
    monkeypatch.setattr(identity, "resolve",
                        lambda conn, t, slide=True: person)
    client = TestClient(create_app(DSN, P.load(["environment"])),
                        raise_server_exceptions=False)
    client.cookies.set(identity.COOKIE, "x")
    body = client.get("/", headers={"accept": "text/html"}).text
    assert "Signs out at" in body
    assert "17:45" in body
    assert 'data-expires="2026-10-16T17:45:00+00:00"' in body


def test_the_deadline_reported_is_the_one_this_request_leaves_behind():
    """
    `resolve` reads the row BEFORE sliding the expiry, so reporting
    row["expires_at"] would show the PREVIOUS deadline - up to fifteen
    minutes in the past by the time a page renders it. A deadline that
    has already gone is ignored once and distrusted afterwards.
    """
    source = Path(identity.__file__).read_text(encoding="utf-8")
    body = source.split("def resolve(")[1].split("def end(")[0]
    assert "expires_at = _expiry()" in body
    assert "expires_at=expires_at" in body
    assert 'expires_at=row["expires_at"]' not in body


def test_the_menu_is_closed_until_it_is_opened(monkeypatch):
    body = signed_in_as(monkeypatch, "viewer").get(
        "/", headers={"accept": "text/html"}).text
    menu = body.split('id="account-menu"', 1)[1][:120]
    assert "hidden" in menu
    assert 'aria-expanded="false"' in body


def test_the_menu_can_be_closed_from_the_keyboard():
    """
    It is not a modal - no focus trap, no dimmed page - so Escape and
    tabbing out are the two ways somebody who is not using a mouse
    leaves it.
    """
    base = (HERE / "templates" / "_base.html").read_text(encoding="utf-8")
    script = base.split("The account menu.", 1)[1]
    assert 'ev.key === "Escape"' in script
    assert "focusout" in script
    assert 'aria-haspopup="menu"' in base


def test_the_countdown_timer_only_runs_while_the_menu_is_open():
    """
    A timer ticking on every page for a line nobody is looking at keeps
    a laptop awake for nothing.
    """
    base = (HERE / "templates" / "_base.html").read_text(encoding="utf-8")
    assert "if (!menu.hidden) { tick(); }" in base


def test_the_sign_out_time_is_not_rendered_in_utc_beside_a_local_clock():
    """
    FOUND BY LOOKING AT IT. The menu said "Signs out at 12:42" while the
    header clock two divs away said "5:57 PM" - the same instant, five
    and a half hours apart, on the one line whose entire job is to be
    believed.

    The clock uses toLocaleTimeString, so the deadline has to as well.
    The server's fallback is labelled UTC rather than left bare: a
    browser with no script then shows a time that is awkward instead of
    a time that is wrong.
    """
    base = (HERE / "templates" / "_base.html").read_text(encoding="utf-8")
    assert "toLocaleTimeString" in base.split("The account menu.", 1)[1]
    assert "}} UTC</strong>" in base
