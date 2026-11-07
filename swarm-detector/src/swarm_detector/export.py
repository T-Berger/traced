"""Render selected-case Swarm reports in Markdown, JSON, or CSV.

These reports preserve specialist run and review fields from supplied detail.
They are distinct from the shared EvidenceBundle/case-package format built by
Evidence Explorer's Swarm adapter, and do not contain the full background input
needed to recompute detection. Rendering performs no storage writes.
"""

import csv
import io
import json


def _safe_cell(value):
    # CSV evidence may be opened in spreadsheets; comments must remain data.
    text = str(value) if value is not None else ""
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def case_record(detail):
    """Project specialist case detail after checking pair-to-message references."""
    case, run, dataset = detail["case"], detail["run"], detail["dataset"]
    selected_refs = {m["message_key"] for m in detail["messages"]}
    pair_refs = {
        event[key]
        for pair in detail["pairs"]
        for event in pair["events"]
        for key in ("message_a", "message_b")
    }
    if not pair_refs <= selected_refs:
        raise ValueError("An evidence reference does not resolve to the selected case.")
    return {
        "schema_version": 1,
        "synthetic": bool(dataset.get("synthetic")),
        "dataset_id": dataset["dataset_id"],
        "dataset_label": dataset["label"],
        "case": case,
        "run_id": run["run_id"],
        "config": run["config"],
        "input_fingerprint": run["input_fingerprint"],
        "source_fingerprint": run.get("source_fingerprint"),
        "software_versions": run.get("software_versions", {}),
        "dataset_manifest": dataset.get("manifest", {}),
        "review": detail["review"],
        "accounts": detail["accounts"],
        "posts": detail["posts"],
        "messages": detail["messages"],
        "pairs": detail["pairs"],
        "counterevidence": detail["counterevidence"],
        "sensitivity": detail["sensitivity"],
        "content_corroboration": detail["content_matches"],
        "concentration": detail["concentration"],
        "diagnostics": run["diagnostics"],
        "interpretation": "Candidate coordination does not establish automation, deception, foreign control, or an operator.",
    }


def export_case(detail, format="markdown"):
    record = case_record(detail)
    if format == "json":
        return json.dumps(record, ensure_ascii=False, indent=2)
    if format == "csv":
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        fields = (
            "synthetic",
            "run_id",
            "case_id",
            "message_key",
            "account_key",
            "post_key",
            "timestamp_utc",
            "source_ref",
            "text_original",
        )
        writer.writerow(fields)
        for message in record["messages"]:
            writer.writerow(
                [
                    _safe_cell(value)
                    for value in (
                        record["synthetic"],
                        record["run_id"],
                        record["case"]["case_id"],
                        *[message.get(key, "") for key in fields[3:]],
                    )
                ]
            )
        return output.getvalue()
    if format not in ("markdown", "md"):
        raise ValueError("Export format must be markdown, json, or csv.")
    case = record["case"]
    lines = [
        f"# {case['name']}",
        "",
        "**SYNTHETIC DEMONSTRATION — fictional accounts and discussions.**"
        if record["synthetic"]
        else "**Observed activity in the imported dataset.**",
        "",
        f"Dataset: {record['dataset_label']}",
        f"Run: `{record['run_id']}`",
        f"Case: `{case['case_id']}`",
        "",
        record["interpretation"],
        "",
        "## Review",
        "",
    ]
    review = record["review"]
    if review:
        lines += [
            f"Decision: {review['decision']}",
            f"Reviewer: {review['reviewer']}",
            f"Reason: {review['reason']}",
            f"Revision: {review.get('revision', '')}",
            f"Updated: {review['updated_at']}",
        ]
        if review.get("claim"):
            lines += [
                "",
                f"Reviewed claim: {review['claim']}",
                f"Endorsement: {review['endorsement']}",
                f"Narrative alignment: {review['alignment']}",
                f"Context and rationale: {review['narrative_notes']}",
                f"Supporting comments: {', '.join(review['evidence_refs'])}",
            ]
    else:
        lines += ["Not reviewed. Narrative alignment remains unestablished."]
    lines += [
        "",
        "## Group and observed participation",
        "",
        "Core accounts: " + ", ".join(case["account_keys"]),
        "Uncertain associations: "
        + (", ".join(case.get("peripheral_keys", [])) or "None displayed"),
        "",
    ]
    concentration = record["concentration"]
    lines += [
        f"The selected accounts produced {concentration['group_comments']} of {concentration['thread_comments']} observed comments in the selected threads ({concentration['share']:.1%}). This is participation, not persuasion.",
        "",
        "## Repeated pair evidence",
        "",
    ]
    for pair in record["pairs"]:
        lines += [
            f"- {pair['account_a']} / {pair['account_b']}: {pair['observed']} distinct families out of {pair['opportunities']} shared opportunities; randomized mean {pair['null_mean']:.2f}, 95th percentile {pair['null_p95']:.2f}; retained={pair['retained']}."
        ]
        for event in pair["events"]:
            lines += [
                f"  - {event['post_key']}: {event['message_a']} at {event['timestamp_a']}; {event['message_b']} at {event['timestamp_b']}; gap {event['delta_seconds']:.0f} seconds."
            ]
    lines += [
        "",
        "## Counterevidence and limitations",
        "",
        *[f"- {note}" for note in record["counterevidence"]],
        "",
        "## Source comments",
        "",
    ]
    for message in record["messages"]:
        # Imported content is quoted, not used as Markdown structure.
        quoted = "\n".join("> " + line for line in message.get("text_original", "").splitlines())
        lines += [
            f"**{message['message_key']}** · {message['account_key']} · {message['timestamp_utc']}",
            f"Source reference: `{message['source_ref']}`",
            "",
            quoted,
            "",
        ]
    lines += [
        "## Reproducibility",
        "",
        "```json",
        json.dumps(
            {
                key: record[key]
                for key in (
                    "config",
                    "input_fingerprint",
                    "source_fingerprint",
                    "software_versions",
                    "dataset_manifest",
                )
            },
            ensure_ascii=False,
            indent=2,
        ),
        "```",
        "",
    ]
    return "\n".join(lines)
