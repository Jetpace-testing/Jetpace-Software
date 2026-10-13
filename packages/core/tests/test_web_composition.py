"""
The composition seam, exercised with a product that does not exist.

Using jetpace_environment here would make core's own test suite depend on
a product, which is the coupling the split removes - so these tests
fabricate a product in a tmp_path instead. The mechanism is proved here;
that the real products describe themselves correctly is proved by their
own tests.

Needs nothing running. TestClient speaks to the application in-process
over ASGI - no socket, no server - and the fabricated product's page
touches no database. The two routes that would (`/` and `/devices`) are
never requested here.
"""

from pathlib import Path

import pytest
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from jetpace_core.web import products as P
from jetpace_core.web.app import HERE, create_app

# Never connected to. Every request in this file is either a static file
# or the fabricated product's page.
DSN = "postgresql://unused:unused@127.0.0.1:1/unused"

CORE_TEMPLATES = HERE / "templates"


def fake_product(tmp_path: Path, slug: str = "demo",
                 stylesheet: str | None = "demo.css") -> P.Product:
    """A product with one page, one stylesheet and nothing else."""
    templates = tmp_path / "templates"
    templates.mkdir(exist_ok=True)
    (templates / f"{slug}.html").write_text(
        '{% extends "_base.html" %}'
        "{% block nav %}" + slug + "{% endblock %}"
        "{% block content %}<p id=demo>fabricated</p>{% endblock %}",
        encoding="utf-8")

    static = tmp_path / "static"
    static.mkdir(exist_ok=True)
    (static / "demo.css").write_text("#demo{color:red}", encoding="utf-8")

    def routes(shell: P.Shell) -> APIRouter:
        router = APIRouter()

        @router.get(f"/{slug}", response_class=HTMLResponse)
        def page(request: Request):
            return shell.templates.TemplateResponse(
                request, f"{slug}.html", {})

        return router

    return P.Product(slug=slug, label=slug.title(), templates=templates,
                     static=static, stylesheet=stylesheet, routes=routes)


# --- the shell on its own -------------------------------------------------

def test_the_shell_stands_up_with_no_products_at_all():
    """
    The shipping shape of an install that has been sold neither product
    yet, and the shape every other core test runs in.
    """
    client = TestClient(create_app(DSN), raise_server_exceptions=False)
    assert client.get("/environment").status_code == 404
    assert client.get("/energy").status_code == 404
    # the shell's own asset still serves, which is what proves the static
    # mount is not conditional on there being products
    assert client.get("/static/app.css").status_code == 200


# --- a product joining in --------------------------------------------------

def test_a_product_contributes_its_route(tmp_path):
    client = TestClient(create_app(DSN, [fake_product(tmp_path)]))
    assert client.get("/demo").status_code == 200
    assert "fabricated" in client.get("/demo").text


def test_a_product_template_resolves_the_shell_it_extends(tmp_path):
    """
    The whole reason the Jinja loader is given a list. The fabricated
    template says `{% extends "_base.html" %}`, and _base.html is in
    core's directory, not the product's.
    """
    body = TestClient(create_app(DSN, [fake_product(tmp_path)])).get("/demo").text
    assert "<!doctype html>" in body
    assert "Jetpace" in body
    # a helper defined only in core's _base.html
    assert "function seriesColours()" in body


def test_the_product_tab_is_current_on_its_own_page(tmp_path):
    body = TestClient(create_app(DSN, [fake_product(tmp_path)])).get("/demo").text
    assert 'href="/demo"' in body
    assert 'class="current"' in body


def test_the_navigation_lists_only_the_products_that_were_loaded(tmp_path):
    body = TestClient(create_app(DSN, [fake_product(tmp_path)])).get("/demo").text
    assert 'href="/demo"' in body
    # the two products that exist in this repo are not loaded here, and a
    # tab for a route that would 404 is the bug this replaced
    assert 'href="/environment"' not in body
    assert 'href="/energy"' not in body


def test_only_a_loaded_products_stylesheet_is_linked(tmp_path):
    body = TestClient(create_app(DSN, [fake_product(tmp_path)])).get("/demo").text
    assert "/static/demo/demo.css" in body
    assert "environment.css" not in body


def test_a_product_without_a_stylesheet_links_none(tmp_path):
    product = fake_product(tmp_path, stylesheet=None)
    body = TestClient(create_app(DSN, [product])).get("/demo").text
    assert "demo.css" not in body
    assert "/static/app.css" in body       # core's is still there


# --- the mount-ordering trap ----------------------------------------------

def test_a_products_static_file_is_reachable(tmp_path):
    """
    Mount ORDER is load-bearing and invisible in the code.

    Starlette matches mounts in registration order and Mount("/static")
    matches everything beneath /static/, so registering core's mount
    first would swallow every product asset and answer 404 from the wrong
    directory. Nothing about create_app makes that visible, and the
    symptom would be one unstyled page.
    """
    client = TestClient(create_app(DSN, [fake_product(tmp_path)]))
    response = client.get("/static/demo/demo.css")
    assert response.status_code == 200
    assert "#demo" in response.text


