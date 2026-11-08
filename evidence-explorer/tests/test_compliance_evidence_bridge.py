from copy import deepcopy
from hashlib import sha256

import pytest

from evidence_explorer.compliance_review.evidence_bridge import project_saved_bundle


def saved_bundle():
    text = "A fictional saved statement."
    return {
        "case": {"id": "case", "title": "Example", "question": "Review?", "synthetic": True},
        "records": [
            {
                "id": "record",
                "kind": "post",
                "source_label": "Fictional post",
                "text": text,
                "content_hash": sha256(text.encode()).hexdigest(),
                "published_at": "2026-01-04T00:00:00Z",
            }
        ],
        "fragments": [
            {
                "id": "fragment",
                "record_id": "record",
                "selector": {"kind": "text", "start": 2, "end": 11},
                "quote": "fictional",
            }
        ],
        "runs": [
            {
                "id": "run",
                "input_fingerprint": "input",
                "input_manifest": ["record"],
                "method": "swarm_detector.detector.analyze",
                "method_version": "1",
                "created_at": "2026-01-05T00:00:00Z",
                "full_inputs_available": False,
                "coverage": {"note": "partial input"},
            }
        ],
        "findings": [
            {
                "snapshot_id": "finding",
                "lineage_id": "lineage",
                "version": 1,
                "run_id": "run",
                "kind": "observation",
                "statement": "Observed timing pattern",
                "details": {"domain": "swarm"},
            },
            {
                "snapshot_id": "unsupported",
                "lineage_id": "other",
                "version": 1,
                "run_id": "run",
                "kind": "inference",
                "statement": "Unsupported candidate",
            },
        ],
        "links": [
            {
                "id": "link",
                "source": {"type": "fragment", "id": "fragment"},
                "target": {"type": "finding", "id": "finding"},
                "relation": "supports",
                "fragment_ids": ["fragment"],
                "basis": "Exact supporting excerpt",
                "run_id": "run",
            }
        ],
        "reviews": [
            {
                "id": "review",
                "finding_snapshot_id": "finding",
                "run_id": "run",
                "reviewer": "Analyst",
                "disposition": "qualify",
                "reason": "Inputs are incomplete",
                "timestamp": "2026-01-06T00:00:00Z",
            }
        ],
    }


def test_bridge_keeps_exact_evidence_and_incomplete_swarm_context():
    result = project_saved_bundle(saved_bundle(), run_ids=["run"])
    row = result["records"][0]
    finding = row["swarm"][0]
    assert row["id"] == "record"
    assert row["kind"] == "post"
    assert row["content_hash"] == saved_bundle()["records"][0]["content_hash"]
    assert finding["producer_reports_full_inputs"] is False
    assert finding["run_coverage"] == {"note": "partial input"}
    assert finding["evidence_links"][0]["fragments"][0]["quote"] == "fictional"
    assert finding["baseline_reviews"][0]["disposition"] == "qualify"
    assert [item["finding_id"] for item in result["unlinked_findings"]] == ["unsupported"]
    assert "annotations" not in result


def test_bridge_requires_explicit_runs_and_revalidates_saved_quote():
    bundle = saved_bundle()
    assert project_saved_bundle(bundle, run_ids=[])["records"][0]["swarm"] == []
    with pytest.raises(ValueError, match="Select distinct run IDs"):
        project_saved_bundle(bundle, run_ids=["missing"])
    damaged = deepcopy(bundle)
    damaged["fragments"][0]["quote"] = "different"
    with pytest.raises(ValueError, match="Quotation mismatch"):
        project_saved_bundle(damaged, run_ids=["run"])


def test_bridge_does_not_infer_a_link_from_matching_text():
    bundle = saved_bundle()
    bundle["links"] = []
    result = project_saved_bundle(bundle, run_ids=["run"])
    assert result["records"][0]["swarm"] == []
    assert {item["finding_id"] for item in result["unlinked_findings"]} == {
        "finding",
        "unsupported",
    }
