"""Small overlapping passages with exact character offsets into the source text."""

import re

from .models import ChunkInput


def chunk_text(text: str, max_words: int = 180, overlap_words: int = 30) -> list[ChunkInput]:
    if max_words < 1 or not 0 <= overlap_words < max_words:
        raise ValueError("Chunk overlap must be smaller than the positive chunk size.")
    words = list(re.finditer(r"\S+", text))
    chunks = []
    start = 0
    while start < len(words):
        end = min(start + max_words, len(words))
        first, last = words[start].start(), words[end - 1].end()
        chunks.append(
            ChunkInput(ordinal=len(chunks), text=text[first:last], start_char=first, end_char=last)
        )
        if end == len(words):
            break
        start = end - overlap_words
    return chunks
