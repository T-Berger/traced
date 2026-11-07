"""Passage boundaries preserve the exact source text used for evidence."""

import pytest

from evidence_explorer.chunking import chunk_text


def test_overlapping_unicode_passages_have_exact_source_offsets():
    text = "  Die Brücke bleibt gesperrt.\n\nA later report mentions reopening on Friday.  "
    chunks = chunk_text(text, max_words=5, overlap_words=2)
    assert len(chunks) > 1
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    assert all(text[chunk.start_char : chunk.end_char] == chunk.text for chunk in chunks)
    assert all(a.end_char > b.start_char for a, b in zip(chunks, chunks[1:], strict=False))
    assert chunks[-1].text.endswith("Friday.")


def test_empty_source_creates_no_passages():
    assert chunk_text(" \n\t ") == []


@pytest.mark.parametrize("size, overlap", [(0, 0), (4, 4), (4, -1)])
def test_invalid_chunk_bounds_fail_explicitly(size, overlap):
    with pytest.raises(ValueError):
        chunk_text("a source passage", max_words=size, overlap_words=overlap)
