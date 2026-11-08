"""Frozen, hashed evidence records for Maltego Data MCP lookups.

A record preserves one tool request and its result exactly as received. Three
SHA-256 digests let any reviewer recompute and verify it:

* ``result_sha256`` covers the canonical JSON of the raw MCP result;
* ``text_sha256`` covers the deterministic text rendering that citations quote
  by character offset (the same digest the review contract stores as a
  record's ``content_hash``);
* ``record_id`` covers every other field, including the request, the times,
  and the acquisition context Traced supplied (capability, input, lineage).

The text is derived from the raw result only. Entity results get a readable
listing (``property: value`` lines with values kept verbatim); everything else
keeps the server's text blocks verbatim. Constructing or loading a record
re-derives the text and re-verifies all three digests, so an edited file is
rejected rather than shown.

A record states only what the Data MCP returned at a given time; it establishes
no truth, independent corroboration, control, or intent. Error, discovery, and
validation results are kept for audit and coverage; by default only entity
results become case evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from ...source_contracts import SourceAcquisition, SourceCase, SourceImport, SourceItem
from ..models import CollectionFailure
from .transport import ToolCall

RECORD_SCHEMA = "traced.maltego.lookup/1"
TEXT_RENDERING = "mcp-content-text/1"
ENTITY_RENDERING = "maltego-entities-text/1"
ENTITY_TOOLS = frozenset({"maltego_run", "maltego_run_status"})
SOURCE_PLATFORM = "maltego"
SOURCE_TEXT_LIMIT = 50_000
EMPTY_RESULT_TEXT = "[The lookup returned no readable content.]"
MAX_RECORD_FILE_BYTES = 40 * 1024 * 1024

Rendering = Literal["mcp-content-text/1", "maltego-entities-text/1"]
EvidenceKind = Literal["entities", "discovery", "error"]

_RECORD_ID = re.compile(r"[0-9a-f]{64}")


def canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _label(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return "unknown"
    return "".join(char if char.isprintable() else " " for char in value)[:500]


def _render_block(block: object) -> str:
    if not isinstance(block, Mapping):
        return "[Unreadable content block]"
    kind = block.get("type")
    if kind == "text":
        text = block.get("text")
        return text if isinstance(text, str) else "[Unreadable text block]"
    if kind == "resource":
        resource = block.get("resource") if isinstance(block.get("resource"), Mapping) else {}
        label = f"{_label(resource.get('uri'))} ({_label(resource.get('mimeType'))})"
        if isinstance(resource.get("text"), str):
            return f"[Embedded resource {label}]\n{resource['text']}"
        return f"[Binary resource omitted: {label}]"
    if kind == "resource_link":
        return f"[Resource link: {_label(block.get('name'))} {_label(block.get('uri'))}]"
    if kind in {"image", "audio"}:
        return f"[{kind.capitalize()} omitted: {_label(block.get('mimeType'))}]"
    return f"[Unsupported content block: {_label(kind)}]"


def render_content_text(result: Mapping[str, Any]) -> str:
    """Keep text blocks verbatim; describe other blocks without their payloads."""
    content = result.get("content")
    blocks = [_render_block(block) for block in content] if isinstance(content, list) else []
    if not any(block.strip() for block in blocks) and result.get("structuredContent") is not None:
        blocks = [
            json.dumps(
                result["structuredContent"],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
        ]
    text = "\n\n".join(block for block in blocks if block)
    return text if text.strip() else EMPTY_RESULT_TEXT


def entity_payload(result: Mapping[str, Any]) -> dict | None:
    """Return the JSON object of an entity result, or None for any other shape."""
    if result.get("isError") is True:
        return None
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("entities"), list):
        return structured
    content = result.get("content")
    if not isinstance(content, list) or len(content) != 1:
        return None
    block = content[0]
    if not isinstance(block, Mapping) or block.get("type") != "text":
        return None
    try:
        payload = json.loads(block.get("text") or "")
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("entities"), list):
        return None
    return payload


def _value(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def render_entities_text(payload: Mapping[str, Any]) -> str:
    """List result entities as ``property: value`` lines; values stay verbatim."""
    header = [
        f"{key}: {_value(value)}"
        for key, value in payload.items()
        if key != "entities" and not isinstance(value, dict | list)
    ]
    sections = ["\n".join(header)] if header else []
    entities = payload["entities"]
    if not entities:
        sections.append("No entities in this result.")
    for index, entity in enumerate(entities, start=1):
        if not isinstance(entity, Mapping):
            sections.append(f"Entity {index}: {_value(entity)}")
            continue
        heading = (
            f"Entity {index} · {_label(entity.get('type'))} · "
            f"{_value(entity.get('value')) if entity.get('value') is not None else 'no value'}"
        )
        lines = [
            f"{key}: {_value(value)}"
            for key, value in entity.items()
            if key not in {"type", "value"}
        ]
        sections.append("\n".join([heading, *lines]))
    extra = [
        f"{key}: {_value(value)}"
        for key, value in payload.items()
        if key != "entities" and isinstance(value, dict | list)
    ]
    if extra:
        sections.append("\n".join(extra))
    return "\n\n".join(sections)


def choose_rendering(tool_name: str, result: Mapping[str, Any]) -> Rendering:
    if tool_name in ENTITY_TOOLS and entity_payload(result) is not None:
        return ENTITY_RENDERING
    return TEXT_RENDERING


def render_result_text(result: Mapping[str, Any], rendering: Rendering = TEXT_RENDERING) -> str:
    if rendering == ENTITY_RENDERING:
        payload = entity_payload(result)
        if payload is None:
            raise ValueError("The result has no entities to list.")
        return render_entities_text(payload)
    return render_content_text(result)


def _aware(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Record times must be ISO 8601 with a timezone.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Record times must be ISO 8601 with a timezone.")
    return value


class MaltegoLookupRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)

    record_schema: Literal["traced.maltego.lookup/1"]
    record_id: str
    rendering: Rendering
    server_url: str
    server_name: str | None
    server_version: str | None
    protocol_version: str
    tool_name: str
    arguments: dict[str, Any]
    context: dict[str, Any]
    requested_at: str
    completed_at: str
    is_error: bool
    result: dict[str, Any]
    result_sha256: str
    text: str
    text_sha256: str
    notices: list[str]

    @field_validator("requested_at", "completed_at")
    @classmethod
    def aware_time(cls, value: str) -> str:
        return _aware(value)

    @model_validator(mode="after")
    def verified(self) -> MaltegoLookupRecord:
        problems = integrity_problems(self)
        if problems:
            raise ValueError("Record integrity check failed: " + "; ".join(problems))
        return self

    def body(self) -> dict:
        return self.model_dump(mode="json", exclude={"record_id"})

    @property
    def evidence_kind(self) -> EvidenceKind:
        if self.is_error:
            return "error"
        return "entities" if self.rendering == ENTITY_RENDERING else "discovery"

    def payload(self) -> dict | None:
        """The parsed JSON object of the result's text, when it has one."""
        structured = self.result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        content = self.result.get("content")
        if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict):
            try:
                value = json.loads(content[0].get("text") or "")
            except (TypeError, ValueError, RecursionError):
                return None
            return value if isinstance(value, dict) else None
        return None