def test_core_static_still_serves_alongside_a_product_mount(tmp_path):
    client = TestClient(create_app(DSN, [fake_product(tmp_path)]))
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/vendor/echarts.min.js").status_code == 200


# --- what a product may not do --------------------------------------------

def test_a_product_cannot_shadow_a_shell_template(tmp_path):
    """
    FileSystemLoader returns the FIRST match across its search path and
    says nothing about the others. Core's directory is first, so a
    product shipping its own _base.html is ignored rather than obeyed -
    which is the safe direction, but only because of the ordering.
    """
    product = fake_product(tmp_path)
    (product.templates / "_base.html").write_text(
        "HIJACKED", encoding="utf-8")
    body = TestClient(create_app(DSN, [product])).get("/demo").text
    assert "HIJACKED" not in body
    assert "<!doctype html>" in body


def test_no_template_basename_is_defined_twice_across_the_search_path():
    """
    The corollary of first-match-wins: a product template that happens to
    share a name with another product's is silently never rendered. This
    is the only place that resolution is checked, because the loader will
    not complain.
    """
    roots = [CORE_TEMPLATES]
    for slug in P.KNOWN:
        if P.importable(slug):
            roots.append(P.load([slug])[0].templates)

    seen: dict[str, Path] = {}
    clashes = []
    for root in roots:
        for path in sorted(root.glob("*.html")):
            if path.name in seen:
                clashes.append(f"{path.name}: {seen[path.name]} and {path}")
            seen[path.name] = path
    assert not clashes, "templates shadow each other:\n  " + "\n  ".join(clashes)


# --- the descriptor refuses to describe something broken ------------------

def test_a_product_naming_a_missing_template_directory_is_refused(tmp_path):
    with pytest.raises(ValueError, match="template directory"):
        P.Product(slug="demo", label="Demo",
                  templates=tmp_path / "nope",
                  routes=lambda shell: APIRouter())


def test_a_product_naming_a_missing_stylesheet_is_refused(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "static").mkdir()
    with pytest.raises(ValueError, match="stylesheet"):
        P.Product(slug="demo", label="Demo",
                  templates=tmp_path / "templates",
                  static=tmp_path / "static",
                  stylesheet="absent.css",
                  routes=lambda shell: APIRouter())


@pytest.mark.parametrize("slug", ["Demo", "de-mo", "de mo", "demo2", "../x"])
def test_a_slug_that_is_not_a_plain_lowercase_word_is_refused(tmp_path, slug):
    """
    The slug becomes a URL path and a static mount point, so "../x" is
    the case that matters and the others keep the rule simple enough to
    state.
    """
    (tmp_path / "templates").mkdir(exist_ok=True)
    with pytest.raises(ValueError, match="slug"):
        P.Product(slug=slug, label="Demo",
                  templates=tmp_path / "templates",
                  routes=lambda shell: APIRouter())


# --- configuration --------------------------------------------------------

def test_no_config_file_means_whatever_is_installed(tmp_path):
    assert P.enabled(config_dir=tmp_path) is None


def test_a_config_file_without_the_key_means_the_same(tmp_path):
    (tmp_path / P.CONFIG_NAME).write_text("# nothing\n", encoding="utf-8")
    assert P.enabled(config_dir=tmp_path) is None


def test_a_configured_list_is_returned_in_order(tmp_path):
    (tmp_path / P.CONFIG_NAME).write_text(
        'enabled = ["energy", "environment"]\n', encoding="utf-8")
    assert P.enabled(config_dir=tmp_path) == ("energy", "environment")


def test_an_empty_list_is_a_real_answer(tmp_path):
    """
    Not the same as a missing file. An install may legitimately serve the
    shell and no product, and must not have both silently switched on.
    """
    (tmp_path / P.CONFIG_NAME).write_text("enabled = []\n", encoding="utf-8")
    assert P.enabled(config_dir=tmp_path) == ()


@pytest.mark.parametrize("body", [
    'enabled = "environment"',        # a string, not a list
    "enabled = [1, 2]",               # not names
    'enabled = ["env-1"]',            # not a plain word
    "enabled = [",                    # not even TOML
])
def test_a_config_file_that_says_something_wrong_is_an_error(tmp_path, body):
    """
    A missing file means "no opinion" and is fine. A file that exists and
    is malformed means somebody tried to say something, and guessing what
    they meant is how an install ends up serving the wrong product.
    """
    (tmp_path / P.CONFIG_NAME).write_text(body + "\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        P.enabled(config_dir=tmp_path)


def test_asking_for_a_product_that_does_not_exist_names_what_to_do():
    with pytest.raises(SystemExit) as exc:
        P.load(["nosuchproduct"])
    message = str(exc.value)
    assert "jetpace_nosuchproduct" in message
    assert "hidden-import" in message, (
        "the frozen case is the one somebody will actually hit")
