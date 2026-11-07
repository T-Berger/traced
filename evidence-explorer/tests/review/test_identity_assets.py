"""The packaged Traced identity must load locally, including behind a URL prefix."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from importlib.resources import files
from urllib.parse import urljoin, urlsplit

import pytest

from evidence_explorer.review.service import ReviewService
from evidence_explorer.review.storage import ReviewRepository
from evidence_explorer.review.web import create_app


class HeadLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "link":
            self.links.append(dict(attrs))


def _components(value):
    if isinstance(value, dict) and "props" in value:
        yield value
        yield from _components(value["props"].get("children"))
    elif isinstance(value, list):
        for child in value:
            yield from _components(child)


@pytest.mark.parametrize("has_case", [False, True])
def test_first_page_offers_cases_for_an_empty_workspace_without_seeding(tmp_path, has_case):
    repository = ReviewRepository(tmp_path / "first-page.sqlite3")
    repository.initialize()
    service = ReviewService(repository)
    if has_case:
        service.seed_demo()
    before = service.list_cases()
    app = create_app(service)
    layout = app.server.test_client().get("/_dash-layout").get_json()
    navigation = next(c for c in _components(layout) if c["props"].get("id") == "rv-page")
    assert navigation["props"]["value"] == ("review" if has_case else "cases")
    assert service.list_cases() == before


@pytest.mark.parametrize("prefix", ["/", "/review/"])
def test_traced_header_fonts_and_favicon_load_from_the_packaged_prefixed_app(tmp_path, prefix):
    repository = ReviewRepository(tmp_path / "identity.sqlite3")
    repository.initialize()
    service = ReviewService(repository)
    service.seed_demo()
    app = create_app(service, prefix=prefix)
    client = app.server.test_client()
    index = client.get(prefix)
    assert index.status_code == 200
    head = HeadLinks()
    head.feed(index.get_data(as_text=True))
    layout = client.get(prefix + "_dash-layout")
    assert layout.status_code == 200
    assets = files("evidence_explorer.review").joinpath("assets")
    logos = [
        component["props"]
        for component in _components(layout.get_json())
        if component["type"] == "Img" and "traced-logo" in component["props"].get("className", "")
    ]
    assert len(logos) == 2
    assert {urlsplit(logo["src"]).path for logo in logos} == {
        prefix + "assets/traced/logos/traced-primary.svg",
        prefix + "assets/traced/logos/traced-reversed.svg",
    }
    for logo in logos:
        name = urlsplit(logo["src"]).path.split("/assets/", 1)[1]
        response = client.get(logo["src"])
        assert response.status_code == 200
        assert response.mimetype == "image/svg+xml"
        assert response.data == assets.joinpath(name).read_bytes()
        assert b"Traced Branch Clock" in response.data

    styles = [link["href"] for link in head.links if link.get("rel") == "stylesheet"]
    for name in ("00_traced_tokens.css", "01_traced_base.css", "zz_traced_theme.css"):
        assert any(urlsplit(url).path == prefix + "assets/" + name for url in styles)
    tokens_url = next(url for url in styles if urlsplit(url).path.endswith("00_traced_tokens.css"))
    tokens = client.get(tokens_url)
    assert tokens.status_code == 200
    assert tokens.data == assets.joinpath("00_traced_tokens.css").read_bytes()
    font_urls = re.findall(r'url\("([^"]+)"\)', tokens.get_data(as_text=True))
    assert set(font_urls) == {
        "./traced/fonts/Manrope-Variable.ttf",
        "./traced/fonts/IBMPlexMono-Regular.ttf",
    }
    for relative_url in font_urls:
        url = urljoin(tokens_url, relative_url)
        assert urlsplit(url).path.startswith(prefix + "assets/traced/fonts/")
        response = client.get(url)
        assert response.status_code == 200
        assert response.mimetype != "text/html"
        assert response.data == assets.joinpath(relative_url.removeprefix("./")).read_bytes()

    favicon = next(link["href"] for link in head.links if link.get("rel") == "icon")
    assert urlsplit(favicon).path.startswith(prefix)
    response = client.get(favicon)
    assert response.status_code == 200
    assert response.mimetype != "text/html"
    assert response.data == assets.joinpath("favicon.ico").read_bytes()
