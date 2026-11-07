"""Read-only, explicit comparison of saved source text in the shared review desk."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from dash import Dash, Input, Output, State, ctx, dcc, html, no_update

from .service import ReviewService

OPTION_LIMIT = 100


def _text_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if isinstance(r.get("text"), str) and r["text"].strip()]


def default_pair(records, selected=None) -> tuple[str | None, str | None]:
    """Start with the selected text record and the next one in saved source order."""
    ids = [str(r["id"]) for r in _text_records(records)]
    if not ids:
        return None, None
    left = selected if selected in ids else ids[0]
    return left, ids[(ids.index(left) + 1) % len(ids)] if len(ids) > 1 else None


def source_options(records, search=None, selected=None) -> list[dict[str, str]]:
    """Bound search results, retain the selection, and preserve saved source order."""
    query = (search or "").strip().casefold()
    options = []
    matches = 0
    for index, record in enumerate(_text_records(records), start=1):
        rid = str(record["id"])
        title = record.get("metadata", {}).get("title")
        label = str(title or record.get("source_label") or rid)
        searchable = f"{label} {record.get('source_label', '')} {rid}".casefold()
        matched = query in searchable and matches < OPTION_LIMIT
        if matched:
            matches += 1
        if matched or rid == selected:
            options.append({"label": f"{index}. {label}", "value": rid})
    return options


def safe_source_url(value: str | None) -> str | None:
    """Only absolute HTTP(S) links may be opened from supplied source metadata."""
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
        ):
            return value
    except ValueError:
        pass
    return None


def source_comparison_panel() -> html.Section:
    return html.Section(
        [
            html.Div(
                [
                    html.H2("Compare saved sources", id="rv-source-heading"),
                    html.P(
                        "Choose two saved text sources to flag possible wording differences. "
                        "Flags need human review; they do not establish contradictions or truth.",
                        className="rv-small rv-muted",
                    ),
                ],
                className="rv-section-heading",
            ),
            dcc.Store(id="rv-source-case", data=None),
            html.Div(
                [
                    html.Div(
                        [
                            html.Label(f"Source {label}", htmlFor=f"rv-source-{side}"),
                            dcc.Dropdown(
                                id=f"rv-source-{side}",
                                options=[],
                                value=None,
                                placeholder="Choose a saved text source",
                                clearable=False,
                                searchable=True,
                            ),
                        ],
                        className="rv-source-picker",
                    )
                    for side, label in [("left", "A"), ("right", "B")]
                ],
                className="rv-source-panes",
            ),
            html.Div(id="rv-source-status", className="rv-small rv-muted", role="status"),
            html.Button(
                "Compare sources",
                id="rv-source-run",
                n_clicks=0,
                disabled=True,
                className="rv-button rv-button--primary",
            ),
            dcc.Loading(
                html.Div(id="rv-source-result", **{"aria-live": "polite"}),
                type="default",
            ),
        ],
        className="rv-source-comparison",
        **{"aria-labelledby": "rv-source-heading"},
    )


def _quote(passage: dict[str, Any]) -> html.Blockquote:
    quote, offset = passage["quote"], passage["start"]
    pieces: list[Any] = []
    cursor = 0
    for span in passage["highlights"]:
        if span["kind"] != "difference":
            continue
        start, end = span["start"] - offset, span["end"] - offset
        if not cursor <= start < end <= len(quote):
            continue
        pieces.extend([quote[cursor:start], html.Mark(quote[start:end])])
        cursor = end
    pieces.append(quote[cursor:])
    return html.Blockquote(pieces, className="rv-source-quote")


def _source_panel(source: dict[str, Any], coverage: dict[str, Any], label: str) -> html.Article:
    metadata = source["metadata"]
    url = safe_source_url(source.get("source_url"))
    fields = [
        ("Publisher", metadata.get("publisher")),
        ("Author", metadata.get("author")),
        (
            "Platform / channel",
            " · ".join(
                dict.fromkeys(
                    item
                    for item in (metadata.get("platform"), metadata.get("channel_name"))
                    if item
                )
            ),
        ),
        ("Published", source.get("published_at") or "Unknown"),
        ("Acquired", source.get("acquired_at") or "Unknown"),
    ]
    return html.Article(
        [
            html.Span(f"SOURCE {label}", className="rv-kicker"),
            html.H3(metadata.get("title") or source["source_label"]),
            html.P(source["source_label"], className="rv-small rv-muted")
            if metadata.get("title") and metadata["title"] != source["source_label"]
            else None,
            html.Span("Synthetic example", className="rv-badge rv-badge--amber")
            if metadata.get("synthetic")
            else None,
            html.Dl(
                [node for key, value in fields if value for node in [html.Dt(key), html.Dd(value)]],
                className="rv-metadata",
            ),
            html.A("Open source", href=url, target="_blank", rel="noopener noreferrer")
            if url
            else html.P("No usable HTTP(S) source link supplied.", className="rv-small rv-muted"),
            html.P(
                f"Examined {coverage['examined_chars']:,} of {coverage['total_chars']:,} "
                f"characters · {coverage['examined_sentences']:,} sentences. "
                f"Displayed {coverage['displayed_chars']:,} characters."
                + (
                    " Coverage is limited; remaining text was not compared."
                    if coverage["truncated"]
                    else " All saved text is within coverage."
                ),
                className="rv-source-coverage rv-small rv-muted",
            ),
            html.Pre(
                source["text"],
                className="rv-source-text",
                tabIndex=0,
                **{"aria-label": f"Exact saved text, source {label}"},
            ),
            html.Details(
                [
                    html.Summary("Saved record references"),
                    html.P(source["record_id"], className="rv-mono"),
                    html.P(f"Content SHA-256: {source['content_hash']}", className="rv-mono"),
                ],
                className="rv-details",
            ),
        ],
        className="rv-source-pane",
        **{"aria-label": f"Source {label} details and exact text"},
    )


def render_comparison(result: dict[str, Any]) -> html.Div:
    """Render literal saved text and exact candidate quotes without interpreting HTML."""
    candidates, coverage = result["candidates"], result["coverage"]
    cards = []
    for index, candidate in enumerate(candidates, start=1):
        cards.append(
            html.Article(
                [
                    html.H3(f"{index}. {candidate['label']}"),
                    html.P(candidate["basis"], className="rv-small rv-muted"),
                    html.Div(
                        [
                            html.Div(
                                [html.Strong(f"Source {label}"), _quote(candidate[side])],
                                className="rv-source-passage",
                            )
                            for side, label in [("left", "A"), ("right", "B")]
                        ],
                        className="rv-source-panes",
                    ),
                ],
                className="rv-source-candidate",
            )
        )
    return html.Div(
        [
            html.Div(
                f"{len(candidates)} possible wording difference(s) flagged for review. "
                "These are not established contradictions."
                if candidates
                else "No possible wording differences flagged by these limited rules. "
                "This does not establish agreement.",
                className="rv-notice",
                role="status",
            ),
            html.Div(
                [
                    _source_panel(result[side], coverage[side], label)
                    for side, label in [("left", "A"), ("right", "B")]
                ],
                className="rv-source-panes",
            ),
            html.Section(
                [
                    html.H2("Possible conflict candidates"),
                    html.P(
                        "Highlighted text marks changed numbers or negation. "
                        "Read both exact passages and their source context before deciding.",
                        className="rv-small rv-muted",
                    ),
                    *cards,
                ],
                className="rv-source-candidates",
            )
            if candidates
            else None,
            html.P(
                f"Showing the first {coverage['candidate_limit']} candidates; additional "
                "candidates were omitted.",
                className="rv-notice",
            )
            if coverage["candidates_truncated"]
            else None,
            html.Details(
                [
                    html.Summary("Comparison coverage and limitations"),
                    html.Ul([html.Li(item) for item in result["limitations"]]),
                    html.P(
                        f"Per source: at most {coverage['left']['character_limit']:,} characters "
                        f"and {coverage['left']['sentence_limit']:,} sentences. "
                        f"At most {coverage['candidate_limit']} candidates are shown. "
                        "This comparison reads saved text only and does not contact a model "
                        "or fetch live pages.",
                        className="rv-small rv-muted",
                    ),
                ],
                className="rv-details",
            ),
        ]
    )


def register_comparison_callbacks(app: Dash, service: ReviewService) -> None:
    @app.callback(
        Output("rv-source-left", "options"),
        Output("rv-source-right", "options"),
        Output("rv-source-left", "value"),
        Output("rv-source-right", "value"),
        Output("rv-source-case", "data"),
        Output("rv-source-status", "children"),
        Input("rv-session", "data"),
        Input("rv-source-left", "search_value"),
        Input("rv-source-right", "search_value"),
        State("rv-source-case", "data"),
        State("rv-source-left", "value"),
        State("rv-source-right", "value"),
    )
    def sync_sources(session, left_search, right_search, previous_case, left_id, right_id):
        case_id = (session or {}).get("case_id")
        if not case_id:
            return [], [], None, None, None, "Choose a case with at least two saved text sources."
        try:
            records = service.get_case(case_id).get("records", [])
        except (ValueError, KeyError):
            return (
                [],
                [],
                None,
                None,
                case_id,
                "This saved case is unavailable. Choose another case.",
            )
        eligible = _text_records(records)
        changed_case = case_id != previous_case
        left, right = (
            default_pair(records, (session or {}).get("record_id"))
            if changed_case
            else (left_id, right_id)
        )
        if not eligible:
            message = "No saved text sources. Media alone cannot be compared."
        elif len(eligible) == 1:
            message = "Only one saved text source. Choose a case with two text sources."
        else:
            message = f"{len(eligible)} saved text sources. Search by title, label, or record ID."
            if len(eligible) > OPTION_LIMIT:
                message += f" Showing up to {OPTION_LIMIT} matches plus the selected source."
            if len(records) > len(eligible):
                message += " Records without saved visible text are unavailable for comparison."
        # Update a new case's options and selections together. Separate callbacks
        # let the dropdown clear a new value while it still has the old options.
        return (
            source_options(records, left_search, left),
            source_options(records, right_search, right),
            left if changed_case else no_update,
            right if changed_case else no_update,
            case_id if changed_case else no_update,
            message,
        )

    @app.callback(
        Output("rv-source-result", "children"),
        Output("rv-source-run", "disabled"),
        Input("rv-source-run", "n_clicks"),
        Input("rv-source-left", "value"),
        Input("rv-source-right", "value"),
        Input("rv-source-case", "data"),
        Input("rv-session", "data"),
    )
    def compare(n_clicks, left_id, right_id, case_id, session):
        if not case_id or case_id != (session or {}).get("case_id"):
            return "Choose a case with two saved text sources.", True
        if not left_id or not right_id:
            return "Select two saved text sources to compare.", True
        if left_id == right_id:
            return "Choose two different saved sources.", True
        # Dash can batch a session change with the new source selections. Never
        # retain an earlier case's result just because session is the first trigger.
        if ctx.triggered_id != "rv-source-run" or not n_clicks:
            return "Selection ready. Choose Compare sources to inspect this pair.", False
        try:
            result = service.compare_sources(case_id, left_id, right_id)
        except (ValueError, KeyError) as exc:
            return html.Div(str(exc), className="rv-notice rv-notice--error", role="alert"), False
        return render_comparison(result), False
