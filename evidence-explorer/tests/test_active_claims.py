"""Current extraction results stay separate from retained model-version history."""

import sqlite3

import pytest

from evidence_explorer.claim_selection import CLAIMS_POLICY_VERSION
from evidence_explorer.graphs import source_graph
from evidence_explorer.models import ChunkInput, ClaimInput
from evidence_explorer.review.integrations.explorer import bundle_from_repository

LEGACY_KEY = "ollama:qwen3.5:9b@old-digest:v1"
CURRENT_KEY = f"ollama:qwen3.5:9b@same-digest:{CLAIMS_POLICY_VERSION}"


@pytest.fixture
def source(repo, article):
    article_id = repo.upsert_article(article)
    chunk = repo.replace_chunks(
        article_id,
        [ChunkInput(ordinal=0, text=article.text, start_char=0, end_char=len(article.text))],
    )[0]
    quote = article.text
    return article_id, ClaimInput(claim_text=quote, quote=quote, chunk_id=chunk["id"])


def publish(repo, article_id, claims, model_key):
    content_hash = repo.get_article(article_id)["content_hash"]
    key = f"claims:{article_id}:{content_hash}:{model_key}"
    repo.replace_claims(
        article_id, claims, model_key, expected_content_hash=content_hash, cache_key=key
    )
    return key


@pytest.mark.parametrize("completed", [False, True])
def test_deprecated_free_form_results_remain_historical_but_not_current(repo, source, completed):
    article_id, claim = source
    legacy = claim.model_copy(
        update={"claim_text": "An inferred claim with unsupported attribution."}
    )
    if completed:
        publish(repo, article_id, [legacy], LEGACY_KEY)
    else:
        repo.replace_claims(article_id, [legacy], LEGACY_KEY)
    assert len(repo.list_claims(article_id)) == 1
    assert repo.list_active_claims(article_id) == []
    assert repo.collection().totals.claims == 0
    assert repo.claims.verified_active_page() == []
    assert bundle_from_repository(repo).findings == []
    historical = bundle_from_repository(repo, model_key=LEGACY_KEY)
    assert historical.findings[0].statement == legacy.claim_text


def test_new_success_publishes_one_current_result_and_preserves_history(repo, source):
    article_id, claim = source
    legacy = claim.model_copy(update={"claim_text": "An earlier ungrounded paraphrase."})
    repo.replace_claims(article_id, [legacy], LEGACY_KEY)
    key = publish(repo, article_id, [claim], CURRENT_KEY)
    pointer = repo.get_meta(f"claims:{article_id}:active")
    assert pointer == {
        "model_key": CURRENT_KEY,
        "content_hash": repo.get_article(article_id)["content_hash"],
    }
    assert repo.get_meta(key) == "done"
    assert {row["model_key"] for row in repo.list_claims(article_id)} == {LEGACY_KEY, CURRENT_KEY}
    assert [row["claim_text"] for row in repo.list_active_claims(article_id)] == [claim.quote]
    current = repo.collection()
    assert current.totals.claims == len(current.claims) == 1
    assert current.claims[0].model_key == CURRENT_KEY
    graph = source_graph(
        [repo.get_article(article_id)], repo.list_active_claims(article_id), article_id
    )
    assert [row["claim_text"] for row in graph.claims] == [claim.quote]
    exported = bundle_from_repository(repo)
    assert [finding.statement for finding in exported.findings] == [claim.quote]
    assert exported.findings[0].details["model_key"] == CURRENT_KEY
    assert (
        bundle_from_repository(repo, model_key=LEGACY_KEY).findings[0].statement
        == legacy.claim_text
    )


