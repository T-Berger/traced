"""Excerpt selection never invents identities or clips source evidence to fit a budget."""

import json
from dataclasses import FrozenInstanceError

import pytest

from evidence_explorer.claim_excerpts import (
    MAX_CANDIDATES,
    MAX_INPUT_CHARS,
    SourceExcerpt,
    build_claim_excerpts,
)

# The live regression's source question and answer: the expert is the speaker,
# not the person whom the pronoun is allowed to identify automatically.
INTERVIEW = (
    "Bayerns Landtagspräsidentin Ilse Aigner wird Unionskandidatin für die Wahl "
    "zur Bundespräsidentin.\n"
    "tagesthemen: Warum fliegen Ilse Aigner alle Herzen zu?\n"
    "Ursula Münch: Weil sie selbst eine herzliche Art hat.\n"
    "Nicht Söders Wunschkandidatin\n"
    "tagesthemen: Wo Markus Söder, eigentlich Ihr langjähriger politischer Rivale, "
    "eine zentrale Rolle gespielt hat. Hat er sie wirklich gewollt oder hat er "
    "einfach nur gepokert?\n"
    "Münch: Also sie ist jetzt sicherlich nicht die Kandidatin seines Herzens gewesen. "
    "Aber Markus Söder hat relativ früh im Frühjahr dieses Jahres erkannt, "
    "dass die Stimmung in der CSU gegen ihn ist."
)


def passage(source, *, chunk_id=1, start=0, end=None):
    end = len(source) if end is None else end
    return {"id": chunk_id, "text": source[start:end], "start_char": start, "end_char": end}


def excerpts(source, chunks=None, **limits):
    return build_claim_excerpts(
        {"title": "Saved source", "text": source},
        [passage(source)] if chunks is None else chunks,
        **limits,
    )


def assert_exact_ranges(source, candidates, chunks):
    by_id = {chunk["id"]: chunk for chunk in chunks[:8]}
    for candidate in candidates:
        assert source[candidate.start_char : candidate.end_char] == candidate.text
        chunk = by_id[candidate.chunk_id]
        assert chunk["start_char"] <= candidate.start_char < candidate.end_char <= chunk["end_char"]
        relative_start = candidate.start_char - chunk["start_char"]
        assert (
            chunk["text"][relative_start : relative_start + len(candidate.text)] == candidate.text
        )
        assert 10 <= len(candidate.text) <= 800


@pytest.mark.parametrize("renamed", [False, True])
def test_interview_keeps_complete_question_answer_context_and_unresolved_pronouns(renamed):
    source = INTERVIEW
    if renamed:
        source = source.replace("Ilse Aigner", "Anna Weber").replace("Ursula Münch", "Lena Wolf")
        source = source.replace("Markus Söder", "Jonas Keller").replace("Münch:", "Wolf:")
    candidates = excerpts(source)
    assert candidates
    rivalry = [
        candidate for candidate in candidates if "langjähriger politischer Rivale" in candidate.text
    ]
    assert len(rivalry) == 1
    # The unpunctuated heading remains potentially meaningful source context.
    assert rivalry[0].text.startswith("Nicht Söders Wunschkandidatin\ntagesthemen: Wo ")
    assert "Hat er sie wirklich gewollt" in rivalry[0].text
    assert "Kandidatin seines Herzens" in rivalry[0].text
    assert "Ihr langjähriger" in rivalry[0].text
    assert not rivalry[0].text.endswith("?")
    assert not any("Rivale von Ursula Münch" in candidate.text for candidate in candidates)
    assert not any("Rivale von Lena Wolf" in candidate.text for candidate in candidates)
    assert_exact_ranges(source, candidates, [passage(source)])


def test_opinions_negation_modality_and_speaker_words_are_not_removed_or_rewritten():
    source = (
        "Die Behörde hält eine Öffnung möglicherweise nicht für sicher.\n"
        "Lena Wolf: Meines Erachtens könnte sie die Entscheidung nicht unterstützt haben."
    )
    candidates = excerpts(source)
    assert [candidate.text for candidate in candidates] == source.splitlines()
    assert "möglicherweise nicht" in candidates[0].text
    assert candidates[1].text.startswith("Lena Wolf: Meines Erachtens könnte sie")


@pytest.mark.parametrize(
    "source",
    [
        "Ist die Brücke bereits wieder geöffnet?",
        "tagesthemen: Wo Markus Söder, eigentlich Ihr langjähriger politischer Rivale, "
        "eine Rolle spielte.",
        "Werden die Behörden die Brücke heute öffnen？",
    ],
)
def test_question_only_sources_and_interviewer_turns_do_not_become_assertions(source):
    assert excerpts(source) == []


def test_url_only_lines_headings_and_incomplete_units_do_not_become_candidates():
    source = (
        "# Overview.\nUPDATES FROM THE CITY.\nNicht Söders Wunschkandidatin\n"
        "https://news.example/report.\nDie Behörde berichtet einen noch nicht"
    )
    assert excerpts(source) == []


