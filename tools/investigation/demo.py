"""Plant one bad assumption in a bundle, to show the checker catching it.

The planted assumption is fictional and labelled as such everywhere a person
looks: it belongs to its own run (method ``planted_for_checker_demo``), its
details say it was planted, and the case limitations name it. The checker never
reads those labels; the answer key is written to a separate file so a demo can
show that the catch was blind (the tests check the labels make no difference).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from evidence_explorer.review.contracts import EvidenceBundle

from .bundle import digest

PLANT_METHOD = "planted_for_checker_demo"


def plant_assumption(
    bundle: Mapping[str, Any],
    *,
    statement: str,
    into: str,
    created_at: str,
    reason: str = "Planted for a checker demonstration.",
) -> tuple[dict, dict]:
    """Return a copy of ``bundle`` where finding ``into`` relies on one new assumption."""
    data = copy.deepcopy(dict(bundle))
    findings = data["findings"]

    def label_of(finding: Mapping[str, Any]) -> str | None:
        details = finding.get("details")
        return details.get("label") if isinstance(details, dict) else None

    target = next((finding for finding in findings if label_of(finding) == into), None)
    if target is None:
        known = ", ".join(sorted(filter(None, map(label_of, findings))))
        raise ValueError(f"No finding is labelled {into}. Labels: {known}")
    if target.get("kind") == "observation":
        raise ValueError("Plant into a claim or a step, not a collection observation.")
    existing = set(filter(None, map(label_of, findings)))
    number = 1
    while f"A{number}" in existing:
        number += 1
    label = f"A{number}"
    run_id = "demo:run:" + digest([data["case"]["id"], statement, into])
    data["runs"].append(
        {
            "id": run_id,
            "input_fingerprint": digest([]),
            "input_manifest": [],
            "method": PLANT_METHOD,
            "method_version": "1",
            "created_at": created_at,
            "full_inputs_available": True,
            "recomputation_capability": "dependency_review_only",
            "configuration": {"planted_into": into},
            "coverage": {},
            "status": "completed",
        }
    )
    finding_id = "demo:finding:" + digest([run_id, label, statement])
    findings.append(
        {
            "snapshot_id": finding_id,
            "lineage_id": f"demo:{label}",
            "version": 1,
            "run_id": run_id,
            "kind": "assumption",
            "statement": statement.strip(),
            "details": {
                "label": label,
                "role": "assumption",
                "reason": reason,
                "planted": True,
                "provenance": "Planted for a checker demonstration; not written by the investigator.",
            },
        }
    )
    data["links"].append(
        {
            "id": "demo:link:" + digest([finding_id, target["snapshot_id"]]),
            "source": {"type": "finding", "id": finding_id},
            "target": {"type": "finding", "id": target["snapshot_id"]},
            "relation": "depends_on",
            "fragment_ids": [],
            "basis": f"{into} is made to rely on planted assumption {label}.",
            "run_id": run_id,
        }
    )
    data["case"]["limitations"] = [
        *data["case"].get("limitations", []),
        f"Contains planted assumption {label} for a checker demonstration; it is fictional and "
        "not an investigator output.",
    ]
    EvidenceBundle.model_validate(data)
    key = {
        "schema": "traced.investigation.planted/1",
        "planted": [
            {"label": label, "finding_id": finding_id, "statement": statement.strip(), "into": into}
        ],
    }
    return data, key


def strip_plant_labels(bundle: Mapping[str, Any]) -> dict:
    """The same bundle without any planted marker (to show the checker never uses them)."""
    data = copy.deepcopy(dict(bundle))
    for finding in data["findings"]:
        details = finding.get("details")
        if isinstance(details, dict):
            details.pop("planted", None)
            details.pop("provenance", None)
            if "reason" in details and "Planted" in str(details["reason"]):
                details["reason"] = ""
    data["case"]["limitations"] = [
        item for item in data["case"].get("limitations", []) if "planted" not in item.casefold()
    ]
    for run in data["runs"]:
        if run.get("method") == PLANT_METHOD:
            run["method"] = "assumption"
            run["configuration"] = {}
    return data