def test_successful_empty_result_never_resurrects_older_claims(repo, source):
    article_id, claim = source
    repo.replace_claims(article_id, [claim], "custom:legacy-import")
    assert len(repo.list_active_claims(article_id)) == 1  # Legacy import compatibility.
    key = publish(repo, article_id, [], CURRENT_KEY)
    assert repo.get_meta(key) == "done"
    assert repo.get_meta(f"claims:{article_id}:active")["model_key"] == CURRENT_KEY
    assert len(repo.list_claims(article_id)) == 1
    assert repo.list_active_claims(article_id) == []
    assert repo.collection().totals.claims == 0
    exported = bundle_from_repository(repo)
    assert exported.findings == []
    assert [run.configuration["model_key"] for run in exported.runs] == [CURRENT_KEY]
    assert len(bundle_from_repository(repo, model_key="custom:legacy-import").findings) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"claim_text": "A fabricated interpretation of the source."},
        {"subject": "An inferred person"},
        {"attribution": "An invented speaker"},
        {"event_time": "2026-11-07"},
        {"location": "An inferred location"},
    ],
)
def test_forged_current_policy_claim_cannot_replace_active_result(repo, source, changes):
    article_id, claim = source
    publish(repo, article_id, [claim], CURRENT_KEY)
    before = repo.get_meta(f"claims:{article_id}:active")
    attempted_model = f"ollama:qwen3.5:9b@new-digest:{CLAIMS_POLICY_VERSION}"
    bad = claim.model_copy(update=changes)
    with pytest.raises(ValueError, match="source wording"):
        publish(repo, article_id, [bad], attempted_model)
    assert repo.get_meta(f"claims:{article_id}:active") == before
    assert {row["model_key"] for row in repo.list_claims(article_id)} == {CURRENT_KEY}
    content_hash = repo.get_article(article_id)["content_hash"]
    assert repo.get_meta(f"claims:{article_id}:{content_hash}:{attempted_model}") is None


def test_current_policy_cannot_clip_a_real_substring_that_loses_source_context(repo, source):
    article_id, claim = source
    publish(repo, article_id, [claim], CURRENT_KEY)
    clipped = claim.quote.split(". ", 1)[0] + "."
    assert clipped != claim.quote and clipped in claim.quote
    shortened = claim.model_copy(update={"claim_text": clipped, "quote": clipped})
    with pytest.raises(ValueError, match="complete eligible source excerpt"):
        publish(repo, article_id, [shortened], CURRENT_KEY)
    assert repo.list_active_claims(article_id)[0]["quote"] == claim.quote