def test_unicode_offsets_and_original_internal_whitespace_survive_unchanged():
    source = "Übersicht\r\n  Anna Müller sagte: „Die  Brücke bleibt nicht geöffnet.“  \r\n"
    candidates = excerpts(source)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.start_char == 0
    assert candidate.text == source.rstrip()
    assert "  Brücke" in candidate.text
    assert_exact_ranges(source, candidates, [passage(source)])


def test_a_separate_speaker_label_is_retained_with_its_following_utterance():
    source = "Lena Wolf:\nDie Brücke bleibt nach meiner Einschätzung heute geschlossen."
    assert [candidate.text for candidate in excerpts(source)] == [source]


def test_consecutive_questions_remain_available_as_contiguous_answer_context():
    source = (
        "Interviewer: Wird Anna Weber kandidieren?\nUnd warum?\n"
        "Lena Wolf: Sie hat es noch nicht entschieden."
    )
    assert [candidate.text for candidate in excerpts(source)] == [source]


@pytest.mark.parametrize(
    "source",
    [
        "市政府宣布大桥今天不会重新开放。",
        "「市当局は橋が本日再開しないと発表した。」",
        "قالت البلدية إن الجسر لن يفتح اليوم.",
        "AI系统目前不会自动重新开放这座桥。",
    ],
)
def test_uncased_languages_are_eligible_without_latin_capitalization_or_word_spacing(source):
    assert [candidate.text for candidate in excerpts(source)] == [source]


def test_uncased_question_ending_is_excluded():
    assert excerpts("市政府今天是否会重新开放这座大桥？") == []


def test_oversized_paragraph_is_omitted_instead_of_clipped_to_eight_hundred_characters():
    source = "Die Behörde erklärte die Entscheidung ausführlich. " * 20
    assert len(source) > 800
    assert excerpts(source) == []


def test_answer_is_withheld_when_complete_question_context_cannot_fit():
    question = "Interviewer: " + "Ist dies eine weitere offene Frage zur Entscheidung " * 10 + "?"
    answer = "Lena Wolf: " + "Sie könnte die Entscheidung nicht unterstützt haben. " * 7
    source = question + "\n" + answer
    assert len(answer.strip()) < 800 < len(source)
    assert excerpts(source) == []


def test_a_chunk_tail_is_not_mistaken_for_a_complete_source_paragraph():
    source = (
        "Die Behörde erklärte, dass die Brücke bis zum Abschluss der Prüfung geschlossen bleibt."
    )
    partial = passage(source, start=20, end=len(source) - 5)
    assert excerpts(source, [partial]) == []


def test_speaker_context_outside_the_supplied_chunk_is_not_silently_dropped():
    source = (
        "Interviewer: Hat Anna Weber die Schließung angeordnet?\n"
        "Lena Wolf: Sie hat diese Entscheidung nicht getroffen."
    )
    tail = passage(source, start=source.index("Lena Wolf:"))
    assert excerpts(source, [tail]) == []


def test_overlap_can_supply_a_complete_unit_without_changing_its_source_range():
    source = "Kurze Übersicht.\nDie Behörde bestätigte, dass die Brücke heute geschlossen bleibt."
    start = source.index("Die Behörde")
    chunks = [
        passage(source, chunk_id=11, end=len(source) - 12),
        passage(source, chunk_id=12, start=start - 2),
    ]
    candidates = excerpts(source, chunks)
    assert len(candidates) == 1
    assert candidates[0].chunk_id == 12
    assert candidates[0].start_char == start
    assert_exact_ranges(source, candidates, chunks)


def test_ninth_chunk_is_never_supplied_even_when_it_contains_a_complete_sentence():
    lines = [f"Record {index} states that the bridge remains closed today." for index in range(9)]
    source = "\n".join(lines)
    chunks = []
    position = 0
    for index, line in enumerate(lines):
        chunks.append(passage(source, chunk_id=index + 1, start=position, end=position + len(line)))
        position += len(line) + 1
    candidates = excerpts(source, chunks)
    assert len(candidates) == 8
    assert {candidate.chunk_id for candidate in candidates} == set(range(1, 9))
    assert not any("Record 8" in candidate.text for candidate in candidates)


def test_candidate_count_and_serialized_prompt_size_are_bounded_and_ids_are_deterministic():
    source = "\n".join(
        f"Record {index} states that the bridge remains closed today." for index in range(50)
    )
    first = excerpts(source)
    assert first == excerpts(source)
    assert len(first) == MAX_CANDIDATES
    assert [candidate.candidate_id for candidate in first] == [
        f"e{index:02}" for index in range(1, 25)
    ]
    assert (
        len(json.dumps([candidate.prompt_record() for candidate in first], ensure_ascii=False))
        <= MAX_INPUT_CHARS
    )
    limited = excerpts(source, max_input_chars=1000)
    assert 0 < len(limited) < len(first)
    assert (
        len(json.dumps([candidate.prompt_record() for candidate in limited], ensure_ascii=False))
        <= 1000
    )
    assert excerpts(source, max_input_chars=1) == []


