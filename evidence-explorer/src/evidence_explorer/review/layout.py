"""Compose the shared review application without case-specific UI branches."""

from __future__ import annotations

from typing import Any

from dash import dcc, html

from .components import (
    assessment_header,
    brief_page,
    case_header,
    cases_page,
    compare_page,
    page_navigation,
    review_page,
)
from .graph import GRAPH_STYLE


def build_layout(cases: list[dict[str, Any]], session: dict[str, Any], error=None):
    """Build one stable component tree for text, behavior, and historical media."""
    return html.Div(
        [
            dcc.Store(id="rv-session", storage_type="session", data=session),
            dcc.Store(id="rv-click-intent", storage_type="memory"),
            dcc.Store(id="rv-draft-store", storage_type="session", data={}),
            dcc.Store(id="rv-scroll-marker", storage_type="memory"),
            dcc.Interval(id="rv-poll", interval=1200, disabled=True),
            html.A("Skip to review workspace", href="#rv-workspace", className="rv-skip"),
            case_header(cases, session),
            page_navigation("review" if cases else "cases"),
            html.Main(
                [
                    html.Div(id="rv-import-success", role="status", **{"aria-live": "polite"}),
                    assessment_header(),
                    html.Div(id="rv-next-action-slot"),
                    html.Div(id="rv-synthetic"),
                    html.Div(
                        error or "",
                        id="rv-action-message",
                        role="status",
                        **{"aria-live": "polite"},
                    ),
                    html.Div(id="rv-scenario-banner", className="rv-scenario-banner"),
                    cases_page(),
                    review_page(GRAPH_STYLE),
                    compare_page(),
                    brief_page(),
                    html.Footer(
                        "Trace the evidence. Make the uncertainty visible. Save your assessment.",
                        className="rv-footer",
                    ),
                ],
                className="rv-main",
            ),
        ],
        className="review-app traced-app",
    )
