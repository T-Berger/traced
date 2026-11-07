"""Bounded, source-neutral JSON import boundary for uploads and pasted bundles.

Preview never persists data. Diagnostics describe fields and constraints without
copying source text, validation inputs, or arbitrary producer exception messages.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .contracts import EvidenceBundle

MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_COLLECTION_ITEMS = 20_000
COLLECTIONS = ("records", "fragments", "runs", "findings", "links", "reviews", "scenarios")


class ImportValidationError(ValueError):
    """A safe, actionable problem with the submitted investigation package."""


@dataclass(frozen=True)
class ParsedImport:
    bundle: EvidenceBundle
    byte_count: int


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ImportValidationError("JSON contains duplicate object keys. Remove duplicates.")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ImportValidationError("JSON must use finite numbers; NaN and Infinity are not supported.")


def decode_content(content: str | bytes, filename: str | None = None) -> bytes:
    if filename is not None and not filename.lower().endswith(".json"):
        raise ImportValidationError("Choose an investigation bundle with a .json extension.")
    if not isinstance(content, (str, bytes)) or not content:
        raise ImportValidationError("Choose a JSON file or paste an investigation bundle first.")
    if isinstance(content, str) and content.startswith("data:"):
        if len(content) > (MAX_IMPORT_BYTES + 2) // 3 * 4 + 256:
            raise ImportValidationError("Investigation exceeds the 5 MiB import limit.")
        header, separator, encoded = content.partition(",")
        if not separator or not header.endswith(";base64") or len(header) > 200:
            raise ImportValidationError("The uploaded file encoding is invalid. Select it again.")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ImportValidationError(
                "The uploaded file encoding is invalid. Select it again."
            ) from None
    else:
        try:
            raw = content.encode("utf-8") if isinstance(content, str) else content
        except UnicodeEncodeError:
            raise ImportValidationError("Investigation must contain valid UTF-8 text.") from None
    if len(raw) > MAX_IMPORT_BYTES:
        raise ImportValidationError("Investigation exceeds the 5 MiB import limit.")
    return raw


def require_text_bundle(bundle: EvidenceBundle) -> None:
    if any(record.artifact_ref for record in bundle.records):
        raise ImportValidationError(
            "This importer accepts saved text bundles. Media files require a verified artifact "
            "loader; remove artifact references or use the specialist package importer."
        )


def _validation_message(exc: ValidationError) -> str:
    messages = []
    # Do not include Pydantic's `input`, `ctx`, or messages containing producer IDs.
    for issue in exc.errors(include_input=False, include_context=False, include_url=False)[:4]:
        path = ".".join(str(part) for part in issue["loc"])
        if path and all(part.isalnum() or part in "._" for part in path):
            label = path[:120]
        else:
            label = "bundle"
        kind = issue["type"]
        if kind == "missing":
            detail = "required field is missing"
        elif kind == "value_error":
            message = issue["msg"]
            if "Content hash mismatch" in message:
                detail = "saved text does not match its SHA-256 content hash"
            elif "Quotation mismatch" in message or "Invalid text span" in message:
                detail = "quoted passage or offsets do not match the saved text"
            elif "Unsupported evidence bundle major version" in message:
                detail = "unsupported schema version; use version 1"
            elif "Duplicate IDs" in message:
                detail = "object IDs must be unique within each collection"
            elif "Cyclic finding dependency" in message:
                detail = "finding dependencies contain a cycle"
            else:
                detail = "references or evidence constraints are invalid"
        else:
            detail = "value has the wrong type or is outside the allowed range"
        messages.append(f"{label}: {detail}")
    return "Invalid investigation bundle. " + "; ".join(messages) + "."


def parse_import(content: str | bytes, filename: str | None = None) -> ParsedImport:
    raw = decode_content(content, filename)
    try:
        decoded = raw.decode("utf-8-sig")
        data = json.loads(
            decoded, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except UnicodeDecodeError:
        raise ImportValidationError("Investigation must contain valid UTF-8 text.") from None
    except json.JSONDecodeError as exc:
        raise ImportValidationError(
            f"Invalid JSON at line {exc.lineno}, column {exc.colno}. Check the file syntax."
        ) from None
    except (RecursionError, OverflowError):
        raise ImportValidationError("Investigation JSON is nested too deeply.") from None
    if not isinstance(data, dict):
        raise ImportValidationError("An investigation bundle must be one JSON object.")
    for name in COLLECTIONS:
        collection = data.get(name)
        if isinstance(collection, list) and len(collection) > MAX_COLLECTION_ITEMS:
            raise ImportValidationError(
                f"The {name} collection exceeds the 20,000 item import limit."
            )
    try:
        bundle = EvidenceBundle.model_validate(data)
    except ValidationError as exc:
        raise ImportValidationError(_validation_message(exc)) from None
    except (RecursionError, OverflowError):
        raise ImportValidationError("Investigation JSON is nested too deeply.") from None
    require_text_bundle(bundle)
    return ParsedImport(bundle=bundle, byte_count=len(raw))


def import_summary(parsed: ParsedImport, status: str) -> dict[str, Any]:
    bundle = parsed.bundle
    warnings = []
    if not bundle.findings:
        warnings.append("No findings are included. Add an assessment before running a challenge.")
    if not bundle.records:
        warnings.append("No saved records are included in this investigation.")
    elif not any(fragment.quote for fragment in bundle.fragments):
        warnings.append("No quoted text fragments are available for model challenges.")
    if bundle.case.synthetic:
        warnings.append(
            "This investigation declares itself synthetic and will be labelled as a demo."
        )
    if status == "update":
        warnings.append(
            "New objects will be appended. Existing records and review history stay intact."
        )
    return {
        "fingerprint": bundle.fingerprint(),
        "case_id": bundle.case.id,
        "title": bundle.case.title,
        "question": bundle.case.question,
        "synthetic": bundle.case.synthetic,
        "counts": {name: len(getattr(bundle, name)) for name in COLLECTIONS},
        "limitations": bundle.case.limitations,
        "warnings": warnings,
        "status": status,
        "byte_count": parsed.byte_count,
    }
