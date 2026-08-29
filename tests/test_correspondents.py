"""The registry and the routes under `/c/<slug>/` (0046).

Two properties carry the design and are worth more than the rest of this file:
a plugin cannot land anywhere core did not put it, and the enabled flag is
enforced by the mount rather than by each view remembering to check.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from episteme.correspondents import registry, rows
from episteme.web import correspondents as web_correspondents
from episteme.web.templating import add_template_dir, correspondent_page


@pytest.fixture(autouse=True)
def clean_registry():
    """The registry is a module global; a test that registers must not leak."""
    saved = dict(registry._PLUGINS)
    registry._PLUGINS.clear()
    yield
    registry._PLUGINS.clear()
    registry._PLUGINS.update(saved)


def make_plugin(slug="fake", label="Fake", stylesheet=None, templates=None, glance=True):
    router = APIRouter()

    @router.get("")
    async def index():
        return {"where": "index"}

    if glance:

        @router.get(registry.GLANCE_PATH)
        async def block():
            return {"where": "glance"}

    # Deliberately the shape of a core route. It must not become one.
    @router.get("/post/{post_id}")
    async def post(post_id: int):
        return {"where": "plugin", "post_id": post_id}

    return SimpleNamespace(
        slug=slug,
        label=label,
        router=router,
        stylesheet=stylesheet,
        templates=templates,
    )


# --- validate -----------------------------------------------------------


@pytest.mark.parametrize(
    "slug",
    ["", "Matsedel", "mat/sedel", "mat sedel", "mat.sedel", "../admin"],
)
def test_a_slug_that_is_not_a_safe_path_segment_is_refused(slug):
    with pytest.raises(registry.RegistryError, match="URL path segment"):
        registry.validate(make_plugin(slug=slug))


def test_a_plugin_needs_a_label_and_a_router():
    with pytest.raises(registry.RegistryError, match="no label"):
        registry.validate(make_plugin(label=""))
    plugin = make_plugin()
    plugin.router = "not a router"
    with pytest.raises(registry.RegistryError, match="APIRouter"):
        registry.validate(plugin)


def test_declared_files_have_to_exist(tmp_path):
    missing = tmp_path / "nope.css"
    with pytest.raises(registry.RegistryError, match="does not exist"):
        registry.validate(make_plugin(stylesheet=missing))
    with pytest.raises(registry.RegistryError, match="template directory"):
        registry.validate(make_plugin(templates=tmp_path / "nodir"))

    sheet = tmp_path / "there.css"
    sheet.write_text(".x {}", encoding="utf-8")
    registry.validate(make_plugin(stylesheet=sheet, templates=tmp_path))


def test_a_correspondent_without_a_glance_route_is_refused():
    """Every correspondent has a block on Glance, and a missing route at render
    time is indistinguishable from a correspondent that is down."""
    with pytest.raises(registry.RegistryError, match="no GET /glance route"):
        registry.validate(make_plugin(glance=False))


def test_a_file_where_a_template_directory_belongs_is_refused(tmp_path):
    sheet = tmp_path / "there.css"
    sheet.write_text(".x {}", encoding="utf-8")
    with pytest.raises(registry.RegistryError, match="template directory"):
        registry.validate(make_plugin(templates=sheet))


# --- register and look up -----------------------------------------------


def test_register_files_the_instance_under_its_slug():
    plugin = make_plugin(slug="matsedel")
    registry.register(lambda: plugin)
    assert registry.get_plugin("matsedel") is plugin
    assert [p.slug for p in registry.plugins()] == ["matsedel"]


def test_two_plugins_cannot_claim_one_slug():
    registry.register(lambda: make_plugin(slug="matsedel"))
    with pytest.raises(registry.RegistryError, match="two plugins claim"):
        registry.register(lambda: make_plugin(slug="matsedel"))


def test_an_unknown_slug_reports_the_known_ones():
    registry.register(lambda: make_plugin(slug="matsedel"))
    with pytest.raises(LookupError, match=r"known: \['matsedel'\]"):
        registry.get_plugin("weather")


def test_plugins_are_in_slug_order():
    for slug in ("weather", "matsedel", "trains"):
        registry.register(lambda slug=slug: make_plugin(slug=slug))
    assert [p.slug for p in registry.plugins()] == ["matsedel", "trains", "weather"]


# --- mounting -----------------------------------------------------------


def build_app(plugin, monkeypatch, *, enabled=True):
    """A plugin mounted the way `web/app.py` mounts it, beside a core route."""
    registry._PLUGINS[plugin.slug] = plugin

    async def fake_enabled(session):
        return {plugin.slug} if enabled else set()

    monkeypatch.setattr(web_correspondents, "enabled_slugs", fake_enabled)

    core = APIRouter(prefix="/c", tags=["correspondents"])
    core.add_api_route("/{slug}/style.css", web_correspondents.correspondent_stylesheet)
    web_correspondents.mount(plugin, into=core)

    app = FastAPI()

    @app.get("/post/{post_id}")
    async def core_post(post_id: int):
        return {"where": "core", "post_id": post_id}

    app.include_router(core)
    return TestClient(app, raise_server_exceptions=False)


def test_a_plugin_route_lands_under_its_own_prefix_and_shadows_nothing(monkeypatch):
    """The plugin declares `/post/{id}`. Core owns that path, and keeps it."""
    client = build_app(make_plugin(), monkeypatch)
    assert client.get("/post/5").json() == {"where": "core", "post_id": 5}
    assert client.get("/c/fake/post/5").json() == {"where": "plugin", "post_id": 5}
    assert client.get("/c/fake").json() == {"where": "index"}


def test_the_glance_block_is_where_the_glance_page_looks_for_it(monkeypatch):
    """`glance.html` hardcodes `/c/<slug>/glance`; the mount has to put it there."""
    client = build_app(make_plugin(), monkeypatch)
    assert client.get("/c/fake/glance").json() == {"where": "glance"}


def test_disabling_a_correspondent_closes_every_route_it_has(monkeypatch):
    """The gate is on the include, so it covers routes nobody enumerated."""
    client = build_app(make_plugin(), monkeypatch, enabled=False)
    assert client.get("/c/fake").status_code == 404
    assert client.get("/c/fake/post/5").status_code == 404


def test_the_stylesheet_is_served_from_the_declared_path(monkeypatch, tmp_path):
    sheet = tmp_path / "matsedel.css"
    sheet.write_text(".correspondent--fake { color: red }", encoding="utf-8")
    client = build_app(make_plugin(stylesheet=sheet), monkeypatch)
    response = client.get("/c/fake/style.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert response.headers["cache-control"] == "no-cache"
    assert "correspondent--fake" in response.text


def test_no_stylesheet_no_route(monkeypatch):
    client = build_app(make_plugin(), monkeypatch)
    assert client.get("/c/fake/style.css").status_code == 404
    assert client.get("/c/unregistered/style.css").status_code == 404


def test_a_disabled_correspondent_does_not_serve_its_stylesheet(monkeypatch, tmp_path):
    sheet = tmp_path / "matsedel.css"
    sheet.write_text(".x {}", encoding="utf-8")
    client = build_app(make_plugin(stylesheet=sheet), monkeypatch, enabled=False)
    assert client.get("/c/fake/style.css").status_code == 404


def test_mounting_adds_the_plugins_template_directory(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(web_correspondents, "add_template_dir", seen.append)
    web_correspondents.mount(make_plugin(templates=tmp_path), into=APIRouter())
    assert seen == [tmp_path]


# --- the page wrapper ----------------------------------------------------


def write_page_template(tmp_path, body="<p>the week</p>"):
    (tmp_path / "week.html").write_text(body, encoding="utf-8")
    add_template_dir(tmp_path)
    return "week.html"


def _request(headers):
    return SimpleNamespace(
        headers=headers, scope={"type": "http"}, url=SimpleNamespace(path="/c/fake")
    )


def test_a_page_is_rendered_inside_the_core_wrapper(tmp_path):
    """The plugin ships the inside of the page; the scoping class and the
    stylesheet link come from core and there is no path around them."""
    sheet = tmp_path / "fake.css"
    sheet.write_text(".x {}", encoding="utf-8")
    name = write_page_template(tmp_path)
    response = correspondent_page(
        _request({}), make_plugin(stylesheet=sheet), name
    )
    html = response.body.decode()
    assert '<div class="correspondent-page correspondent--fake">' in html
    assert '<link rel="stylesheet" href="/c/fake/style.css">' in html
    assert "<p>the week</p>" in html


def test_a_plugin_without_a_stylesheet_gets_no_link(tmp_path):
    name = write_page_template(tmp_path)
    html = correspondent_page(_request({}), make_plugin(), name).body.decode()
    # Core's own sheet is in <head> on a full document; the correspondent's is not.
    assert "/c/fake/style.css" not in html


def test_a_page_cannot_claim_a_slug_it_does_not_have(tmp_path):
    """`correspondent` is built from the plugin, so a context key of that name is
    ignored rather than trusted."""
    name = write_page_template(tmp_path)
    html = correspondent_page(
        _request({}),
        make_plugin(slug="fake"),
        name,
        {
            "correspondent": {"slug": "admin", "label": "x", "stylesheet": False},
            "inner_template": "base.html",
        },
    ).body.decode()
    assert "correspondent--fake" in html
    assert "correspondent--admin" not in html
    # And the include is core's too: a context key cannot redirect it.
    assert "the week" in html


def test_a_boosted_click_gets_a_fragment_and_not_the_whole_document(tmp_path):
    """`render_block` resolves blocks defined in the LEAF template only. When a
    plugin template EXTENDED the wrapper it defined neither `content` nor
    `title`, so a boosted click swapped an entire document - header, sprite and
    all - into #main-content. Watched in the browser on 2026-08-28."""
    name = write_page_template(tmp_path)
    html = correspondent_page(
        _request({"HX-Request": "true", "HX-Target": "main-content"}),
        make_plugin(),
        name,
    ).body.decode()
    assert html.startswith("<title>Episteme • Fake</title>")
    assert "<html" not in html
    assert "site-header" not in html
    assert "icon-sprite" not in html
    assert "correspondent--fake" in html


