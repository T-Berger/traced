"""Targeted reasoning checks through the existing review verifier.

The checker hands the existing ``Verifier`` (``OpenRouterVerifier`` by default:
prompt ``evidence-challenge-v3``, temperature 0, strict JSON schema) one
statement and a few exact windows of saved record text. The verifier proposes
objections: contradiction, unsupported inference, insufficient evidence, or an
alternative explanation.

An objection is accepted only when every quote is a verbatim substring of a
passage it was given; it is then mapped back to the exact record span. Other
objections are discarded and reported, never repaired. Results are cached by
a digest of the exact model input, so a re-check of the same bundle is
reproducible and makes no new model call. Model output is a proposal for
human assessment, not a finding of fact.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from evidence_explorer.review.verifier import (
    PROMPT_VERSION,
    CheckError,
    CheckOutput,
    Passage,
    Verifier,
)

from .bundle import digest

WINDOW_CHARS = 1200
MAX_WINDOWS = 8


@dataclass(frozen=True)
class Window:
    """Exact text shown to the model: a record span, or a declared assumption."""

    text: str
    record_id: str | None = None
    start: int = 0
    end: int = 0
    truncated: bool = False
    note: str = ""


@dataclass(frozen=True)
class Objection:
    kind: str
    explanation: str
    spans: tuple[dict, ...]  # record spans, or {"assumption": …, "quote": …}


@dataclass
class Verdict:
    status: str  # "ok", "error", or "off"
    objections: list[Objection] = field(default_factory=list)
    discarded: list[str] = field(default_factory=list)
    error: str | None = None
    cached: bool = False

    def of(self, *kinds: str) -> list[Objection]:
        return [item for item in self.objections if item.kind in kinds]


class Reasoner:
    """Ask the verifier about one statement at a time, with a digest cache."""

    def __init__(
        self,
        verifier: Verifier | None,
        *,
        cache_path: Path | None = None,
        not_run_reason: str | None = None,
    ):
        self.verifier = verifier
        self.not_run_reason = (
            None if verifier is not None else (not_run_reason or "no model verifier was given")
        )
        self.cache_path = cache_path
        self.cache: dict[str, Any] = {}
        if cache_path is not None and cache_path.is_file():
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("entries"), dict):
                self.cache = loaded["entries"]
        self.calls = 0
        self.cached = 0
        self.errors = 0

    @property
    def model(self) -> str | None:
        return getattr(self.verifier, "model", None) if self.verifier else None

    def describe(self) -> dict:
        return {
            "mode": "verifier" if self.verifier else "not_run",
            "not_run_reason": self.not_run_reason,
            "provider": getattr(self.verifier, "provider", None) if self.verifier else None,
            "model": self.model,
            "actual_model": getattr(self.verifier, "actual_model", None) if self.verifier else None,
            "prompt_version": PROMPT_VERSION if self.verifier else None,
            "calls": self.calls,
            "cached": self.cached,
            "errors": self.errors,
        }

    def save(self) -> None:
        if self.cache_path is None or not self.cache:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema": "traced.investigation.reasoning-cache/1", "entries": self.cache}
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.cache_path)

    def challenge(self, statement: str, windows: Sequence[Window]) -> Verdict:
        if self.verifier is None:
            return Verdict("off")
        shown = [window for window in windows if window.text.strip()][:MAX_WINDOWS]
        if not shown:
            return Verdict("off", error="no passage to check against")
        aliases: dict[str, str] = {}
        passages: list[Passage] = []
        for index, window in enumerate(shown, start=1):
            record_key = window.record_id or "declared-assumption"
            aliases.setdefault(record_key, f"R{len(aliases) + 1}")
            text = f"{window.note}{window.text}" if window.note else window.text
            passages.append(
                {
                    "fragment_id": f"P{index}",
                    "record_id": aliases[record_key] if window.record_id else "ASSUMPTION",
                    "text": text,
                    "truncated": window.truncated,
                }
            )
        key = digest(
            {
                "model": self.model,
                "prompt_version": PROMPT_VERSION,
                "statement": statement,
                "passages": passages,
            }
        )
        cached = key in self.cache
        if cached:
            raw = self.cache[key]
            self.cached += 1
        else:
            try:
                self.calls += 1
                raw = self.verifier.check(statement, passages)
            except CheckError as exc:
                self.errors += 1
                return Verdict("error", error=str(exc))
            self.cache[key] = raw
        try:
            checked = CheckOutput.model_validate({"findings": (raw or {}).get("findings")})
        except (ValidationError, AttributeError) as exc:
            self.errors += 1
            return Verdict(
                "error", error=f"verifier output failed its schema: {exc.__class__.__name__}"
            )
        verdict = Verdict("ok", cached=cached)
        by_alias = {
            passage["fragment_id"]: (passage, window)
            for passage, window in zip(passages, shown, strict=True)
        }
        for item in checked.findings:
            problem = None
            spans: list[dict] = []
            if len(item.fragment_ids) != len(item.quotes):
                problem = "quotes and passages are not aligned one-to-one"
            elif len(set(item.fragment_ids)) != len(item.fragment_ids):
                problem = "the same passage is cited twice"
            else:
                for alias, quote in zip(item.fragment_ids, item.quotes, strict=True):
                    if alias not in by_alias:
                        problem = f"cites unknown passage {alias}"
                        break
                    passage, window = by_alias[alias]
                    position = passage["text"].find(quote) if quote.strip() else -1
                    if position == -1:
                        problem = f"quote is not verbatim in passage {alias}"
                        break
                    offset = position - len(window.note)
                    if window.record_id is None or offset < 0:
                        spans.append({"assumption": window.text, "quote": quote})
                    else:
                        start = window.start + offset
                        spans.append(
                            {
                                "record_id": window.record_id,
                                "start": start,
                                "end": start + len(quote),
                                "quote": quote,
                            }
                        )
            if problem:
                verdict.discarded.append(f"{item.finding_type}: {problem}")
                continue
            verdict.objections.append(Objection(item.finding_type, item.explanation, tuple(spans)))
        return verdict
