"""Review semantics, provenance distinctions, and verifier boundaries."""

import hashlib

import pytest

from evidence_explorer.review import analysis
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.verifier import OpenRouterVerifier


def make_bundle(*, synthetic=True) -> EvidenceBundle:
    texts = {
        "r1": "Registry lists Harbor House at 4 Quay Street.",
        "r2": "Harbor House is a shared business centre.",
        "r3": "Original post: read the port announcement.",
        "r4": "Original post: read the port announcement.",
    }
    records = [
        {
            "id": rid,
            "kind": "post",
            "source_label": rid,
            "text": text,
            "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        }
        for rid, text in texts.items()
    ]
    fragments = [
        {
            "id": f"p{rid[1:]}",
            "record_id": rid,
            "quote": text,
            "selector": {"kind": "text", "start": 0, "end": len(text)},
        }
        for rid, text in texts.items()
    ]
    findings = [
        {
            "snapshot_id": fid,
            "lineage_id": fid,
            "version": 1,
            "run_id": "run1",
            "kind": "inference",
            "statement": statement,
            "details": {"demo_check": []},
        }
        for fid, statement in [
            ("c1", "Company X controls the accounts."),
            ("c2", "Company X directs the campaign."),
            ("c3", "The accounts shared the port announcement."),
        ]
    ]
    findings[0]["details"]["demo_check"] = [
        {
            "finding_type": "unsupported_inference",
            "explanation": "A shared business address does not establish account control.",
            "fragment_ids": ["p2"],
            "quotes": [texts["r2"]],
        }
    ]

    def link(lid, source_type, source, target_type, target, relation, fragments=None):
        return {
            "id": lid,
            "source": {"type": source_type, "id": source},
            "target": {"type": target_type, "id": target},
            "relation": relation,
            "fragment_ids": fragments or [],
            "basis": "Fixture relation",
            "run_id": "run1",
        }

    return EvidenceBundle.model_validate(
        {
            "case": {
                "id": "test",
                "title": "Synthetic review",
                "question": "Who controls it?",
                "synthetic": synthetic,
            },
            "records": records,
            "fragments": fragments,
            "runs": [
                {
                    "id": "run1",
                    "input_fingerprint": "fixture",
                    "input_manifest": list(texts),
                    "method": "fixture",
                    "method_version": "1",
                    "created_at": "2026-11-06",
                }
            ],
            "findings": findings,
            "links": [
                link("l1", "fragment", "p1", "finding", "c1", "supports"),
                link("l2", "record", "r2", "finding", "c1", "supports", ["p2"]),
                link("l3", "finding", "c1", "finding", "c2", "depends_on"),
                link("l4", "record", "r3", "finding", "c3", "supports", ["p3"]),
                link("l5", "finding", "c1", "finding", "c3", "contradicts"),
                link("l6", "record", "r1", "record", "r3", "similar_wording"),
                link("l7", "record", "r3", "record", "r4", "documented_repost"),
            ],
        }
    )


def objection(**changes):
    return {
        "finding_type": "unsupported_inference",
        "explanation": "Address does not prove control.",
        "fragment_ids": ["p2"],
        "quotes": ["Harbor House is a shared business centre."],
        **changes,
    }


def test_exclusion_follows_only_dependencies_and_preserves_alternative_evidence():
    bundle = make_bundle()
    before = bundle.model_dump()
    result = analysis.compute_impact(bundle, ["r1"])
    assert result["affected_finding_ids"] == ["c1", "c2"]
    assert result["remaining_evidence"]["c1"] == ["r2"]
    assert result["remaining_evidence"]["c2"] == ["r2"]
    assert result["remaining_evidence"]["c3"] == ["r3"]
    assert bundle.model_dump() == before
    assert "verdict" not in result


def test_scenario_computations_do_not_leak_exclusions():
    bundle = make_bundle()
    analysis.compute_impact(bundle, ["r1", "r2"])
    other = analysis.compute_impact(bundle, ["r3"])
    assert other["affected_finding_ids"] == ["c3"]
    assert other["remaining_evidence"]["c1"] == ["r1", "r2"]
    restored = analysis.compute_impact(bundle, [])
    assert restored["affected_finding_ids"] == []


def test_unknown_exclusion_fails_explicitly():
    with pytest.raises(analysis.CheckError, match="Unknown excluded record"):
        analysis.compute_impact(make_bundle(), ["missing"])


def test_equal_text_and_documented_lineage_are_separate():
    bundle = make_bundle()
    before = bundle.model_dump()
    groups = analysis.group_sources(bundle)
    exact = next(g for g in groups if g["kind"] == "exact_text")
    lineage = next(g for g in groups if g["kind"] == "documented_repost")
    assert exact["record_ids"] == ["r3", "r4"]
    assert exact["origin_record_id"] is None
    assert exact["independence"] == "unknown"
    assert lineage["origin_record_id"] == "r3"
    assert lineage["record_ids"] == ["r3", "r4"]
    assert all("r1" not in group["record_ids"] for group in groups)
    assert bundle.model_dump() == before


@pytest.mark.parametrize("source,target", [("r4", "r3"), ("r2", "r4")])
def test_cyclic_or_conflicting_repost_lineage_has_unknown_origin(source, target):
    raw = make_bundle().model_dump()
    raw["links"].append(
        {
            "id": "conflict",
            "source": {"type": "record", "id": source},
            "target": {"type": "record", "id": target},
            "relation": "documented_repost",
            "basis": "Contradictory source metadata",
        }
    )
    bundle = EvidenceBundle.model_validate(raw)
    lineage = [g for g in analysis.group_sources(bundle) if g["kind"] == "documented_repost"]
    assert len(lineage) == 1
    assert lineage[0]["origin_record_id"] is None


@pytest.mark.parametrize(
    "item,message",
    [
        (objection(fragment_ids=["missing"]), "unknown fragment"),
        (objection(quotes=["Company X owns all accounts."]), "does not match"),
        (objection(quotes=[""]), "does not match"),
        (objection(fragment_ids=["p1", "p2"]), "exactly one"),
    ],
)
def test_invalid_citation_is_rejected(item, message):
    with pytest.raises(analysis.CheckError, match=message):
        analysis.validate_check(make_bundle(), {"findings": [item]})


def test_verifier_cannot_cite_excluded_or_unseen_passage():
    for scope, message in [
        ({"fragment_ids": ["p1"]}, "outside its search scope"),
        ({"excluded_record_ids": ["r2"]}, "excluded record"),
    ]:
        with pytest.raises(analysis.CheckError, match=message):
            analysis.validate_check(
                make_bundle(), {"findings": [objection()], "search_scope": scope}
            )


def test_demo_is_explicit_and_never_substituted_for_real_case():
    result = analysis.challenge_finding(make_bundle(), "c1")
    assert result["mode"] == "demo"
    assert "preset" in result["label"]
    assert result["findings"][0]["fragment_ids"] == ["p2"]
    assert result["search_scope"]["bundle_fingerprint"] == make_bundle().fingerprint()
    with pytest.raises(analysis.CheckError, match="explicitly synthetic"):
        analysis.challenge_finding(make_bundle(synthetic=False), "c1")


def test_demo_reports_omitted_presets_after_exclusion():
    result = analysis.challenge_finding(make_bundle(), "c1", ["r2"])
    assert result["findings"] == []
    assert result["search_scope"]["omitted_fixture_findings"] == 1
    assert result["search_scope"]["excluded_record_ids"] == ["r2"]


def test_real_provider_missing_configuration_fails_without_fallback(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("EVIDENCE_REVIEW_MODEL", raising=False)
    with pytest.raises(analysis.CheckError, match="requires OPENROUTER_API_KEY"):
        analysis.challenge_finding(make_bundle(synthetic=False), "c1", mode="openrouter")


class FakeVerifier:
    model = "configured/model"

    def __init__(self, findings):
        self.findings = findings
        self.statement = None
        self.passages = []

    def check(self, statement, passages):
        self.statement = statement
        self.passages = passages
        return {"findings": self.findings}


def test_real_review_returns_versioned_scope_and_exact_quotes():
    verifier = FakeVerifier([objection()])
    result = analysis.challenge_finding(
        make_bundle(synthetic=False), "c1", mode="openrouter", verifier=verifier
    )
    assert result["mode"] == "openrouter"
    assert result["search_scope"]["finding_snapshot_id"] == "c1"
    assert result["search_scope"]["run_id"] == "run1"
    assert result["search_scope"]["input_bytes"] <= analysis.MAX_PASSAGE_BYTES
    assert result["search_scope"]["model"] == verifier.model
    assert len(verifier.passages) <= analysis.MAX_FRAGMENTS
    assert verifier.statement == "Company X controls the accounts."
    assert result["findings"][0]["quotes"] == ["Harbor House is a shared business centre."]


def test_real_verifier_cannot_cite_an_unseen_tail_of_truncated_text():
    raw = make_bundle(synthetic=False).model_dump()
    text = "A" * (analysis.MAX_FRAGMENT_BYTES + 100) + " hidden tail"
    raw["records"][1]["text"] = text
    raw["records"][1]["content_hash"] = hashlib.sha256(text.encode()).hexdigest()
    raw["fragments"][1]["quote"] = text
    raw["fragments"][1]["selector"]["end"] = len(text)
    bundle = EvidenceBundle.model_validate(raw)
    verifier = FakeVerifier([objection(quotes=["hidden tail"])])
    with pytest.raises(analysis.CheckError, match="not in its supplied input"):
        analysis.challenge_finding(bundle, "c1", mode="openrouter", verifier=verifier)


def test_configured_model_identity_does_not_change_with_environment(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only-secret")
    monkeypatch.setenv("EVIDENCE_REVIEW_MODEL", "original/model")
    verifier = OpenRouterVerifier.from_env()
    monkeypatch.setenv("EVIDENCE_REVIEW_MODEL", "changed/model")
    verifier.check = lambda *_: {"findings": []}
    result = analysis.challenge_finding(
        make_bundle(synthetic=False), "c1", mode="openrouter", verifier=verifier
    )
    assert result["search_scope"]["model"] == "original/model"
