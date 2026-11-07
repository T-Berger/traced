"""Graph summaries, full connection labels, and accessible evidence navigation."""

from __future__ import annotations

from dash import Input, Output, html

from .graph import build_evidence_graph, direct_connections
from .presentation import badge, empty, record_button


def premise_button(edge: dict, location: str):
    if edge["source_kind"] == "record":
        return record_button({"id": edge["source_ref"]}, location, edge["source_label"])
    return html.Button(
        edge["source_label"],
        id={"type": "rv-graph-premise", "fid": edge["source_ref"], "location": location},
        n_clicks=0,
        className="rv-text-button",
    )


def connection_outline(bundle: dict, finding_id: str | None, excluded: set[str]):
    edges = direct_connections(bundle, finding_id)
    if not edges:
        return empty("No direct connections", "The finding has no linked record or premise.")
    return html.Details(
        [
            html.Summary(f"Read all direct connections ({len(edges)})"),
            html.P(
                "Full labels and stated relationships. "
                "Records omitted from the canvas remain inspectable here.",
                className="rv-small rv-muted",
            ),
            html.Ul(
                [
                    html.Li(
                        [
                            premise_button(edge, f"graph-outline-{i}"),
                            html.Div(
                                [
                                    badge(edge["label"]),
                                    badge(f"{len(edge['link_ids'])} citation(s)"),
                                    *(
                                        [badge("Excluded record", "amber")]
                                        if edge["source_kind"] == "record"
                                        and edge["source_ref"] in excluded
                                        else []
                                    ),
                                ],
                                className="rv-badges",
                            ),
                            *[
                                html.P(basis, className="rv-small rv-muted")
                                for basis in edge["bases"]
                            ],
                        ]
                    )
                    for i, edge in enumerate(edges)
                ],
                className="rv-connection-list",
            ),
        ],
        className="rv-details rv-connection-outline",
    )


def connection_detail(bundle: dict, state: dict):
    edge_id = state.get("graph_edge_id")
    edge = next(
        (e for e in direct_connections(bundle, state.get("finding_id")) if e["id"] == edge_id), None
    )
    if not edge:
        return html.P(
            "Select a connection to read its basis, or select a record to open its exact evidence.",
            className="rv-small rv-muted",
        )
    fragments = {f["id"]: f for f in bundle.get("fragments", [])}
    quotes = [fragments[fid].get("quote") for fid in edge["fragment_ids"] if fid in fragments]
    quotes = list(dict.fromkeys(q for q in quotes if q))
    return html.Section(
        [
            html.H3("Selected connection"),
            html.P(edge["source_label"]),
            html.Div(
                [badge(edge["label"]), badge(f"{len(edge['link_ids'])} citation(s) combined")],
                className="rv-badges",
            ),
            *[html.P(basis) for basis in edge["bases"]],
            *[html.Blockquote(quote) for quote in quotes[:2]],
            *(
                [
                    html.Details(
                        [
                            html.Summary(f"Further quoted passages ({len(quotes) - 2})"),
                            *[html.Blockquote(q) for q in quotes[2:]],
                        ]
                    )
                ]
                if len(quotes) > 2
                else []
            ),
            html.A("Open selected evidence ↑", href="#rv-source", className="rv-text-button")
            if edge["source_kind"] == "record"
            else premise_button(edge, "graph-selected"),
            html.Details(
                [
                    html.Summary("Exact connection references"),
                    html.P(" · ".join(edge["link_ids"]), className="rv-mono"),
                    html.P(" · ".join(edge["fragment_ids"]), className="rv-mono"),
                ],
                className="rv-audit-details",
            ),
        ],
        className="rv-selected-connection",
    )


def register_graph_callbacks(app, service):
    @app.callback(
        Output("rv-graph-summary", "children"),
        Output("rv-graph-outline", "children"),
        Output("rv-graph-detail", "children"),
        Output("rv-graph", "style"),
        Input("rv-session", "data"),
    )
    def graph_context(state):
        if not state or not state.get("case_id"):
            return (
                "Choose a case to explore its evidence.",
                "",
                "",
                {"height": "320px", "width": "100%"},
            )
        view = service.view(state["case_id"], state.get("scenario_id"))
        bundle = view["bundle"]
        excluded = set(view.get("impact", {}).get("excluded_record_ids", []))
        model = build_evidence_graph(
            bundle,
            state.get("finding_id"),
            view.get("impact", {}).get("affected_finding_ids", []),
            excluded,
            active_record_id=state.get("record_id"),
        )
        return (
            html.P(model.summary, className="rv-small rv-muted"),
            connection_outline(bundle, state.get("finding_id"), excluded),
            connection_detail(bundle, state),
            {"width": "100%", "height": f"{max(320, model.shown_premises * 120 + 80)}px"},
        )

    app.clientside_callback(
        """function(open, tab, clicks, page, state, style, elements, toggleClicks) {
            const no = dash_clientside.no_update;
            const el = document.getElementById('rv-graph');
            if (!el) return no;
            if (tab !== 'path' || page !== 'review') {
                delete el.dataset.fitKey;
                return no;
            }
            return new Promise(resolve => requestAnimationFrame(() => {
                requestAnimationFrame(() => {
                    const disclosure = document.getElementById('rv-evidence-connections');
                    if (!disclosure || !disclosure.open ||
                        !el.getClientRects().length || el.clientWidth === 0) {
                        delete el.dataset.fitKey;
                        resolve(no); return;
                    }
                    const geometry = (elements || []).filter(e => e.position)
                        .map(e => [e.data.id, e.position]);
                    const key = JSON.stringify([state && state.case_id,
                        state && state.finding_id, state && state.scenario_id,
                        clicks, tab, page, style && style.height, geometry]);
                    if (el.dataset.fitKey === key) { resolve(no); return; }
                    el.dataset.fitKey = key;
                    const cy = window.cy;
                    const container = cy && !cy.destroyed() && cy.container();
                    if (container && (container === el || el.contains(container))) {
                        cy.resize();
                        const positions = {};
                        (elements || []).forEach(e => {
                            if (e.position) positions[e.data.id] = e.position;
                        });
                        cy.nodes().positions(node => positions[node.id()] || node.position());
                        cy.fit(cy.elements(), 32);
                        if (cy.zoom() > 1.25) { cy.zoom(1.25); cy.center(); }
                        resolve({name:'preset', fit:false, padding:32, animate:false});
                    } else {
                        resolve({name:'preset', fit:true, padding:32, animate:false});
                    }
                });
            }));
        }""",
        Output("rv-graph", "layout"),
        Input("rv-evidence-connections", "open"),
        Input("rv-tab", "value"),
        Input("rv-graph-fit", "n_clicks"),
        Input("rv-page", "value"),
        Input("rv-session", "data"),
        Input("rv-graph", "style"),
        Input("rv-graph", "elements"),
        Input("rv-evidence-connections", "n_clicks"),
    )
