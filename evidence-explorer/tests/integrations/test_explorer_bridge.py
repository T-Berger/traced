"""Saved Explorer findings remain source-linked and honest about provenance."""

import hashlib

import pytest

from evidence_explorer.demo import DEMO_CLAIMS_KEY, seed_demo
from evidence_explorer.models import ChunkInput, ClaimInput
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.integrations.explorer import (
    bundle_for_selected_articles,
    bundle_from_repository,
)
from evidence_explorer.source_contracts import SourceAcquisition, SourceItem


def _save_claim(repo, article, *, model_key="test:claims-v1", is_sample=False):
    article = article.model_copy(update={"is_sample": is_sample})
    article_id = repo.upsert_article(article)
    # A nonzero start ensures offsets are translated into the original article,
    # rather than accidentally indexing the selected chunk as a whole document.
    start = article.text.index("The transport")
    text = article.text[start:]
    chunk = repo.replace_chunks(
        article_id,
        [ChunkInput(ordinal=0, text=text, start_char=start, end_char=len(article.text))],
    )[0]
    quote = "the bridge would remain open to pedestrians"
    repo.replace_claims(
        article_id,
        [
            ClaimInput(
                claim_text="The transport office said the bridge would remain open.",
                quote=quote,
                chunk_id=chunk["id"],
                attribution="The transport office",
                event_time=None,
                location=None,
            )
        ],
        model_key,
    )
    return article_id, chunk, quote


def test_saved_claim_roundtrip_keeps_exact_offsets_and_unknown_context(repo, article):
    _, _, quote = _save_claim(repo, article)
    bundle = bundle_from_repository(repo)
    reopened = EvidenceBundle.model_validate_json(bundle.model_dump_json())
    finding = reopened.findings[0]
    support = next(link for link in reopened.links if link.relation == "supports")
    fragment = next(f for f in reopened.fragments if f.id == support.fragment_ids[0])
    record = next(r for r in reopened.records if r.id == fragment.record_id)
    assert record.text[fragment.selector.start : fragment.selector.end] == quote
    assert fragment.selector.start == article.text.index(quote)
    assert record.content_hash == hashlib.sha256(article.text.encode()).hexdigest()
    assert record.source_url == article.url
    assert record.acquired_at is None
    assert finding.details["attribution"] == "The transport office"
    assert finding.details["claimed_event_time"] is None
    assert finding.details["claimed_location"] is None
    assert finding.details["review_state"] == "unreviewed"
    assert reopened.runs[0].recomputation_capability == "dependency_review_only"
    assert reopened.runs[0].model_extra["original_model_created_at"] is None


def test_bad_saved_quote_is_rejected_instead_of_exported(repo, article, monkeypatch):
    _save_claim(repo, article)
    old_or_corrupt_rows = repo.list_claims()
    old_or_corrupt_rows[0]["quote"] = "A quotation that never appeared in the saved original."
    monkeypatch.setattr(repo, "list_active_claims", lambda: old_or_corrupt_rows)
    with pytest.raises(ValueError, match="does not occur verbatim"):
        bundle_from_repository(repo)


def test_stale_chunk_cannot_support_a_finding(repo, article, monkeypatch):
    _save_claim(repo, article)
    old_or_corrupt_rows = repo.list_chunks()
    old_or_corrupt_rows[0]["text"] = "X" * len(old_or_corrupt_rows[0]["text"])
    monkeypatch.setattr(repo, "list_chunks", lambda: old_or_corrupt_rows)
    with pytest.raises(ValueError, match="does not match its original text"):
        bundle_from_repository(repo)


def test_repeated_exports_and_reinserted_claim_rows_keep_immutable_identity(repo, article):
    article_id, chunk, quote = _save_claim(repo, article)
    original = bundle_from_repository(repo)
    assert bundle_from_repository(repo).model_dump() == original.model_dump()
    # The starter deletes/reinserts extraction rows when results are saved. SQL
    # row identity must not manufacture a different finding from the same result.
    before_id = repo.list_claims()[0]["id"]
    saved = ClaimInput.model_validate(repo.list_claims()[0])
    repo.replace_claims(article_id, [saved], "test:claims-v1")
    assert repo.list_claims()[0]["id"] != before_id
    assert bundle_from_repository(repo).model_dump() == original.model_dump()
    assert quote in original.records[0].text
    assert chunk["start_char"] > 0


def test_changed_extraction_creates_new_snapshot_without_changing_saved_source(repo, article):
    article_id, chunk, quote = _save_claim(repo, article)
    original = bundle_from_repository(repo)
    repo.replace_claims(
        article_id,
        [
            ClaimInput(
                claim_text="A narrower reviewed extraction of the quoted statement.",
                quote=quote,
                chunk_id=chunk["id"],
            )
        ],
        "test:claims-v1",
    )
    updated = bundle_from_repository(repo)
    assert updated.records[0].id == original.records[0].id
    assert updated.runs[0].id != original.runs[0].id
    assert updated.findings[0].snapshot_id != original.findings[0].snapshot_id
    assert original.findings[0].statement != updated.findings[0].statement


