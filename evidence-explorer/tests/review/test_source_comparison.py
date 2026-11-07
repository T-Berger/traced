"""Source comparisons stay grounded, bounded and visibly provisional."""

import hashlib
import sqlite3

import pytest

from evidence_explorer.review.comparison import (
    MAX_CANDIDATES,
    MAX_SENTENCES,
    MAX_SOURCE_CHARS,
    compare_sources,
)
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.service import ReviewService
from evidence_explorer.review.storage import ReviewRepository

from .comparison_fixture import LEFT_TEXT, make_comparison_bundle


def compare_texts(left, right):
    return compare_sources(make_comparison_bundle({"left": left, "right": right}), "left", "right")


def test_numeric_and_negation_candidates_reference_exact_saved_unicode_spans():
    bundle = make_comparison_bundle()
    before = bundle.model_dump(mode="json")
    result = compare_sources(bundle, "left", "right")
    assert {candidate.kind for candidate in result.candidates} == {
        "numeric_difference",
        "negation_difference",
    }
    assert len(result.candidates) == 2
    sources = {record.id: record for record in bundle.records}
    for side in (result.left, result.right):
        saved = sources[side.record_id]
        assert side.text == saved.text
        assert side.content_hash == hashlib.sha256(saved.text.encode()).hexdigest()
        assert side.published_at is None and side.acquired_at is None
    for candidate in result.candidates:
        for passage in (candidate.left, candidate.right):
            saved = sources[passage.record_id].text
            assert saved[passage.start : passage.end] == passage.quote
            assert passage.start > 0, "The fixture includes non-ASCII text before the citation"
            for highlight in passage.highlights:
                assert passage.start <= highlight.start < highlight.end <= passage.end
                relative_start = highlight.start - passage.start
                relative_end = highlight.end - passage.start
                assert (
                    saved[highlight.start : highlight.end]
                    == passage.quote[relative_start:relative_end]
                )
        assert "possible" in candidate.label.lower()
        assert candidate.basis
    numeric = next(item for item in result.candidates if item.kind == "numeric_difference")
    assert "20 vehicles" in numeric.left.quote
    assert "35 vehicles" in numeric.right.quote
    assert {
        sources[passage.record_id].text[highlight.start : highlight.end]
        for passage in (numeric.left, numeric.right)
        for highlight in passage.highlights
        if highlight.kind == "difference"
    } == {"20", "35"}
    assert bundle.model_dump(mode="json") == before


@pytest.mark.parametrize(
    "right",
    [
        "River bridge carries 35 vehicles per hour on Monday.",
        "Harbor bridge carries 35 vehicles per day on Monday.",
        "Harbor bridge carries 35 vehicles per hour on Tuesday.",
        "Harbor bridge carries at least 35 vehicles per hour on Monday.",
        "Harbor bridge carries 35 vehicles per hour in another district.",
    ],
)
def test_changed_entity_unit_qualification_or_context_is_not_a_conflict(right):
    result = compare_texts("Harbor bridge carries 20 vehicles per hour on Monday.", right)
    assert result.candidates == ()
    assert "agreement" in " ".join(result.limitations).lower()


def test_equivalent_units_are_not_misreported_as_incompatible_numbers():
    result = compare_texts(
        "Harbor bridge spans 1 km across the river.",
        "Harbor bridge spans 1000 m across the river.",
    )
    assert result.candidates == ()
    # The deterministic MVP does not claim to prove equivalence by converting units.
    assert "unit" in " ".join(result.limitations).lower()


@pytest.mark.parametrize(
    "left,right",
    [
        (
            "Harbor bridge spans 1.0 km across the river.",
            "Harbor bridge spans 1.00 km across the river.",
        ),
        (
            "Harbor council did not approve the bridge reopening on Monday.",
            "Harbor council did never approve the bridge reopening on Monday.",
        ),
        ("20 cars arrived.", "35 cars arrived."),
    ],
)
def test_equivalent_numbers_existing_negation_and_insufficient_context_are_not_flags(left, right):
    assert compare_texts(left, right).candidates == ()