def integrity_problems(record: MaltegoLookupRecord) -> list[str]:
    """Recompute every digest and the rendering; an empty list means verified."""
    problems = []
    try:
        if sha256_hex(canonical_json(record.result)) != record.result_sha256:
            problems.append("result_sha256 does not match the saved result")
        if sha256_hex(canonical_json(record.body())) != record.record_id:
            problems.append("record_id does not match the record contents")
        if choose_rendering(record.tool_name, record.result) != record.rendering:
            problems.append("the rendering does not match the saved result")
        elif render_result_text(record.result, record.rendering) != record.text:
            problems.append("text does not match the rendering of the saved result")
    except (TypeError, ValueError, RecursionError):
        return ["the saved result or context is not plain JSON"]
    if sha256_hex(record.text) != record.text_sha256:
        problems.append("text_sha256 does not match the saved text")
    if (record.result.get("isError") is True) is not record.is_error:
        problems.append("is_error does not match the saved result")
    if not _RECORD_ID.fullmatch(record.record_id):
        problems.append("record_id is not a SHA-256 digest")
    return problems


def build_record(
    call: ToolCall, server_url: str, context: Mapping[str, Any] | None = None
) -> MaltegoLookupRecord:
    rendering = choose_rendering(call.tool_name, call.result)
    text = render_result_text(call.result, rendering)
    body = {
        "record_schema": RECORD_SCHEMA,
        "rendering": rendering,
        "server_url": server_url,
        "server_name": call.server.name,
        "server_version": call.server.version,
        "protocol_version": call.server.protocol_version,
        "tool_name": call.tool_name,
        "arguments": call.arguments,
        "context": json.loads(canonical_json(dict(context or {}))),
        "requested_at": call.requested_at,
        "completed_at": call.completed_at,
        "is_error": call.result.get("isError") is True,
        "result": call.result,
        "result_sha256": sha256_hex(canonical_json(call.result)),
        "text": text,
        "text_sha256": sha256_hex(text),
        "notices": list(call.notices),
    }
    return MaltegoLookupRecord.model_validate(
        {**body, "record_id": sha256_hex(canonical_json(body))}
    )


