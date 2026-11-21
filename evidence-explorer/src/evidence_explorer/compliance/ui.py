"""Compliance audit page: does labeled content show the enforcement a platform says it applies?

Charts the metrics a regulatory complaint (e.g. NYS GBS § 1102, FTC Act § 5) relies on:
volume per statutory category, posting velocity, volatility, reach relative to unflagged
content, persistence until capture, account category coverage and harassment bursts.
Everything comes from the analytics database; model flags are proposals and the page lists
what the data cannot show. Each chart carries a plain-language reading guide and a takeaway
computed from the data, and the Executive Compliance Audit Report downloads as a PDF.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import plotly.graph_objects as go
from dash import Input, Output, State, ctx, dcc, html, no_update

from ..alerts.repository import AnalyticsRepository, AnalyticsUnavailable
from ..alerts.settings import CATEGORIES, CATEGORY_LABELS, AlertSettingsStore
from ..alerts.ui import COLORS, FONT, INK, _cat, _compact, _notice, _section
from ..policies.catalog import load_catalog
from . import metrics
from .report import build_report

PAGE_ID = "rv-compliance-page"
PAGE = "compliance"
REGULATION_ID = "ny-stop-hiding-hate-act"
BASELINE_COLOR = "#7A7471"
FLAGGED_COLOR = "#C81E1E"
GRID = "rgba(19,18,18,0.07)"
AXIS = "#D8D4D0"
COVERAGE_LIMIT = 15
N_CATS = len(CATEGORIES)

_DOWNLOAD = "Download Executive Compliance Audit Report"
_PREPARING = "Preparing..."

# Plain-language definitions, shown as ⓘ tooltips and in the glossary.
GLOSSARY = {
    "flagged": "A third-party post whose model score in at least one statutory category meets "
    "the Alerts candidate score (0.60 by default). A proposal, not a human finding.",
    "baseline": "Unflagged third-party posts from the same collection: the comparison group "
    "for what 'normal' reach looks like.",
    "reach": "Reach index = median views of flagged posts ÷ median views of unflagged posts. "
    "1.00× means equal reach; well below 1.00× would be consistent with down-ranking.",
    "share": "Share-velocity index = reposts per 1,000 views for flagged posts ÷ the same for "
    "unflagged posts. Above 1.00× means flagged content spreads faster per view.",
    "engagement": "Engagement index = (likes + reposts + replies) per view for flagged posts ÷ "
    "the same for unflagged posts.",
    "cv": "Coefficient of variation = standard deviation ÷ mean of the weekly flagged share. "
    "Rule of thumb: under 0.3 steady, 0.3–0.7 uneven, over 0.7 bursty.",
    "spike": "A week whose flagged share is more than two standard deviations above the "
    "average week.",
    "days": "Days between a post being published and the capture that found it still publicly "
    "available. Long times mean the post was not removed before capture.",
    "coverage": "How many of the four statutory categories (disinformation, racism, hate "
    "speech, harassment) an account's flagged posts span: 4/4 is the broadest pattern.",
}


def _tip(text: str) -> html.Span:
    """Keyboard-focusable ⓘ with a CSS tooltip; the label carries the text for screen readers."""
    return html.Span(
        "i", className="cp-tip", tabIndex=0, role="img", **{"data-tip": text, "aria-label": text}
    )


# id, title, subtitle, tooltip, how-to-read bullets (what it shows / how to read / what to look for)
CHARTS: list[dict[str, Any]] = [
    {
        "id": "cp-velocity",
        "title": "Velocity: flagged posts and views per week",
        "subtitle": "How much flagged content was published each week, and how many views it got.",
        "tip": "Bars count flagged posts by posting week and category (left axis). The line "
        "sums the views those posts had when captured (right axis).",
        "how": [
            ("Shows", "Weekly volume of flagged posts, split by category, with their total views."),
            ("Read it", "Tall bars = many flagged posts that week. A high line = they reached "
             "many people. Compare both: few posts can still carry huge reach."),
            ("Look for", "Sustained weekly activity rather than a single incident. Enforcement "
             "would show volume or views falling after peaks."),
        ],
    },
    {
        "id": "cp-volatility",
        "title": "Volatility: weekly flagged share with 4-week band",
        "subtitle": "Is flagged content a steady presence or a series of bursts?",
        "tip": GLOSSARY["cv"] + " " + GLOSSARY["spike"],
        "how": [
            ("Shows", "The percentage of each week's posts that were flagged, normalised so "
             "busier collection weeks do not look worse."),
            ("Read it", "The black line is each week. The shaded band is the recent normal "
             "(4-week average ± 1 standard deviation). Red diamonds are spike weeks."),
            ("Look for", "Spikes point to coordinated pushes worth examining first. A flat line "
             "means a constant share of flagged content, week after week."),
        ],
    },
    {
        "id": "cp-ecdf",
        "title": "Reach vs restriction: view distributions",
        "subtitle": "Do flagged posts reach fewer people than ordinary posts?",
        "tip": "Each curve shows, for any view count on the bottom axis, what share of posts "
        "got that many views or fewer. The dotted line marks the median (50%).",
        "how": [
            ("Shows", "All flagged posts (red) and all unflagged posts (grey), ordered by views."),
            ("Read it", "Follow the dotted 50% line to each curve: that point is the group's "
             "median views. The further left a curve sits, the less reach the group got."),
            ("Look for", "If restriction were applied, the red curve would sit far to the left "
             "of the grey one. Overlapping curves mean similar reach."),
        ],
    },
    {
        "id": "cp-reach-time",
        "title": "Reach over time: median views per week",
        "subtitle": "Week by week, did flagged posts get fewer views than unflagged ones?",
        "tip": "Median views of posts published in each week, flagged (red) and unflagged "
        "(grey). Log scale: each gridline is ten times the one below.",
        "how": [
            ("Shows", "The typical (median) reach of flagged and unflagged posts per week."),
            ("Read it", "When the red line is above the grey, flagged posts out-performed "
             "ordinary posts that week. The axis is logarithmic, so small gaps are large."),
            ("Look for", "A consistent gap below grey would be consistent with down-ranking; "
             "lines that cross or track together are not."),
        ],
    },
    {
        "id": "cp-index",
        "title": "Reach and engagement index by category",
        "subtitle": "Each category compared with ordinary posts, where 1.00× means equal.",
        "tip": GLOSSARY["reach"] + " " + GLOSSARY["engagement"],
        "how": [
            ("Shows", "Filled bars: reach relative to unflagged posts. Outlined bars: "
             "interactions per view relative to unflagged posts."),
            ("Read it", "The dashed line at 1.00× is parity. Left of it = less than ordinary "
             "posts; right of it = more."),
            ("Look for", "A 'Freedom of Reach' policy would push filled bars well left of "
             "1.00×. Outlined bars right of 1.00× mean audiences engaged more, not less."),
        ],
    },
    {
        "id": "cp-coverage",
        "title": "Accounts posting across harmful categories",
        "subtitle": "Accounts ranked by how many of the four categories their flagged posts "
        "span, starting with the highest threshold.",
        "tip": GLOSSARY["coverage"],
        "how": [
            ("Shows", "The accounts with the broadest harmful-content pattern. Each bar is one "
             "account; colours show how many flagged posts fall in each category."),
            ("Read it", "The label shows coverage, e.g. 3/4 = flagged in three categories. "
             "Use the selector to lower the threshold and include narrower patterns."),
            ("Look for", "Accounts at 3/4 or 4/4 with many posts are repeat, multi-category "
             "sources and the strongest candidates for account-level reporting."),
        ],
    },
    {
        "id": "cp-persistence",
        "title": "Platform reaction latency: days still live at capture",
        "subtitle": "How long flagged posts stayed publicly available.",
        "tip": GLOSSARY["days"],
        "how": [
            ("Shows", "A histogram of flagged posts by how many days they had been public when "
             "captured. Each bar covers two weeks."),
            ("Read it", "Bars far to the right = posts that stayed up for a long time. The "
             "dashed line is the median."),
            ("Look for", "Quick removal would mean few posts survive long. Posts removed before "
             "capture are not in the data, so this shows survival, not removal rate."),
        ],
    },
    {
        "id": "cp-periods",
        "title": "Reporting periods: flagged posts per month",
        "subtitle": "Flagged volume by statutory category in each month of the audit window.",
        "tip": "Grouped bars per month, labeled with the reporting quarter. Hover for the share "
        "of that month's third-party posts.",
        "how": [
            ("Shows", "Monthly flagged post counts per category, aligned to the quarters a "
             "platform reports on under GBS § 1102."),
            ("Read it", "Compare months: rising or steady bars mean the problem persisted "
             "across the reporting period."),
            ("Look for", "Compare these counts with the platform's filed figures for the same "
             "quarter."),
        ],
    },
    {
        "id": "cp-bursts",
        "title": "Harassment: burst campaigns",
        "subtitle": "Many posts or replies by one account at one target in a short window.",
        "tip": "Detected by the analytics pipeline: at least 4 posts within 60 minutes, or 6 "
        "replies within 24 hours, by one account aimed at one target. Bubble size = posts.",
        "how": [
            ("Shows", "Each bubble is a burst: when it started (across) and how many posts it "
             "contained (up)."),
            ("Read it", "Hover a bubble for the account, target, replies and duration."),
            ("Look for", "Repeated bursts by the same account. A burst is a volume pattern; "
             "check the posts for abusive content before calling it harassment."),
        ],
    },
]  # fmt: skip
CHART_IDS = [c["id"] for c in CHARTS]


# --------------------------------------------------------------------------- figures


def _layout(fig: go.Figure, *, height: int = 300, legend: bool = True) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=48, r=16, t=8, b=36),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT, color=INK, size=13),
        showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title=None),
        hoverlabel=dict(font=dict(family=FONT)),
    )
    fig.update_xaxes(showgrid=False, linecolor=AXIS, ticks="outside", tickcolor=AXIS)
    fig.update_yaxes(gridcolor=GRID, zeroline=False)
    return fig


def _empty(message: str = "No data for this subject.") -> go.Figure:
    fig = _layout(go.Figure(), legend=False)
    fig.add_annotation(text=message, showarrow=False, x=0.5, y=0.5, xref="paper", yref="paper",
                       font=dict(color=BASELINE_COLOR))  # fmt: skip
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return fig


def velocity_figure(weeks: list[dict]) -> go.Figure:
    if not any(w["flagged"] for w in weeks):
        return _empty("No flagged posts in this collection.")
    fig = go.Figure()
    x = [w["week"] for w in weeks]
    for c in CATEGORIES:
        if any(w[c] for w in weeks):
            fig.add_bar(
                x=x,
                y=[w[c] for w in weeks],
                name=CATEGORY_LABELS[c],
                marker_color=COLORS[c],
                hovertemplate="%{y} posts<extra>" + CATEGORY_LABELS[c] + "</extra>",
            )
    fig.add_scatter(
        x=x,
        y=[w["flagged_views"] for w in weeks],
        name="Views on flagged posts",
        yaxis="y2",
        mode="lines+markers",
        line=dict(color=INK, width=2),
        marker=dict(size=5),
        hovertemplate="%{y:,.0f} views<extra></extra>",
    )
    _layout(fig, height=320)
    fig.update_layout(
        barmode="stack",
        hovermode="x unified",
        yaxis=dict(title="Flagged posts", rangemode="tozero", tickformat="d"),
        yaxis2=dict(
            title="Views",
            overlaying="y",
            side="right",
            showgrid=False,
            rangemode="tozero",
            tickformat=".2~s",
        ),
        margin=dict(l=56, r=56, t=8, b=36),
    )
    fig.update_xaxes(title="Posting week (UTC, Monday start)")
    return fig  # fmt: skip


def volatility_figure(weeks: list[dict]) -> go.Figure:
    weeks = [w for w in weeks if w["posts"]]
    if len(weeks) < 2:
        return _empty("Not enough weeks to measure volatility.")
    x = [w["week"] for w in weeks]
    upper = [100 * (w["rolling_mean"] + w["rolling_sd"]) for w in weeks]
    lower = [100 * max(0.0, w["rolling_mean"] - w["rolling_sd"]) for w in weeks]
    fig = go.Figure()
    fig.add_scatter(
        x=x + x[::-1],
        y=upper + lower[::-1],
        fill="toself",
        name="4-week mean ± 1σ",
        fillcolor="rgba(3,128,149,0.14)",
        line=dict(width=0),
        hoverinfo="skip",
    )
    fig.add_scatter(
        x=x,
        y=[100 * w["rolling_mean"] for w in weeks],
        name="4-week mean",
        mode="lines",
        line=dict(color="#038095", width=2, dash="dot"),
        hovertemplate="%{y:.1f}%<extra>4-week mean</extra>",
    )
    fig.add_scatter(
        x=x,
        y=[100 * w["rate"] for w in weeks],
        name="Weekly flagged share",
        mode="lines+markers",
        line=dict(color=INK, width=2),
        marker=dict(size=5),
        customdata=[[w["flagged"], w["posts"]] for w in weeks],
        hovertemplate="%{y:.1f}% (%{customdata[0]} of %{customdata[1]} posts)<extra></extra>",
    )
    spikes = [w for w in weeks if w["spike"]]
    if spikes:
        fig.add_scatter(
            x=[w["week"] for w in spikes],
            y=[100 * w["rate"] for w in spikes],
            name="Spike week (> mean + 2σ)",
            mode="markers",
            marker=dict(
                color=FLAGGED_COLOR,
                size=12,
                symbol="diamond",
                line=dict(color="#FDFCFD", width=1.5),
            ),
            hovertemplate="Spike: %{y:.1f}%<extra></extra>",
        )
    _layout(fig, height=320)
    fig.update_layout(hovermode="x unified")
    fig.update_yaxes(title="Flagged share of posts (%)", rangemode="tozero", ticksuffix="%")
    fig.update_xaxes(title="Posting week (UTC)")
    return fig  # fmt: skip


def ecdf_figure(samples: dict[str, list[float]]) -> go.Figure:
    flagged, baseline = samples["flagged"], samples[metrics.BASELINE]
    if not flagged or not baseline:
        return _empty("Both flagged and unflagged posts with recorded views are needed.")
    fig = go.Figure()
    for values, name, color in (
        (baseline, "Unflagged baseline", BASELINE_COLOR),
        (flagged, "Flagged posts", FLAGGED_COLOR),
    ):
        xs, ys = metrics.ecdf([max(v, 1.0) for v in values])  # log axis: floor zero views at 1
        fig.add_scatter(
            x=xs,
            y=[100 * y for y in ys],
            name=f"{name} (n={len(values):,})",
            mode="lines",
            line=dict(color=color, width=2.5, shape="hv"),
            hovertemplate="%{y:.0f}% of posts had ≤ %{x:,.0f} views<extra></extra>",
        )
    fig.add_hline(
        y=50,
        line=dict(color=AXIS, dash="dot"),
        annotation_text="median",
        annotation_position="top left",
    )
    _layout(fig, height=320)
    fig.update_xaxes(type="log", title="Recorded views (log scale)")
    fig.update_yaxes(title="Share of posts (%)", range=[0, 100], ticksuffix="%")
    return fig  # fmt: skip


def reach_time_figure(weeks: list[dict]) -> go.Figure:
    pts = [w for w in weeks if w["median_flagged_views"] is not None]
    if not pts:
        return _empty("No flagged posts with recorded views.")
    fig = go.Figure()
    fig.add_scatter(
        x=[w["week"] for w in weeks],
        y=[w["median_baseline_views"] for w in weeks],
        name="Unflagged median",
        mode="lines+markers",
        line=dict(color=BASELINE_COLOR, width=2),
        marker=dict(size=5),
        connectgaps=True,
        hovertemplate="%{y:,.0f} views<extra>Unflagged</extra>",
    )
    fig.add_scatter(
        x=[w["week"] for w in pts],
        y=[w["median_flagged_views"] for w in pts],
        name="Flagged median",
        mode="lines+markers",
        line=dict(color=FLAGGED_COLOR, width=2),
        marker=dict(size=6),
        customdata=[w["flagged"] for w in pts],
        hovertemplate="%{y:,.0f} views (%{customdata} posts)<extra>Flagged</extra>",
    )
    _layout(fig, height=320)
    fig.update_layout(hovermode="x unified")
    fig.update_yaxes(type="log", title="Median recorded views (log)")
    fig.update_xaxes(title="Posting week (UTC)")
    return fig  # fmt: skip


def index_figure(matrix: list[dict]) -> go.Figure:
    rows = [r for r in matrix if r["posts"]]
    if not rows:
        return _empty("No flagged categories.")
    labels = [CATEGORY_LABELS[r["category"]] for r in rows]
    fig = go.Figure()
    fig.add_bar(
        y=labels,
        x=[r["reach_index"] for r in rows],
        orientation="h",
        name="Reach index (median views)",
        marker_color=[COLORS[r["category"]] for r in rows],
        text=[_fmt_index(r["reach_index"]) for r in rows],
        textposition="outside",
        hovertemplate="%{x:.2f}× the median views of unflagged posts<extra></extra>",
    )
    fig.add_bar(
        y=labels,
        x=[r["engagement_index"] for r in rows],
        orientation="h",
        name="Engagement index (interactions per view)",
        marker=dict(color="rgba(0,0,0,0)", line=dict(color=INK, width=1.5)),
        text=[_fmt_index(r["engagement_index"]) for r in rows],
        textposition="outside",
        hovertemplate="%{x:.2f}× the engagement rate of unflagged posts<extra></extra>",
    )
    fig.add_vline(
        x=1,
        line=dict(color=INK, dash="dash"),
        annotation_text="1.00× = same as unflagged",
        annotation_position="top",
    )
    _layout(fig, height=100 + 70 * len(rows))
    fig.update_layout(
        barmode="group",
        bargap=0.3,
        margin=dict(l=110, r=56, t=64, b=36),
        legend=dict(orientation="h", yanchor="bottom", y=1.16, x=0),
    )
    peak = max(
        [1.2]
        + [v for r in rows for v in (r["reach_index"], r["engagement_index"]) if v is not None]
    )
    fig.update_xaxes(
        range=[0, peak * 1.2], title="× unflagged median", showgrid=True, gridcolor=GRID
    )
    fig.update_yaxes(autorange="reversed")
    return fig  # fmt: skip


def coverage_figure(accounts: list[dict], minimum: int) -> go.Figure:
    rows = [a for a in accounts if a["coverage"] >= minimum][:COVERAGE_LIMIT]
    if not rows:
        return _empty(f"No account has flagged posts in {minimum} or more of {N_CATS} categories.")
    labels = [f"@{a['handle'] or 'unknown'}  ·  {a['coverage']}/{N_CATS}" for a in rows]
    custom = [[a["posts"], _compact(a["views"]), _compact(a["followers"]), a["coverage"]]
              for a in rows]  # fmt: skip
    fig = go.Figure()
    for c in CATEGORIES:
        if any(a[c] for a in rows):
            fig.add_bar(y=labels, x=[a[c] for a in rows], orientation="h",
                        name=CATEGORY_LABELS[c], marker_color=COLORS[c], customdata=custom,
                        hovertemplate=f"%{{x}} {CATEGORY_LABELS[c].lower()} posts<br>"
                        "%{customdata[0]} flagged posts · %{customdata[1]} views · "
                        "%{customdata[2]} followers<extra>%{customdata[3]}/" + str(N_CATS)
                        + " categories</extra>")  # fmt: skip
    _layout(fig, height=90 + 30 * len(rows))
    fig.update_layout(barmode="stack", bargap=0.3, margin=dict(l=210, r=24, t=8, b=36))
    fig.update_xaxes(title="Flagged posts (a post can count in more than one category)",
                     showgrid=True, gridcolor=GRID, tickformat="d")  # fmt: skip
    fig.update_yaxes(autorange="reversed")
    return fig


def persistence_figure(days: list[float]) -> go.Figure:
    if not days:
        return _empty("No flagged posts with posting and capture times.")
    fig = go.Figure()
    fig.add_histogram(
        x=days,
        xbins=dict(size=14),
        marker_color=FLAGGED_COLOR,
        marker_line=dict(color="#FDFCFD", width=1),
        name="Flagged posts",
        hovertemplate="%{x} days live: %{y} posts<extra></extra>",
    )
    med = metrics.median(days)
    fig.add_vline(
        x=med,
        line=dict(color=INK, dash="dash"),
        annotation_text=f"median {med:.0f} days",
        annotation_position="top right",
    )
    _layout(fig, height=300, legend=False)
    fig.update_xaxes(title="Days between posting and capture (still publicly available)")
    fig.update_yaxes(title="Flagged posts", tickformat="d")
    return fig  # fmt: skip


def periods_figure(periods: list[dict]) -> go.Figure:
    if not periods:
        return _empty()
    fig = go.Figure()
    x = [p["period"] for p in periods]
    for c in CATEGORIES:
        if any(p[c] for p in periods):
            fig.add_bar(
                x=x,
                y=[p[c] for p in periods],
                name=CATEGORY_LABELS[c],
                marker_color=COLORS[c],
                text=[p[c] or "" for p in periods],
                textposition="outside",
                customdata=[100 * p[c] / p["posts"] if p["posts"] else 0 for p in periods],
                hovertemplate="%{y} posts (%{customdata:.1f}% of the month's posts)"
                "<extra>" + CATEGORY_LABELS[c] + "</extra>",
            )
    _layout(fig, height=300)
    fig.update_layout(barmode="group", bargap=0.25)
    fig.update_xaxes(type="category")
    fig.update_yaxes(title="Flagged posts", rangemode="tozero", tickformat="d")
    return fig  # fmt: skip


def bursts_figure(bursts: list[dict]) -> go.Figure:
    pts = [b for b in bursts if b.get("window_start")]
    if not pts:
        return _empty("No harassment bursts were detected in this collection.")
    fig = go.Figure()
    fig.add_scatter(
        x=[b["window_start"] for b in pts],
        y=[b.get("post_count") or 0 for b in pts],
        mode="markers",
        marker=dict(size=[10 + 4 * math.sqrt(b.get("post_count") or 0) for b in pts],
                    color=COLORS["harassment"], opacity=0.8, line=dict(color="#FDFCFD", width=1)),
        customdata=[[f"@{b.get('author_handle') or 'unknown'}", b.get("target") or "unknown",
                     b.get("reply_count") or 0, _burst_minutes(b)] for b in pts],
        hovertemplate="%{customdata[0]} → %{customdata[1]}<br>%{y} posts, %{customdata[2]} "
                      "replies in %{customdata[3]} min<extra></extra>",
    )  # fmt: skip
    _layout(fig, height=300, legend=False)
    fig.update_yaxes(title="Posts in burst", rangemode="tozero", tickformat="d")
    fig.update_xaxes(title="Burst start (UTC)")
    return fig


def _burst_minutes(b: dict) -> int:
    if not (b.get("window_end") and b.get("window_start")):
        return 0
    return round((b["window_end"] - b["window_start"]).total_seconds() / 60)


# --------------------------------------------------------------------------- takeaways


def _fmt_index(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.2f}×"


def _fmt_pct(v: float | None, digits: int = 1) -> str:
    return "n/a" if v is None else f"{100 * v:.{digits}f}%"


def _fmt_date(ts) -> str:
    return ts.strftime("%d %b %Y") if ts else "unknown"


def _cv_word(cv: float | None) -> str:
    if cv is None:
        return "not measurable"
    return "steady" if cv < 0.3 else "uneven" if cv <= 0.7 else "bursty"


def takeaways(audit: metrics.Audit, s: dict, matrix: list[dict], weeks: list[dict],
              coverage: list[dict]) -> dict[str, str]:  # fmt: skip
    """One plain-language sentence per chart, computed from the same data the chart shows."""
    out: dict[str, str] = {}
    active = [w for w in weeks if w["posts"]]
    peak = max(active, key=lambda w: w["flagged"], default=None)
    if peak and peak["flagged"]:
        out["cp-velocity"] = (
            f"Flagged posts appeared in {sum(1 for w in active if w['flagged'])} of {len(active)} "
            f"weeks. The busiest week ({_fmt_date(peak['week'])}) had {peak['flagged']} flagged "
            f"posts with {_compact(peak['flagged_views'])} views."
        )
    cv = s["volatility_cv"]
    if cv is not None:
        spikes = [w for w in active if w["spike"]]
        out["cp-volatility"] = (
            f"Volatility is {cv:.2f}: {_cv_word(cv)}. On average "
            f"{_fmt_pct(s['flagged_share'])} of posts were flagged each week"
            + (f"; spike week{'s' if len(spikes) > 1 else ''}: "
               f"{', '.join(_fmt_date(w['week']) for w in spikes)}." if spikes
               else "; no week stood out as a spike.")
        )  # fmt: skip
    samples = metrics.view_samples(audit)
    if samples["flagged"] and s["median_views_baseline"] is not None:
        below = sum(1 for v in samples["flagged"] if v <= s["median_views_baseline"])
        share = below / len(samples["flagged"])
        out["cp-ecdf"] = (
            f"{_fmt_pct(share, 0)} of flagged posts got no more views than the typical unflagged "
            "post (50% would mean identical reach). The curves "
            + ("largely overlap: little sign of restricted reach."
               if 0.4 <= share <= 0.65 else
               "are clearly apart: flagged posts reached "
               + ("fewer" if share > 0.65 else "more") + " people.")
        )  # fmt: skip
    both = [w for w in weeks if w["median_flagged_views"] is not None
            and w["median_baseline_views"] is not None]  # fmt: skip
    if both:
        above = sum(1 for w in both if w["median_flagged_views"] >= w["median_baseline_views"])
        out["cp-reach-time"] = (
            f"Flagged posts matched or beat unflagged reach in {above} of {len(both)} weeks."
        )
    flagged_rows = [r for r in matrix if r["posts"] and r["reach_index"] is not None]
    if flagged_rows:
        low = min(flagged_rows, key=lambda r: r["reach_index"])
        lo, hi = low["reach_index"], max(r["reach_index"] for r in flagged_rows)
        eng = [r["engagement_index"] for r in flagged_rows if r["engagement_index"] is not None]
        text = f"Reach ranges from {lo:.2f}× to {hi:.2f}× of unflagged posts" + (
            f", while engagement per view is {min(eng):.2f}×–{max(eng):.2f}×" if eng else ""
        )
        if lo >= 0.75:
            text += ". Every category is close to parity: no strong sign of down-ranking."
        else:
            text += (
                f". {CATEGORY_LABELS[low['category']]} reached about {lo:.0%} of typical reach; "
                "that could reflect down-ranking or smaller audiences, and needs repeat "
                "captures or like-for-like accounts to tell apart."
            )
        out["cp-index"] = text
    tiers = metrics.coverage_tiers(coverage)
    if coverage:
        top = max(k for k, n in tiers.items() if n)
        lead = coverage[0]
        out["cp-coverage"] = (
            " · ".join(f"{k}/{N_CATS}: {n} account{'s' if n != 1 else ''}"
                       for k, n in tiers.items() if n)
            + f". Highest threshold reached: {top}/{N_CATS}, led by @{lead['handle']} "
            f"({lead['posts']} flagged posts)."
            + ("" if top == N_CATS else
               " No account reached 4/4 because "
               + ", ".join(CATEGORY_LABELS[r["category"]].lower() for r in matrix
                           if not r["posts"]) + " had no flagged posts."
               if any(not r["posts"] for r in matrix) else "")
        )  # fmt: skip
    if s["median_days_live"] is not None:
        out["cp-persistence"] = (
            f"All {s['flagged']:,} flagged posts were still public at capture: a median of "
            f"{s['median_days_live']:.0f} days after posting, and never fewer than "
            f"{s['min_days_live']:.0f} days."
        )
    periods = metrics.periods(audit)
    if periods:
        busiest = max(periods, key=lambda p: sum(p[c] for c in CATEGORIES))
        out["cp-periods"] = (
            f"{len(periods)} month{'s' if len(periods) != 1 else ''} with third-party posts; "
            f"most flagged: {busiest['period']}."
        )
    if audit.bursts:
        big = max(audit.bursts, key=lambda b: b.get("post_count") or 0)
        out["cp-bursts"] = (
            f"{len(audit.bursts)} bursts ({s['burst_posts']} posts) by "
            f"{len({b.get('author_handle') for b in audit.bursts})} accounts. Largest: "
            f"@{big.get('author_handle')} with {big.get('post_count')} posts in "
            f"{_burst_minutes(big)} minutes."
        )
    return out


# --------------------------------------------------------------------------- page parts


def _stat(label: str, value: str, unit: str, kind: str = "", tip: str = "") -> html.Div:
    return html.Div(
        [html.Span([label, _tip(tip)] if tip else label, className="al-stat-label"),
         html.Strong(value, className="al-stat-value"),
         html.Span(unit, className="al-stat-unit")],
        className=f"al-stat {('al-stat--' + kind) if kind else ''}".strip(),
    )  # fmt: skip


def _summary(case: dict, s: dict) -> html.Div:
    days = s["median_days_live"]
    stats = [
        _stat("Flagged posts", f"{s['flagged']:,}",
              f"{_fmt_pct(s['flagged_share'])} of {s['posts']:,} third-party posts", "red",
              GLOSSARY["flagged"]),
        _stat("Accounts behind them", f"{s['accounts']:,}", "distinct authors", "amber",
              "Distinct accounts that published at least one flagged post."),
        _stat("Views on flagged posts", _compact(s["flagged_views"]),
              f"recorded at capture · {s['views_unknown']} unknown", "",
              "Total views X reported for the flagged posts at the moment they were captured."),
        _stat("Median days still live", "n/a" if days is None else f"{days:.0f}",
              f"minimum {s['min_days_live']:.0f} days" if s["min_days_live"] is not None else "",
              "red", GLOSSARY["days"]),
        _stat("Reach index", _fmt_index(s["reach_index"]),
              "flagged ÷ unflagged median views", "", GLOSSARY["reach"]),
        _stat("Share-velocity index", _fmt_index(s["share_index"]),
              "flagged ÷ unflagged reposts per 1K views", "", GLOSSARY["share"]),
        _stat("Volatility (CV)", "n/a" if s["volatility_cv"] is None
              else f"{s['volatility_cv']:.2f}", _cv_word(s["volatility_cv"]) + " week to week",
              "", GLOSSARY["cv"]),
        _stat("Spike weeks", str(s["spike_weeks"]), "flagged share > mean + 2σ", "amber",
              GLOSSARY["spike"]),
    ]  # fmt: skip
    capture = _fmt_date(s["capture_start"])
    if (
        s["capture_end"]
        and s["capture_start"]
        and s["capture_start"].date() != s["capture_end"].date()
    ):
        capture += f" – {_fmt_date(s['capture_end'])}"
    return html.Div(
        [
            html.Div(stats, className="al-stats"),
            html.P(
                [html.Strong(case["subject_name"]),
                 f" · posts dated {_fmt_date(s['posted_start'])} – {_fmt_date(s['posted_end'])}"
                 f" · captured {capture} · {s['excluded']:,} subject-owned, affiliated or "
                 "unclassified posts excluded from both groups."],
                className="al-coverage",
            ),
        ],
        className="al-summary",
    )  # fmt: skip


def _guide() -> html.Details:
    steps = [
        ("Two groups", "Every chart compares flagged posts (red) with unflagged posts from the "
         "same collection (grey). The unflagged group shows what normal reach looks like."),
        ("What enforcement would look like", "If X restricted violating content as its "
         "'Freedom of Reach' policy states, flagged posts would get clearly fewer views and "
         "shares than unflagged ones, and would disappear quickly."),
        ("Index values", "1.00× means flagged and unflagged posts perform the same. Below 1.00× "
         "= flagged content did worse; above = it did better."),
        ("Velocity and volatility", "Velocity is how much flagged content was published and "
         "viewed per week. Volatility is whether that came steadily or in bursts."),
        ("Limits", "Flags are model proposals. One capture cannot show removals or reach over "
         "time; see 'What this data cannot show' before citing a number."),
    ]  # fmt: skip
    return html.Details(
        [
            html.Summary([html.Span("How to read this page", className="al-h2"),
                          html.Span("Start here · two minutes", className="al-sub")],
                         className="al-settings-summary"),
            html.Ol([html.Li([html.Strong(t + ". "), d]) for t, d in steps], className="cp-steps"),
            html.Dl([item for key, label in (
                ("flagged", "Flagged"), ("baseline", "Baseline"), ("reach", "Reach index"),
                ("share", "Share-velocity index"), ("engagement", "Engagement index"),
                ("cv", "Volatility (CV)"), ("spike", "Spike week"), ("days", "Days live"),
                ("coverage", "Category coverage"))
                for item in (html.Dt(label), html.Dd(GLOSSARY[key]))], className="cp-glossary"),
        ],
        className="al-card cp-guide",
        open=True,
    )  # fmt: skip


_MATRIX_HEAD = [
    ("Category (GBS § 1102(1)(c))", "Statutory category the model score was assessed against."),
    ("Flagged posts", GLOSSARY["flagged"]),
    ("Accounts", "Distinct accounts with a flagged post in this category."),
    ("Total views", "Sum of recorded views at capture."),
    ("Median views", "The middle value: half of the category's posts had more views."),
    ("Peak views", "Views on the category's most-viewed post."),
    ("Reach index", GLOSSARY["reach"]),
    ("Engagement index", GLOSSARY["engagement"]),
    ("Median days live", GLOSSARY["days"]),
]


def _matrix_table(matrix: list[dict]) -> html.Div:
    rows = [
        html.Tr([
            html.Td(_cat(r["category"])),
            html.Td(f"{r['posts']:,}", className="al-num"),
            html.Td(f"{r['accounts']:,}", className="al-num"),
            html.Td(_compact(r["views"]), className="al-num"),
            html.Td(_compact(r["median_views"]), className="al-num"),
            html.Td(_compact(r["max_views"]), className="al-num"),
            html.Td(_fmt_index(r["reach_index"]), className="al-num"),
            html.Td(_fmt_index(r["engagement_index"]), className="al-num"),
            html.Td("n/a" if r["median_days_live"] is None else f"{r['median_days_live']:.0f}",
                    className="al-num"),
        ])
        for r in matrix
    ]  # fmt: skip
    head = html.Tr([html.Th([h, _tip(t)]) for h, t in _MATRIX_HEAD])
    return html.Div(
        html.Table([html.Thead(head), html.Tbody(rows)], className="al-table"),
        className="al-table-wrap",
    )


def _accounts_table(accounts: list[dict]) -> Any:
    if not accounts:
        return html.P("No flagged accounts.", className="al-empty")
    rows = [
        html.Tr([
            html.Td(html.Strong(f"@{a['handle'] or 'unknown'}")),
            html.Td(_compact(a["followers"]), className="al-num"),
            html.Td("Yes" if a["verified"] else "No"),
            html.Td(f"{a['posts']:,}", className="al-num"),
            html.Td(_compact(a["views"]), className="al-num"),
            html.Td([_cat(c) for c in a["categories"]], className="al-cats"),
        ])
        for a in accounts
    ]  # fmt: skip
    head = ("Account", "Followers", "Verified", "Flagged posts", "Views", "Categories")
    return html.Div(
        html.Table([html.Thead(html.Tr([html.Th(h) for h in head])), html.Tbody(rows)],
                   className="al-table"),
        className="al-table-wrap",
    )  # fmt: skip


def _gaps(s: dict, matrix: list[dict], case: dict | None = None) -> html.Ul:
    harassment = next((r for r in matrix if r["category"] == "harassment"), {"posts": 0})
    items = []
    window_end = (case or {}).get("window_end")
    as_date = lambda v: v.date() if isinstance(v, datetime) else v  # noqa: E731
    if window_end and s["posted_end"] and (as_date(window_end) - as_date(s["posted_end"])).days > 7:
        items.append(
            f"Coverage: the case window runs to {_fmt_date(window_end)}, but third-party posts "
            f"in this collection end on {_fmt_date(s['posted_end'])}. Later periods cannot be "
            "assessed until they are collected."
        )
    items += [
        "Single capture: each post was collected once, so reach is cumulative at capture. "
        "Reach decay over time (de-amplification after flagging) cannot be measured; "
        "repeat captures of the same posts are needed.",
        "Removals are not observable: posts removed before capture are absent, so every "
        "captured post was available by construction. Count removals from the platform's own "
        "filings or from repeat captures.",
        "No visibility-limited or restriction label is recorded in the captured records.",
        "Monetization: advertising placement beside posts is not captured in this database.",
        f"Archive copies: {s['archived']} of {s['flagged']} flagged posts have an "
        "independent archive URL. Preserve copies before filing.",
        "Model flags are proposals at the Alerts candidate score; each post needs human "
        "review against the statutory definition before it is cited.",
        "Reach is confounded by audience size: compare like accounts before attributing a "
        "difference to enforcement.",
    ]
    if not harassment["posts"]:
        items.append(
            f"Harassment: no post reached the candidate score; the {s['bursts']} detected "
            "bursts are the volume evidence for this category."
        )
    return html.Ul([html.Li(i) for i in items], className="cp-gaps")


def _legal_basis() -> html.Div:
    reg = load_catalog().regulations.get(REGULATION_ID)
    if not reg:
        return html.P("The regulation entry is missing from policy-guidelines/regulations.",
                      className="al-muted")  # fmt: skip
    return html.Div(
        [
            html.P([html.Strong(reg["title"]), f" · {reg.get('citation', '')}"]),
            html.P(reg["summary"], className="al-muted"),
            html.Ul([html.Li(r) for r in reg.get("requirements", [])], className="cp-gaps"),
            html.P([html.Strong("Penalty: "), reg.get("penalties", "")]),
            _notice(reg["status_note"], "warning") if reg.get("status_note") else None,
            html.P(
                "FTC Act § 5 (15 U.S.C. § 45): a complaint compares the platform's stated "
                "enforcement (e.g. reduced reach for violating posts) with the measured reach "
                "of flagged content below.",
                className="al-muted",
            ),
        ],
        className="cp-legal",
    )


def _how(points: list[tuple[str, str]]) -> html.Dl:
    return html.Dl(
        [item for label, text in points for item in (html.Dt(label), html.Dd(text))],
        className="cp-how",
    )


def _graph_style(fig: go.Figure) -> dict:
    """Size the graph box to the figure; a responsive graph otherwise overflows its card."""
    return {"height": f"{fig.layout.height or 300}px", "width": "100%"}


def _chart(spec: dict, controls=None) -> html.Section:
    cid = spec["id"]
    body = html.Div(
        [
            _how(spec["how"]),
            controls,
            dcc.Graph(id=cid, config={"displayModeBar": False}, responsive=True,
                      className="al-chart cp-chart", figure=_empty("Loading…"),
                      style={"height": "320px", "width": "100%"}),
            html.P([html.Strong("In this data: "), html.Span(id=f"{cid}-takeaway")],
                   className="cp-takeaway", **{"aria-live": "polite"}),
        ]
    )  # fmt: skip
    return _section([spec["title"], _tip(spec["tip"])], spec["subtitle"], body,
                    section_id=f"{cid}-section")  # fmt: skip


def _coverage_controls() -> html.Div:
    return html.Div(
        [
            html.Label("Minimum categories per account", htmlFor="cp-coverage-min",
                       className="al-label"),
            dcc.RadioItems(id="cp-coverage-min", options=[], value=None, inline=True,
                           className="cp-radio", inputClassName="cp-radio-input",
                           labelClassName="cp-radio-label"),
        ],
        className="cp-controls",
    )  # fmt: skip


def compliance_page() -> html.Section:
    charts = {c["id"]: c for c in CHARTS}
    chart = lambda cid, controls=None: _chart(charts[cid], controls)  # noqa: E731
    return html.Section(
        html.Div(
            [
                dcc.Download(id="cp-download"),
                html.Header(
                    [
                        html.Div(
                            [
                                html.Span("Regulatory evidence", className="al-eyebrow"),
                                html.H1("Compliance audit", className="al-title"),
                                html.P(
                                    "Enforcement metrics for a takedown or compliance filing: "
                                    "how much flagged content circulated, how fast, and whether "
                                    "its reach differs from unflagged content.",
                                    className="al-lede",
                                ),
                            ],
                            className="al-top-copy",
                        ),
                        html.Div(
                            [
                                html.Label("Subject", htmlFor="cp-case", className="al-label"),
                                html.Div(
                                    [
                                        dcc.Dropdown(id="cp-case", options=[], value=None,
                                                     clearable=False, searchable=False,
                                                     placeholder="Loading…",
                                                     className="al-dropdown"),
                                        html.Button("Refresh", id="cp-refresh", n_clicks=0,
                                                    className="al-btn al-btn--ghost-light"),
                                    ],
                                    className="al-top-row",
                                ),
                                html.Button(_DOWNLOAD, id="cp-download-btn", n_clicks=0,
                                            className="al-btn al-btn--primary cp-download-btn"),
                                html.Span(id="cp-download-message", className="cp-download-msg",
                                          **{"aria-live": "polite"}),
                            ],
                            className="al-top-controls",
                        ),
                    ],
                    className="al-top",
                ),
                dcc.Loading(html.Div(id="cp-summary"), type="dot", color="#038095"),
                _guide(),
                _section("Legal basis", "The statute and the enforcement claim being tested.",
                         _legal_basis(), section_id="cp-legal-section"),
                _section("Evidence matrix by statutory category",
                         "Flagged third-party posts per category, their reach, and reach "
                         "relative to the unflagged baseline. Hover ⓘ for definitions.",
                         html.Div(id="cp-matrix"), section_id="cp-matrix-section"),
                html.Div([chart("cp-velocity"), chart("cp-volatility")], className="cp-grid"),
                chart("cp-coverage", _coverage_controls()),
                html.Div([chart("cp-ecdf"), chart("cp-reach-time")], className="cp-grid"),
                chart("cp-index"),
                html.Div([chart("cp-persistence"), chart("cp-periods")], className="cp-grid"),
                chart("cp-bursts"),
                _section("Accounts with the most flagged reach",
                         "Top 10 authors by recorded views on flagged posts. Inspect each post "
                         "on the Alerts page before naming an account in a filing.",
                         html.Div(id="cp-accounts"), section_id="cp-accounts-section"),
                _section("What this data cannot show",
                         "Limits to state in any filing that cites these metrics.",
                         html.Div(id="cp-gaps"), section_id="cp-gaps-section"),
                html.P(
                    "Metrics are computed from model-labeled posts in the analytics database. "
                    "They are evidence for human review, not a finding that a law was violated.",
                    className="al-footnote",
                ),
            ],
            className="al-shell",
        ),
        id=PAGE_ID,
        className="rv-page-surface al-page",
        style={"display": "none"},
    )  # fmt: skip


# --------------------------------------------------------------------------- callbacks

# Figures rendered by the main callback; the coverage chart has its own threshold callback.
MAIN_CHARTS = [c for c in CHART_IDS if c != "cp-coverage"]


def _coverage_options(accounts: list[dict]) -> tuple[list[dict], int | None]:
    tiers = metrics.coverage_tiers(accounts)
    options, running = [], 0
    for k in range(N_CATS, 0, -1):
        running += tiers[k]
        options.append({"label": f"{k}/{N_CATS}+ ({running} accounts)", "value": k,
                        "disabled": running == 0})  # fmt: skip
    default = next((k for k in range(N_CATS, 0, -1) if tiers[k]), None)
    return options, default


def register_compliance_callbacks(app, service) -> None:
    store = AlertSettingsStore(service.settings.data_dir)
    repo = AnalyticsRepository()

    def _audit(case_id: str, refresh: bool = False):
        data = repo.case_data(case_id, refresh=refresh)
        settings, _ = store.load()
        return data, settings, metrics.build_audit(data.labels, settings, data.bursts)

    @app.callback(
        Output("cp-case", "options"),
        Output("cp-case", "value"),
        Input("rv-page", "value"),
        Input("cp-refresh", "n_clicks"),
        State("cp-case", "value"),
    )
    def load_cases(page, _refresh, current):
        if page != PAGE:
            return no_update, no_update
        try:
            cases = repo.list_cases()
        except AnalyticsUnavailable:
            return [], None
        options = [{"label": c["subject_name"], "value": c["id"]} for c in cases]
        values = {o["value"] for o in options}
        return options, current if current in values else (options[0]["value"] if options else None)

    outputs = ["cp-summary", "cp-matrix", "cp-accounts", "cp-gaps"]
    takeaway_ids = [f"{c}-takeaway" for c in CHART_IDS]

    @app.callback(
        *[Output(i, "children") for i in outputs],
        *[Output(i, "figure") for i in MAIN_CHARTS],
        *[Output(i, "style") for i in MAIN_CHARTS],
        *[Output(i, "children") for i in takeaway_ids],
        Output("cp-coverage-min", "options"),
        Output("cp-coverage-min", "value"),
        Input("cp-case", "value"),
        Input("cp-refresh", "n_clicks"),
        Input("rv-page", "value"),
    )
    def render(case_id, _refresh, page):
        n = len(outputs) + 2 * len(MAIN_CHARTS) + len(takeaway_ids) + 2
        if page != PAGE:
            return (no_update,) * n
        empty = _empty()
        blank = ("", "", "", *[empty] * len(MAIN_CHARTS), *[_graph_style(empty)] * len(MAIN_CHARTS),
                 *[""] * len(takeaway_ids), [], None)  # fmt: skip
        if not case_id:
            try:
                repo.list_cases()
                message = "No analytics subjects found in the database."
            except AnalyticsUnavailable as exc:
                message = str(exc)
            return (_notice(message, "warning"), *blank)
        try:
            data, _settings, audit = _audit(case_id, ctx.triggered_id == "cp-refresh")
        except AnalyticsUnavailable as exc:
            return (_notice(str(exc), "warning"), *blank)
        s = metrics.summary(audit)
        matrix = metrics.category_matrix(audit)
        weeks = metrics.weekly_series(audit)
        coverage = metrics.account_coverage(audit)
        notes = takeaways(audit, s, matrix, weeks, coverage)
        options, default = _coverage_options(coverage)
        figures = {
            "cp-velocity": velocity_figure(weeks),
            "cp-volatility": volatility_figure(weeks),
            "cp-ecdf": ecdf_figure(metrics.view_samples(audit)),
            "cp-reach-time": reach_time_figure(weeks),
            "cp-index": index_figure(matrix),
            "cp-persistence": persistence_figure(metrics.persistence(audit)),
            "cp-periods": periods_figure(metrics.periods(audit)),
            "cp-bursts": bursts_figure(audit.bursts),
        }
        return (
            _summary(data.case, s),
            _matrix_table(matrix),
            _accounts_table(metrics.top_accounts(audit)),
            _gaps(s, matrix, data.case),
            *[figures[c] for c in MAIN_CHARTS],
            *[_graph_style(figures[c]) for c in MAIN_CHARTS],
            *[notes.get(c, "Not enough data for a summary.") for c in CHART_IDS],
            options,
            default,
        )

    @app.callback(
        Output("cp-coverage", "figure"),
        Output("cp-coverage", "style"),
        Input("cp-coverage-min", "value"),
        Input("cp-coverage-min", "options"),
        State("cp-case", "value"),
        State("rv-page", "value"),
    )
    def render_coverage(minimum, _options, case_id, page):
        if page != PAGE:
            return no_update, no_update
        if not case_id or not minimum:
            fig = _empty("No account has a flagged post.")
        else:
            try:
                _data, _settings, audit = _audit(case_id)
                fig = coverage_figure(metrics.account_coverage(audit), int(minimum))
            except AnalyticsUnavailable as exc:
                fig = _empty(str(exc))
        return fig, _graph_style(fig)

    @app.callback(
        Output("cp-download", "data"),
        Output("cp-download-message", "children"),
        Input("cp-download-btn", "n_clicks"),
        State("cp-case", "value"),
        prevent_initial_call=True,
        running=[
            (Output("cp-download-btn", "disabled"), True, False),
            (Output("cp-download-btn", "children"), _PREPARING, _DOWNLOAD),
            (Output("cp-download-btn", "aria-busy"), "true", "false"),
        ],
    )
    def download(_clicks, case_id):
        if not case_id:
            return no_update, "Choose a subject first."
        try:
            data = repo.case_data(case_id, refresh=True)
            settings, _ = store.load()
            content, filename, digest = build_report(data, settings)
        except (AnalyticsUnavailable, ValueError) as exc:
            return no_update, str(exc)
        return dcc.send_bytes(content, filename), f"PDF SHA-256 {digest[:16]}…"
