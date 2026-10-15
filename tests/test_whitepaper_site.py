"""The whitepaper page published to lapsecoin.github.io/core/ is the node's own
page: same renderer, same templates, same stylesheets. These pin that it is, and
that nothing in it points at a path that only exists on a node."""

import importlib.util
import os
import re

import pytest

import api
import peerpool as peerpool_mod
import whitepaper_render
from tests import test_api as _api_tests

ROOT = os.path.join(os.path.dirname(__file__), "..")


@pytest.fixture(scope="module")
def build():
    spec = importlib.util.spec_from_file_location(
        "build_whitepaper", os.path.join(ROOT, "scripts", "build_whitepaper.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def site(build, tmp_path_factory):
    out = tmp_path_factory.mktemp("site")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("sys.argv", ["build_whitepaper.py", "--out", str(out)])
        assert build.main() == 0
    return out


def _read(site, *parts):
    with open(os.path.join(site, *parts), encoding="utf-8") as f:
        return f.read()


def test_the_page_carries_the_same_article_as_a_node(site):
    node = _api_tests.TestOddsPage._OddsNode(False)
    client = api.create_private_app(node, peerpool_mod.PeerPool()).test_client()
    node_html = client.get("/whitepaper").get_data(as_text=True)
    article = whitepaper_render.render(_read(ROOT, "docs", "whitepaper.md"))
    assert article in node_html
    assert article in _read(site, "whitepaper.html")


def test_the_page_uses_the_node_stylesheets_under_core(site):
    html = _read(site, "whitepaper.html")
    assert 'href="/core/css/base.css?v=' in html
    assert 'href="/core/css/pages/whitepaper.css?v=' in html
    assert os.path.isfile(os.path.join(site, "css", "pages", "whitepaper.css"))
    assert os.path.isfile(os.path.join(site, "favicon.svg"))


def test_the_fonts_are_found_relative_to_the_stylesheet(site):
    css = _read(site, "css", "base.css")
    assert 'url("fonts/Geist-Variable.woff2")' in css
    assert "/css/fonts/" not in css
    assert os.path.isfile(os.path.join(site, "css", "fonts", "Geist-Variable.woff2"))
    assert os.path.isfile(os.path.join(site, "css", "fonts", "geist-mono.woff2"))


def test_nothing_points_at_a_path_that_only_exists_on_a_node(site):
    html = _read(site, "whitepaper.html")
    assert not re.findall(r'(?:href|src)="/(?!core/)[^"]*"', html)
    for node_only in ("/send", "/blocks", "/network", "/board", "/settings", "/odds"):
        assert f'href="{node_only}' not in html


def test_the_nav_is_the_websites_not_the_nodes(site):
    html = _read(site, "whitepaper.html")
    assert 'href="https://lapsecoin.github.io/downloads.html"' in html
    assert ">Website<" in html and ">Whitepaper<" in html
    assert "Dashboard" not in html and "Mempool" not in html


def test_the_page_has_its_own_share_card(site):
    html = _read(site, "whitepaper.html")
    assert '<meta property="og:title" content="LapseCoin whitepaper">' in html
    assert ('<meta property="og:url" content="https://lapsecoin.github.io/core/whitepaper.html">'
            in html)
    assert '<meta property="og:image" content="https://lapsecoin.github.io/logo-512.png">' in html


def test_a_node_still_shows_its_own_nav_and_version(site):
    node = _api_tests.TestOddsPage._OddsNode(False)
    client = api.create_private_app(node, peerpool_mod.PeerPool()).test_client()
    html = client.get("/whitepaper").get_data(as_text=True)
    assert "Dashboard" in html and 'href="/odds"' in html
    assert re.search(r"LapseCoin v\d", html)
    assert "Website" not in html
    assert "/lapsecoin-512x512.png" in html and "logo-512.png" not in html
