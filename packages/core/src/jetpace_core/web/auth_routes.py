"""
The authentication seam: the gate, the login page, and the way out.

Kept out of app.py because it is the one part of this application that
has to be read as a whole by somebody who is not changing it - a
reviewer, or an auditor asking how access is controlled. app.py is nine
hundred lines of panels.

THE GATE IS A MIDDLEWARE AND NOT A DEPENDENCY, and that is the decision
this file turns on. FastAPI dependencies are opt-in: a route without one
is open, and the failure mode of forgetting is an unprotected endpoint
that looks exactly like a protected one. A middleware is opt-OUT: every
request is checked, and the handful that must be public are named in one
list that a test reads back. Getting it wrong means something breaks
loudly rather than opening quietly.

WHAT COUNTS AS BEING PRESENT. The overview refreshes four panels every
five seconds. If that counted as activity, the automatic logoff required
by 21 CFR 11.10(d) and Annex 11 §12 would never fire on a machine with a
browser open - which is every machine this runs on. So a GET of
`/partials/...` resolves the session WITHOUT extending it, and everything
else extends it.

That alone was not enough, and the fault it left is worth recording
because the reasoning looked sound. Requests extend the session, but a
person working in this dashboard makes almost none: the panels refresh
themselves, the live table's filters run in the browser, and reading a
screen is not a request at all. On this install `session.last_seen_at`
never moved more than a minute and a half past sign-in across fifteen
sessions - so the fifteen minutes ran from SIGNING IN rather than from
the last interaction, and an operator at the keyboard was signed out
mid-task. `POST /activity` fixes that by reporting real input explicitly;
see the route for why that keeps the control intact.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from fastapi import Form, Request
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               Response)

from .. import permissions as perms
from .. import security
from . import identity, queries
from .writes import WriteRejected

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# what is reachable without signing in
#
# EXACT paths and one prefix, listed rather than pattern-matched, because
# a pattern is how `/static` quietly comes to mean `/static/../../`.
# A test asserts every route in the application is either here or guarded.
# ---------------------------------------------------------------------------

PUBLIC_PATHS = frozenset({
    "/login",        # obviously
    "/first-run",    # creates the first administrator; refuses once one exists
    "/logout",       # ending a session needs no session
    # The supervisor polls this every two seconds while a release is on
    # trial, from a process with no browser and no account. Putting it
    # behind the login would mean an update could never be promoted -
    # and it deliberately reports only liveness and a reading age, which
    # is why §10's `support` role can have diagnostics without being able
    # to read instrument data.
    "/healthz",
})

PUBLIC_PREFIXES = ("/static/",)

# Reached while signed in but not yet allowed to do anything else.
PASSWORD_PATHS = frozenset({"/password"})

# Behind the gate, and asking for nothing beyond being signed in.
#
# Listed here rather than waved through inside the test that walks the
# routes, so the exemption lives in the application where a reviewer
# meets it. `/activity` carries no instrument data and names no account:
# it says "the person holding this session touched the keyboard", which
# every role may say about itself and no role may say about anybody else.
SIGNED_IN_IS_ENOUGH = frozenset({"/activity"})


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def counts_as_activity(request: Request) -> bool:
    """
    Whether this request should push the idle timeout forward.

    A GET of a partial is the page refreshing itself on a timer - four of
    them do, between every 5 and every 30 seconds. Everything else is a
    person: a page, a form, a chart they asked for by clicking a range,
    or `POST /activity`, which is the browser saying somebody touched the
    keyboard.

    POSTs to `/partials/...` DO count, because those are the
    commissioning form, which is somebody typing.
    """
    return not (request.method == "GET"
                and request.url.path.startswith("/partials/"))


def wants_html(request: Request) -> bool:
    """
    Whether to redirect to the login page or answer with a status code.

    An HTMX request that is redirected renders the login page INSIDE the
    panel it was refreshing, which is both useless and alarming. Those
    get a 401 and a header telling HTMX to navigate the whole window
    instead.
    """
    if request.headers.get("HX-Request") == "true":
        return False
    if request.url.path.startswith(("/api/", "/export/", "/partials/")):
        return False
    return "text/html" in request.headers.get("accept", "")


def safe_next(target: str | None) -> str:
    """
    A redirect target, or "/".

    ONLY a path on this site. A login page that redirects to whatever a
    query string says is the standard way one becomes part of somebody
    else's phishing page: the link looks like yours, the credentials are
    typed into yours, and the browser lands on theirs afterwards.
    Anything with a scheme, a host, or a leading `//` is discarded.
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    # A BACKSLASH TOO. Browsers normalise "/\evil.example" to
    # "//evil.example" and follow it off-site, so checking only for "//"
    # leaves the open redirect wide open. Found by a test that tried it.
    if "\\" in target:
        return "/"
    split = urlsplit(target)
    if split.scheme or split.netloc:
        return "/"
    return target


