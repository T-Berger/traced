"""Shared style for the planning editor's single-line text inputs."""

PLANNING_TEXT_INPUT_STYLE = {
    "width": "100%",
    "minWidth": 0,
    "maxWidth": "100%",
    "minHeight": "var(--traced-layout-control-height)",
    "padding": "11px 12px",
    "border": "1px solid var(--rv-input-border)",
    "borderRadius": "6px",
    "color": "var(--rv-ink)",
    "backgroundColor": "var(--rv-surface)",
    # Dash applies these roles to the wrapper focus ring and selected text.
    "--Dash-Fill-Interactive-Strong": "var(--rv-focus)",
    "--Dash-Fill-Inverse-Strong": "var(--rv-surface)",
}
