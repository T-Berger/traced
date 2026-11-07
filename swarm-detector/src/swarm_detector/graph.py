"""Readable, bounded projections of saved account-pair evidence.

The graph is a navigation aid. A hidden link is not absent evidence, and a
retained pair is a detector result rather than an identity or ownership claim.
"""

from __future__ import annotations

import math
from typing import Any

MAX_ACCOUNTS = 10
MAX_PAIRS = 14


def _number(value: Any) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _pairs(detail: dict) -> list[dict]:
    """Keep exact saved pair identifiers and a deterministic display order."""
    unique = {}
    for pair in detail.get("pairs") or []:
        key = pair.get("pair_key")
        a, b = pair.get("account_a"), pair.get("account_b")
        if key is None or a is None or b is None or str(a) == str(b):
            continue
        unique[str(key)] = pair
    return sorted(
        unique.values(),
        key=lambda p: (
            not bool(p.get("retained")),
            -_number(p.get("observed")),
            str(p["pair_key"]),
        ),
    )


def _declared_total(detail: dict, section: str, loaded: int) -> int:
    total = ((detail.get("pagination") or {}).get(section) or {}).get("total")
    return max(loaded, int(_number(total)))


def graph_view(detail: dict, selected_pair_key: str | None = None) -> dict:
    """Focus the selected pair and its neighbors, without hiding display limits."""
    case = detail.get("case") or {}
    pairs = _pairs(detail)
    core = {str(key) for key in case.get("account_keys") or []}
    universe = core | {str(key) for key in case.get("peripheral_keys") or []}
    universe.update(
        str(account["account_key"])
        for account in detail.get("accounts") or []
        if account.get("account_key") is not None
    )
    universe.update(str(pair[field]) for pair in pairs for field in ("account_a", "account_b"))
    aliases = {key: f"A{index + 1:02d}" for index, key in enumerate(sorted(universe))}
    focus = next((p for p in pairs if str(p["pair_key"]) == selected_pair_key), None)
    focus = focus or (pairs[0] if pairs else None)
    anchors = sorted({str(focus["account_a"]), str(focus["account_b"])}) if focus else []
    visible_keys = set(anchors)
    chosen = [focus] if focus else []
    neighbors = [
        p
        for p in pairs
        if p is not focus and ({str(p["account_a"]), str(p["account_b"])} & set(anchors))
    ]
    for pair in neighbors:
        endpoints = {str(pair["account_a"]), str(pair["account_b"])}
        if len(chosen) < MAX_PAIRS and len(visible_keys | endpoints) <= MAX_ACCOUNTS:
            chosen.append(pair)
            visible_keys.update(endpoints)
    if not focus:
        visible_keys = set(sorted(universe)[:MAX_ACCOUNTS])
    keys = anchors + sorted(visible_keys - set(anchors))
    elements = []
    for index, key in enumerate(keys):
        # Two roomy columns preserve reading size on narrow screens. The first
        # row is always the selected pair; no force-layout jitter is introduced.
        label_id = key if len(key) <= 14 else "…" + key[-13:]
        elements.append(
            {
                "data": {
                    "id": key,
                    "account_key": key,
                    "alias": aliases[key],
                    "label": aliases[key] + "\n" + label_id,
                },
                "position": {
                    "x": 70 + 140 * (index % 2),
                    "y": 50 + 96 * (index // 2),
                },
                "classes": ("core" if key in core else "peripheral")
                + (" focus-account" if key in anchors else ""),
            }
        )
    for pair in chosen:
        selected = pair is focus
        elements.append(
            {
                "data": {
                    "id": str(pair["pair_key"]),
                    "source": str(pair["account_a"]),
                    "target": str(pair["account_b"]),
                    "pair_key": pair["pair_key"],
                    "observed": pair.get("observed", 0),
                    "null_mean": pair.get("null_mean"),
                    "null_p95": pair.get("null_p95"),
                    "retained": bool(pair.get("retained")),
                    "width": 4 if selected else 2,
                },
                "classes": ("retained" if pair.get("retained") else "context")
                + (" focused-pair" if selected else ""),
            }
        )
    total_accounts = _declared_total(detail, "accounts", len(universe))
    total_pairs = _declared_total(detail, "pairs", len(pairs))
    return {
        "elements": elements,
        "shown_pair_keys": [str(p["pair_key"]) for p in chosen],
        "account_count": len(keys),
        "pair_count": len(chosen),
        "loaded_account_count": len(universe),
        "loaded_pair_count": len(pairs),
        "total_account_count": total_accounts,
        "total_pair_count": total_pairs,
        "omitted_account_count": total_accounts - len(keys),
        "omitted_pair_count": total_pairs - len(chosen),
        "selected_pair_key": str(focus["pair_key"]) if focus else None,
        "selected_pair": focus,
        "aliases": aliases,
    }


def graph_summary(view: dict) -> str:
    """State what is displayed and what the graph cannot establish."""
    text = (
        f"Showing {view['account_count']} of {view['total_account_count']} accounts and "
        f"{view['pair_count']} of {view['total_pair_count']} saved pair links. "
    )
    if view.get("selected_pair_key"):
        text += "Focused on the selected pair and its direct neighbors. "
    if view["loaded_pair_count"] < view["total_pair_count"]:
        text += f"This page contains {view['loaded_pair_count']} of the saved pairs. "
    return (
        text
        + "Links not shown are not absent. Use the account-pair selector for other loaded pairs. "
        "Connections do not establish common ownership, automation, or intent."
    )


def graph_pair_rows(detail: dict, view: dict) -> list[dict]:
    """Mirror visible links in a keyboard- and screen-reader-readable table."""
    selected = set(view["shown_pair_keys"])
    return [
        {
            "pair_key": str(pair["pair_key"]),
            "account_a": str(pair["account_a"]),
            "account_b": str(pair["account_b"]),
            "observed": pair.get("observed", 0),
            "null_mean": pair.get("null_mean"),
            "null_p95": pair.get("null_p95"),
            "retained": bool(pair.get("retained")),
            "selected": str(pair["pair_key"]) == view.get("selected_pair_key"),
        }
        for pair in _pairs(detail)
        if str(pair["pair_key"]) in selected
    ]