# --- rows ---------------------------------------------------------------


class FakeSession:
    def __init__(self, existing):
        self.existing = existing
        self.added = []
        self.committed = False
        self.dirty = ()

    async def execute(self, _statement):
        return SimpleNamespace(scalars=lambda: iter(self.existing))

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        self.committed = True


async def test_ensure_rows_creates_one_row_per_plugin():
    registry.register(lambda: make_plugin(slug="matsedel", label="Matsedel"))
    session = FakeSession([])
    assert await rows.ensure_rows(session) == 1
    assert [(r.slug, r.label, r.config) for r in session.added] == [
        ("matsedel", "Matsedel", {})
    ]
    assert session.committed


async def test_ensure_rows_is_idempotent_and_refreshes_a_renamed_label():
    registry.register(lambda: make_plugin(slug="matsedel", label="Lunch menus"))
    existing = SimpleNamespace(slug="matsedel", label="Matsedel", enabled=True)
    session = FakeSession([existing])
    assert await rows.ensure_rows(session) == 0
    assert session.added == []
    assert existing.label == "Lunch menus"


async def test_a_row_whose_plugin_is_gone_is_left_alone():
    """Its config may be the only record of how that correspondent was set up."""
    orphan = SimpleNamespace(slug="weather", label="Weather", enabled=True)
    session = FakeSession([orphan])
    assert await rows.ensure_rows(session) == 0
    assert session.added == []
    assert orphan.enabled is True


def test_the_shipped_template_extends_base_and_scopes_the_wrapper():
    source = Path("src/episteme/web/templates/correspondent.html").read_text(
        encoding="utf-8"
    )
    assert 'extends "base.html"' in source
    assert "correspondent--{{ correspondent.slug }}" in source
    assert "/c/{{ correspondent.slug }}/style.css" in source
