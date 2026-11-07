"""Provider defaults distinguish real evidence checks from synthetic fixture outcomes."""

from types import SimpleNamespace

from dash import Dash

from evidence_explorer.review import provider_ui


def visible_text(component):
    if isinstance(component, str):
        return component
    if isinstance(component, (list, tuple)):
        return " ".join(visible_text(item) for item in component)
    return visible_text(getattr(component, "children", ""))


def provider_config():
    return {
        "default_mode": "ollama",
        "providers": {
            "ollama": {"ready": True, "model": "test/local-model", "message": "Ready"},
            "openrouter": {
                "ready": False,
                "model": "test/cloud-model",
                "message": "Missing OPENROUTER_API_KEY",
                "missing": ["OPENROUTER_API_KEY"],
            },
        },
    }


def provider_callbacks():
    service = SimpleNamespace(
        get_case=lambda case_id: {
            "case": {"synthetic": case_id == "demo"},
            "findings": [{"snapshot_id": "finding", "details": {"demo_check": []}}],
        },
        challenge_config=provider_config,
    )
    app = Dash(__name__, suppress_callback_exceptions=True)
    provider_ui.register_provider_callbacks(app, service)
    return {
        "configure": next(
            item["callback"].__wrapped__
            for key, item in app.callback_map.items()
            if key == "rv-provider-config.data"
        ),
        "modes": next(
            item["callback"].__wrapped__
            for key, item in app.callback_map.items()
            if "rv-challenge-mode.options" in key
        ),
    }


def test_real_case_defaults_to_local_model_and_cannot_select_demo():
    callbacks = provider_callbacks()
    config = callbacks["configure"]("real", 0)
    options, mode = callbacks["modes"]({"case_id": "real", "finding_id": "finding"}, config, "demo")
    assert mode == "ollama"
    assert {item["value"] for item in options} == {"ollama", "openrouter"}
    assert provider_ui.provider_ready(config, mode) is True
    assert provider_ui.provider_ready(config, "openrouter") is False


def test_connection_refresh_preserves_explicit_provider_choice():
    callbacks = provider_callbacks()
    config = callbacks["configure"]("real", 1)
    _, mode = callbacks["modes"]({"case_id": "real", "finding_id": "finding"}, config, "openrouter")
    assert mode == "openrouter"


def test_only_synthetic_findings_with_fixtures_offer_deterministic_choice():
    callbacks = provider_callbacks()
    config = callbacks["configure"]("demo", 0)
    options, mode = callbacks["modes"]({"case_id": "demo", "finding_id": "finding"}, config, "demo")
    assert mode == "demo"
    assert "demo" in {item["value"] for item in options}
    assert "No model runs" in visible_text(provider_ui.provider_status(config, mode))
    options, mode = callbacks["modes"](
        {"case_id": "demo", "finding_id": "no-fixture"}, config, "demo"
    )
    assert mode == "ollama"
    assert "demo" not in {item["value"] for item in options}


def test_status_distinguishes_local_processing_and_optional_external_transmission():
    config = provider_config()
    local = visible_text(provider_ui.provider_status(config, "ollama"))
    remote = visible_text(provider_ui.provider_status(config, "openrouter"))
    assert "configured Ollama server" in local
    assert "test/local-model" in local
    assert "OpenRouter and its model provider" in remote
    assert "OPENROUTER_API_KEY" in remote
    assert "Keys are never entered or displayed" in remote
