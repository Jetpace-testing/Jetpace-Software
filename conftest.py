"""
Test fixtures for the whole workspace.

ONE FIXTURE, and it exists because of one change: the application now
refuses every request that is not signed in. Before that, a test about
the distribution donut built a TestClient and asked for a page. After
it, the same test gets a 303 to the login form, and the donut is never
rendered.

So every test is signed in as an administrator unless it says otherwise.
That is the right default for a suite whose subject is panels, queries
and templates - those tests are about what a page says, and making each
of them perform a sign-in would be a hundred copies of the same six
lines, all of them testing the login rather than the thing they are
named after.

IT IS NOT A HOLE IN THE COVERAGE. `test_web_auth.py` patches the same
two functions itself, inside the test body or in its own fixture, both
of which run after this one and therefore win. The gate, the permission
checks, the anonymous cases and the first-run case are all exercised
there against the real behaviour. This fixture only decides what the
OTHER four hundred tests assume about who is asking.

If a test here starts passing because of this and should not, the tell
is that it never mentions a role.
"""

from __future__ import annotations

import pytest


# The modules whose SUBJECT is authentication. This fixture must not
# touch them: patching `identity.resolve` for a test of
# `identity.resolve` replaces the thing being tested with a stub that
# always succeeds, and every assertion about refusing a session passes
# for the wrong reason. Named explicitly rather than guessed from a
# marker, because the failure is silent and a forgotten marker looks
# exactly like a passing test.
AUTHENTICATION_IS_THE_SUBJECT = {"test_identity", "test_web_auth",
                                 "test_security", "test_permissions"}


@pytest.fixture(autouse=True)
def signed_in_by_default(monkeypatch, request):
    """
    Make every request look like an administrator at site 1.

    Patches the two functions the gate calls - `count_users`, so the
    install does not look brand new, and `resolve`, so the cookie that
    is not there resolves to somebody anyway.
    """
    module = request.module.__name__.rsplit(".", 1)[-1]
    if module in AUTHENTICATION_IS_THE_SUBJECT:
        return

    try:
        from jetpace_core.web import identity
    except ImportError:          # a test run that does not import the web
        return

    person = identity.Principal(
        user_id=1,
        username="testadmin",
        full_name="Test Administrator",
        tenant_id=1,
        site_roles={1: frozenset({"administrator"})},
        must_change_password=False,
    )
    monkeypatch.setattr(identity, "count_users", lambda conn: 1, raising=True)
    monkeypatch.setattr(identity, "resolve",
                        lambda conn, token, slide=True: person, raising=True)

    # AND THE CONNECTION THE GATE OPENS.
    #
    # The gate runs on every request and opens its own connection to
    # resolve the session. Several test files point at a deliberately
    # unreachable DSN - "nothing in this test touches a database" - and
    # `queries.connect` has connect_timeout=10, so after the gate landed
    # those files took ten seconds per request and the suite stopped
    # finishing rather than failing.
    #
    # A test that wants a real stub patches `connect` itself, after this
    # fixture, and wins.
    from jetpace_core.web import queries

    class _Cursor:
        def execute(self, sql, params=None): pass
        def fetchone(self): return None
        def fetchall(self): return []
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    class _Conn:
        def cursor(self): return _Cursor()
        def commit(self): pass
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    monkeypatch.setattr(queries, "connect", lambda dsn: _Conn(),
                        raising=True)
    return person
