"""Read-only, mountable preview of the fictional category timeline and matrix.

This panel has no callbacks, network access, or authority to record decisions.
"""

import plotly.graph_objects as go
from dash import dcc, html

from .matrix import CATEGORIES, build_monthly_matrix, fictional_demo


def fictional_compliance_panel():
    """Display the example alone; it must never be mixed with a selected case."""
    demo = fictional_demo()
    result = build_monthly_matrix(
        demo["records"], demo["annotations"], demo["coverage"], fictional=True
    )
    figure = go.Figure()
    for category, color in zip(
        CATEGORIES, ("#276B63", "#52636C", "#142229", "#F36643"), strict=True
    ):
        figure.add_bar(
            name=category.replace("_", " ").title(),
            marker_color=color,
            x=[month["month"] for month in result["months"]],
            y=[month["categories"][category]["confirmed"] for month in result["months"]],
        )
    figure.update_layout(
        barmode="group",
        xaxis_title="Publication month (UTC)",
        yaxis_title="Distinct saved posts",
        legend={"orientation": "h", "x": 0, "y": -0.4, "font": {"size": 10}},
        margin={"l": 55, "r": 15, "t": 15, "b": 115},
        height=380,
        template="plotly_white",
    )
    figure.update_xaxes(type="category")
    figure.update_yaxes(dtick=1, rangemode="tozero")

    month_details = []
    for month in result["months"]:
        categories = []
        for name, counts in month["categories"].items():
            if counts["confirmed"] is None:
                label = (
                    f"{name.replace('_', ' ')}: unavailable; "
                    f"{counts['observed_confirmed']} reviewed and "
                    f"{counts['observed_proposed']} proposed observed in the saved sample"
                )
            else:
                label = (
                    f"{name.replace('_', ' ')}: {counts['confirmed']} reviewed, "
                    f"{counts['proposed']} proposed"
                )
            ids = sorted(set(counts["record_ids"] + counts["proposed_record_ids"]))
            categories.append(html.Li([label, html.Span(" · " + ", ".join(ids)) if ids else ""]))
        month_details.append(
            html.Details(
                [
                    html.Summary(f"{month['month']} · declared coverage: {month['coverage']}"),
                    html.Ul(categories),
                ]
            )
        )

    rows = []
    for row in result["rows"]:
        categories = ", ".join(a["category"].replace("_", " ") for a in row["categories"]) or "None"
        proposals = ", ".join(a["category"].replace("_", " ") for a in row["proposals"]) or "None"
        rows.append(
            html.Tr(
                [
                    html.Td(row["record_id"]),
                    html.Td(row["published_at"] or "Unknown"),
                    html.Td(row["text"]),
                    html.Td(categories),
                    html.Td(proposals),
                    html.Td(_observation(row["notice"])),
                    html.Td(_observation(row["response"])),
                ]
            )
        )
    return html.Section(
        [
            html.H2("Fictional example · category timeline and evidence matrix"),
            html.P(
                "All records and reviews below are invented. They do not describe a real "
                "platform or person."
            ),
            html.P(
                "Publication month in UTC · one count per saved post ID per category. "
                "Categories can overlap; model proposals are not human decisions. "
                "Partial or unknown coverage is unavailable, not zero."
            ),
            html.H3("Reviewed categories in the fictional sample"),
            dcc.Graph(figure=figure, config={"displayModeBar": False}, responsive=True),
            html.P(
                "No severity colors are configured. Category review does not establish a "
                "violation, platform action, or legal conclusion."
            ),
            html.H3("Monthly counts and contributing evidence"),
            *month_details,
            html.H3("Evidence matrix"),
            html.P(
                "Scroll horizontally to see all evidence columns.", className="rv-small rv-muted"
            ),
            html.Div(
                html.Table(
                    [
                        html.Thead(
                            html.Tr(
                                [
                                    html.Th(label, scope="col")
                                    for label in (
                                        "Saved record",
                                        "Published",
                                        "Saved text",
                                        "Human category review",
                                        "Model proposal",
                                        "Notice evidence",
                                        "Response observation",
                                    )
                                ]
                            )
                        ),
                        html.Tbody(rows),
                    ],
                    style={"minWidth": "1200px", "tableLayout": "auto"},
                ),
                style={"overflowX": "auto"},
            ),
            html.P("Undated saved records: " + (", ".join(result["undated_record_ids"]) or "none")),
        ],
        className="rv-case-card",
        **{"aria-label": "Fictional compliance review example"},
    )


def _observation(value):
    if value["status"] != "documented":
        return "Unknown · " + value.get("reason", "No saved observation")
    return f"{value['record_id']} · {value['quote']}"
