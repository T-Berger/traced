"""Observed outcomes per category and X rule: did flagged posts remain, look restricted, or go?

One capture can show whether a post was still public and how far it reached; it cannot show
removals (removed posts are never collected) or a platform's internal reach decisions. So:

* Remained       – still publicly available at capture with ordinary reach for its audience.
* Low reach      – still available, but views per follower in the bottom 10% of unflagged
                   posts. A proxy for restricted reach: by construction 10% of unflagged posts
                   fall here, so a rule well above 10% is consistent with restriction.
* Removed        – not observable from one capture; always 0 here and labelled as such.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import plotly.graph_objects as go

from ..alerts.analysis import ROUTES, is_classified, is_third_party, route_for, violations
from ..alerts.settings import CATEGORY_LABELS, AlertSettings

REMAINED, LOW, REMOVED = "remained", "low_reach", "removed"
OUTCOME_LABELS = {
    REMAINED: "Remain (live, ordinary reach)",
    LOW: "Restrict? (live, low reach)",
    REMOVED: "Remove (not observable in one capture)",
}
OUTCOME_COLORS = {REMAINED: "#C81E1E", LOW: "#D9A21B", REMOVED: "#7A7471"}
LOW_PERCENTILE = 0.10
BASELINE_KEY = ("baseline", "baseline")
FONT = '"Trebuchet MS", "Lucida Grande", "Segoe UI", sans-serif'
INK = "#131212"

# Which analytics route (alerts.analysis.ROUTES) each X policy id corresponds to.
POLICY_ROUTE = {
    "civic-integrity": "disinformation",
    "manipulated-media": "manipulated_media",
    "impersonation": "impersonation",
    "hateful-conduct": "racism",  # racism and hate speech share X's Hateful conduct rule
    "abuse-harassment": "harassment",
}


@dataclass
class Row:
    category: str  # analytics category, or "baseline"
    policy_id: str
    label: str
    counts: dict[str, int]
    note: str = ""

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def _vpf(row: dict) -> float | None:
    views, followers = row.get("view_count"), row.get("followers_count")
    if views is None or not followers:
        return None
    return float(views) / float(followers)


def _threshold(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[max(0, int(len(values) * LOW_PERCENTILE) - 1)]


def _outcome(row: dict, cutoff: float | None) -> str:
    v = _vpf(row)
    return LOW if cutoff is not None and v is not None and v <= cutoff else REMAINED


def _empty_counts() -> dict[str, int]:
    return {REMAINED: 0, LOW: 0, REMOVED: 0}


def outcome_rows(
    labels: list[dict[str, Any]],
    bursts: list[dict[str, Any]],
    settings: AlertSettings,
    case: dict,
    policies: dict[str, dict],
) -> tuple[list[Row], float | None]:
    """One row per (category, rule) in the case guide, plus the unflagged baseline first."""
    third = [r for r in labels if is_classified(r) and is_third_party(r)]
    flagged = {r["id"]: violations(r, settings) for r in third}
    baseline = [r for r in third if not flagged[r["id"]]]
    cutoff = _threshold([v for r in baseline if (v := _vpf(r)) is not None])

    base_counts = _empty_counts()
    for r in baseline:
        base_counts[_outcome(r, cutoff)] += 1
    rows = [Row("baseline", "baseline", "Unflagged posts (baseline)", base_counts,
                "Reference: 10% sit in the low-reach band by definition.")]  # fmt: skip

    by_id = {r["id"]: r for r in labels}
    burst_ids = {str(i) for b in bursts for i in (b.get("item_ids") or [])}

    for cat in case.get("categories", []):
        c = cat["id"]
        ids = cat.get("x_rules") or [p for p, v in policies.items() if c in v.get("covers", [])]
        for pid in (i for i in ids if i in policies):
            title = policies[pid]["title"]
            label = f"{CATEGORY_LABELS.get(c, cat.get('name', c))} · {title}"
            counts, note = _empty_counts(), ""
            if c == "harassment":
                posts = [by_id[i] for i in burst_ids if i in by_id]
                note = "Posts in detected harassment bursts (no single post met the score)."
                if any(c in v for v in flagged.values()):
                    posts = [r for r in third if c in flagged[r["id"]]]
                    note = ""
            elif pid in POLICY_ROUTE:
                route = ROUTES[POLICY_ROUTE[pid]]["rule"]
                posts = [r for r in third if c in flagged[r["id"]]
                         and route_for(r, c)["rule"] == route]  # fmt: skip
            else:
                posts = []
                note = "No posts mapped: the classifier has no signal for this rule."
            for r in posts:
                counts[_outcome(r, cutoff)] += 1
            if not posts and not note:
                note = "No flagged posts routed to this rule."
            rows.append(Row(c, pid, label, counts, note))
    return rows, cutoff


def outcome_figure(rows: list[Row]) -> go.Figure:
    labels = [r.label for r in rows]
    fig = go.Figure()
    for key in (REMAINED, LOW, REMOVED):
        share = [100 * r.counts[key] / r.total if r.total else 0 for r in rows]
        fig.add_bar(
            y=labels,
            x=share,
            orientation="h",
            name=OUTCOME_LABELS[key],
            marker=dict(color=OUTCOME_COLORS[key], line=dict(color="#FDFCFD", width=1)),
            text=[f"{s:.0f}% ({r.counts[key]})" if r.total and s >= 6 else "" for s, r in
                  zip(share, rows, strict=True)],
            textposition="inside",
            insidetextanchor="middle",
            textfont=dict(color="#FDFCFD" if key != LOW else INK),
            customdata=[[r.counts[key], r.total] for r in rows],
            hovertemplate="%{customdata[0]} of %{customdata[1]} posts (%{x:.1f}%)<extra>"
            + OUTCOME_LABELS[key] + "</extra>",
        )  # fmt: skip
    # Rows with no posts: say so in place of an empty bar.
    for r in rows:
        if not r.total:
            fig.add_annotation(x=2, y=r.label, text=r.note or "No posts", showarrow=False,
                               xanchor="left", font=dict(size=12, color="#7A7471"))  # fmt: skip
        else:
            fig.add_annotation(x=101, y=r.label, text=f"n={r.total:,}", showarrow=False,
                               xanchor="left", font=dict(size=12, color="#7A7471"))  # fmt: skip
    fig.add_vline(x=100 * (1 - LOW_PERCENTILE), line=dict(color=INK, dash="dot", width=1))
    fig.update_layout(
        barmode="stack",
        height=90 + 44 * len(rows),
        margin=dict(l=320, r=64, t=8, b=40),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT, color=INK, size=13),
        legend=dict(orientation="h", yanchor="top", y=-0.12, x=0, title=None, traceorder="normal"),
        bargap=0.35,
    )
    fig.update_xaxes(range=[0, 100], ticksuffix="%", showgrid=True,
                     gridcolor="rgba(19,18,18,0.07)", title=None)  # fmt: skip
    fig.update_yaxes(autorange="reversed", automargin=False)
    return fig
