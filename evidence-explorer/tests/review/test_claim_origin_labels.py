"""Claim origin belongs to each saved finding, independently of its case and records."""

from __future__ import annotations

import hashlib

import pytest

from evidence_explorer.config import Settings
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.review.origins import claim_origin_label, claim_synthetic_origin
from evidence_explorer.review.workflow import brief_projection
from evidence_explorer.review.workspace_views import brief_preview

SYNTHETIC = "Synthetic test claim"
COLLECTED = "Declared non-synthetic claim; factual accuracy requires review"
UNKNOWN = "Claim origin not specified"
_MISSING = object()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, SYNTHETIC),
        (False, COLLECTED),
        (_MISSING, UNKNOWN),
        (None, UNKNOWN),
        ("true", UNKNOWN),
        ("false", UNKNOWN),
        ("", UNKNOWN),
        (1, UNKNOWN),
        (0, UNKNOWN),
        ([], UNKNOWN),
        ({}, UNKNOWN),
    ],
    ids=[
        "true",
        "false",
        "missing",
        "null",
        "text-true",
        "text-false",
        "empty",
        "one",
        "zero",
        "list",
        "mapping",
    ],
)
def test_claim_origin_requires_an_explicit_literal_boolean(value, expected):
    details = {"domain": "claim_check", "claim_check": {"source_synthetic": True}}
    if value is not _MISSING:
        details["synthetic"] = value
    finding = {"snapshot_id": "claim-check:synthetic-looking-id", "details": details}
    assert claim_origin_label(finding) == expected
    assert claim_synthetic_origin(finding) is (
        True if expected == SYNTHETIC else False if expected == COLLECTED else None
    )


@pytest.mark.parametrize("domain", [None, "explorer", "claim_check"])
@pytest.mark.parametrize("declared", [True, False])
def test_origin_extraction_accepts_explicit_booleans_from_any_finding_domain(domain, declared):
    finding = {"details": {"domain": domain, "synthetic": declared}}
    assert claim_synthetic_origin(finding) is declared


@pytest.mark.parametrize("declared", [None, "true", "false", 1, 0])
def test_origin_extraction_does_not_coerce_other_domain_declarations(declared):
    finding = {"details": {"domain": "explorer", "synthetic": declared}}
    assert claim_synthetic_origin(finding) is None


@pytest.mark.parametrize("domain", [None, "explorer", "claim-check"])
def test_other_domains_cannot_acquire_origin_from_claim_check_pinned_metadata(domain):
    finding = {
        "synthetic": True,
        "details": {
            "domain": domain,
            "claim_check": {
                "source_synthetic": True,
                "source_finding_snapshot": {"details": {"synthetic": True}},
            },
        },
    }
    assert claim_synthetic_origin(finding) is None


@pytest.mark.parametrize(
    ("direct", "pinned", "expected"),
    [
        (_MISSING, True, SYNTHETIC),
        (_MISSING, False, COLLECTED),
        (_MISSING, "true", UNKNOWN),
        (_MISSING, 1, UNKNOWN),
        (_MISSING, 0, UNKNOWN),
        (_MISSING, None, UNKNOWN),
        (True, False, SYNTHETIC),
        (False, True, COLLECTED),
        (None, True, UNKNOWN),
        ("false", False, UNKNOWN),
        (1, True, UNKNOWN),
        (0, False, UNKNOWN),
    ],
    ids=[
        "pinned-true",
        "pinned-false",
        "pinned-text",
        "pinned-one",
        "pinned-zero",
        "pinned-null",
        "direct-true-wins",
        "direct-false-wins",
        "null-shadows",
        "text-shadows",
        "one-shadows",
        "zero-shadows",
    ],
)
def test_only_absent_direct_origin_uses_exact_pinned_finding_details(direct, pinned, expected):
    details = {
        "domain": "claim_check",
        "claim_check": {
            "source_synthetic": not bool(pinned),
            "source_finding_snapshot": {"details": {"synthetic": pinned}},
        },
    }
    if direct is not _MISSING:
        details["synthetic"] = direct
    finding = {"details": details}
    assert claim_origin_label(finding) == expected
    assert claim_synthetic_origin(finding) is (
        True if expected == SYNTHETIC else False if expected == COLLECTED else None
    )


