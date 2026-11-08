"""Resolve hash-pinned excerpts from local case evidence."""

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceSpan:
    start: int
    end: int
    sha256: str

    def extract(self, text: str) -> str | None:
        if not 0 <= self.start < self.end <= len(text):
            return None
        excerpt = text[self.start : self.end]
        return excerpt if self.matches(excerpt) else None

    def matches(self, excerpt: str) -> bool:
        return (
            len(excerpt) == self.end - self.start
            and hashlib.sha256(excerpt.encode("utf-8")).hexdigest() == self.sha256
        )
