"""Render a detached category and Swarm projection into a draft dossier."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from html import escape


def _escape_text(value: str) -> str:
    raw = value.replace("\n", " ").replace("\r", " ").replace("\u2028", " ").replace("\u2029", " ")
    return re.sub(r"([\\`*_{}\[\]()#+.!|~\-])", r"\\\1", escape(raw, quote=False))


def _escape_values(value, *, escape_keys=False):
    if isinstance(value, str):
        return _escape_text(value)
    if isinstance(value, list):
        return [_escape_values(item, escape_keys=escape_keys) for item in value]
    if isinstance(value, dict):
        return {
            _escape_text(str(key)) if escape_keys else key: _escape_values(
                item, escape_keys=escape_keys
            )
            for key, item in value.items()
        }
    return value


def redact_category_brief(handoff: dict) -> dict:
    """Render selected category findings only in the escaped evidence matrix."""
    result = deepcopy(handoff)
    category = result["category_review"]
    in_scope = {row["finding_id"] for row in category["rows"]}
    in_scope.update(row["finding_id"] for row in category["invalid_findings"])
    selected = set(category.get("all_category_finding_ids", [])) | in_scope
    off_scope = selected - in_scope
    # Dossier gap descriptions may carry a finding identifier even though the
    # category itself is outside the expressly selected target and kinds.
    result["gaps"] = [gap for gap in result["gaps"] if gap.get("finding_id") not in off_scope]
    brief = result["brief"]
    for group in ("included", "omitted"):
        brief[group] = [item for item in brief[group] if item["finding_id"] not in selected]
    for key in ("title", "question"):
        if isinstance(brief["case"].get(key), str):
            brief["case"][key] = _escape_text(brief["case"][key])
    for key in ("label", "base_run_id"):
        if isinstance(brief["assessment"].get(key), str):
            brief["assessment"][key] = _escape_text(brief["assessment"][key])
    for item in [*brief["included"], *brief["omitted"]]:
        for key in ("statement", "finding_id", "reason"):
            if isinstance(item.get(key), str):
                item[key] = _escape_text(item[key])
        if item.get("review"):
            for key in ("reviewer", "created_at"):
                if isinstance(item["review"].get(key), str):
                    item["review"][key] = _escape_text(item["review"][key])
        for citation in item.get("evidence", []):
            for key in (
                "record_id",
                "fragment_id",
                "source_label",
                "quote",
                "artifact_ref",
                "source_url",
            ):
                if isinstance(citation.get(key), str):
                    citation[key] = _escape_text(citation[key])
            citation["fragment_ids"] = [_escape_text(v) for v in citation.get("fragment_ids", [])]
    for run in brief["methods"]:
        for key in ("id", "method", "method_version", "input_fingerprint"):
            if isinstance(run.get(key), str):
                run[key] = _escape_text(run[key])
        for key in ("configuration", "coverage"):
            run[key] = _escape_values(run.get(key, {}), escape_keys=True)
    brief["coverage"] = _escape_values(brief["coverage"], escape_keys=True)
    brief["limitations"] = _escape_values(brief["limitations"])
    brief["excluded_record_ids"] = _escape_values(brief["excluded_record_ids"])
    result["scope"]["collection_scope"] = _escape_values(
        result["scope"]["collection_scope"], escape_keys=True
    )
    for gap in result["gaps"]:
        for key in ("label", "detail", "finding_id", "record_id"):
            if isinstance(gap.get(key), str):
                gap[key] = _escape_text(gap[key])
    result["planning"] = _escape_values(result["planning"])
    return result


def _text(value):
    """Keep supplied text on one Markdown line without introducing new blocks."""
    return _escape_text(str("Unknown" if value is None or value == "" else value))


def render_category_dossier(projection: dict) -> str:
    """Keep selected-review decisions and separately selected Swarm context distinct."""
    lines = [
        "## Category review and behavioral context",
        "",
        f"Target: {_text(projection['target'])} · Record kinds: "
        + ", ".join(_text(kind) for kind in projection["record_kinds"]),
        "Counts describe observed saved record IDs in the selected assessment; "
        "sample coverage is unknown. Categories may overlap.",
        "",
        "### Monthly observed category findings",
        "",
    ]
    for month in projection["months"]:
        counts = []
        for category, states in month["categories"].items():
            nonzero = [
                f"{state}: {value['observed_count']}"
                for state, value in states.items()
                if value["observed_count"]
            ]
            if nonzero:
                counts.append(f"{category.replace('_', ' ')} ({', '.join(nonzero)})")
        lines.append(f"- {_text(month['month'])}: " + ("; ".join(counts) or "no counted findings"))
    if not projection["months"]:
        lines.append("No dated category findings in the selected scope; this is not zero activity.")
    lines += ["", "### Evidence matrix", ""]
    for row in projection["rows"]:
        decision = row.get("decision") or {}
        include_text = not row.get("excluded") and row.get("disposition") in {"use", "qualify"}
        lines += [
            f"- {_text(row['category'].replace('_', ' '))} · "
            f"{_text(row.get('disposition') or 'unreviewed')}"
            + (" · EXCLUDED from active counts" if row.get("excluded") else ""),
            f"  - Saved record: {_text(row['record_id'])} · "
            f"finding: {_text(row['finding_id'])} · run: {_text(row['run_id'])}",
        ]
        if not include_text:
            lines.append("  - Details omitted: this finding is withheld, unreviewed, or excluded.")
            continue
        origin = row.get("record_synthetic")
        provenance = (
            "Synthetic record"
            if origin is True
            else "Collected source; accuracy requires review"
            if origin is False
            else "Record origin not specified"
        )
        lines.append(
            f"  - Source: {_text(row.get('source_label'))} · {provenance} · "
            f"text hash: {_text(row.get('record_content_hash'))}"
        )
        lines.append(
            f"  - Published: {_text(row.get('published_at'))} · "
            f"model: {_text(row.get('model'))} · method: {_text(row.get('method'))}"
        )
        if decision:
            lines.append(
                f"  - Current decision: {_text(decision.get('disposition'))} · "
                f"reviewer: {_text(decision.get('reviewer'))} · "
                f"reason: {_text(decision.get('reason'))}"
            )
        for fragment in row["fragments"]:
            lines.append(
                f"  - Exact saved fragment {_text(fragment['id'])}: {_text(fragment.get('quote'))}"
            )
        for context in row.get("swarm_context", []):
            lines += [
                "  - Explicitly selected Swarm context (unreviewed here; not a category decision): "
                + _text(context.get("statement")),
                f"    - Finding: {_text(context['finding_id'])} · "
                f"run: {_text(context['run_id'])} · method: {_text(context['method'])} "
                f"version {_text(context['method_version'])}",
                f"    - Input fingerprint: {_text(context.get('input_fingerprint'))} · "
                "producer reports full inputs: "
                + _text(context.get("producer_reports_full_inputs")),
                "    - Coverage: " + _text(json.dumps(context.get("run_coverage"), sort_keys=True)),
            ]
            for link in context.get("evidence_links", []):
                for fragment in link.get("fragments", []):
                    lines.append(
                        f"    - {_text(link.get('relation'))} fragment {_text(fragment['id'])}: "
                        f"{_text(fragment.get('quote'))}"
                    )
    if not projection["rows"]:
        lines.append("No category findings in the selected scope.")
    lines += [
        "",
        "Undated saved record IDs: "
        + (", ".join(_text(item) for item in projection["undated_record_ids"]) or "none"),
    ]
    for item in projection["invalid_findings"]:
        lines.append(f"- Unavailable finding {_text(item['finding_id'])}: {_text(item['reason'])}")
    lines += ["", "### Selected Swarm runs and unlinked findings", ""]
    lines.append(
        "Selected runs: "
        + (", ".join(_text(item) for item in projection["selected_swarm_run_ids"]) or "none")
    )
    for item in projection["unlinked_swarm_findings"]:
        lines.append(
            f"- Unlinked finding {_text(item['finding_id'])} · run {_text(item['run_id'])} "
            "(statement omitted; no linked saved record)"
        )
    lines += [
        "",
        "Swarm observations and their reviews are separate from category findings. "
        "No shared record proves automation, intent, a violation, or an operator.",
        "Analytics capture bytes are not included in the shared case package; "
        "preserve the external evidence separately.",
    ]
    return "\n".join(lines)