def test_excerpt_records_are_immutable_and_expose_only_source_fields():
    candidate = excerpts("The office confirmed that the bridge remains closed today.")[0]
    assert isinstance(candidate, SourceExcerpt)
    assert set(candidate.prompt_record()) == {
        "candidate_id",
        "chunk_id",
        "text",
        "start_char",
        "end_char",
    }
    with pytest.raises(FrozenInstanceError):
        candidate.text = "Invented interpretation."


@pytest.mark.parametrize("article", [{"title": "Missing body"}, {"text": ""}, {"text": None}])
def test_missing_or_malformed_saved_body_is_an_integrity_error_not_valid_abstention(article):
    with pytest.raises(ValueError, match="saved article body"):
        build_claim_excerpts(article, [])


@pytest.mark.parametrize(
    "changes",
    [
        {"id": True},
        {"start_char": True},
        {"start_char": -1},
        {"end_char": 10000},
        {"text": "A different source body."},
        {"start_char": None},
    ],
)
def test_malformed_or_mismatched_chunks_raise_before_candidate_selection(changes):
    source = "The office confirmed that the bridge remains closed today."
    with pytest.raises(ValueError, match="match the saved article"):
        excerpts(source, [{**passage(source), **changes}])


def test_duplicate_chunk_ids_are_an_integrity_error():
    source = "The office confirmed that the bridge remains closed today."
    with pytest.raises(ValueError, match="match the saved article"):
        excerpts(source, [passage(source), passage(source)])


@pytest.mark.parametrize("person", ["Morgan", "Alex Weber"])
@pytest.mark.parametrize(
    "prefix",
    [
        "The minister denied that",
        "There is no evidence that",
        "The board questioned whether",
        "The report said it was false that",
        "The board said it might be possible that",
    ],
)
def test_unfinished_denial_reporting_and_modality_prefix_is_never_removed(person, prefix):
    source = prefix + "\n" + person + " had approved the plan."
    candidates = excerpts(source)
    assert [candidate.text for candidate in candidates] == [source]
    assert_exact_ranges(source, candidates, [passage(source)])


def test_lowercase_short_continuation_can_complete_the_preserved_prefix():
    source = "The minister denied that the plan had been\napproved."
    assert [candidate.text for candidate in excerpts(source)] == [source]


@pytest.mark.parametrize(
    "separator", ["\n\n", "\n\n# Background\n", "\nFALSE ALLEGATION.\n", "\nFalse claim.\n"]
)
def test_blank_lines_and_heading_ambiguity_cannot_remove_an_unfinished_qualifier(separator):
    source = "The minister denied that" + separator + "Morgan had approved the plan."
    assert [candidate.text for candidate in excerpts(source)] == [source]


@pytest.mark.parametrize(
    "following",
    [
        "Dana: Morgan had approved the plan.",
        "Interviewer: Had Morgan approved the plan?\nDana: Morgan had approved the plan.",
        "Dana:\nMorgan had approved the plan.",
    ],
)
def test_new_speaker_or_question_cannot_reset_a_pending_semantic_prefix(following):
    source = "The report disputed the following statement" + "\n" + following
    assert [candidate.text for candidate in excerpts(source)] == [source]


def test_unfinished_source_label_remains_when_another_speaker_marker_follows():
    source = "False allegation:\nDana: Morgan had approved the plan."
    assert [candidate.text for candidate in excerpts(source)] == [source]


def test_unfinished_prefix_outside_supplied_chunk_forces_abstention():
    source = "The minister denied that\nMorgan had approved the plan."
    tail = passage(source, start=source.index("Morgan"))
    assert excerpts(source, [tail]) == []


def test_separate_chunks_cannot_be_used_to_drop_a_prefix_between_them():
    source = "The minister denied that\nMorgan had approved the plan."
    split = source.index("Morgan")
    chunks = [passage(source, chunk_id=1, end=split), passage(source, chunk_id=2, start=split)]
    assert excerpts(source, chunks) == []


def test_oversized_unfinished_prefix_cannot_be_reset_by_a_new_speaker():
    prefix = "The report disputed the following account " * 19
    source = prefix + "\nDana: Morgan had approved the plan."
    assert len(prefix) < 800 < len(source)
    assert excerpts(source) == []


def test_input_budget_must_fit_the_whole_qualified_excerpt_not_only_its_tail():
    tail = "Morgan had approved the plan."
    tail_candidate = excerpts(tail)[0]
    tail_budget = len(json.dumps([tail_candidate.prompt_record()], ensure_ascii=False))
    source = "The minister denied that\n" + tail
    assert excerpts(source, max_input_chars=tail_budget) == []
