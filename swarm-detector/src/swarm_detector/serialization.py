"""Stable wire serialization and provenance fingerprints."""

import hashlib
import json
from datetime import UTC, datetime


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()
