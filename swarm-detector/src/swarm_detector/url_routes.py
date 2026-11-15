"""Shareable page URLs for Dash menus (copy of evidence_explorer.url_routes; keep in sync).

A page menu (tabs, radio items, or a store) keeps its value in the browser only, so
every page has the same URL. ``register_url_routes`` mirrors that value into the
path, e.g. ``/alerts`` or ``/swarm/verify``, in both directions:

* choosing a menu item pushes the matching path (browser history works);
* opening or reloading a path, or using back/forward, selects that menu item.

The bare app root keeps the menu's own default page. Unknown paths are ignored.
Everything runs in the browser; the server already serves the app at every path
under its prefix, so no Flask routes are needed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from dash import Dash, Input, Output, dcc

_SYNC = """function(pathname, value) {
    const prefix = %(prefix)s;
    const routes = %(routes)s;
    const pages = {};
    for (const [page, slug] of Object.entries(routes)) { pages[slug] = page; }
    const noUpdate = window.dash_clientside.no_update;
    const triggered = (window.dash_clientside.callback_context.triggered || [])
        .map(item => item.prop_id);
    const path = pathname || '';
    const slug = (path.startsWith(prefix) ? path.slice(prefix.length) : '')
        .replace(/^\\/+|\\/+$/g, '');
    if (triggered.length === 1 && triggered[0] === %(menu_prop)s) {
        const target = routes[value];
        if (!target || target === slug) { return [noUpdate, noUpdate]; }
        return [noUpdate, prefix + target];
    }
    const page = pages[slug];
    if (page !== undefined && page !== value) { return [page, noUpdate]; }
    return [noUpdate, noUpdate];
}"""


def url_location(location_id: str) -> dcc.Location:
    """The URL component to place once in the app layout."""
    return dcc.Location(id=location_id, refresh=False)


def register_url_routes(
    app: Dash,
    *,
    location_id: str,
    component_id: str,
    prop: str,
    routes: Mapping[str, str],
) -> None:
    """Keep ``component_id.prop`` and the URL path in sync (``routes``: page value -> slug)."""
    prefix = app.config.requests_pathname_prefix or "/"
    if not prefix.endswith("/"):
        prefix += "/"
    app.clientside_callback(
        _SYNC
        % {
            "prefix": json.dumps(prefix),
            "routes": json.dumps(dict(routes)),
            "menu_prop": json.dumps(f"{component_id}.{prop}"),
        },
        Output(component_id, prop, allow_duplicate=True),
        Output(location_id, "pathname"),
        Input(location_id, "pathname"),
        Input(component_id, prop),
        prevent_initial_call="initial_duplicate",
    )
