"""Small offline UI icons used with visible labels in the case preview."""

from html import escape

PATHS = {
    "document": '<path d="M6 3h8l4 4v14H6zM14 3v5h4M9 12h6m-6 4h6"/>',
    "video": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m10 9 5 3-5 3z"/>',
    "link": '<path d="m10 13 4-4m-5 7-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m3 1 1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0"/>',
    "repeat": '<path d="M4 9h14l-3-3m3 3-3 3m5 3H6l3 3m-3-3 3-3"/>',
    "quote": '<path d="M3 16V8h7v6l-4 5m8-3V8h7v6l-4 5"/>',
    "question": '<circle cx="12" cy="12" r="9"/><path d="M9 9a3 3 0 1 1 4 3c-.7.4-1 .7-1 2m0 3h.01"/>',
    "flag": '<path d="M5 21V4m0 1c4-4 7 3 14 0v10c-7 3-10-4-14 0"/>',
    "target": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
    "up": '<path d="m4 16 6-6 4 4 6-8m-5 0h5v5"/>',
    "down": '<path d="m4 8 6 6 4-4 6 8m-5 0h5v-5"/>',
    "split": '<path d="M4 12h6l9-7m-5 0h5v5m-9 2 9 7m-5 0h5v-5"/>',
    "flat": '<path d="M4 12h16m-4-4 4 4-4 4"/>',
    "arrow": '<path d="M4 12h16m-6-6 6 6-6 6"/>',
    "check": '<path d="M4 12l5 5L20 6"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "graph": '<circle cx="12" cy="5" r="2"/><circle cx="5" cy="19" r="2"/><circle cx="19" cy="19" r="2"/><path d="m11 7-5 10m7-10 5 10"/>',
    "stack": '<path d="m12 3 9 5-9 5-9-5 9-5Zm-8 9 8 5 8-5M4 16l8 5 8-5"/>',
}

ICON_CSS = """
.case-icon{display:inline-block;width:1.05em;height:1.05em;flex:none;vertical-align:-.15em}
.signal-chip{display:inline-flex;align-items:center;gap:5px;line-height:1.35;padding:4px 7px;border:1px solid var(--traced-border-subtle,#d9e0de);border-radius:4px;background:var(--traced-surface-subtle,#f4f3ee);color:var(--traced-foreground-secondary,#52636c);font-size:11px;font-weight:750}
.signal-chip[data-state="found"]{background:#e0eeea;color:#205850;border-color:#accfc6}
.signal-chip[data-state="review"]{background:#f8e8da;color:#673b26;border-color:#e5c2a9}
.signal-chip[data-state="context"]{background:#e8eef2;color:#244b61;border-color:#bfced8}
"""


def icon(name):
    return (
        '<svg class="case-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false" '
        'fill="none" stroke="currentColor" stroke-width="1.7" '
        f'stroke-linecap="round" stroke-linejoin="round">{PATHS[name]}</svg>'
    )


def signal_chip(label, symbol, state="neutral"):
    return (
        f'<span class="signal-chip" data-state="{escape(state, quote=True)}">'
        f"{icon(symbol)}{escape(label)}</span>"
    )
