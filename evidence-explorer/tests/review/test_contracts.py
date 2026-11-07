import hashlib

import pytest
from pydantic import ValidationError

from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.demo import load_demo_bundle


def test_demo_is_complete_and_explicitly_synthetic():
    bundle = load_demo_bundle()
    assert bundle.case.synthetic is True
    assert len(bundle.records) == 10
    assert len(bundle.findings) == 3
    assert len(bundle.fragments) == 10
    assert bundle.case.coverage["original_agent_search_history"] == "unavailable"
    assert all(record.source_url is None for record in bundle.records)
    assert EvidenceBundle.model_validate_json(bundle.model_dump_json()) == bundle


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("quote", "Quotation mismatch"),
        ("hash", "Content hash mismatch"),
        ("reference", "Unresolved finding"),
        ("cycle", "Cyclic finding dependency"),
        ("version", "Unsupported evidence bundle major version"),
        ("span", "Invalid text span"),
        ("duplicate", "Duplicate IDs"),
    ],
)
def test_invalid_bundles_are_rejected(mutation, expected):
    data = load_demo_bundle().model_dump(mode="json")
    if mutation == "quote":
        data["fragments"][0]["quote"] = "A fabricated quotation."
    elif mutation == "hash":
        data["records"][0]["text"] += " Changed after capture."
    elif mutation == "reference":
        data["links"][0]["target"]["id"] = "missing:finding"
    elif mutation == "cycle":
        data["links"].append(
            dict(
                id="cycle",
                source={"type": "finding", "id": "harbor:c3-v1"},
                target={"type": "finding", "id": "harbor:c1-v1"},
                relation="depends_on",
                basis="cycle",
            )
        )
    elif mutation == "version":
        data["schema_version"] = "2.0"
    elif mutation == "span":
        data["fragments"][0]["selector"]["end"] = 100_000
    elif mutation == "duplicate":
        data["records"].append(data["records"][0])
    with pytest.raises(ValidationError, match=expected):
        EvidenceBundle.model_validate(data)


def test_additive_specialist_metadata_survives_round_trip():
    data = load_demo_bundle().model_dump(mode="json")
    data["case"]["specialist_context"] = {"exercise_week": "Week 2", "capture_date": None}
    data["records"][0]["supplied_local_reference"] = "archive/domain.txt"
    loaded = EvidenceBundle.model_validate(data)
    assert loaded.model_dump()["case"]["specialist_context"]["capture_date"] is None
    assert loaded.model_dump()["records"][0]["supplied_local_reference"] == "archive/domain.txt"


def test_hash_uses_exact_saved_text_not_normalized_duplicate_text():
    data = load_demo_bundle().model_dump(mode="json")
    data["records"][0]["text"] += "\n"
    data["records"][0]["content_hash"] = hashlib.sha256(
        data["records"][0]["text"].encode()
    ).hexdigest()
    # Existing span still refers to its original, unchanged substring.
    assert EvidenceBundle.model_validate(data).records[0].text.endswith("\n")


@pytest.mark.parametrize("cyclic", [False, True])
def test_deep_finding_dependencies_do_not_depend_on_recursion_limit(cyclic):
    data = load_demo_bundle().model_dump(mode="json")
    template = data["findings"][0]
    data["findings"] = [{**template, "snapshot_id": f"chain:{index}"} for index in range(1500)]
    data["links"] = [
        {
            "id": f"chain-link:{index}",
            "source": {"type": "finding", "id": f"chain:{index}"},
            "target": {"type": "finding", "id": f"chain:{index + 1}"},
            "relation": "depends_on",
            "basis": "Previous finding is a premise.",
        }
        for index in range(1499)
    ]
    data["reviews"] = []
    if cyclic:
        data["links"][-1]["target"]["id"] = "chain:0"
        with pytest.raises(ValidationError, match="Cyclic finding dependency"):
            EvidenceBundle.model_validate(data)
    else:
        assert len(EvidenceBundle.model_validate(data).findings) == 1500


def test_nested_instances_are_revalidated_after_mutation():
    bundle = load_demo_bundle()
    bundle.records[0].text += " Changed after validation."
    with pytest.raises(ValidationError, match="Content hash mismatch"):
        EvidenceBundle.model_validate(bundle)