def test_seeded_demo_is_synthetic_and_mixed_content_is_explicit(repo, article):
    seed_demo(repo)
    demo = bundle_from_repository(repo)
    assert demo.case.synthetic is True
    assert demo.case.coverage["content_provenance"] == "synthetic"
    assert demo.findings
    assert all(f.details["extraction_origin"] == "curated_demo" for f in demo.findings)
    assert {run.configuration["model_key"] for run in demo.runs} == {DEMO_CLAIMS_KEY}
    _save_claim(repo, article)
    mixed = bundle_from_repository(repo)
    assert mixed.case.synthetic is False
    assert mixed.case.coverage["content_provenance"] == "mixed"
    assert any("mixes synthetic" in limitation for limitation in mixed.case.limitations)
    assert {record.metadata["synthetic"] for record in mixed.records} == {True, False}


def test_model_filter_preserves_extraction_identity_without_inference(repo, article):
    article_id, chunk, quote = _save_claim(repo, article)
    repo.replace_claims(
        article_id,
        [
            ClaimInput(
                claim_text="Another saved model's interpretation of the same passage.",
                quote=quote,
                chunk_id=chunk["id"],
            )
        ],
        "ollama:qwen@retained-digest",
    )
    all_models = bundle_from_repository(repo)
    selected = bundle_from_repository(repo, model_key="ollama:qwen@retained-digest")
    assert len(all_models.runs) == 2
    assert len(selected.findings) == 1
    assert selected.runs[0].configuration["model_key"] == "ollama:qwen@retained-digest"
    assert selected.runs[0].configuration["inference_performed_by_adapter"] is False
    assert len(repo.list_claims()) == 2


def test_selected_snapshot_excludes_unselected_versions_and_keeps_claim_origins(repo, article):
    first, _, _ = _save_claim(repo, article, model_key="demo:quoted", is_sample=True)
    item = SourceItem(
        platform="youtube",
        channel_id="source-channel",
        channel_name="Example video channel",
        native_id="source-video",
        source_url="https://www.youtube.com/watch?v=source-video",
        text=article.text,
        acquired_at="2026-11-01T09:30:00Z",
        provenance=SourceAcquisition(method="youtube_feed", content_scope="title_description"),
    )
    second_article = article.model_copy(
        update={
            "url": item.source_url,
            "title": "Platform description",
            "content_source": "platform_post",
            "source_platform": "youtube",
            "source_channel_id": item.channel_id,
            "source_native_id": "source-video",
            "content_scope": "title_description",
            "source_metadata_json": item.model_dump_json(exclude={"text"}),
            "retrieved_at": item.acquired_at,
        }
    )
    second, _, _ = _save_claim(repo, second_article, model_key="test:other")
    unrelated = article.model_copy(update={"url": "https://other.example/unselected"})
    _save_claim(repo, unrelated, model_key="test:excluded")

    bundle = bundle_for_selected_articles(repo, [second, first])
    assert {r.source_url for r in bundle.records} == {article.url, second_article.url}
    assert bundle.case.coverage["content_provenance"] == "mixed"
    assert len(bundle.runs) == 1
    assert bundle.runs[0].configuration["model_keys"] == ["demo:quoted", "test:other"]
    assert {finding.details["model_key"] for finding in bundle.findings} == {
        "demo:quoted",
        "test:other",
    }
    platform_record = next(r for r in bundle.records if r.source_url == second_article.url)
    assert platform_record.metadata["content_scope"] == "title_description"
    assert platform_record.metadata["source_native_id"] == "source-video"
    assert platform_record.metadata["source_item"] == item.model_dump(mode="json", exclude={"text"})
    repo.upsert_article(unrelated.model_copy(update={"title": "Changed unrelated reporting"}))
    assert bundle_for_selected_articles(repo, [first, second]) == bundle
    with pytest.raises(ValueError, match="unavailable"):
        bundle_for_selected_articles(repo, [first, 2**63 - 1])


def test_duplicate_wording_does_not_become_documented_origin(repo, article):
    _save_claim(repo, article)
    second = article.model_copy(
        update={"url": "https://other.example/bridge", "publisher": "Another publisher"}
    )
    repo.upsert_article(second)
    bundle = bundle_from_repository(repo)
    repeats = [link for link in bundle.links if link.relation == "same_content"]
    assert len(repeats) == 1
    assert not any(link.relation == "documented_repost" for link in bundle.links)
    assert len(bundle.records) == 2


def test_empty_repository_and_unknown_publication_date_stay_unknown(repo, article):
    empty = bundle_from_repository(repo)
    assert empty.records == empty.runs == empty.findings == []
    assert empty.case.coverage["content_provenance"] == "empty"
    assert empty.case.synthetic is False
    repo.upsert_article(article.model_copy(update={"published_at": ""}))
    bundle = bundle_from_repository(repo)
    assert bundle.records[0].published_at is None
    assert bundle.findings == []
    assert len(bundle.runs) == 1
