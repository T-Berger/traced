"""Readable check reports: conclusions first, then each chain back to its record.

Renders the JSON form of a ``CheckReport`` so a saved ``report.json`` can be
shown again without re-running any check.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SYMBOL = {
    "supported": "✓",
    "unchecked": "?",
    "assumption_not_backed": "!",
    "unsupported": "✗",
    "contradicted": "✗✗",
}
NAME = {
    "supported": "supported",
    "unchecked": "unchecked",
    "assumption_not_backed": "assumption not backed",
    "unsupported": "unsupported",
    "contradicted": "contradicted",
}
STATUS = {"holds": "HOLDS", "weak": "WEAK", "undermined": "UNDERMINED"}
STATUS_SYMBOL = {"holds": "✓", "weak": "!", "undermined": "✗"}
ORDER = ("supported", "unchecked", "assumption_not_backed", "unsupported", "contradicted")


def _short(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _quote(text: str, limit: int = 120) -> str:
    return '"' + _short(text, limit).replace('"', "'") + '"'


def _span(item: Mapping[str, Any]) -> str:
    line = f" line {item['line']}" if item.get("line") else ""
    key = f" `{item['key']}`" if item.get("key") else ""
    return f"{item.get('record') or '?'}{line}{key} {_quote(item.get('quote', ''))} [{item['start']}–{item['end']}]"


class _Index:
    def __init__(self, report: Mapping[str, Any]):
        self.report = report
        self.findings = {item["id"]: item for item in report["findings"]}
        self.records = {item["id"]: item for item in report["records"]}
        self.fragments = {item["id"]: item for item in report["fragments"]}
        self.links = report["links"]
        self.into: dict[str, list[Mapping[str, Any]]] = {}
        for link in self.links:
            target = link["target"]
            if target.get("type") == "finding":
                self.into.setdefault(target["id"], []).append(link)

    def name(self, finding_id: str) -> str:
        finding = self.findings.get(finding_id)
        return finding["label"] if finding else finding_id[:16]

    def evidence_text(self, link: Mapping[str, Any]) -> str:
        if link.get("evidence"):
            return "; ".join(_span(item) for item in link["evidence"])
        parts = []
        for fragment_id in link.get("fragment_ids") or []:
            fragment = self.fragments.get(fragment_id)
            record = self.records.get(fragment["record_id"]) if fragment else None
            if fragment and record:
                parts.append(
                    f"{record['label']} {_quote(fragment.get('quote') or '')} "
                    f"[{fragment.get('start')}–{fragment.get('end')}]"
                )
        if not parts and link["source"].get("type") == "record":
            record = self.records.get(link["source"]["id"])
            parts.append(f"{record['label'] if record else '?'} (whole record)")
        return "; ".join(parts)


def _reason(link: Mapping[str, Any]) -> str:
    if link.get("reasons"):
        return "; ".join(link["reasons"])
    opened = [item["detail"] for item in link.get("checks", []) if item["result"] == "open"]
    passed = [item["detail"] for item in link.get("checks", []) if item["result"] == "pass"]
    return (opened or passed or [""])[0]


def _tree(index: _Index, finding_id: str, depth: int, seen: frozenset[str], out: list[str]) -> None:
    pad = "  " * depth
    for link in sorted(
        index.into.get(finding_id, []), key=lambda item: (item["relation"], item["id"])
    ):
        source = link["source"]
        symbol, name = SYMBOL[link["label"]], NAME[link["label"]]
        if source.get("type") in {"record", "fragment"}:
            verb = "contradicted by" if link["relation"] == "contradicts" else "cites"
            if link["relation"] == "supports" and link.get("origin") == "checker":
                verb = "backed by"
            out.append(f"{pad}- {symbol} {name} · {verb} {index.evidence_text(link)}")
            reason = _reason(link)
            if reason and link["label"] != "supported":
                out.append(f"{pad}  ↳ {_short(reason, 400)}")
            continue
        premise = index.findings.get(source.get("id"))
        if premise is None:
            out.append(f"{pad}- {symbol} {name} · unresolved premise {source.get('id')}")
            continue
        role = "assumes" if link["relation"] == "depends_on" else "from step"
        out.append(
            f"{pad}- {symbol} {name} · {role} **{premise['label']}**: {_short(premise['statement'], 200)}"
        )
        reason = _reason(link)
        if reason and link["label"] != "supported":
            out.append(f"{pad}  ↳ {_short(reason, 400)}")
        if premise["id"] not in seen:
            _tree(index, premise["id"], depth + 1, seen | {premise["id"]}, out)
    finding = index.findings.get(finding_id)
    for problem in (finding or {}).get("problems", []):
        if problem["code"] in {"cites_nothing", "unsupported_conclusion"}:
            out.append(f"{pad}- ✗ unsupported · {problem['message']}")
    for caveat in (finding or {}).get("caveats", []):
        out.append(f"{pad}- caveat · {_short(caveat, 300)}")


def render_markdown(report: Mapping[str, Any]) -> str:
    index = _Index(report)
    summary = report["summary"]
    reasoning = report["checker"]["reasoning"]
    if reasoning.get("mode") == "verifier":
        reasoning_text = (
            f"ran with {reasoning.get('provider')} `{reasoning.get('model')}` "
            f"({reasoning.get('calls')} calls, {reasoning.get('cached')} cached, "
            f"{reasoning.get('errors')} errors; prompt {reasoning.get('prompt_version')})"
        )
    else:
        open_links = summary.get("model_checks", {}).get(
            "unchecked_links_a_model_check_could_decide"
        )
        reasoning_text = (
            f"**not run**: {reasoning.get('not_run_reason') or 'no model verifier'}. Labels come "
            "from mechanical and deterministic checks only"
            + (
                f"; {open_links} unchecked links are ones a model check could decide"
                if open_links
                else ""
            )
        )
    links = summary["links"]
    conclusions = summary["conclusions"]
    out = [
        f"# Check report: {report['bundle'].get('title') or 'investigation'}",
        "",
        f"- Bundle `{report['bundle']['fingerprint'][:16]}…` · checked {report['checked_at']} · "
        f"checker v{report['checker']['version']}",
        f"- Model checks: {reasoning_text}",
        f"- Contract: {report['bundle']['contract']['detail']}",
        f"- Records: {len(report['records'])} · {summary['records_verified']} re-verified against "
        f"frozen Maltego records · {summary['records_failed']} failed · "
        f"{summary['broken_fragments']} broken quotes",
        "- Links: " + " · ".join(f"{links[label]} {NAME[label]}" for label in ORDER),
        "- Conclusions: "
        + " · ".join(
            f"{conclusions[status]} {status}" for status in ("holds", "weak", "undermined")
        ),
        "",
        "Labels check traceability and consistency against the saved records only. They do not",
        "establish truth, ownership, control, or intent, and they are not a human review decision.",
        "",
        "## Conclusions",
        "",
    ]
    for conclusion in report["conclusions"]:
        status = conclusion["status"]
        out.append(f"### {STATUS_SYMBOL[status]} {conclusion['label']} — {STATUS[status]}")
        out.append("")
        out.append(f"> {_short(conclusion['statement'], 600)}")
        out.append("")
        if conclusion["depends_on"]:
            out.append("Rests on assumptions: " + ", ".join(conclusion["depends_on"]))
            out.append("")
        if not conclusion["flagged"]:
            out.append("Every link in this chain passed its checks.")
        else:
            out.append("Weak points in this chain:")
            for item in conclusion["flagged"]:
                via = " → ".join(item["via"])
                out.append(
                    f"- {SYMBOL[item['label']]} **{NAME[item['label']]}** (via {via}): "
                    f"{_short(item['reason'], 500)}"
                )
        if conclusion.get("caveats"):
            out.append("")
            out.append("Caveats (not failures):")
            for item in conclusion["caveats"]:
                out.append(f"- via {' → '.join(item['via'])}: {_short(item['caveat'], 500)}")
        out.append("")
    out.extend(["## Chains back to the records", ""])
    for conclusion in report["conclusions"]:
        out.append(f"**{conclusion['label']}** {_short(conclusion['statement'], 300)}")
        out.append("")
        lines: list[str] = []
        _tree(index, conclusion["id"], 0, frozenset({conclusion["id"]}), lines)
        out.extend(lines or ["- (nothing supports this conclusion)"])
        out.append("")
    out.extend(["## Assumptions", ""])
    for finding in report["findings"]:
        if finding["role"] != "assumption":
            continue
        symbol = SYMBOL.get(finding["verdict"], "?")
        out.append(
            f"- {symbol} **{finding['label']}** {NAME.get(finding['verdict'], finding['verdict'])}: "
            f"{_short(finding['statement'], 300)}"
        )
        for problem in finding["problems"]:
            out.append(f"  - {_short(problem['message'], 400)}")
        for item in finding["backing"]:
            out.append(f"  - backed by {_span(item)}")
        for item in finding["contradictions"]:
            out.append(f"  - contradicted by {_span(item)} ({item.get('basis')})")
    out.extend(
        [
            "",
            "## Records",
            "",
            "| Label | Lookup | Frozen-record check | Characters |",
            "| --- | --- | --- | --- |",
        ]
    )
    for record in report["records"]:
        frozen = record["frozen"].replace("_", " ")
        maltego = (
            f" `{record['maltego_record_id'][:12]}…`" if record.get("maltego_record_id") else ""
        )
        out.append(
            f"| {record['label']} | {_short(record.get('title') or record['id'], 90)}{maltego} | "
            f"{frozen}{'' if record['hash_ok'] else ' · hash mismatch'} | {record['characters']} |"
        )
    model_notes = [
        f"{finding['label']}: {note}"
        for finding in report["findings"]
        for note in finding.get("model_notes", [])
    ]
    if report["notes"] or model_notes:
        out.extend(["", "## Notes", ""])
        out.extend(f"- {_short(note, 400)}" for note in [*report["notes"], *model_notes])
    return "\n".join(out).rstrip() + "\n"


def render_summary(report: Mapping[str, Any]) -> str:
    """A few terminal lines: one per conclusion with its worst weak point."""
    lines = []
    for conclusion in report["conclusions"]:
        status = conclusion["status"]
        lines.append(
            f"{STATUS_SYMBOL[status]} {conclusion['label']} {STATUS[status]}: {_short(conclusion['statement'], 110)}"
        )
        for item in conclusion["flagged"][:3]:
            lines.append(
                f"    {SYMBOL[item['label']]} {NAME[item['label']]} via {' → '.join(item['via'])}: "
                f"{_short(item['reason'], 150)}"
            )
        if len(conclusion["flagged"]) > 3:
            lines.append(f"    … {len(conclusion['flagged']) - 3} more in the report")
    links = report["summary"]["links"]
    lines.append("Links: " + " · ".join(f"{links[label]} {NAME[label]}" for label in ORDER))
    reasoning = report["checker"]["reasoning"]
    if reasoning.get("mode") != "verifier":
        lines.append(
            f"Model checks not run: {reasoning.get('not_run_reason') or 'no model verifier'}"
        )
    return "\n".join(lines)