def is_access_denied(record: MaltegoLookupRecord) -> bool:
    """The Data MCP reports transient permission refreshes as an "Access denied" error."""
    return record.is_error and "access denied" in record.text.lower()


def _printable(text: str, limit: int) -> str:
    text = "".join(char if char.isprintable() else " " for char in text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def argument_summary(arguments: Mapping[str, Any], limit: int = 300) -> str:
    parts = []
    for key, value in arguments.items():
        shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        parts.append(f"{key}: {shown}")
    return _printable("; ".join(parts) or "no arguments", limit)


def input_summary(context: Mapping[str, Any]) -> str | None:
    entity = context.get("input") if isinstance(context.get("input"), Mapping) else {}
    if isinstance(entity.get("entity_ref"), str):
        return f"result entity {entity['entity_ref']}"
    if isinstance(entity.get("entity_type"), str) and isinstance(entity.get("entity_value"), str):
        return f"{entity['entity_type']} {entity['entity_value']}"
    return None


def capability_label(context: Mapping[str, Any]) -> str | None:
    name, identifier = context.get("capability_name"), context.get("capability_id")
    if isinstance(name, str) and isinstance(identifier, str):
        return f"{name} ({identifier})"
    return identifier if isinstance(identifier, str) else None


def lookup_title(record: MaltegoLookupRecord) -> str:
    """Name the capability and input; settings and properties never enter titles."""
    capability = capability_label(record.context)
    if capability is None:
        return f"{record.tool_name} · {argument_summary(record.arguments)}"
    parts = [capability]
    entity = input_summary(record.context)
    if entity:
        parts.append(f"on {entity}")
    if record.tool_name == "maltego_run_status":
        offset = record.arguments.get("entities_offset")
        parts.append(f"results from entity {offset + 1}" if isinstance(offset, int) else "results")
    return _printable(" · ".join(parts), 1000)


def source_text(record: MaltegoLookupRecord) -> str:
    """The saved-source text: the full rendering, or an explicitly marked prefix."""
    if len(record.text) <= SOURCE_TEXT_LIMIT:
        return record.text
    marker = (
        f"\n[Traced: text truncated after {{kept}} of {len(record.text)} characters; "
        f"the full frozen Maltego record is {record.record_id}.]"
    )
    kept = SOURCE_TEXT_LIMIT - len(marker.format(kept=SOURCE_TEXT_LIMIT))
    return record.text[:kept] + marker.format(kept=kept)


def to_source_item(record: MaltegoLookupRecord) -> SourceItem:
    """Project a record into the shared saved-source contract without new meaning."""
    capability = record.context.get("capability_id")
    if isinstance(capability, str):
        name = record.context.get("capability_name")
        channel_id, channel_name = (
            f"datamcp/{capability}",
            name if isinstance(name, str) else capability,
        )
    else:
        channel_id, channel_name = f"datamcp/{record.tool_name}", f"Data MCP · {record.tool_name}"
    return SourceItem(
        platform=SOURCE_PLATFORM,
        channel_id=channel_id,
        channel_name=_printable(channel_name, 512),
        native_id=record.record_id,
        text=source_text(record),
        title=lookup_title(record),
        acquired_at=record.completed_at,
        provenance=SourceAcquisition(method="maltego_datamcp", content_scope="lookup_result"),
    )


def build_source_import(
    records: Iterable[MaltegoLookupRecord],
    *,
    title: str,
    question: str,
    include_errors: bool = False,
    include_discovery: bool = False,
) -> SourceImport:
    allowed = {"entities"} | ({"error"} if include_errors else set())
    allowed |= {"discovery"} if include_discovery else set()
    selected: dict[str, MaltegoLookupRecord] = {}
    skipped = 0
    for record in records:
        if record.evidence_kind in allowed:
            selected.setdefault(record.record_id, record)
        else:
            skipped += 1
    if not selected:
        raise CollectionFailure(
            "Choose at least one record with result entities. Search, describe, validation, "
            f"and error records stay out of cases by default ({skipped} skipped)."
        )
    return SourceImport(
        source_schema="1.0",
        case=SourceCase(title=title, question=question, synthetic=False),
        items=[to_source_item(record) for record in selected.values()],
    )


class RecordStore:
    """Content-addressed record files under ``<data_dir>/maltego/records``.

    Files are written once, made read-only, and re-verified whenever loaded.
    The folder ignores itself in git: lookup results are investigation data.
    """

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "maltego" / "records"

    def path(self, record_id: str) -> Path:
        if not isinstance(record_id, str) or not _RECORD_ID.fullmatch(record_id):
            raise CollectionFailure("Use the 64-character ID of a saved Maltego record.")
        return self.root / f"{record_id}.json"

    def _prepare(self) -> None:
        base = self.root.parent
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        ignore = base / ".gitignore"
        if not ignore.exists():
            ignore.write_text(
                "# Maltego lookup records are investigation data; keep them out of git.\n*\n",
                encoding="utf-8",
            )
        self.root.mkdir(exist_ok=True, mode=0o700)

    def save(self, record: MaltegoLookupRecord) -> Path:
        path = self.path(record.record_id)
        if path.exists():
            self.load(record.record_id)
            return path
        self._prepare()
        descriptor, temporary = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(record.model_dump_json(indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o444)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        return path

    def load(self, record_id: str) -> MaltegoLookupRecord:
        path = self.path(record_id)
        short = record_id[:12] + "…"
        try:
            with path.open("rb") as handle:
                raw = handle.read(MAX_RECORD_FILE_BYTES + 1)
        except FileNotFoundError:
            raise CollectionFailure(f"No saved Maltego record {short} in this workspace.") from None
        if len(raw) > MAX_RECORD_FILE_BYTES:
            raise CollectionFailure(f"Saved Maltego record {short} is too large to verify.")
        try:
            record = MaltegoLookupRecord.model_validate_json(raw)
        except ValidationError:
            raise CollectionFailure(
                f"Saved Maltego record {short} failed its integrity check and was not used."
            ) from None
        if record.record_id != record_id:
            raise CollectionFailure(f"Saved Maltego record {short} is stored under another ID.")
        return record

    def ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            path.stem for path in self.root.glob("*.json") if _RECORD_ID.fullmatch(path.stem)
        )

    def inventory(self) -> list[dict]:
        """Summaries of every saved record, newest first; failed checks stay visible."""
        rows = []
        for record_id in self.ids():
            try:
                record = self.load(record_id)
            except CollectionFailure as exc:
                rows.append({"record_id": record_id, "verified": False, "problem": str(exc)})
                continue
            rows.append(
                {
                    "record_id": record_id,
                    "verified": True,
                    "tool": record.tool_name,
                    "kind": record.evidence_kind,
                    "title": lookup_title(record),
                    "completed_at": record.completed_at,
                    "is_error": record.is_error,
                    "characters": len(record.text),
                }
            )
        return sorted(rows, key=lambda row: row.get("completed_at", ""), reverse=True)
