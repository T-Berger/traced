"""Model readiness and disclosure controls for optional evidence checks."""

from __future__ import annotations

from typing import Any

from dash import Dash, Input, Output, State, dcc, html

from .presentation import badge
from .service import ReviewService


def provider_controls() -> html.Div:
    return html.Div(
        [
            dcc.Store(id="rv-provider-config", storage_type="memory"),
            html.Div(id="rv-provider-status", role="status", **{"aria-live": "polite"}),
            html.Button(
                "Check connection",
                id="rv-provider-refresh",
                n_clicks=0,
                className="rv-text-button",
            ),
        ],
        className="rv-provider-controls",
    )


def provider_ready(config: dict[str, Any] | None, mode: str | None) -> bool:
    if mode == "demo":
        return True
    return bool((config or {}).get("providers", {}).get(mode or "ollama", {}).get("ready"))


def provider_status(config: dict[str, Any] | None, mode: str | None):
    if mode == "demo":
        return html.Div(
            [
                badge("Deterministic demo", "amber"),
                html.P(
                    "Uses the synthetic case's saved test outcome. No model runs and no evidence "
                    "leaves this application.",
                    className="rv-small rv-muted",
                ),
            ]
        )
    selected = mode or "ollama"
    settings = (config or {}).get("providers", {}).get(selected, {})
    local = selected == "ollama"
    ready = bool(settings.get("ready"))
    label = "Local model" if local else "OpenRouter"
    text = (
        "The selected finding and a bounded selection of saved passages are processed by your "
        "configured Ollama server. Sources are treated as evidence, not instructions."
        if local
        else "Running this check sends the selected finding and a bounded selection of saved "
        "passages to OpenRouter and its model provider. Importing a case alone does not send data."
    )
    children = [
        html.Div(
            [badge(f"{label} {'ready' if ready else 'not ready'}", "green" if ready else "amber")],
            className="rv-badges",
        ),
        html.P(settings.get("model") or "No model configured", className="rv-mono"),
        html.P(text, className="rv-small rv-muted"),
    ]
    if settings.get("message"):
        children.append(html.P(settings["message"], className="rv-small rv-muted"))
    if not ready:
        instruction = (
            "Start Ollama and install the configured model. The server uses EVIDENCE_CHAT_MODEL "
            "and OLLAMA_BASE_URL. Then check the connection again."
            if local
            else "Configure OPENROUTER_API_KEY and EVIDENCE_REVIEW_MODEL in the server environment "
            "or project .env file, restart the app and worker, then check the connection again. "
            "Keys are never entered or displayed in this browser."
        )
        children.append(html.P(instruction, className="rv-notice"))
    children.append(
        html.P(
            "The result reports the passages actually checked. It is not a complete search of "
            "the internet or a truth verdict.",
            className="rv-small rv-muted",
        )
    )
    return html.Div(children)


def register_provider_callbacks(app: Dash, service: ReviewService) -> None:
    @app.callback(
        Output("rv-provider-config", "data"),
        Input("rv-case", "value"),
        Input("rv-provider-refresh", "n_clicks"),
    )
    def configure(case_id, _refresh):
        try:
            bundle = service.get_case(case_id) if case_id else {}
            config = service.challenge_config()
            config["case_id"] = case_id
            config["demo_finding_ids"] = [
                finding["snapshot_id"]
                for finding in bundle.get("findings", [])
                if bundle.get("case", {}).get("synthetic")
                and "demo_check" in finding.get("details", {})
            ]
            return config
        except Exception:
            return {"default_mode": "ollama", "providers": {}, "case_id": case_id}

    @app.callback(
        Output("rv-challenge-mode", "options"),
        Output("rv-challenge-mode", "value"),
        Input("rv-session", "data"),
        Input("rv-provider-config", "data"),
        State("rv-challenge-mode", "value"),
    )
    def available_modes(state, config, selected):
        state, config = state or {}, config or {}
        has_fixture = config.get("case_id") == state.get("case_id") and state.get(
            "finding_id"
        ) in config.get("demo_finding_ids", [])
        options = [
            {"label": "Ollama · local model", "value": "ollama"},
            {"label": "OpenRouter · external model", "value": "openrouter"},
        ]
        if has_fixture:
            options.append({"label": "Demo · deterministic fixture", "value": "demo"})
        allowed = {option["value"] for option in options}
        value = selected if selected in allowed else "ollama"
        return options, value

    @app.callback(
        Output("rv-provider-status", "children"),
        Input("rv-provider-config", "data"),
        Input("rv-challenge-mode", "value"),
    )
    def show_status(config, selected):
        return provider_status(config, selected)
