#!/usr/bin/env python3
"""Read-only ZIP inventory and explicitly selected, bounded structural samples.

Default operation reads central metadata only. Python's ZipFile indexes the central
directory in memory; payload limits do not bound that metadata allocation. No files
are extracted, executed, modified, downloaded, or sent over a network.

Selected reads consume complete small members only; prefix sampling is unsupported.
The challenge's reported 640,832,808-byte metadata.json is deliberately rejected by
--entry. Its separately recorded bounded-prefix mapping fixture was produced by a
different inspection and is not functionality claimed by this command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import zipfile
import zlib
from collections import Counter
from pathlib import Path, PureWindowsPath

DEFAULT_MEMBER_BYTES = 65_536
DEFAULT_TOTAL_BYTES = 262_144
HARD_MEMBER_BYTES = 1_048_576
HARD_TOTAL_BYTES = 4_194_304
MAX_SELECTIONS = 32
READ_CHUNK = 4096
TEXT_SUFFIXES = {
    ".json",
    ".jsonl",
    ".ndjson",
    ".txt",
    ".csv",
    ".tsv",
    ".md",
    ".log",
    ".yaml",
    ".yml",
}
SUPPORTED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
MAX_SHAPE_FIELDS = 24
MAX_SHAPE_NODES = 96
MAX_SHAPE_DEPTH = 3


class ProbeError(ValueError):
    """A requested operation is outside the bounded probe contract."""


def _bounded_label(value: str, limit: int = 160) -> str:
    return value if len(value) <= limit else value[:limit] + "…"


def _kind(info: zipfile.ZipInfo) -> str:
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if info.is_dir() or file_type == stat.S_IFDIR or info.external_attr & 0x10:
        return "directory"
    if file_type == stat.S_IFLNK:
        return "symlink"
    if file_type not in (0, stat.S_IFREG):
        return "nonregular"
    return "regular"


def _safe_name(name: str) -> None:
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or PureWindowsPath(name).drive
        or any(part in ("", ".", "..") for part in name.split("/"))
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        raise ProbeError("unsafe_member_path")


def _validate_member(info: zipfile.ZipInfo, max_member_bytes: int) -> int:
    _safe_name(info.filename)
    _safe_name(info.orig_filename)
    if _kind(info) != "regular":
        raise ProbeError("member_is_not_regular")
    if info.external_attr >> 16 & 0o111:
        raise ProbeError("executable_member")
    if Path(info.filename).suffix.lower() not in TEXT_SUFFIXES:
        raise ProbeError("unsupported_text_member_suffix")
    if info.flag_bits & 1:
        raise ProbeError("encrypted_member")
    if info.compress_type not in SUPPORTED_COMPRESSION:
        raise ProbeError("unsupported_compression")
    charge = max(info.file_size, info.compress_size)
    if charge > max_member_bytes:
        raise ProbeError("member_exceeds_byte_limit")
    return charge


def _read_payload(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    chunks = []
    remaining = info.file_size
    with archive.open(info, "r") as source:
        while remaining:
            requested = min(READ_CHUNK, remaining)
            chunk = source.read(requested)
            if not chunk or len(chunk) > requested:
                raise ProbeError("member_length_mismatch")
            chunks.append(chunk)
            remaining -= len(chunk)
    payload = b"".join(chunks)
    if zlib.crc32(payload) & 0xFFFFFFFF != info.CRC:
        raise ProbeError("crc_mismatch")
    return payload


def _text(payload: bytes) -> str:
    signatures = (
        b"\x7fELF",
        b"MZ",
        b"\x89PNG",
        b"\xff\xd8\xff",
        b"GIF8",
        b"PK\x03\x04",
        b"%PDF-",
        b"SQLite format 3",
    )
    if payload.startswith(signatures) or b"\x00" in payload:
        raise ProbeError("binary_payload")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ProbeError("not_utf8_text") from exc
    if any((ord(char) < 32 and char not in "\t\n\r\f") or 127 <= ord(char) < 160 for char in text):
        raise ProbeError("binary_control_characters")
    return text


def _shape(value, *, depth: int = 0, budget: list[int] | None = None) -> dict:
    budget = [MAX_SHAPE_NODES] if budget is None else budget
    if budget[0] <= 0:
        return {"kind": "omitted", "reason": "shape_node_limit"}
    budget[0] -= 1
    if isinstance(value, dict):
        keys = list(value)[:MAX_SHAPE_FIELDS]
        result = {
            "kind": "object",
            "field_count": len(value),
            "field_names": [_bounded_label(key) for key in keys],
            "omitted_fields": len(value) - len(keys),
        }
        if depth < MAX_SHAPE_DEPTH:
            result["field_shapes"] = [
                {
                    "field": _bounded_label(key),
                    "shape": _shape(value[key], depth=depth + 1, budget=budget),
                }
                for key in keys
                if budget[0] > 0
            ]
        else:
            result["nested_details_omitted"] = True
        return result
    if isinstance(value, list):
        result = {"kind": "array", "length": len(value)}
        if depth < MAX_SHAPE_DEPTH:
            result["sampled_item_shapes"] = [
                _shape(item, depth=depth + 1, budget=budget) for item in value[:3] if budget[0] > 0
            ]
        else:
            result["nested_details_omitted"] = True
        return result
    if isinstance(value, str):
        return {"kind": "string", "characters": len(value)}
    if value is None:
        return {"kind": "null"}
    if isinstance(value, bool):
        return {"kind": "boolean"}
    return {"kind": "number"}


def _nonstandard_json_constant(_value: str):
    raise ValueError("Nonstandard JSON constant")


def _structure(text: str, suffix: str) -> dict:
    if suffix == ".json":
        try:
            parsed = json.loads(text, parse_constant=_nonstandard_json_constant)
        except (ValueError, RecursionError) as exc:
            raise ProbeError("invalid_or_too_deep_json") from exc
        return {"format": "json", "shape": _shape(parsed)}
    if suffix in (".jsonl", ".ndjson"):
        lines = [line for line in text.splitlines() if line.strip()]
        samples = []
        for line in lines[:16]:
            try:
                samples.append(_shape(json.loads(line, parse_constant=_nonstandard_json_constant)))
            except (ValueError, RecursionError) as exc:
                raise ProbeError("invalid_or_too_deep_sampled_json_line") from exc
        return {
            "format": "json_lines",
            "nonempty_lines": len(lines),
            "parsed_sample_lines": len(samples),
            "remaining_lines_not_parsed": max(0, len(lines) - len(samples)),
            "sample_shapes": samples,
        }
    return {
        "format": "utf8_text",
        "shape": {"kind": "text", "characters": len(text), "lines": len(text.splitlines())},
        "text_values_returned": False,
    }


def probe_archive(
    archive_path: str | Path,
    *,
    entries: list[str] | tuple[str, ...] = (),
    max_member_bytes: int = DEFAULT_MEMBER_BYTES,
    max_total_read_bytes: int = DEFAULT_TOTAL_BYTES,
    max_examples: int = 12,
) -> dict:
    """Inventory a completed ZIP; read bodies only for explicitly named members."""
    for name, value, upper in (
        ("max_member_bytes", max_member_bytes, HARD_MEMBER_BYTES),
        ("max_total_read_bytes", max_total_read_bytes, HARD_TOTAL_BYTES),
        ("max_examples", max_examples, 100),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
            raise ProbeError(f"{name}_must_be_between_1_and_{upper}")
    if len(entries) > MAX_SELECTIONS or len(set(entries)) != len(entries):
        raise ProbeError("select_at_most_32_distinct_exact_member_names")
    path = Path(archive_path).expanduser().resolve(strict=True)
    if path.suffix.lower() != ".zip" or not stat.S_ISREG(path.stat().st_mode):
        raise ProbeError("archive_must_be_a_completed_regular_zip_file")
    before = path.stat()
    requested = set(entries)
    selected_infos = {}
    ambiguous = set()
    prefixes = {}
    kinds = Counter()
    sizes = Counter()
    examples = []
    untracked_prefix_members = 0
    with path.open("rb") as handle, zipfile.ZipFile(handle, "r") as archive:
        infos = archive.infolist()
        for info in infos:
            kinds[_kind(info)] += 1
            sizes["declared_uncompressed_bytes"] += info.file_size
            sizes["declared_compressed_bytes"] += info.compress_size
            sizes["encrypted_members"] += bool(info.flag_bits & 1)
            prefix = info.filename.split("/", 1)[0]
            if prefix in prefixes:
                prefixes[prefix] += 1
            elif len(prefixes) < max_examples:
                prefixes[prefix] = 1
            else:
                untracked_prefix_members += 1
            if len(examples) < max_examples:
                examples.append(_bounded_label(info.filename))
            if info.filename in requested:
                if info.filename in selected_infos:
                    ambiguous.add(info.filename)
                else:
                    selected_infos[info.filename] = info
        selected = []
        reserved = 0
        for name in entries:
            info = selected_infos.get(name)
            result = {"member_name": name, "status": "rejected"}
            if info is not None:
                result.update(
                    {
                        "declared_bytes": info.file_size,
                        "compressed_bytes": info.compress_size,
                        "crc32": f"{info.CRC:08x}",
                    }
                )
            try:
                _safe_name(name)
                if info is None:
                    raise ProbeError("exact_member_not_found")
                if name in ambiguous:
                    raise ProbeError("ambiguous_duplicate_member_name")
                charge = _validate_member(info, max_member_bytes)
                if charge > max_total_read_bytes - reserved:
                    raise ProbeError("total_payload_budget_would_be_exceeded")
                reserved += charge
                payload = _read_payload(archive, info)
                result.update(
                    {
                        "read_bytes": len(payload),
                        "payload_sha256": hashlib.sha256(payload).hexdigest(),
                        "crc_verified": True,
                    }
                )
                text = _text(payload)
                result.update(_structure(text, Path(name).suffix.lower()))
                result["status"] = "accepted"
            except zlib.error:
                result["reason"] = "invalid_compressed_stream"
            except (ProbeError, OSError, zipfile.BadZipFile, RuntimeError) as exc:
                result["reason"] = str(exc) if isinstance(exc, ProbeError) else type(exc).__name__
            selected.append(result)
        after = os.fstat(handle.fileno())
    metadata_unchanged = all(
        getattr(before, field) == getattr(after, field)
        for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    )
    return {
        "schema_version": 1,
        "ok": metadata_unchanged and all(row["status"] == "accepted" for row in selected),
        "archive": {
            "path": str(path),
            "bytes": before.st_size,
            "size_inode_mtime_unchanged": metadata_unchanged,
            "whole_archive_hash_computed": False,
        },
        "inventory": {
            "members": len(infos),
            "kinds": dict(kinds),
            **dict(sizes),
            "member_name_examples": examples,
            "prefix_examples": [
                {"prefix": _bounded_label(prefix), "members": count}
                for prefix, count in prefixes.items()
            ],
            "members_in_untracked_prefixes": untracked_prefix_members,
        },
        "limits": {
            "max_member_bytes": max_member_bytes,
            "max_total_read_bytes": max_total_read_bytes,
            "max_examples": max_examples,
            "reserved_payload_bytes": reserved,
            "reservation_rule": "max(declared compressed, declared uncompressed) per opened member",
            "scope": "Member bodies only; ZipFile's central-directory index and metadata reads are not memory/IO bounded by these limits.",
        },
        "metadata_only": not entries,
        "selected_members": selected,
        "source_text_values_returned": False,
        "extracted_files": 0,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive", type=Path, required=True, help="Completed .zip file; never a partial download."
    )
    parser.add_argument(
        "--entry",
        action="append",
        default=[],
        help="Exact text/JSON member name; repeat for up to 32 distinct members.",
    )
    parser.add_argument(
        "--max-member-bytes",
        type=int,
        default=DEFAULT_MEMBER_BYTES,
        help="Compressed/uncompressed body cap (default 65536; hard maximum 1048576).",
    )
    parser.add_argument(
        "--max-total-read-bytes",
        type=int,
        default=DEFAULT_TOTAL_BYTES,
        help="Aggregate body budget (default 262144; hard maximum 4194304).",
    )
    parser.add_argument(
        "--max-examples", type=int, default=12, help="Bound inventory examples (1–100)."
    )
    args = parser.parse_args(argv)
    try:
        report = probe_archive(
            args.archive,
            entries=args.entry,
            max_member_bytes=args.max_member_bytes,
            max_total_read_bytes=args.max_total_read_bytes,
            max_examples=args.max_examples,
        )
    except (ProbeError, OSError, zipfile.BadZipFile) as exc:
        report = {
            "schema_version": 1,
            "ok": False,
            "error": str(exc) if isinstance(exc, ProbeError) else type(exc).__name__,
        }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
