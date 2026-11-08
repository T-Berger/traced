"""Guard account analysis until the source identifies its authors reliably."""

from __future__ import annotations

import csv
import gzip
import zlib
from pathlib import Path

from . import config

SAMPLE_LIMIT = 1000
COMMENT = "SOCIAL_MEDIA_COMMENT"


class IdentityUnavailable(ValueError):
    """The saved inputs cannot support account-level analysis."""


def require_source_identity(data_dir: Path | None = None) -> None:
    """Reject account analysis until a supported source-identity contract exists.

    The bounded sample is a diagnostic only; it cannot establish completeness or
    validate an identity. In particular, arbitrary columns and name-derived handles
    are not treated as source-native sender IDs.
    """
    path = (data_dir if data_dir is not None else config.DATA) / "items.csv.gz"
    if not path.is_file():
        raise IdentityUnavailable(f"Account analysis unavailable: missing input {path}.")
    try:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames or "type" not in reader.fieldnames:
                raise IdentityUnavailable("Account analysis unavailable: input has no type column.")
            sampled = 0
            comments = 0
            truncated = False
            for row in reader:
                if sampled == SAMPLE_LIMIT:
                    truncated = True
                    break
                sampled += 1
                comments += row.get("type") == COMMENT
    except (OSError, EOFError, UnicodeError, csv.Error, zlib.error) as exc:
        raise IdentityUnavailable(
            f"Account analysis unavailable: cannot read {path.name}."
        ) from exc
    scope = "first " if truncated else "all "
    raise IdentityUnavailable(
        "Account analysis unavailable: the export has no supported, verified source-native "
        "sender identity contract. Display names and guessed handles cannot identify accounts. "
        f"Inspected {scope}{sampled} input records; {comments} are comments. "
        "Account metrics, graphs, model results, and new embeddings are unavailable."
    )
