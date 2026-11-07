"""Static exact-evidence page using the same Dash inspector and Traced assets.

No browser state, source fetching, or image request is needed to open a citation.
The small serializer accepts only our own HTML components; all values are escaped.
"""

from __future__ import annotations

from html import escape
from typing import Any
from urllib.parse import urlencode

from dash import html
from dash.development.base_component import Component

from .components import evidence_inspector

_TAGS = {
    "Div",
    "Span",
    "Code",
    "P",
    "H1",
    "H2",
    "H3",
    "Table",
    "Thead",
    "Tbody",
    "Tr",
    "Th",
    "Td",
    "Dl",
    "Dt",
    "Dd",
    "Img",
    "Strong",
    "A",
    "Main",
    "Header",
}
_ATTRIBUTES = {
    "className": "class",
    "id": "id",
    "alt": "alt",
    "src": "src",
    "href": "href",
    "role": "role",
    "scope": "scope",
}


def _static_html(value: Any, verified_media_url: str | None = None) -> str:
    if value is None:
        return ""
    if isinstance(value, (tuple, list)):
        return "".join(_static_html(child, verified_media_url) for child in value)
    if not isinstance(value, Component):
        return escape(str(value))
    component = value.to_plotly_json()
    name, props = component["type"], component["props"]
    if component["namespace"] != "dash_html_components" or name not in _TAGS:
        raise ValueError("Unsupported static citation component.")
    if name == "Img" and verified_media_url and str(props.get("src", "")).startswith("/media/"):
        props = {**props, "src": verified_media_url}
    attributes = "".join(
        f' {target}="{escape(str(props[source]), quote=True)}"'
        for source, target in _ATTRIBUTES.items()
        if source in props
    )
    tag = name.lower()
    if tag == "img":
        return f"<{tag}{attributes}>"
    return (
        f"<{tag}{attributes}>"
        + _static_html(props.get("children"), verified_media_url)
        + f"</{tag}>"
    )


def citation_page(frame: dict, release: dict, *, reveal: bool = False) -> str:
    release_id, frame_id = release["release_id"], frame["frame_id"]
    query = urlencode({"release_id": release_id, "frame_id": frame_id})
    if frame.get("citation_expected_sha256"):
        query += "&" + urlencode({"sha256": frame["citation_expected_sha256"]})
    toggle = f"/evidence?{query}" + ("" if reveal else "&reveal=1")
    content = html.Main(
        [
            html.Header(
                [
                    html.Img(
                        src="/assets/traced/logos/traced-primary.svg",
                        alt="Traced",
                        className="traced-logo",
                    ),
                    html.H1("Exact evidence reference"),
                    html.P(f"Release: {release.get('title') or release_id}"),
                    html.A("Return to workbench", href="/"),
                ],
                className="ss-page-header",
            ),
            html.Div(
                [
                    html.Strong("Images may contain graphic material."),
                    html.A(
                        "Hide preview" if reveal else "Reveal this image",
                        href=toggle,
                        className="ss-button",
                    ),
                ],
                className="ss-reveal-bar",
            ),
            html.Div(
                [
                    html.Strong(
                        "Saved citation bytes verified"
                        if frame.get("citation_hash_matches")
                        else "Saved citation unavailable · image preview blocked"
                    ),
                    html.P("Expected SHA-256: " + frame["citation_expected_sha256"]),
                ],
                className="ss-panel",
                role="status",
            )
            if frame.get("citation_expected_sha256")
            else None,
            html.P(
                "Exercise week means first posted. Side describes the posting channel, "
                "not who is depicted. Capture time, order and location remain unknown."
            ),
            html.Div(evidence_inspector(frame, release_id, reveal), className="ss-panel"),
        ],
        className="ss-citation-main",
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Traced · Exact evidence</title>"
        '<link rel="icon" href="/assets/favicon.ico">'
        + "".join(
            f'<link rel="stylesheet" href="/assets/{name}">'
            for name in (
                "tokens.css",
                "00_traced_tokens.css",
                "01_traced_base.css",
                "workbench.css",
                "zz_traced_theme.css",
            )
        )
        + "<style>.ss-citation-main{max-width:960px;margin:auto;padding:24px;min-width:0}"
        ".ss-citation-main .ss-page-header{display:block}"
        ".ss-citation-main .traced-logo{max-width:190px;height:auto}"
        ".ss-citation-main .ss-panel{margin-top:20px}"
        ".ss-citation-main p,.ss-citation-main code{overflow-wrap:anywhere}"
        "@media(max-width:500px){.ss-citation-main{padding:12px}}</style>"
        '</head><body class="traced-app">'
        + _static_html(
            content,
            "/evidence-media?" + query if frame.get("citation_expected_sha256") else None,
        )
        + "</body></html>"
    )
