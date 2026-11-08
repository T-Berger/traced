from types import SimpleNamespace

import pytest
from dash import Dash, no_update
from dash.development.base_component import Component

from evidence_explorer.alerts import ui
from evidence_explorer.alerts.settings import AlertSettings, AlertSettingsStore


def walk(node):
    if isinstance(node, Component):
        yield node
        yield from walk(getattr(node, "children", None))
    elif isinstance(node, (tuple, list)):
        for child in node:
            yield from walk(child)


@pytest.mark.parametrize("trigger", ["al-save", "al-reset"])
def test_read_only_settings_actions_cannot_write(tmp_path, monkeypatch, trigger):
    store = AlertSettingsStore(tmp_path)
    store.save(AlertSettings(notifier_name="Existing setting"))
    before = store.path.read_bytes()
    page = ui.alerts_page(store, read_only=True)
    buttons = {getattr(node, "id", None): node for node in walk(page)}
    assert buttons["al-save"].disabled
    assert buttons["al-reset"].disabled
    assert buttons["al-case"].placeholder == "Select an analytics subject"

    app = Dash(__name__)
    ui.register_alert_callbacks(
        app, SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path, read_only=True))
    )
    callback = next(
        entry["callback"].__wrapped__
        for key, entry in app.callback_map.items()
        if "al-settings-rev.data" in key
    )
    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id=trigger))
    args = [None] * (len(ui.FORM_KEYS) + len(ui._SETTINGS_INPUTS) + 1)
    result = callback(1, 1, *args)
    assert result[0] is no_update
    assert "read-only" in str(result[1])
    assert store.path.read_bytes() == before
