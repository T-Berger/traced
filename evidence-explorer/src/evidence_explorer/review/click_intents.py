"""Filter dynamic button remounts before they can schedule the session writer.

A server-side PreventUpdate is too late: Dash may already supersede an in-flight
selection request for the same callback. Only a synchronous accepted click changes
the stable memory store consumed by the existing action callback.
"""

from __future__ import annotations

import json

from dash import ALL, Input, Output
from dash.exceptions import PreventUpdate

_ID_FIELDS = {
    "rv-finding": ("type", "fid"),
    "rv-record": ("type", "rid", "location"),
    "rv-open-case": ("type", "cid"),
    "rv-citation": ("type", "fid", "rid"),
    "rv-graph-premise": ("type", "fid", "location"),
    "rv-next-action": ("type", "action", "context", "fid", "rid", "location"),
}
CLICK_PATTERNS = tuple(
    {field: family if field == "type" else ALL for field in fields}
    for family, fields in _ID_FIELDS.items()
)

INTENT_SCRIPT = """function() {
    const changes = dash_clientside.callback_context.triggered || [];
    if (!Array.isArray(changes)) return dash_clientside.no_update;
    function candidate(changed) {
        if (!changed || typeof changed.prop_id !== 'string' ||
                !changed.prop_id.endsWith('.n_clicks') ||
                !Number.isSafeInteger(changed.value) || changed.value <= 0) return null;
        let trigger;
        try { trigger = JSON.parse(changed.prop_id.slice(0, -'.n_clicks'.length)); }
        catch (_) { return null; }
        if (!trigger || typeof trigger !== 'object' || Array.isArray(trigger)) return null;
        const fields = __ID_FIELDS__[trigger.type];
        if (!Array.isArray(fields) || Object.keys(trigger).length !== fields.length ||
                !fields.every(key => Object.prototype.hasOwnProperty.call(trigger, key) &&
                    typeof trigger[key] === 'string')) return null;
        return {trigger: trigger, clicks: changed.value};
    }
    // A real click can be batched with zero-valued remounts. Only genuinely
    // eligible changed entries participate; other mounted positive counts do not.
    const eligible = changes.map(candidate).filter(Boolean);
    if (eligible.length !== 1) return dash_clientside.no_update;
    // Counts reset on remount; a fresh token preserves repeated genuine clicks.
    const sequence = (dash_clientside.traced_click_intent_sequence || 0) + 1;
    dash_clientside.traced_click_intent_sequence = sequence;
    return {event_id: Date.now().toString(36) + '-' + sequence.toString(36),
        trigger: eligible[0].trigger, clicks: eligible[0].clicks};
}""".replace("__ID_FIELDS__", json.dumps(_ID_FIELDS))


def register_click_intents(app) -> None:
    app.clientside_callback(
        INTENT_SCRIPT,
        Output("rv-click-intent", "data"),
        *(Input(pattern, "n_clicks") for pattern in CLICK_PATTERNS),
        prevent_initial_call=True,
    )


def trigger_from_intent(intent) -> dict[str, str]:
    """Validate the event envelope; target/context authorization stays in act()."""
    if not isinstance(intent, dict) or set(intent) != {"event_id", "trigger", "clicks"}:
        raise PreventUpdate
    event_id, trigger, clicks = intent["event_id"], intent["trigger"], intent["clicks"]
    if (
        not isinstance(event_id, str)
        or not 1 <= len(event_id) <= 120
        or type(clicks) is not int
        or not 0 < clicks <= 2**53 - 1
        or not isinstance(trigger, dict)
    ):
        raise PreventUpdate
    fields = _ID_FIELDS.get(trigger.get("type")) if isinstance(trigger.get("type"), str) else None
    if (
        fields is None
        or set(trigger) != set(fields)
        or not all(isinstance(value, str) for value in trigger.values())
    ):
        raise PreventUpdate
    return dict(trigger)