def test_publication_and_collection_times_do_not_supply_event_context():
    bundle = make_comparison_bundle()
    original = compare_sources(bundle, "left", "right")
    payload = bundle.model_dump(mode="json")
    payload["records"][0].update(
        published_at="2020-01-01T00:00:00Z", acquired_at="2026-11-06T09:00:00Z"
    )
    payload["records"][1].update(
        published_at="2026-11-05T12:00:00Z", acquired_at="2026-11-06T10:00:00Z"
    )
    result = compare_sources(EvidenceBundle.model_validate(payload), "left", "right")
    assert result.candidates == original.candidates
    assert result.left.published_at == "2020-01-01T00:00:00Z"
    assert result.right.acquired_at == "2026-11-06T10:00:00Z"
    assert "event_time" not in result.left.metadata.model_dump()
    assert "event_time" not in result.right.metadata.model_dump()
    limitations = " ".join(result.limitations).lower()
    assert "publication" in limitations and "event" in limitations


def test_comparison_preserves_collection_provenance_and_feed_summary_scope():
    payload = make_comparison_bundle().model_dump(mode="json")
    supplied = {
        "publisher": "Synthetic publisher",
        "author": "Synthetic author",
        "requested_url": "https://example.org/story?utm_source=fixture",
        "final_url": "https://example.org/story",
        "feed_url": "https://example.org/feed.xml",
        "retrieved_at": "2026-11-06T10:30:00Z",
        "publication_source": "feed",
        "content_source": "feed_summary",
    }
    payload["records"][0]["metadata"].update(supplied)
    result = compare_sources(EvidenceBundle.model_validate(payload), "left", "right")
    preserved = result.left.metadata.model_dump()
    assert {key: preserved[key] for key in supplied} == supplied
    assert result.left.published_at is None
    assert result.right.metadata.author is None
    assert result.right.metadata.content_source is None


@pytest.mark.parametrize("case_synthetic", [True, False])
@pytest.mark.parametrize("supplied", [True, False, None, "true", "false", 1, 0, "missing"])
def test_source_origin_is_never_inferred_from_case_label(case_synthetic, supplied):
    payload = make_comparison_bundle().model_dump(mode="json")
    payload["case"]["synthetic"] = case_synthetic
    metadata = payload["records"][0]["metadata"]
    if supplied == "missing":
        metadata.pop("synthetic")
    else:
        metadata["synthetic"] = supplied
    result = compare_sources(EvidenceBundle.model_validate(payload), "left", "right")
    expected = supplied if type(supplied) is bool else None
    assert result.left.metadata.synthetic is expected


def test_numeric_dates_are_only_possible_differences_with_context_limitations():
    result = compare_texts(
        "Harbor bridge inspection started on 2026-11-05.",
        "Harbor bridge inspection started on 2026-11-06.",
    )
    assert len(result.candidates) == 1
    assert result.candidates[0].kind == "numeric_difference"
    limitations = " ".join(result.limitations).lower()
    assert "date" in limitations and "context" in limitations
    assert "contradiction" in limitations


def test_exact_duplicates_do_not_establish_independent_agreement():
    result = compare_texts(LEFT_TEXT, LEFT_TEXT)
    assert result.candidates == ()
    limitations = " ".join(result.limitations).lower()
    assert "independen" in limitations and "agreement" in limitations
    assert result.left.record_id != result.right.record_id
    assert result.left.content_hash == result.right.content_hash


def test_comparison_ids_and_exact_citations_survive_source_order_and_selection_reversal():
    bundle = make_comparison_bundle()
    original = compare_sources(bundle, "left", "right")
    payload = bundle.model_dump(mode="json")
    payload["records"].reverse()
    reordered = compare_sources(EvidenceBundle.model_validate(payload), "left", "right")
    assert reordered == original
    reverse = compare_sources(bundle, "right", "left")
    assert reverse.pair_id == original.pair_id
    assert reverse.left.record_id == "right" and reverse.right.record_id == "left"
    assert {candidate.id for candidate in reverse.candidates} == {
        candidate.id for candidate in original.candidates
    }
    reversed_by_id = {candidate.id: candidate for candidate in reverse.candidates}
    for candidate in original.candidates:
        assert reversed_by_id[candidate.id].left == candidate.right
        assert reversed_by_id[candidate.id].right == candidate.left


def test_actual_text_not_the_synthetic_flag_controls_comparison():
    payload = make_comparison_bundle().model_dump(mode="json")
    synthetic = compare_sources(EvidenceBundle.model_validate(payload), "left", "right")
    payload["case"]["synthetic"] = False
    real = compare_sources(EvidenceBundle.model_validate(payload), "left", "right")
    assert real.candidates == synthetic.candidates
    assert (
        compare_texts("Unrelated saved source material.", "Entirely separate reporting.").candidates
        == ()
    )


