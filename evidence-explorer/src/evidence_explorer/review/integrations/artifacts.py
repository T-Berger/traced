"""Capture bounded local artifact bytes and validate the exact returned payload.

A caller serving media must send ``content`` rather than reopen ``path`` after
validation. The path remains metadata and supports existing local-file callers.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..contracts import EvidenceBundle
from .packages import MAX_FILE_BYTES, safe_artifact_path


@dataclass(frozen=True, slots=True)
class CapturedArtifact:
    path: Path
    content: bytes
    content_hash: str

    @property
    def filename(self) -> str:
        return self.path.name


def capture_verified_artifact(
    bundle: EvidenceBundle,
    root: Path,
    record_id: str,
    expected_hash: str | None = None,
) -> CapturedArtifact:
    """Read once, within the size bound, and validate those immutable bytes."""
    record = next((item for item in bundle.records if item.id == record_id), None)
    if record is None or not record.artifact_ref:
        raise ValueError("This record has no supplied media artifact.")
    saved_hash = record.content_hash
    if expected_hash is not None and expected_hash != saved_hash:
        raise ValueError("The saved media no longer matches its evidence hash.")
    path = safe_artifact_path(root, record.artifact_ref)
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("The supplied media artifact is unavailable or too large.")
    with path.open("rb") as stream:
        content = bytes(stream.read(MAX_FILE_BYTES + 1))
    # The file can grow after stat. Validate the captured length before hashing
    # or returning; no later path read may replace these checked bytes.
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("The supplied media artifact is unavailable or too large.")
    digest = hashlib.sha256(content).hexdigest()
    if digest != saved_hash:
        raise ValueError("The saved media no longer matches its evidence hash.")
    return CapturedArtifact(path=path, content=content, content_hash=digest)