def test_active_pointer_failure_rolls_back_claims_and_completion_marker(repo, source):
    article_id, claim = source
    repo.replace_claims(article_id, [claim], "custom:previous")
    pointer_key = f"claims:{article_id}:active"
    with repo._connection(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_active_pointer BEFORE INSERT ON metadata "
            f"WHEN NEW.key = '{pointer_key}' BEGIN SELECT RAISE(ABORT, 'pointer failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="pointer failed"):
        publish(repo, article_id, [claim], CURRENT_KEY)
    assert repo.get_meta(pointer_key) is None
    assert {row["model_key"] for row in repo.list_claims(article_id)} == {"custom:previous"}
    content_hash = repo.get_article(article_id)["content_hash"]
    assert repo.get_meta(f"claims:{article_id}:{content_hash}:{CURRENT_KEY}") is None
    assert len(repo.list_active_claims(article_id)) == 1


@pytest.mark.parametrize(
    "pointer", [{"model_key": CURRENT_KEY, "content_hash": "stale"}, "invalid"]
)
def test_stale_or_malformed_pointer_fails_closed_without_historical_fallback(repo, source, pointer):
    article_id, claim = source
    repo.replace_claims(article_id, [claim], "custom:historical")
    publish(repo, article_id, [claim], CURRENT_KEY)
    repo.set_meta(f"claims:{article_id}:active", pointer)
    assert repo.list_active_claims(article_id) == []
    assert repo.collection().claims == []
    assert len(repo.list_claims(article_id)) == 2


def test_later_historical_row_ids_do_not_choose_the_active_result(repo, source):
    article_id, claim = source
    publish(repo, article_id, [claim], CURRENT_KEY)
    repo.replace_claims(article_id, [claim], "custom:manual-history")
    assert repo.list_claims(article_id)[-1]["model_key"] == "custom:manual-history"
    assert [row["model_key"] for row in repo.list_active_claims(article_id)] == [CURRENT_KEY]


def test_source_edit_clears_pointer_and_completion_markers(repo, source, article):
    article_id, claim = source
    cache_key = publish(repo, article_id, [claim], CURRENT_KEY)
    repo.upsert_article(article.model_copy(update={"text": article.text + " Updated source."}))
    assert repo.get_meta(f"claims:{article_id}:active") is None
    assert repo.get_meta(cache_key) is None
    assert repo.list_active_claims(article_id) == []


def test_cached_model_can_become_current_again_without_rewriting_evidence(repo, source):
    article_id, claim = source
    key_a = publish(repo, article_id, [claim], CURRENT_KEY)
    model_b = f"ollama:another-model@revision:{CLAIMS_POLICY_VERSION}"
    publish(repo, article_id, [claim], model_b)
    before = repo.list_claims(article_id)
    repo.set_meta(f"{key_a}:quality", {"rejected_claims": 1})
    content_hash = repo.get_article(article_id)["content_hash"]
    repo.claims.activate_cached_result(
        article_id, CURRENT_KEY, expected_content_hash=content_hash, cache_key=key_a
    )
    assert [row["model_key"] for row in repo.list_active_claims(article_id)] == [CURRENT_KEY]
    assert repo.list_claims(article_id) == before
    assert repo.get_meta(key_a) == "done"
    assert repo.get_meta(f"{key_a}:quality") == {"rejected_claims": 1}


def test_cached_empty_result_can_replace_another_models_current_claims(repo, source):
    article_id, claim = source
    empty_key = publish(repo, article_id, [], CURRENT_KEY)
    model_b = f"ollama:another-model@revision:{CLAIMS_POLICY_VERSION}"
    publish(repo, article_id, [claim], model_b)
    assert len(repo.list_active_claims(article_id)) == 1
    repo.claims.activate_cached_result(
        article_id,
        CURRENT_KEY,
        expected_content_hash=repo.get_article(article_id)["content_hash"],
        cache_key=empty_key,
    )
    assert repo.list_active_claims(article_id) == []
    assert len(repo.list_claims(article_id)) == 1
    assert repo.get_meta(f"claims:{article_id}:active")["model_key"] == CURRENT_KEY


@pytest.mark.parametrize("invalid_context", ["source_hash", "cache_key", "missing", "not_done"])
def test_invalid_cached_context_cannot_change_current_result(repo, source, invalid_context):
    article_id, claim = source
    old_key = publish(repo, article_id, [claim], CURRENT_KEY)
    model_b = f"ollama:another-model@revision:{CLAIMS_POLICY_VERSION}"
    publish(repo, article_id, [claim], model_b)
    before_pointer = repo.get_meta(f"claims:{article_id}:active")
    before_rows = repo.list_claims(article_id)
    content_hash = repo.get_article(article_id)["content_hash"]
    if invalid_context == "missing":
        with repo._connection(write=True) as connection:
            connection.execute("DELETE FROM metadata WHERE key = ?", (old_key,))
    elif invalid_context == "not_done":
        repo.set_meta(old_key, "running")
    with pytest.raises(ValueError):
        repo.claims.activate_cached_result(
            article_id,
            CURRENT_KEY,
            expected_content_hash="stale" if invalid_context == "source_hash" else content_hash,
            cache_key="unrelated:key" if invalid_context == "cache_key" else old_key,
        )
    assert repo.get_meta(f"claims:{article_id}:active") == before_pointer
    assert repo.list_claims(article_id) == before_rows


def test_reactivating_the_same_current_result_does_not_rewrite_pointer(repo, source):
    article_id, claim = source
    key = publish(repo, article_id, [claim], CURRENT_KEY)
    pointer = f"claims:{article_id}:active"
    with repo._connection(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER prevent_pointer_rewrite BEFORE INSERT ON metadata "
            f"WHEN NEW.key = '{pointer}' BEGIN SELECT RAISE(ABORT, 'unnecessary write'); END"
        )
    repo.claims.activate_cached_result(
        article_id,
        CURRENT_KEY,
        expected_content_hash=repo.get_article(article_id)["content_hash"],
        cache_key=key,
    )
    assert len(repo.list_active_claims(article_id)) == 1