@pytest.mark.parametrize(
    "left,right", [("left", "left"), ("missing", "right"), ("left", "missing"), ("", "right")]
)
def test_invalid_or_same_source_selection_fails_explicitly(left, right):
    with pytest.raises(ValueError):
        compare_sources(make_comparison_bundle(), left, right)


@pytest.mark.parametrize("text", [None, "", "  \n\t"])
def test_sources_without_visible_saved_text_cannot_be_compared(text):
    bundle = make_comparison_bundle({"left": LEFT_TEXT, "right": text})
    with pytest.raises(ValueError):
        compare_sources(bundle, "left", "right")


def test_empty_case_has_no_placeholder_or_invented_comparison():
    with pytest.raises(ValueError):
        compare_sources(make_comparison_bundle({}), "left", "right")


def test_modified_source_text_cannot_bypass_saved_hash_validation():
    bundle = make_comparison_bundle()
    bundle.records[0].text = "Tampered text that does not match the saved content hash."
    with pytest.raises(ValueError, match="hash"):
        compare_sources(bundle, "left", "right")


def test_character_bound_reports_omitted_content_and_never_cites_it():
    padding = "Synthetic background context " * (MAX_SOURCE_CHARS // 10)
    result = compare_texts(
        padding + "\nHarbor bridge carries 20 vehicles per hour on Monday.",
        padding + "\nHarbor bridge carries 35 vehicles per hour on Monday.",
    )
    assert result.candidates == ()
    for source, coverage in (
        (result.left, result.coverage.left),
        (result.right, result.coverage.right),
    ):
        assert coverage.total_chars > MAX_SOURCE_CHARS
        assert coverage.displayed_chars == len(source.text) <= MAX_SOURCE_CHARS
        assert coverage.examined_chars <= MAX_SOURCE_CHARS
        assert coverage.truncated is True
        assert coverage.character_limit == MAX_SOURCE_CHARS


def test_sentence_bound_does_not_imply_the_whole_source_was_compared():
    padding = "Shared background facts remain unchanged here.\n" * MAX_SENTENCES
    result = compare_texts(
        padding + "Harbor bridge carries 20 vehicles per hour on Monday.",
        padding + "Harbor bridge carries 35 vehicles per hour on Monday.",
    )
    assert result.candidates == ()
    for coverage in (result.coverage.left, result.coverage.right):
        assert coverage.examined_sentences <= MAX_SENTENCES
        assert coverage.truncated is True
        assert coverage.sentence_limit == MAX_SENTENCES


def test_candidate_limit_is_explicit_and_still_retains_exact_source_references():
    stations = [f"Port{chr(65 + index)}" for index in range(MAX_CANDIDATES + 5)]
    left = "\n".join(
        f"Harbor station {station} reported 20 arrivals on Monday." for station in stations
    )
    right = "\n".join(
        f"Harbor station {station} reported 35 arrivals on Monday." for station in stations
    )
    result = compare_texts(left, right)
    assert len(result.candidates) == MAX_CANDIDATES
    assert result.coverage.candidate_limit == MAX_CANDIDATES
    assert result.coverage.candidates_truncated is True
    for candidate in result.candidates:
        assert left[candidate.left.start : candidate.left.end] == candidate.left.quote
        assert right[candidate.right.start : candidate.right.end] == candidate.right.quote


def test_service_comparison_is_read_only_and_preserves_existing_review_history(tmp_path):
    repository = ReviewRepository(tmp_path / "comparison.sqlite3")
    repository.initialize()
    service = ReviewService(repository)
    case_id = service.seed_demo()
    scenario = service.start_scenario(case_id)
    service.review(case_id, "harbor:c3-v1", scenario["id"], "withhold", "Attribution is unproven.")
    fingerprint = service.case_fingerprint(case_id)
    history = service.load_events(case_id)
    with sqlite3.connect(repository.db_path) as connection:
        before = list(connection.iterdump())
    result = service.compare_sources(case_id, "harbor:r01", "harbor:r02")
    assert result["left"]["record_id"] == "harbor:r01"
    assert result["right"]["record_id"] == "harbor:r02"
    with sqlite3.connect(repository.db_path) as connection:
        assert list(connection.iterdump()) == before
    assert service.case_fingerprint(case_id) == fingerprint
    assert service.load_events(case_id) == history
    with pytest.raises(ValueError):
        service.compare_sources("unavailable-case", "harbor:r01", "harbor:r02")
