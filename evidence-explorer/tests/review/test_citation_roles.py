"""Readable exports retain every saved role when exact passages overlap."""

import hashlib
from copy import deepcopy

from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.integrations.briefs import render_brief
from evidence_explorer.review.integrations.packages import export_case_package, read_case_package
from evidence_explorer.review.workflow import _citations


def bundle_data():
    texts = {
        "source": "The launch occurred on 6 November — exact saved wording.",
        "context": "The launch schedule was provisional.",
        "premise-source": "This source concerns the separate premise.",
    }
    fragments = [
        {
            "id": fid,
            "record_id": rid,
            "selector": {"kind": "text", "start": 0, "end": len(texts[rid])},
            "quote": texts[rid],
        }
        for fid, rid in (
            ("support", "source"),
            ("counter", "source"),
            ("objection", "source"),
            ("context", "context"),
            ("premise", "premise-source"),
        )
    ]
    findings = [
        {
            "snapshot_id": fid,
            "lineage_id": fid,
            "version": 1,
            "run_id": "run",
            "kind": "inference",
            "statement": statement,
            "details": {
                "domain": "claim_check",
                "assessment": {
                    "citations": citations,
                    "metadata": {"challenge": {"findings": [{"fragment_ids": ["objection"]}]}}
                    if fid == "selected"
                    else {},
                },
            },
        }
        for fid, statement, citations in (
            (
                "selected",
                "The selected claim needs review.",
                [
                    {"fragment_id": "support", "stance": "supports"},
                    {"fragment_id": "counter", "stance": "contradicts"},
                    {"fragment_id": "context", "stance": "context"},
                ],
            ),
            (
                "premise-finding",
                "The separate premise needs review.",
                [{"fragment_id": "premise", "stance": "supports"}],
            ),
        )
    ]
    links = [
        {
            "id": fid + "-link",
            "source": {"type": "fragment", "id": fid},
            "target": {"type": "finding", "id": target},
            "relation": relation,
            "fragment_ids": [fid],
            "basis": "Declared audit fixture relationship.",
        }
        for fid, target, relation in (
            ("support", "selected", "supports"),
            ("counter", "selected", "contradicts"),
            ("objection", "selected", "depends_on"),
            ("context", "selected", "depends_on"),
            ("premise", "premise-finding", "supports"),
        )
    ]
    links.append(
        {
            "id": "premise-dependency",
            "source": {"type": "finding", "id": "premise-finding"},
            "target": {"type": "finding", "id": "selected"},
            "relation": "depends_on",
            "basis": "The selected finding depends on this separate finding.",
        }
    )
    return EvidenceBundle.model_validate(
        {
            "case": {"id": "roles", "title": "Role audit", "question": "What is declared?"},
            "records": [
                {
                    "id": rid,
                    "kind": "article",
                    "source_label": rid,
                    "text": text,
                    "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                }
                for rid, text in texts.items()
            ],
            "fragments": fragments,
            "findings": findings,
            "links": links,
            "runs": [
                {
                    "id": "run",
                    "input_fingerprint": "audit",
                    "input_manifest": list(texts),
                    "method": "offline_fixture",
                    "method_version": "1",
                    "created_at": "2026-11-07T00:00:00Z",
                }
            ],
        }
    ).model_dump(mode="json")


def test_same_span_retains_opposing_roles_objection_and_exact_fragment_ids():
    bundle = bundle_data()
    before = deepcopy(bundle)
    rows = _citations(bundle, "selected", set())
    row = next(row for row in rows if row["record_id"] == "source")
    assert len([row for row in rows if row["record_id"] == "source"]) == 1
    assert row["evidence_roles"] == ["candidate_objection", "contradicts", "supports"]
    assert set(row["fragment_ids"]) == {"support", "counter", "objection"}
    assert row["fragment_id"] == "counter"  # Existing deterministic scalar stays compatible.
    assert row["evidence_role"] == "contradicts"
    for fid in row["fragment_ids"]:
        fragment = next(item for item in bundle["fragments"] if item["id"] == fid)
        assert fragment["selector"] == row["selector"]
        assert fragment["quote"] == row["quote"]
    assert bundle == before


def test_same_fragment_can_retain_more_than_one_declared_stance():
    bundle = bundle_data()
    citations = bundle["findings"][0]["details"]["assessment"]["citations"]
    citations[1]["fragment_id"] = "support"
    row = next(row for row in _citations(bundle, "selected", set()) if row["record_id"] == "source")
    assert {"supports", "contradicts", "candidate_objection"} <= set(row["evidence_roles"])


def test_premise_stance_is_not_promoted_to_selected_claim_and_context_stays_context():
    rows = {row["record_id"]: row for row in _citations(bundle_data(), "selected", set())}
    assert rows["premise-source"]["evidence_roles"] == ["assessment_input"]
    assert rows["context"]["evidence_roles"] == ["context"]


def test_exclusion_removes_all_roles_and_aliases_of_the_excluded_record():
    rows = _citations(bundle_data(), "selected", {"source"})
    assert {row["record_id"] for row in rows} == {"context", "premise-source"}
    assert not {"support", "counter", "objection"}.intersection(
        fid for row in rows for fid in row["fragment_ids"]
    )


def projection(bundle, citations):
    return {
        "case": bundle["case"],
        "assessment": {"label": "Audit", "version": 1, "base_run_id": "run"},
        "included": [
            {
                "statement": "The selected claim needs review.",
                "finding_id": "selected",
                "kind": "inference",
                "disposition": "qualify",
                "review": {"reviewer": "Offline fixture", "created_at": "2026-11-07"},
                "reason": "Review all recorded evidence roles.",
                "evidence": citations,
            }
        ],
        "omitted": [],
        "excluded_record_ids": [],
        "coverage": {},
        "limitations": [],
        "methods": [],
    }


def test_readable_brief_lists_all_roles_and_ids_but_quotes_each_span_once():
    bundle = bundle_data()
    rows = _citations(bundle, "selected", set())
    rendered = render_brief(projection(bundle, rows))
    for label in (
        "Supports the checked claim (model proposal)",
        "Contradicts the checked claim (model proposal)",
        "Candidate objection from the second model pass",
    ):
        assert f"Evidence role: {label}." in rendered
    assert rendered.count("> The launch occurred on 6 November — exact saved wording.") == 1
    assert "Exact fragment references:" in rendered
    assert all(f"`{fid}`" in rendered for fid in ("support", "counter", "objection"))
    legacy = deepcopy(rows)
    for row in legacy:
        row.pop("evidence_roles")
        row.pop("fragment_ids")
    assert "Contradicts the checked claim (model proposal)" in render_brief(
        projection(bundle, legacy)
    )


def test_native_package_reopen_reproduces_role_projection_without_changing_evidence(tmp_path):
    bundle = EvidenceBundle.model_validate(bundle_data())
    before = bundle.model_dump(mode="json")
    reopened = read_case_package(
        export_case_package(bundle), tmp_path, install_artifacts=False
    ).bundle
    assert reopened == bundle
    assert _citations(reopened.model_dump(mode="json"), "selected", set()) == _citations(
        before, "selected", set()
    )