def set_cookie(response: Response, token: str, request: Request) -> None:
    """
    The session cookie.

    httponly  - no script can read it, which is what keeps a cross-site
                scripting bug from becoming a stolen session. This is the
                reason for a cookie rather than the Bearer token in
                sessionStorage that RTD uses; the other reason is that a
                plain <a href> cannot carry an Authorization header, and
                this application is server-rendered pages.
    samesite  - lax. A cross-site POST does not carry the cookie, which
                is the cross-site request forgery defence; following a
                link from elsewhere still does, so a bookmark works.
    secure    - only over https, because a Secure cookie on http is not
                stored at all and the sign-in would appear to succeed and
                then not. These dashboards are http on 127.0.0.1 today.
    """
    response.set_cookie(
        identity.COOKIE, token,
        httponly=True, samesite="lax",
        secure=request.url.scheme == "https",
        max_age=identity.ABSOLUTE_HOURS * 3600,
        path="/",
    )


def install(app, dsn: str, templates) -> None:
    """Put the gate and the four routes on the application."""

    throttle = security.Throttle()

    # --- the gate ---------------------------------------------------------

    @app.middleware("http")
    async def gate(request: Request, call_next):
        path = request.url.path
        if is_public(path):
            return await call_next(request)

        token = request.cookies.get(identity.COOKIE)
        principal = None
        try:
            with queries.connect(dsn) as conn:
                # FIRST RUN. No account exists, so there is nobody to be.
                # Everything redirects to the page that creates the first
                # administrator - including, deliberately, the API and the
                # exports, because an install with no users must not be
                # readable either.
                if identity.count_users(conn) == 0:
                    if wants_html(request):
                        return RedirectResponse("/login", status_code=303)
                    return Response(status_code=401,
                                    headers={"HX-Redirect": "/login"})
                principal = identity.resolve(
                    conn, token, slide=counts_as_activity(request))
        except Exception as exc:                        # noqa: BLE001
            # The database is unreachable. Refuse rather than fall open:
            # an authentication check that cannot run has not passed.
            log.error("the gate could not reach the database: %s", exc)
            return Response("The database is unreachable.", status_code=503)

        if principal is None:
            if wants_html(request):
                return RedirectResponse(
                    f"/login?next={request.url.path}", status_code=303)
            return Response(status_code=401,
                            headers={"HX-Redirect": "/login"})

        # An administrator-issued password has to be replaced before the
        # account does anything, or nothing it does is attributable.
        if principal.must_change_password and path not in PASSWORD_PATHS:
            if wants_html(request):
                return RedirectResponse("/password", status_code=303)
            return Response(status_code=401,
                            headers={"HX-Redirect": "/password"})

        request.state.principal = principal
        return await call_next(request)

    # --- signing in -------------------------------------------------------

    def login_page(request: Request, **over):
        with queries.connect(dsn) as conn:
            first_run = identity.count_users(conn) == 0
            site = None
            if not first_run:
                with conn.cursor() as cur:
                    cur.execute("SELECT name FROM site ORDER BY id LIMIT 1")
                    site = cur.fetchone()
        context = {
            "first_run": first_run,
            "site_label": site["name"] if site else None,
            "min_length": security.MIN_LENGTH,
            "idle_minutes": identity.IDLE_MINUTES,
            "next_url": safe_next(request.query_params.get("next")),
        }
        context.update(over)
        return templates.TemplateResponse(
            request, "login.html", context,
            status_code=over.pop("status_code", 200))

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return login_page(request)

    @app.post("/login", response_class=HTMLResponse)
    def login_submit(request: Request,
                     username: str = Form(""),
                     password: str = Form(""),
                     next: str = Form("")):
        address = request.client.host if request.client else "unknown"
        try:
            with queries.connect(dsn) as conn:
                principal, token = identity.sign_in(
                    conn, username=username, password=password,
                    ip=address,
                    user_agent=request.headers.get("user-agent", ""),
                    throttle=throttle)
        except identity.SignInRejected as refused:
            log.info("sign-in refused for %r from %s: %s",
                     username, address, refused.reason)
            return login_page(request, error=refused.message,
                              username=username, next_url=safe_next(next),
                              status_code=401)

        target = "/password" if principal.must_change_password \
            else safe_next(next)
        response = RedirectResponse(target, status_code=303)
        set_cookie(response, token, request)
        return response

    # --- the first administrator -----------------------------------------

    @app.post("/first-run", response_class=HTMLResponse)
    def first_run(request: Request,
                  username: str = Form(""), full_name: str = Form(""),
                  password: str = Form(""), confirm: str = Form("")):
        """
        Create the one account that can create the others.

        REFUSES ONCE ANY ACCOUNT EXISTS, which is the whole security of
        this endpoint: it is public, so without that check it would be a
        way for anybody who can reach the port to mint an administrator.
        """
        try:
            with queries.connect(dsn) as conn:
                if identity.count_users(conn) != 0:
                    return login_page(
                        request,
                        error="This installation already has accounts.",
                        status_code=409)
                if password != confirm:
                    raise WriteRejected(
                        "The two passwords are not the same.", "password")
                identity.create_user(
                    conn, username=username, full_name=full_name,
                    password=password, roles=("administrator",),
                    actor=None)
                principal, token = identity.sign_in(
                    conn, username=username, password=password,
                    ip=request.client.host if request.client else "",
                    user_agent=request.headers.get("user-agent", ""))
        except (WriteRejected, security.PasswordRejected) as refused:
            return login_page(request, error=str(refused),
                              username=username, status_code=400)
        except identity.SignInRejected:
            return login_page(
                request,
                notice="The account was created. Sign in with it.",
                username=username)

        log.warning("the first administrator was created: %s", username)
        response = RedirectResponse("/", status_code=303)
        set_cookie(response, token, request)
        return response

    # --- signing out ------------------------------------------------------

    # BOTH VERBS, on purpose.
    #
    # The account menu signs out with a POST, which is the right shape
    # for something that changes state and is immune to being fired by
    # an <img> on another page. The GET stays because "Sign out instead"
    # on the forced password-change screen is a link, and because
    # somebody typing /logout into the address bar should be signed out
    # rather than shown a 405.
    #
    # The cross-site risk the GET would otherwise carry is already
    # closed by SameSite=lax: a subresource request to this path does not
    # carry the cookie, so an <img> cannot end anybody's session. What
    # lax does allow is a top-level navigation, which means a link
    # somebody deliberately clicked - and signing out when you click
    # "sign out" is the behaviour, not the attack.
    @app.get("/logout")
    @app.post("/logout")
    def logout(request: Request):
        token = request.cookies.get(identity.COOKIE)
        if token:
            try:
                with queries.connect(dsn) as conn:
                    identity.end(conn, token, "signed out")
            except Exception as exc:                    # noqa: BLE001
                # Clear the cookie anyway. A sign-out that fails because
                # the database is down must still take the browser's
                # credential away.
                log.warning("sign-out could not reach the database: %s", exc)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(identity.COOKIE, path="/")
        return response

    # --- somebody is still here -------------------------------------------
    #
    # THE ONE REQUEST WHOSE ONLY JOB IS TO SAY A PERSON IS PRESENT.
    #
    # The idle timer is pushed forward by requests, and this dashboard is
    # built so that a person using it hardly makes any - the panels poll
    # themselves and are deliberately not activity, the live table's
    # filters run in the browser, and reading a screen is not a request.
    # The result was a fifteen-minute limit measured from SIGNING IN.
    #
    # So the browser reports presence instead of it being inferred: real
    # input - a key, a click, a wheel, the pointer moving - sets a flag,
    # and at most once a minute this fires. NOBODY AT THE KEYBOARD MEANS
    # NO INPUT, means no request, means the logoff still happens on an
    # unattended machine, which is the entire point of the control. An
    # idle tab is silent; it is presence that costs a request.
    #
    # The header is a browser's word, and that is fine: the worst a
    # person can do by forging it is hold their OWN session open, which
    # they could already do by reloading the page every ten minutes. It
    # is not a permission check and it never names another account.
    #
    # A POST, so `counts_as_activity` already returned True and the gate
    # slid the session before this body runs. It answers with the new
    # deadline because the account menu prints that deadline, and a
    # countdown that disagrees with the session is worse than none.
    @app.post("/activity")
    def activity(request: Request):
        deadline = request.state.principal.expires_at
        return JSONResponse(
            {"expires_at": deadline.isoformat() if deadline else None})

    # --- changing your own password ---------------------------------------

    @app.get("/password", response_class=HTMLResponse)
    def password_form(request: Request):
        # No **kwargs. FastAPI reads an unannotated keyword argument on a
        # route handler as a query parameter and answers 422 to every
        # request - including, here, the forced password change that a
        # new account cannot get past.
        principal = request.state.principal
        return templates.TemplateResponse(request, "password.html", {
            "principal": principal,
            "forced": principal.must_change_password,
            "min_length": security.MIN_LENGTH,
        })

    @app.post("/password", response_class=HTMLResponse)
    def password_submit(request: Request,
                        current: str = Form(""), password: str = Form(""),
                        confirm: str = Form("")):
        principal = request.state.principal
        try:
            with queries.connect(dsn) as conn:
                # The current password, even though the session is valid.
                # A signed-in browser left unattended is the ordinary way
                # an account is taken over, and changing the password is
                # the move that makes it permanent.
                user = identity.user_by_name(conn, principal.username)
                if not security.verify_password(current,
                                                user["password_hash"]):
                    raise WriteRejected(
                        "That is not your current password.", "current")
                if password != confirm:
                    raise WriteRejected(
                        "The two new passwords are not the same.", "password")
                if password == current:
                    raise WriteRejected(
                        "That is the password you already have.", "password")
                identity.set_password(
                    conn, user_id=principal.user_id,
                    username=principal.username,
                    full_name=principal.full_name,
                    password=password, actor=principal.username,
                    must_change=False)
                # set_password ends every session, including this one, so
                # sign the person straight back in rather than bouncing
                # them to a login page they just proved themselves at.
                _fresh, token = identity.sign_in(
                    conn, username=principal.username, password=password,
                    ip=request.client.host if request.client else "",
                    user_agent=request.headers.get("user-agent", ""))
        except (WriteRejected, security.PasswordRejected) as refused:
            return templates.TemplateResponse(
                request, "password.html", {
                    "principal": principal,
                    "forced": principal.must_change_password,
                    "min_length": security.MIN_LENGTH,
                    "error": str(refused),
                }, status_code=400)

        response = RedirectResponse("/", status_code=303)
        set_cookie(response, token, request)
        return response


