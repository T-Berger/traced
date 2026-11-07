"""Explicit, context-bound display of an already saved media record.

The first render contains metadata and a reveal control, never an image URL or
artifact preflight. Only an explicit reveal/retry asks the existing artifact
service to verify the file; its HTTP route retains the independent hash guard.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

from dash import ALL, Input, Output, State, ctx, dcc, html
from dash.exceptions import MissingCallbackContextException

from ..presentation import badge, record_button, record_origin_label
from ..workflow import assessment_context

_ERRORS = {
    "unavailable": (
        "Saved media is unavailable. Inspect metadata or retry after the file is restored."
    ),
    "changed": (
        "Saved media does not match its recorded content hash. "
        "Inspect metadata or retry after the original file is restored."
    ),
    "display": (
        "The saved media could not be displayed. "
        "Check the local connection or file format, then retry."
    ),
}


def media_panel() -> html.Div:
    """Mount the two stores outside the changing media body once per app layout."""
    return html.Div(
        [
            dcc.Store(id="trail-media-view", storage_type="memory", data={}),
            dcc.Store(id="trail-media-event", storage_type="memory", data={}),
            html.Div(id="trail-media"),
        ],
        className="rv-media-panel",
    )


def _is_metadata(record: Mapping[str, Any]) -> bool:
    kind = str(record.get("kind", ""))
    return kind == "metadata" or kind.endswith("frame_metadata") or kind == "media_metadata"


def _metadata_links(bundle: Mapping[str, Any], media_id: str) -> set[str]:
    records = {record["id"]: record for record in bundle.get("records", [])}
    return {
        link["source"]["id"]
        for link in bundle.get("links", [])
        if link.get("relation") == "part_of"
        and link.get("source", {}).get("type") == "record"
        and link.get("target") == {"type": "record", "id": media_id}
        and link["source"]["id"] in records
        and _is_metadata(records[link["source"]["id"]])
    }


def resolve_media_selection(service: Any, state: Mapping[str, Any] | None) -> dict | None:
    """Resolve a direct file or one exact metadata→frame link, without file access."""
    if not isinstance(state, Mapping) or not state.get("case_id") or not state.get("record_id"):
        return None
    view = service.view(state["case_id"], state.get("scenario_id"))
    if state.get("case_fingerprint") is not None and state["case_fingerprint"] != view.get(
        "case_fingerprint"
    ):
        raise ValueError("The selected evidence snapshot changed.")
    assessment = assessment_context(view)
    version = state.get("scenario_version")
    if version is not None and (type(version) is not int or version != assessment["version"]):
        raise ValueError("The selected assessment version changed.")
    bundle = view["bundle"]
    if bundle["case"]["id"] != state["case_id"]:
        return None
    records = {record["id"]: record for record in bundle.get("records", [])}
    selected = records.get(state["record_id"])
    if selected is None:
        return None
    media = selected if selected.get("artifact_ref") else None
    if media is None and _is_metadata(selected):
        candidates = {
            link["target"]["id"]
            for link in bundle.get("links", [])
            if link.get("relation") == "part_of"
            and link.get("source") == {"type": "record", "id": selected["id"]}
            and link.get("target", {}).get("type") == "record"
            and link["target"]["id"] in records
            and records[link["target"]["id"]].get("artifact_ref")
        }
        if len(candidates) == 1:
            media = records[next(iter(candidates))]
    if media is None:
        return None
    metadata_ids = _metadata_links(bundle, media["id"])
    metadata_record = (
        selected
        if selected["id"] in metadata_ids
        else records[next(iter(metadata_ids))]
        if len(metadata_ids) == 1
        else None
    )
    binding = [
        state["case_id"],
        view.get("case_fingerprint"),
        "baseline" if assessment["is_baseline"] else assessment["id"],
        assessment["version"],
        assessment["base_run_id"],
        selected["id"],
        media["id"],
        media["content_hash"],
    ]
    serialized = json.dumps(binding, ensure_ascii=False, separators=(",", ":"))
    return {
        "context": "media-v1:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "case_id": state["case_id"],
        "case_fingerprint": view.get("case_fingerprint"),
        "selected_record_id": selected["id"],
        "media_record_id": media["id"],
        "expected_hash": media["content_hash"],
        "record": media,
        "selected_record": selected,
        "metadata_record": metadata_record,
        "case_synthetic": bundle["case"].get("synthetic"),
        "scenario_id": assessment["id"],
        "scenario_version": assessment["version"],
        "base_run_id": assessment["base_run_id"],
        "is_baseline": assessment["is_baseline"],
    }


def _hidden(selection: Mapping[str, Any]) -> dict:
    return {"context": selection["context"], "status": "hidden", "attempt": None}


def _stored_view(selection: Mapping[str, Any], stored: Any) -> dict:
    if not isinstance(stored, Mapping) or stored.get("context") != selection["context"]:
        return _hidden(selection)
    status = stored.get("status")
    if status == "hidden":
        return _hidden(selection)
    if status not in ("loading", "ready", "error"):
        return _hidden(selection)
    attempt = stored.get("attempt")
    try:
        if not isinstance(attempt, str) or len(attempt) > 64:
            return _hidden(selection)
        UUID(attempt)
    except ValueError:
        return _hidden(selection)
    result = {"context": selection["context"], "status": status, "attempt": attempt}
    if status == "error":
        error = stored.get("error")
        result["error"] = error if isinstance(error, str) and error in _ERRORS else "unavailable"
    return result


def _button(selection: Mapping[str, Any], action: str, label: str) -> html.Button:
    return html.Button(
        label,
        id={"type": "trail-media-action", "context": selection["context"], "action": action},
        n_clicks=0,
        className="rv-button rv-button--outline",
    )


def _metadata_action(selection: Mapping[str, Any]):
    metadata = selection.get("metadata_record")
    if metadata and metadata["id"] != selection["selected_record_id"]:
        return record_button(
            metadata, "media-metadata-" + selection["context"][-16:], "Inspect saved metadata"
        )
    return html.A(
        "Inspect saved metadata",
        href="#rv-source",
        className="rv-text-button rv-media-metadata-link",
    )


def render_media(selection: Mapping[str, Any] | None, store: Mapping[str, Any]):
    """Render image markup only for a currently requested or completed attempt."""
    if selection is None:
        return ""
    store = _stored_view(selection, store)
    record = selection["record"]
    metadata = record.get("metadata", {})
    synthetic = metadata.get("synthetic") is True
    content = [
        html.Div(
            [
                badge("Supplied local media"),
                *([badge("Synthetic media fixture", "amber")] if synthetic else []),
            ],
            className="rv-badges",
        ),
        html.P(
            "Record provenance: " + record_origin_label(metadata.get("synthetic")) + ".",
            className="rv-small rv-muted",
        ),
    ]
    if not synthetic or selection.get("case_synthetic") is False:
        content.append(
            html.P(
                "Supplied historical media may contain graphic content. "
                "Reveal it only when you are ready to inspect it.",
                className="rv-media-warning",
            )
        )
    status = store["status"]
    if status in {"loading", "ready"}:
        content += [
            html.P(
                "Loading saved media…" if status == "loading" else "Saved media loaded.",
                role="status",
                **{"aria-live": "polite"},
            ),
            html.Img(
                src="/trail-artifact?"
                + urlencode(
                    {
                        "case_id": selection["case_id"],
                        "record_id": selection["media_record_id"],
                        "expected_hash": selection["expected_hash"],
                        "attempt": store["attempt"],
                    }
                ),
                alt=record.get("source_label") or "Supplied saved media",
                className="rv-media-image",
                style={"display": "none"} if status == "loading" else {},
                **{
                    "data-media-context": selection["context"],
                    "data-media-attempt": store["attempt"],
                },
            ),
            html.Div([_button(selection, "hide", "Hide media")], className="rv-media-actions"),
        ]
    elif status == "error":
        content += [
            html.Div(
                _ERRORS[store["error"]],
                role="alert",
                className="rv-notice rv-notice--error",
            ),
            html.Div(
                [
                    _button(selection, "retry", "Retry saved media"),
                    _metadata_action(selection),
                    _button(selection, "hide", "Hide media"),
                ],
                className="rv-media-actions",
            ),
        ]
    else:
        content += [
            html.P("Media is hidden. The saved metadata remains available.", className="rv-muted"),
            html.Div(
                [_button(selection, "reveal", "Reveal supplied media")],
                className="rv-media-actions",
            ),
        ]
    content.append(
        html.Dl(
            [
                html.Dt("First-posted exercise week"),
                html.Dd(str(metadata.get("first_posted_exercise_week") or "Unknown")),
                html.Dt("Capture or event time"),
                html.Dd("Unknown"),
                html.Dt("Posting-channel affiliation"),
                html.Dd(metadata.get("posting_channel_affiliation") or "Unknown"),
                html.Dt("Depicted side / location"),
                html.Dd("Unknown"),
                html.Dt("Clip / source video"),
                html.Dd(
                    f"{metadata.get('clip_id') or 'Unknown'} / "
                    f"{metadata.get('video_id') or 'Unknown'}"
                ),
            ],
            className="rv-metadata",
        )
    )
    content.append(
        html.Details(
            [
                html.Summary("Media file details"),
                html.Dl(
                    [
                        html.Dt("Saved media reference"),
                        html.Dd(selection["media_record_id"], className="rv-mono"),
                        html.Dt("Expected content hash"),
                        html.Dd(selection["expected_hash"], className="rv-mono"),
                    ],
                    className="rv-metadata",
                ),
            ],
            className="rv-details",
        )
    )
    content.append(
        html.P(
            "Supplied local evidence. Original publication context is unavailable.",
            className="rv-small rv-muted",
        )
    )
    return html.Div(content, className="rv-media-viewer")


def _clicked(clicks: Any) -> bool:
    if type(clicks) is int:
        return clicks > 0
    return (
        isinstance(clicks, Sequence)
        and not isinstance(clicks, str | bytes)
        and any(type(value) is int and value > 0 for value in clicks)
    )


def transition_media(
    service: Any,
    state: Mapping[str, Any] | None,
    trigger: Any,
    clicks: Any = None,
    event: Mapping[str, Any] | None = None,
    stored: Mapping[str, Any] | None = None,
) -> tuple[Any, dict]:
    """Apply one display transition while leaving the review session untouched."""
    try:
        selection = resolve_media_selection(service, state)
    except Exception:
        return (
            html.Div(
                [
                    html.P("Saved media metadata is unavailable for this selection."),
                    html.A("Inspect saved metadata", href="#rv-source", className="rv-text-button"),
                ],
                role="alert",
                className="rv-notice rv-notice--error",
            ),
            {},
        )
    if selection is None:
        return "", {}
    store = _stored_view(selection, stored)
    if trigger == "trail-media-event" and isinstance(event, Mapping):
        if (
            store["status"] == "loading"
            and event.get("context") == selection["context"]
            and event.get("attempt") == store["attempt"]
            and event.get("status") in ("loaded", "error")
        ):
            store = {
                **store,
                "status": "ready" if event["status"] == "loaded" else "error",
            }
            if event["status"] == "error":
                store["error"] = "display"
    elif isinstance(trigger, Mapping) and _clicked(clicks):
        if (
            trigger.get("type") != "trail-media-action"
            or trigger.get("context") != selection["context"]
        ):
            return render_media(selection, store), store
        action = trigger.get("action")
        if action == "hide":
            store = _hidden(selection)
        elif (action == "reveal" and store["status"] == "hidden") or (
            action == "retry" and store["status"] == "error"
        ):
            store = {
                "context": selection["context"],
                "status": "loading",
                "attempt": str(uuid4()),
            }
            try:
                service.artifact(selection["case_id"], selection["media_record_id"])
            except Exception as exc:
                store["status"] = "error"
                store["error"] = (
                    "changed"
                    if isinstance(exc, ValueError)
                    and "no longer matches its evidence hash" in str(exc).lower()
                    else "unavailable"
                )
    return render_media(selection, store), store


def register_media_callbacks(app: Any, service: Any) -> None:
    @app.callback(
        Output("trail-media", "children"),
        Output("trail-media-view", "data"),
        Input("rv-session", "data"),
        Input({"type": "trail-media-action", "context": ALL, "action": ALL}, "n_clicks"),
        Input("trail-media-event", "data"),
        State("trail-media-view", "data"),
    )
    def media(state, clicks=None, event=None, stored=None):
        try:
            trigger = ctx.triggered_id
        except MissingCallbackContextException:
            trigger = "rv-session"
        return transition_media(service, state, trigger, clicks, event, stored)