def test_pinned_origin_requires_the_exact_saved_details_location():
    finding = {
        "details": {
            "domain": "claim_check",
            "claim_check": {
                "source_synthetic": True,
                "source_finding_snapshot": {"synthetic": True},
            },
        }
    }
    assert claim_origin_label(finding) == UNKNOWN


@pytest.mark.parametrize("domain", [None, "", "claim-check", "other"])
def test_generic_findings_do_not_acquire_claim_labels_from_identifiers_or_metadata(domain):
    finding = {
        "snapshot_id": "claim-check:test",
        "synthetic": True,
        "details": {"domain": domain, "synthetic": True},
    }
    assert claim_origin_label(finding) is None


def workspace(path):
    settings = Settings(data_dir=path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    return WorkspaceService(repository, settings, path / "artifacts")


def mixed_findings_bundle(case_synthetic):
    text = "A saved public source whose origin is independent of the reviewed claims."
    origins = [
        ("included-synthetic", True),
        ("included-collected", False),
        ("included-unknown", _MISSING),
        ("omitted-synthetic", True),
        ("omitted-collected", False),
        ("omitted-unknown", _MISSING),
        ("omitted-numeric", 1),
        ("omitted-string", "false"),
        ("omitted-pinned-synthetic", _MISSING),
        ("omitted-pinned-nonsynthetic", _MISSING),
        ("omitted-shadowed", None),
    ]
    pinned_origins = {
        "omitted-pinned-synthetic": True,
        "omitted-pinned-nonsynthetic": False,
        "omitted-shadowed": True,
    }
    findings = []
    for identifier, origin in origins:
        details = {"domain": "claim_check", "claim_check": {"source_synthetic": True}}
        if origin is not _MISSING:
            details["synthetic"] = origin
        if identifier in pinned_origins:
            details["claim_check"]["source_finding_snapshot"] = {
                "snapshot_id": f"original-{identifier}",
                "details": {"synthetic": pinned_origins[identifier]},
            }
        findings.append(
            {
                "snapshot_id": identifier,
                "lineage_id": identifier,
                "run_id": "saved-run",
                "version": 1,
                "kind": "inference",
                "statement": f"Saved statement for {identifier}.",
                "details": details,
            }
        )
    findings.append(
        {
            "snapshot_id": "generic-finding",
            "lineage_id": "generic-finding",
            "run_id": "saved-run",
            "version": 1,
            "kind": "observation",
            "statement": "A generic observation with separately recorded source provenance.",
            "details": {"synthetic": True},
        }
    )
    return EvidenceBundle.model_validate(
        {
            "case": {
                "id": "claim-check:independent-origins",
                "title": "Independent claim origin labels",
                "question": "Keep every claim's origin separate.",
                "synthetic": case_synthetic,
            },
            "records": [
                {
                    "id": "collected-record",
                    "kind": "article",
                    "source_label": "Saved public source",
                    "text": text,
                    "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                    "metadata": {"synthetic": False},
                }
            ],
            "fragments": [],
            "runs": [
                {
                    "id": "saved-run",
                    "input_fingerprint": "saved-input",
                    "input_manifest": ["collected-record"],
                    "method": "offline_native_fixture",
                    "method_version": "1",
                    "created_at": "2026-11-07T12:00:00Z",
                }
            ],
            "findings": findings,
            "links": [
                {
                    "id": f"link-{finding['snapshot_id']}",
                    "source": {"type": "record", "id": "collected-record"},
                    "target": {"type": "finding", "id": finding["snapshot_id"]},
                    "relation": "supports",
                    "basis": "Saved native reference; no claim origin is implied.",
                }
                for finding in findings
            ],
        }
    )


def component_text(node):
    if isinstance(node, str):
        return node
    if isinstance(node, (list, tuple)):
        return " ".join(component_text(child) for child in node)
    return component_text(getattr(node, "children", ""))


def components(node):
    if isinstance(node, (list, tuple)):
        for child in node:
            yield from components(child)
    elif hasattr(node, "children"):
        yield node
        yield from components(node.children)


def statement_block(document, statement, all_statements):
    start = document.index(statement)
    next_statements = [
        position
        for other in all_statements
        if other != statement
        for position in [document.find(other, start + len(statement))]
        if position >= 0
    ]
    return document[start : min(next_statements)] if next_statements else document[start:]


@pytest.mark.parametrize("case_synthetic", [True, False], ids=["synthetic-case", "ordinary-case"])
def test_independent_origins_survive_included_omitted_exports_and_package_reopening(
    tmp_path, case_synthetic
):
    service = workspace(tmp_path / "source")
    bundle = mixed_findings_bundle(case_synthetic)
    service.import_bundle(bundle.model_dump(mode="json"))
    assessment = service.baseline_scenario(bundle.case.id, "included-synthetic")
    for finding_id, disposition in [
        ("included-synthetic", "use"),
        ("included-collected", "qualify"),
        ("included-unknown", "use"),
        ("omitted-synthetic", "withhold"),
        ("omitted-unknown", "withhold"),
    ]:
        service.review(
            bundle.case.id,
            finding_id,
            assessment["id"],
            disposition,
            "An explicit saved decision for this exact finding.",
        )
    expected = {
        "included-synthetic": SYNTHETIC,
        "included-collected": COLLECTED,
        "included-unknown": UNKNOWN,
        "omitted-synthetic": SYNTHETIC,
        "omitted-collected": COLLECTED,
        "omitted-unknown": UNKNOWN,
        "omitted-numeric": UNKNOWN,
        "omitted-string": UNKNOWN,
        "omitted-pinned-synthetic": SYNTHETIC,
        "omitted-pinned-nonsynthetic": COLLECTED,
        "omitted-shadowed": UNKNOWN,
    }
    statements = {finding.snapshot_id: finding.statement for finding in bundle.findings}

    def check(current_service, scenario_id):
        view = current_service.view(bundle.case.id, scenario_id)
        projection = brief_projection(view)
        rows = {row["finding_id"]: row for row in projection["included"] + projection["omitted"]}
        assert {identifier: rows[identifier]["claim_origin"] for identifier in expected} == expected
        assert "claim_origin" not in rows["generic-finding"]
        assert {row["finding_id"] for row in projection["included"]} == {
            "included-synthetic",
            "included-collected",
            "included-unknown",
        }
        documents = [
            current_service.export_brief(bundle.case.id, scenario_id=scenario_id),
            current_service.export_brief(bundle.case.id),
        ]
        for document in documents:
            for identifier, origin in expected.items():
                block = statement_block(document, statements[identifier], statements.values())
                assert origin in block, (identifier, block)
                assert all(
                    other not in block for other in {SYNTHETIC, COLLECTED, UNKNOWN} - {origin}
                )
            generic = statement_block(document, statements["generic-finding"], statements.values())
            assert not any(origin in generic for origin in (SYNTHETIC, COLLECTED, UNKNOWN))
        preview = brief_preview(view)
        for identifier, statement in statements.items():
            matching = [
                text
                for node in components(preview)
                for text in [component_text(node)]
                if statement in text
                and not any(other in text for other in statements.values() if other != statement)
            ]
            assert matching
            if identifier in expected:
                assert any(expected[identifier] in text for text in matching)
                assert all(
                    other not in text
                    for text in matching
                    for other in {SYNTHETIC, COLLECTED, UNKNOWN} - {expected[identifier]}
                )
            else:
                assert not any(origin in text for text in matching for origin in expected.values())
        for document in documents:
            assert (
                "Record provenance: Collected source; factual accuracy requires review." in document
            )
            assert "The originating claim is synthetic" not in document
            assert (
                "**Selected case: Synthetic example.**" in document
                if case_synthetic
                else ("**Selected case: Synthetic example.**" not in document)
            )

    saved_before = service.get_case(bundle.case.id)
    events_before = service.repository.list_events(bundle.case.id)
    check(service, assessment["id"])
    captured = service.export_bundle(bundle.case.id)
    recipient = workspace(tmp_path / "recipient")
    assert recipient.import_package(service.export_package(bundle.case.id)) == bundle.case.id
    restored = recipient.load_case(bundle.case.id)
    assert restored.model_dump(mode="json") == captured.model_dump(mode="json")
    assert restored.records[0].metadata == {"synthetic": False}
    reopened_assessment = next(
        review.scenario_id
        for review in restored.reviews
        if review.finding_snapshot_id == "included-synthetic"
    )
    check(recipient, reopened_assessment)
    assert service.get_case(bundle.case.id) == saved_before
    assert service.repository.list_events(bundle.case.id) == events_before
