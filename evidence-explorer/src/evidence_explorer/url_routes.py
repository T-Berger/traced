"""Shareable page URLs for Dash menus.

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
    const key = %(menu_prop)s;
    const initial = %(default)s;
    const pages = {};
    for (const [page, slug] of Object.entries(routes)) { pages[slug] = page; }
    // The bare app root means the menu's starting page (e.g. back to "/").
    if (initial !== null && !('' in pages)) { pages[''] = initial; }
    const noUpdate = window.dash_clientside.no_update;
    const path = pathname || '';
    const slug = (path.startsWith(prefix) ? path.slice(prefix.length) : '')
        .replace(/^\\/+|\\/+$/g, '');
    // Compare with the last synced state instead of the trigger list: on a slow
    // server the first menu click can arrive batched with the initial URL read.
    const memory = (window.__tracedUrlSync = window.__tracedUrlSync || {});
    const last = memory[key];
    if (last === undefined) {
        const page = pages[slug];
        if (page !== undefined && slug !== '') {
            memory[key] = {value: page, slug: slug};
            return page !== value ? [page, noUpdate] : [noUpdate, noUpdate];
        }
        // App root: remember the starting page, so a menu click that arrives in
        // this first run is still recognised below and gets its own URL.
        memory[key] = {value: initial !== null ? initial : value, slug: slug};
        return syncMenu(memory[key]);
    }
    return syncMenu(last);

    function syncMenu(last) {
    if (value !== last.value) {
        const target = routes[value];
        memory[key] = {value: value, slug: target !== undefined ? target : slug};
        if (target === undefined || target === slug) { return [noUpdate, noUpdate]; }
        return [noUpdate, prefix + target];
    }
    if (slug !== last.slug) {
        const page = pages[slug];
        memory[key] = {value: page !== undefined ? page : value, slug: slug};
        if (page !== undefined && page !== value) { return [page, noUpdate]; }
    }
    return [noUpdate, noUpdate];
    }
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
    default: str | None = None,
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
            "default": json.dumps(default),
        },
        Output(component_id, prop, allow_duplicate=True),
        Output(location_id, "pathname"),
        Input(location_id, "pathname"),
        Input(component_id, prop),
        prevent_initial_call="initial_duplicate",
    )
