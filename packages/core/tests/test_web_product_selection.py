"""
Which products a dashboard serves, and what that does and does not
restrict.

One process serves one set of products, so a site can run several: an
administrative instance carrying everything on 127.0.0.1, and one per
product on its own port for the people who use it.

The thing these tests are careful to state is what the arrangement is
NOT. A separate port is a separate URL, not a separate permission.
Nothing here authenticates anybody - §10 is unbuilt - so the only
enforced boundary is the interface a process binds to, which is the
operating system's doing and not this code's. A test that implied
otherwise would be worse than no test.

ROUTES ARE READ FROM /openapi.json, not by requesting them. Requesting a
page runs its handler, which opens a database connection and waits out
psycopg's 10-second connect timeout; six of those turned this suite from
17 seconds into two minutes. The schema is built from the router, needs
no database, and - unlike app.routes - sees through the _IncludedRouter
wrapper that FastAPI puts around an included product router.
"""

import pytest
from fastapi.testclient import TestClient

from jetpace_core.cli import build_parser
from jetpace_core.web import products as P
from jetpace_core.web.app import create_app

DSN = "postgresql://unused:unused@127.0.0.1:1/unused"

INSTALLED = [s for s in P.KNOWN if P.importable(s)]

# The products that have a page of their own. A product may legitimately
# have none - jetpace_environment has carried no pages since its
# instrument was removed - and "serves its own routes" is not a question
# that can be asked of one that declares none.
WITH_PAGES = [s for s in INSTALLED if P.load([s])[0].routes is not None]


def paths_of(slugs) -> set[str]:
    """Every route a dashboard serving `slugs` would answer."""
    client = TestClient(create_app(DSN, P.load(slugs)))
    return set(client.get("/openapi.json").json()["paths"])


# --- one dashboard per product --------------------------------------------

@pytest.mark.parametrize("mine", WITH_PAGES)
def test_a_product_dashboard_serves_its_own_product(mine):
    assert f"/{mine}" in paths_of([mine])


@pytest.mark.parametrize("slug", INSTALLED)
def test_a_product_with_no_pages_contributes_no_routes(slug):
    """
    The other half of the same rule. An empty product is loaded - it
    still carries its instruments to the commissioning form - and it adds
    nothing to the URL space, so there is no path to find by guessing and
    no navigation entry pointing at one.
    """
    if slug in WITH_PAGES:
        pytest.skip(f"{slug} has pages")
    assert f"/{slug}" not in paths_of([slug])


@pytest.mark.parametrize("mine,theirs", [("environment", "energy"),
                                         ("energy", "environment")])
def test_a_product_dashboard_does_not_serve_the_other_product(mine, theirs):
    """
    Absent, not hidden. The other product's route does not exist, so
    there is nothing to find by guessing a URL - which is the isolation a
    port cannot give, and the reason a single-product install is stronger
    than a single-product port.
    """
    assert f"/{theirs}" not in paths_of([mine])


@pytest.mark.parametrize("slug", ["environment", "energy"])
def test_guessing_the_other_products_url_gets_a_404(slug):
    """
    The same claim from the outside. 404 costs nothing here because
    routing fails before any handler opens a connection.
    """
    other = "energy" if slug == "environment" else "environment"
    client = TestClient(create_app(DSN, P.load([slug])),
                        raise_server_exceptions=False)
    assert client.get(f"/{other}").status_code == 404


def test_the_administrative_dashboard_carries_every_product():
    """
    The admin instance is not a cut-down one. Whoever looks after the
    system should see all of it; what keeps a customer out of it is the
    interface it binds to, not a shorter menu.
    """
    served = paths_of(INSTALLED)
    for slug in WITH_PAGES:
        assert f"/{slug}" in served


@pytest.mark.parametrize("slugs", [[], ["environment"], ["energy"]])
def test_every_dashboard_keeps_the_shell_whichever_products_it_has(slugs):
    """
    The device list is not an administrative screen. A customer who
    bought one product still needs to see their devices, so the shell
    travels with every instance.
    """
    served = paths_of(slugs)
    for route in ("/", "/devices", "/devices/{code}", "/api/trend",
                  "/partials/live", "/healthz"):
        assert route in served, f"{route} missing from a {slugs} dashboard"


@pytest.mark.parametrize("slugs", [[], ["energy"], INSTALLED])
def test_every_instance_answers_for_its_own_health(slugs):
    """
    Each process has its own /healthz. The supervisor's gate watches one
    of them, so a single product instance failing is caught by the
    supervisor restarting a dead child rather than by the gate - stated
    here because it is the gap in this arrangement, not a property of it.
    """
    assert "/healthz" in paths_of(slugs)


def test_a_products_static_files_are_only_mounted_where_it_is_served(
        tmp_path):
    """
    A dashboard that does not serve a product does not serve its assets
    either, so there is nothing to find under /static/<other> - and a
    single-product install downloads one stylesheet, not two.

    Fabricated rather than taken from a real product: neither product in
    the repository ships a stylesheet today, and a test that quietly
    stopped exercising the mount when the last one was deleted would be
    a test that passes by not running.
    """
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "demo.html").write_text("{% extends '_base.html' %}",
                                         encoding="utf-8")
    static = tmp_path / "static"
    static.mkdir()
    (static / "demo.css").write_text("#demo{color:var(--ink)}",
                                     encoding="utf-8")

    def routes(shell):
        from fastapi import APIRouter
        return APIRouter()

    demo = P.Product(slug="demo", label="Demo", templates=templates,
                     static=static, stylesheet="demo.css", routes=routes)

    with_it = TestClient(create_app(DSN, [demo]),
                         raise_server_exceptions=False)
    without = TestClient(create_app(DSN, []),
                         raise_server_exceptions=False)
    asset = "/static/demo/demo.css"
    assert with_it.get(asset).status_code == 200
    assert without.get(asset).status_code == 404


# --- the command line -----------------------------------------------------

def test_products_and_shell_only_cannot_both_be_given():
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["web", "--products", "energy", "--shell-only"])


def test_products_takes_a_comma_separated_list():
    args = build_parser().parse_args(
        ["web", "--products", "energy,environment"])
    assert args.products == "energy,environment"
    assert args.shell_only is False


def test_the_default_host_keeps_a_dashboard_off_the_network():
    """
    The one boundary in this design that is actually enforced, and so the
    one worth pinning. A default of 0.0.0.0 would put an unauthenticated
    dashboard on the plant LAN the first time somebody forgot the flag.
    """
    assert build_parser().parse_args(["web"]).host == "127.0.0.1"


def test_asking_for_an_unknown_product_fails_loudly():
    """
    Not a smaller dashboard. A site told to serve a product it does not
    have is a broken install, and the operator needs to hear that at
    startup rather than wonder where the tab went.
    """
    with pytest.raises(SystemExit):
        P.load(["nosuchproduct"])
