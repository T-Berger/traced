"""Pure presentation helpers for evidence charts, tables, and display values.

These functions render supplied case data without opening a database or creating
an application. They can be used and tested independently of Dash callbacks.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import plotly.graph_objects as go
from dash import html

from .graph import graph_view

# Resolved canonical roles for plotting libraries that do not read CSS tokens.
TEAL = "#276B63"  # Moss: observed data, not a truth assessment.
AMBER = "#754B05"  # Review-needed text remains distinct from brand focus.
TRACE = "#F36643"
INK = "#142229"
MUTED = "#52636C"
GRAPH_STYLES = [
    {
        "selector": "node",
        "style": {
            "background-color": "#D9E9E4",
            "width": 112,
            "height": 54,
            "shape": "round-rectangle",
            "label": "data(label)",
            "font-size": 15,
            "font-family": "Manrope, sans-serif",
            "color": INK,
            "text-valign": "center",
            "text-halign": "center",
            "text-wrap": "wrap",
            "text-max-width": 100,
            "border-color": TEAL,
            "border-width": 1.5,
        },
    },
    {
        "selector": "node.peripheral",
        "style": {"background-color": "#F4F3EE", "border-color": "#52636C"},
    },
    {
        "selector": "node.focus-account",
        "style": {"border-width": 2.5},
    },
    {
        "selector": "edge",
        "style": {
            "line-color": TEAL,
            "width": "data(width)",
            "curve-style": "bezier",
            "opacity": 0.8,
        },
    },
    {
        "selector": "edge.context",
        "style": {"line-style": "dashed", "line-color": MUTED, "opacity": 0.7},
    },
    {
        "selector": "edge.focused-pair, edge:selected",
        "style": {"line-color": TRACE, "opacity": 1, "width": 4},
    },
    {
        "selector": "node:selected",
        "style": {"border-color": TRACE, "border-width": 3},
    },
]


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, tz=UTC)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        return None


def stamp(value: Any) -> str:
    dt = parse_time(value)
    return dt.strftime("%d %b %H:%M:%S UTC") if dt else "Time unavailable"


def short(value: Any, limit: int = 20) -> str:
    text = str(value or "Unknown")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def number(value: Any, digits: int = 1) -> str:
    try:
        return f"{float(value):,.{digits}f}" if digits else f"{int(value):,}"
    except (ValueError, TypeError):
        return "—"


def empty_figure(message: str, height: int = 280) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(
        text=message,
        x=0.5,
        y=0.5,
        xref="paper",
        yref="paper",
        showarrow=False,
        font={"color": MUTED, "size": 13},
    )
    return figure_style(fig, height=height, hide_axes=True)


def figure_style(fig: go.Figure, height: int = 280, hide_axes: bool = False) -> go.Figure:
    fig.update_layout(
        template="plotly_white",
        height=height,
        autosize=True,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": "Manrope, sans-serif", "size": 13, "color": MUTED},
        margin={"l": 80, "r": 18, "t": 14, "b": 44},
        hoverlabel={"bgcolor": INK, "font_color": "#F4F3EE"},
        showlegend=False,
    )
    fig.update_xaxes(
        showgrid=True, gridcolor="#D9E0DE", zeroline=False, showline=False, automargin=True
    )
    fig.update_yaxes(showgrid=False, zeroline=False, automargin=True)
    if hide_axes:
        fig.update_xaxes(visible=False)
        fig.update_yaxes(visible=False)
    return fig


def card_label(label: str, value: Any, context: str = "") -> html.Div:
    return html.Div(
        [
            html.Span(label, className="metric-label"),
            html.Strong(str(value), className="metric-value"),
            html.Span(context, className="metric-context"),
        ],
        className="metric",
    )


def section_heading(kicker: str, title: str, note: str | None = None) -> html.Div:
    return html.Div(
        [
            html.Div([html.Span(kicker, className="eyebrow"), html.H3(title)]),
            html.P(note, className="section-note") if note else None,
        ],
        className="section-heading",
    )


def safe_source(ref: Any):
    """Only real, directly provided HTTP(S) references become links."""
    text = str(ref or "")
    if text.startswith(("https://", "http://")):
        return html.A(
            "Open original source ↗", href=text, target="_blank", rel="noopener noreferrer"
        )
    return html.Code(text or "Source reference unavailable", className="source-ref")


def table(headers: list[str], rows: list[list[Any]], class_name: str = "") -> html.Div:
    if not rows:
        return html.Div("No records available for this selection.", className="empty-state compact")
    return html.Div(
        html.Table(
            [
                html.Thead(html.Tr([html.Th(header) for header in headers])),
                html.Tbody([html.Tr([html.Td(cell) for cell in row]) for row in rows]),
            ],
            className="data-table " + class_name,
        ),
        className="table-wrap",
    )


def case_messages(detail: dict) -> list[dict]:
    return detail.get("messages") or []


def case_channels(detail: dict) -> list[dict]:
    dataset = detail.get("dataset") or {}
    return dataset.get("channels", []) if isinstance(dataset, dict) else []


def post_map(detail: dict) -> dict:
    return {str(p.get("post_key")): p for p in detail.get("posts", [])}


def kind_name(case: dict) -> str:
    kind = str(case.get("kind", "candidate"))
    return "Comparison discussion" if "compar" in kind else "Coordination candidate"


def graph_elements(detail: dict, selected_pair_key: str | None = None) -> list[dict]:
    """Return the readable selected-pair projection without changing case evidence."""
    return graph_view(detail, selected_pair_key)["elements"]


def matrix(detail: dict) -> go.Figure:
    messages = case_messages(detail)
    if not messages:
        return empty_figure("No account activity is available for this case.")
    posts = post_map(detail)
    keys = sorted(
        {str(m.get("post_key")) for m in messages},
        key=lambda key: str(posts.get(key, {}).get("timestamp_utc", key)),
    )[:24]
    accounts = list(dict.fromkeys((detail.get("case") or {}).get("account_keys", [])))
    accounts += [
        a.get("account_key")
        for a in detail.get("accounts", [])
        if a.get("account_key") not in accounts
    ]
    accounts = accounts[:24]
    first: dict = {}
    for message in messages:
        key = (message.get("account_key"), str(message.get("post_key")))
        timestamp = parse_time(message.get("timestamp_utc"))
        if timestamp and (key not in first or timestamp < first[key]):
            first[key] = timestamp
    values, custom = [], []
    for account in accounts:
        row, hover = [], []
        for key in keys:
            arrival = first.get((account, key))
            parent = parse_time(posts.get(key, {}).get("timestamp_utc"))
            minutes = (arrival - parent).total_seconds() / 60 if arrival and parent else None
            row.append(minutes)
            hover.append([str(account), key, stamp(arrival), number(minutes)])
        values.append(row)
        custom.append(hover)
    fig = go.Figure(
        go.Heatmap(
            z=values,
            x=[f"{i + 1:02d}" for i in range(len(keys))],
            y=[short(a, 16) for a in accounts],
            customdata=custom,
            colorscale=[[0, "#F4F3EE"], [0.4, "#D9E9E4"], [1, TEAL]],
            colorbar={"title": {"text": "Minutes"}, "thickness": 8, "len": 0.7},
            xgap=5,
            ygap=5,
            hoverongaps=False,
            hovertemplate="%{customdata[0]}<br>Post: %{customdata[1]}<br>%{customdata[2]}<br>First arrival: %{customdata[3]} min after post<extra></extra>",
        )
    )
    fig.update_yaxes(autorange="reversed")
    fig.update_xaxes(title="Discussion, ordered by parent-post time", side="bottom")
    return figure_style(fig, height=max(230, min(490, 55 + len(accounts) * 26)))


def timeline(detail: dict) -> go.Figure:
    messages = [m for m in case_messages(detail) if parse_time(m.get("timestamp_utc"))]
    if not messages:
        return empty_figure("No cross-community activity is available.", 320)
    channels = {c.get("channel_key"): c for c in case_channels(detail)}
    categories: dict[str, list[dict]] = defaultdict(list)
    for message in messages:
        channel = channels.get(message.get("channel_key"), {})
        categories[str(channel.get("category", "Unclassified"))].append(message)
    fig = go.Figure()
    for category, entries in sorted(categories.items()):
        bins: dict = defaultdict(list)
        for message in entries:
            dt = parse_time(message.get("timestamp_utc"))
            rounded = dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)
            bins[(message.get("channel_key"), rounded)].append(message)
        xs, ys, sizes, hovers = [], [], [], []
        for (channel_key, stamp), comments in sorted(bins.items(), key=lambda item: item[0][1]):
            xs.append(stamp)
            ys.append(channels.get(channel_key, {}).get("name", channel_key))
            sizes.append(min(25, 8 + 3 * math.sqrt(len(comments))))
            hovers.append([category, len(comments), len({m.get("account_key") for m in comments})])
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=ys,
                mode="markers",
                name=category,
                marker={
                    "color": TEAL,
                    "size": sizes,
                    "opacity": 0.82,
                    "line": {"color": "white", "width": 1},
                },
                customdata=hovers,
                hovertemplate="%{y}<br>%{x|%d %b %H:%M} UTC<br>%{customdata[0]}<br>%{customdata[1]} comments · %{customdata[2]} accounts<extra></extra>",
            )
        )
    fig.update_xaxes(title="Observed activity · UTC", tickformat="%d %b\n%H:%M")
    figure_style(
        fig, height=max(300, min(480, 90 + len({m.get("channel_key") for m in messages}) * 55))
    )
    fig.update_layout(
        showlegend=False,
        legend={"orientation": "h", "y": 1.2, "x": 0},
        margin={"l": 120, "r": 18, "t": 45, "b": 55},
    )
    return fig


def episode_figure(detail: dict, post_key: str | None, progress: int = 100) -> go.Figure:
    messages = [
        m
        for m in (detail.get("context_messages") or case_messages(detail))
        if str(m.get("post_key")) == str(post_key)
    ]
    messages = [m for m in messages if parse_time(m.get("timestamp_utc"))]
    if not messages:
        return empty_figure("Select a discussion to replay observed arrivals.", 240)
    post = post_map(detail).get(str(post_key), {})
    origin = parse_time(post.get("timestamp_utc")) or min(
        parse_time(m["timestamp_utc"]) for m in messages
    )
    accounts = list(
        dict.fromkeys(
            m.get("account_key") for m in sorted(messages, key=lambda m: str(m.get("account_key")))
        )
    )
    end = max(
        ((parse_time(m["timestamp_utc"]) - origin).total_seconds() for m in messages), default=1
    )
    end = max(end, 1)
    current = end * max(0, min(progress or 0, 100)) / 100
    shown = [
        m for m in messages if (parse_time(m["timestamp_utc"]) - origin).total_seconds() <= current
    ]
    core = set((detail.get("case") or {}).get("account_keys", []))
    fig = go.Figure(
        go.Scatter(
            x=[(parse_time(m["timestamp_utc"]) - origin).total_seconds() for m in shown],
            y=[short(m.get("account_key"), 18) for m in shown],
            mode="markers",
            customdata=[[m.get("message_key"), stamp(m.get("timestamp_utc"))] for m in shown],
            marker={
                "size": 11,
                "color": [TEAL if m.get("account_key") in core else MUTED for m in shown],
                "line": {"width": 1, "color": "white"},
            },
            hovertemplate="%{y}<br>%{customdata[0]}<br>%{customdata[1]}<br>%{x:.0f}s after post<extra></extra>",
        )
    )
    fig.add_vline(x=current, line_width=1, line_dash="dot", line_color=TRACE)
    fig.update_xaxes(range=[-max(end * 0.025, 1), end * 1.035], title="Seconds after parent post")
    fig.update_yaxes(
        categoryorder="array", categoryarray=[short(a, 18) for a in accounts], autorange="reversed"
    )
    return figure_style(fig, height=max(220, min(430, 70 + len(accounts) * 27)))


def baseline(detail: dict, pair_key: str | None = None) -> go.Figure:
    pairs = detail.get("pairs", [])
    pair = next((p for p in pairs if str(p.get("pair_key")) == str(pair_key)), None)
    if pair is None:
        pair = next((p for p in pairs if p.get("retained")), pairs[0] if pairs else None)
    if not pair:
        return empty_figure("No pair evidence meets the current case selection.", 190)
    fig = go.Figure(
        go.Bar(
            x=[float(pair.get("null_mean", 0)), float(pair.get("observed", 0))],
            y=["Shuffled mean", "Observed"],
            orientation="h",
            marker_color=[MUTED, TEAL],
            text=[number(pair.get("null_mean", 0)), number(pair.get("observed", 0), 0)],
            textposition="outside",
            cliponaxis=False,
            hovertemplate="%{y}: %{x:.1f} recurring families<extra></extra>",
        )
    )
    fig.update_xaxes(title="Distinct discussion families", rangemode="tozero")
    figure_style(fig, 190)
    fig.update_layout(margin={"l": 112, "r": 40, "t": 10, "b": 42})
    return fig
