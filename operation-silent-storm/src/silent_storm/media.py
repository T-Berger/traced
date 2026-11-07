"""Constrained local media access and resumable byte checksums."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .storage import Store

ALLOWED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})


def normalize_relative_path(relative_path: str) -> str:
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError("A nonempty relative media path is required.")
    if "\x00" in relative_path or "\\" in relative_path:
        raise ValueError("Media paths must use safe relative POSIX components.")
    if relative_path.startswith("/") or re.match(r"^[A-Za-z]:", relative_path):
        raise ValueError("Absolute media paths are not allowed.")
    if any(part in ("", ".", "..") for part in relative_path.split("/")):
        raise ValueError("Empty, dot, and parent media path components are not allowed.")
    path = PurePosixPath(relative_path)
    if path.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError("Unsupported media extension; use a supplied still-image file.")
    return path.as_posix()


def resolve_frame_path(dataset_root: str | Path, relative_path: str) -> Path:
    relative = normalize_relative_path(relative_path)
    root = Path(dataset_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("The configured dataset root must be a directory.")
    resolved = (root / relative).resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError("Media path or symlink escapes the configured dataset root.")
    if resolved.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError("Resolved media target has an unsupported image extension.")
    if not resolved.is_file():
        raise FileNotFoundError(f"Media file is unavailable: {relative}")
    return resolved


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def hash_media(store: Store, release_id: str, limit: int | None = None) -> dict[str, int]:
    """Hash pending/failed records once per invocation; saved successes are resumable.

    Each result is committed independently. An interruption therefore leaves earlier
    checks available and later files unknown. Failed files are retried on a later call.
    """
    from .storage import utc_now

    if limit is not None and (not isinstance(limit, int) or limit < 0):
        raise ValueError("Hash limit must be a nonnegative integer or None.")
    release = store.get_release(release_id)
    if release is None:
        raise KeyError(f"Unknown release: {release_id}")
    processed, started_at = 0, utc_now()
    # Pending work takes precedence over old failures so a missing first file cannot
    # starve later files when callers use small limits. Newly failed files wait for
    # another invocation rather than being retried twice in the same run.
    for status in ("pending", "failed"):
        last = None
        while limit is None or processed < limit:
            batch_size = min(100, limit - processed) if limit is not None else 100
            with store.connection() as conn:
                rows = conn.execute(
                    "SELECT frame_id,relative_path FROM frames WHERE release_id=? "
                    "AND hash_status=? AND (? IS NULL OR frame_id>?) "
                    "AND (?='pending' OR hash_checked_at IS NULL OR hash_checked_at<?) "
                    "ORDER BY frame_id LIMIT ?",
                    (release_id, status, last, last, status, started_at, batch_size),
                ).fetchall()
            if not rows:
                break
            for row in rows:
                last = row["frame_id"]
                available = 0
                try:
                    path = resolve_frame_path(release["dataset_root"], row["relative_path"])
                    available = 1
                    before = path.stat()
                    digest = sha256_file(path)
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ino,
                    ):
                        raise ValueError(
                            "Media file changed while its checksum was being computed."
                        )
                    values = (
                        digest,
                        "hashed",
                        None,
                        utc_now(),
                        after.st_size,
                        after.st_mtime_ns,
                        1,
                    )
                except (OSError, ValueError) as exc:
                    # Do not persist absolute host paths from OS errors in visible records.
                    message = (
                        f"Media unavailable ({type(exc).__name__})"
                        if isinstance(exc, OSError)
                        else str(exc)
                    )
                    if isinstance(exc, OSError):
                        available = 0
                    values = (None, "failed", message, utc_now(), None, None, available)
                with store.connection() as conn:
                    conn.execute(
                        "UPDATE frames SET media_sha256=?,hash_status=?,hash_error=?,hash_checked_at=?,"
                        "media_size_bytes=?,media_mtime_ns=?,media_available=? "
                        "WHERE release_id=? AND frame_id=?",
                        (*values, release_id, row["frame_id"]),
                    )
                processed += 1
    return {**store.hash_counts(release_id), "processed": processed}