def install_users(app, dsn: str, templates) -> None:
    """
    The users screen. Administrator only, on every route.

    Separate from `install` because that one is the gate - the thing a
    reviewer reads to find out how access is controlled - and this is a
    CRUD page. Mixing them makes the gate harder to read for no gain.
    """
    from . import identity as ident

    def page(request, **over):
        with queries.connect(dsn) as conn:
            people = ident.list_users(conn)
        return templates.TemplateResponse(request, "users.html", {
            "users": people,
            "me": principal_of(request),
            "min_length": security.MIN_LENGTH,
            **over,
        }, status_code=over.pop("status_code", 200))

    @app.get("/users", response_class=HTMLResponse)
    def users_page(request: Request):
        require(request, perms.USER_MANAGE)
        return page(request)

    @app.post("/users", response_class=HTMLResponse)
    def users_add(request: Request,
                  username: str = Form(""), full_name: str = Form(""),
                  password: str = Form(""), role: list[str] = Form(default=[])):
        me = require(request, perms.USER_MANAGE)
        try:
            with queries.connect(dsn) as conn:
                ident.create_user(
                    conn, username=username, full_name=full_name,
                    password=password, roles=role,
                    # ALWAYS. The administrator chose this password, so
                    # they know it; until the holder replaces it, nothing
                    # the account does is attributable to the person
                    # whose name is on it.
                    must_change=True,
                    actor=me.username)
        except (WriteRejected, security.PasswordRejected) as refused:
            return page(request, message=str(refused), failed=True,
                        status_code=400)
        return page(request,
                    message=f"{username} can now sign in, and must choose "
                            f"their own password when they do.")

    @app.post("/users/{username}/state", response_class=HTMLResponse)
    def users_state(request: Request, username: str,
                    disabled: str = Form("true")):
        me = require(request, perms.USER_MANAGE)
        off = disabled == "true"
        if username == me.username:
            # An administrator who disables themselves on a
            # single-administrator install has disabled the
            # installation, and the only way back is a command line on
            # the machine.
            return page(request, failed=True, status_code=400,
                        message="You cannot switch off your own account.")
        try:
            with queries.connect(dsn) as conn:
                user = ident.user_by_name(conn, username)
                if user is None:
                    raise WriteRejected(f"No account called {username!r}.")
                if off and _last_administrator(conn, user["id"]):
                    raise WriteRejected(
                        f"{username} is the only administrator left. "
                        f"Give somebody else that role first, or this "
                        f"installation has nobody who can manage it.")
                ident.set_disabled(conn, user_id=user["id"],
                                   username=user["username"],
                                   disabled=off, actor=me.username)
        except WriteRejected as refused:
            return page(request, message=str(refused), failed=True,
                        status_code=400)
        return page(request,
                    message=f"{username} is now "
                            f"{'disabled' if off else 'enabled'}.")

    def _last_administrator(conn, user_id: int) -> bool:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM user_role r "
                "  JOIN app_user u ON u.id = r.user_id "
                " WHERE r.role = 'administrator' AND u.disabled_at IS NULL "
                "   AND u.id <> %(id)s", {"id": user_id})
            return cur.fetchone()["n"] == 0


def principal_of(request: Request):
    """
    The signed-in person, for a route that needs them.

    The gate puts it on request.state before any handler runs, so this is
    a read and never a lookup. A route reached without it is one the gate
    let through, which is a bug in PUBLIC_PATHS rather than something to
    handle here.
    """
    return getattr(request.state, "principal", None)


def require(request: Request, permission: str, site_id: int | None = None):
    """
    Check a permission, or raise.

    Raises `Forbidden`, which app.py turns into a 403 page or a 403
    response depending on who asked. The gate has already established
    WHO; this establishes WHETHER.
    """
    principal = principal_of(request)
    if principal is None:
        raise Forbidden("Not signed in.", permission)
    if not principal.can(permission, site_id):
        raise Forbidden(
            f"Your role does not allow this. "
            f"{perms.PERMISSIONS[permission].what} is for "
            f"{_english(perms.roles_with(permission))}.",
            permission)
    return principal


class Forbidden(Exception):
    def __init__(self, message: str, permission: str = ""):
        super().__init__(message)
        self.message = message
        self.permission = permission


def _english(roles) -> str:
    labels = [perms.ROLE_LABELS[r] for r in roles]
    if not labels:
        return "nobody"
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " and " + labels[-1]
