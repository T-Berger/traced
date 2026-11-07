import hashlib
import importlib.util
import json
import stat
import struct
import zipfile
import zlib
from pathlib import Path

import pytest

_path = Path(__file__).resolve().parents[1] / "scripts" / "probe_swarm_archive.py"
_spec = importlib.util.spec_from_file_location("bounded_archive_probe", _path)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Unable to load archive probe.")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def make_zip(tmp_path, members):
    path = tmp_path / "fixture.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in members:
            archive.writestr(name, body)
    return path


def test_metadata_only_opens_no_member_and_preserves_archive(tmp_path, monkeypatch):
    path = make_zip(
        tmp_path, [("project/a.json", b'{"text":"PRIVATE BODY"}'), ("other/b.txt", b"body")]
    )
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    before_mtime = path.stat().st_mtime_ns
    before_files = set(tmp_path.rglob("*"))

    def forbidden(*args, **kwargs):
        raise AssertionError("Metadata-only probe must not open or extract members.")

    monkeypatch.setattr(zipfile.ZipFile, "open", forbidden)
    monkeypatch.setattr(zipfile.ZipFile, "extract", forbidden)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", forbidden)
    report = probe.probe_archive(path, max_examples=1)
    assert report["ok"] and report["metadata_only"]
    assert report["inventory"]["members"] == 2
    assert len(report["inventory"]["member_name_examples"]) == 1
    assert len(report["inventory"]["prefix_examples"]) == 1
    assert report["limits"]["reserved_payload_bytes"] == 0
    assert report["selected_members"] == []
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
    assert path.stat().st_mtime_ns == before_mtime
    assert set(tmp_path.rglob("*")) == before_files


def test_selected_json_has_shape_crc_hash_but_no_message_body(tmp_path, monkeypatch):
    payload = b'{"message":{"text":"PRIVATE MESSAGE BODY","account_id":123},"items":[true,null]}'
    path = make_zip(tmp_path, [("data/message.json", payload)])
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    original_mtime = path.stat().st_mtime_ns

    def forbidden(*args, **kwargs):
        raise AssertionError("Extraction and unbounded ZipFile.read are forbidden.")

    monkeypatch.setattr(zipfile.ZipFile, "read", forbidden)
    monkeypatch.setattr(zipfile.ZipFile, "extract", forbidden)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", forbidden)
    report = probe.probe_archive(path, entries=["data/message.json"])
    row = report["selected_members"][0]
    assert report["ok"] and not report["metadata_only"]
    assert row["read_bytes"] == row["declared_bytes"] == len(payload)
    assert row["payload_sha256"] == hashlib.sha256(payload).hexdigest()
    assert row["crc_verified"] is True and len(row["crc32"]) == 8
    assert row["shape"]["field_names"] == ["message", "items"]
    assert "PRIVATE MESSAGE BODY" not in json.dumps(report)
    assert set(tmp_path.iterdir()) == {path}
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash
    assert path.stat().st_mtime_ns == original_mtime


def test_stream_reads_always_use_explicit_small_bounds(tmp_path, monkeypatch):
    path = make_zip(tmp_path, [("body.txt", b"a" * 10_000)])
    original = zipfile.ZipExtFile.read
    calls = []

    def bounded(source, size=-1):
        assert 0 < size <= probe.READ_CHUNK
        calls.append(size)
        return original(source, size)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", bounded)
    report = probe.probe_archive(
        path, entries=["body.txt"], max_member_bytes=10_000, max_total_read_bytes=10_000
    )
    assert report["ok"] and sum(calls) == 10_000
    assert report["limits"]["reserved_payload_bytes"] <= 10_000


def test_oversized_member_is_rejected_before_open(tmp_path, monkeypatch):
    path = make_zip(tmp_path, [("large.txt", b"x" * 101)])

    def forbidden(*args, **kwargs):
        raise AssertionError("Oversized members must not be opened.")

    monkeypatch.setattr(zipfile.ZipFile, "open", forbidden)
    report = probe.probe_archive(path, entries=["large.txt"], max_member_bytes=100)
    assert not report["ok"]
    assert report["selected_members"][0]["reason"] == "member_exceeds_byte_limit"
    assert report["limits"]["reserved_payload_bytes"] == 0


def test_total_budget_prevents_opening_second_member(tmp_path, monkeypatch):
    path = make_zip(tmp_path, [("first.txt", b"a" * 50), ("second.txt", b"b" * 50)])
    original = zipfile.ZipFile.open
    opened = []

    def watched(archive, info, *args, **kwargs):
        opened.append(info.filename)
        return original(archive, info, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", watched)
    report = probe.probe_archive(
        path, entries=["first.txt", "second.txt"], max_member_bytes=60, max_total_read_bytes=75
    )
    assert not report["ok"] and opened == ["first.txt"]
    assert report["limits"]["reserved_payload_bytes"] == 50
    assert report["selected_members"][1]["reason"] == "total_payload_budget_would_be_exceeded"


@pytest.mark.parametrize(
    "name",
    [
        "../escape.json",
        "/absolute.json",
        "C:/drive.json",
        "dir\\evil.json",
        "./dot.json",
        "dir//empty.json",
        "run.py",
        "run.sh",
        "image.png",
    ],
)
def test_unsafe_and_executable_names_are_rejected_before_open(tmp_path, monkeypatch, name):
    path = make_zip(tmp_path, [(name, b"{}")])

    def forbidden(*args, **kwargs):
        raise AssertionError("Unsafe selected names must not be opened.")

    monkeypatch.setattr(zipfile.ZipFile, "open", forbidden)
    report = probe.probe_archive(path, entries=[name])
    assert not report["ok"] and report["selected_members"][0]["status"] == "rejected"


@pytest.mark.parametrize(
    "mode", [stat.S_IFLNK | 0o600, stat.S_IFIFO | 0o600, stat.S_IFREG | 0o755, stat.S_IFDIR | 0o700]
)
def test_nonregular_and_executable_modes_are_rejected(tmp_path, monkeypatch, mode):
    path = tmp_path / "fixture.zip"
    info = zipfile.ZipInfo("member.json")
    info.create_system = 3
    info.external_attr = mode << 16
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(info, b"{}")

    def forbidden(*args, **kwargs):
        raise AssertionError("Nonregular or executable members must not be opened.")

    monkeypatch.setattr(zipfile.ZipFile, "open", forbidden)
    assert not probe.probe_archive(path, entries=["member.json"])["ok"]


@pytest.mark.parametrize("body", [b"\x00binary", b"\xff\xfe", b"MZexecutable", b"plain\x01control"])
def test_binary_payloads_are_rejected_without_text_dump(tmp_path, body):
    path = make_zip(tmp_path, [("looks-like-text.txt", body)])
    report = probe.probe_archive(path, entries=["looks-like-text.txt"])
    row = report["selected_members"][0]
    assert not report["ok"] and row["status"] == "rejected"
    assert row["payload_sha256"] == hashlib.sha256(body).hexdigest()
    assert "shape" not in row


def test_duplicate_names_are_ambiguous_and_not_opened(tmp_path, monkeypatch):
    with pytest.warns(UserWarning, match="Duplicate name"):
        path = make_zip(tmp_path, [("same.json", b"{}"), ("same.json", b"[]")])

    def forbidden(*args, **kwargs):
        raise AssertionError("Ambiguous members must not be opened.")

    monkeypatch.setattr(zipfile.ZipFile, "open", forbidden)
    report = probe.probe_archive(path, entries=["same.json"])
    assert report["selected_members"][0]["reason"] == "ambiguous_duplicate_member_name"


def test_partial_download_and_unbounded_configuration_are_rejected(tmp_path):
    path = make_zip(tmp_path, [("a.json", b"{}")])
    partial = path.with_suffix(".crdownload")
    path.rename(partial)
    with pytest.raises(probe.ProbeError, match="completed_regular_zip"):
        probe.probe_archive(partial)
    with pytest.raises(probe.ProbeError, match="max_member_bytes"):
        probe.probe_archive(partial, max_member_bytes=probe.HARD_MEMBER_BYTES + 1)


def test_cli_default_is_metadata_only_and_selected_failure_is_nonzero(tmp_path, capsys):
    path = make_zip(tmp_path, [("a.json", b'{"text":"DO NOT PRINT THIS VALUE"}')])
    assert probe.main(["--archive", str(path)]) == 0
    default = json.loads(capsys.readouterr().out)
    assert default["metadata_only"] and not default["selected_members"]
    assert probe.main(["--archive", str(path), "--entry", "not-present.json"]) == 1
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["selected_members"][0]["reason"] == "exact_member_not_found"


def test_corrupt_deflate_returns_rejected_member_json_without_mutation(tmp_path, capsys):
    path = make_zip(tmp_path, [("broken.json", b'{"text":"PRIVATE CORRUPT FIXTURE BODY"}')])
    with zipfile.ZipFile(path, "r") as archive:
        member = archive.getinfo("broken.json")
        assert member.compress_type == zipfile.ZIP_DEFLATED
        header_offset, compressed_size = member.header_offset, member.compress_size

    content = bytearray(path.read_bytes())
    name_bytes, extra_bytes = struct.unpack_from("<HH", content, header_offset + 26)
    body_offset = header_offset + 30 + name_bytes + extra_bytes
    # DEFLATE BTYPE=3 is reserved. Corrupt the actual block, not the ZIP CRC.
    content[body_offset] |= 0b00000110
    path.write_bytes(content)
    with pytest.raises(zlib.error):
        zlib.decompress(bytes(content[body_offset : body_offset + compressed_size]), wbits=-15)

    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    original_mtime = path.stat().st_mtime_ns
    original_files = set(tmp_path.rglob("*"))

    assert probe.main(["--archive", str(path), "--entry", "broken.json"]) != 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["ok"] is False
    row = report["selected_members"][0]
    assert row["member_name"] == "broken.json"
    assert row["status"] == "rejected"
    assert row["reason"] == "invalid_compressed_stream"
    assert "payload_sha256" not in row
    assert "PRIVATE CORRUPT FIXTURE BODY" not in captured.out
    assert "Traceback" not in captured.out + captured.err
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash
    assert path.stat().st_mtime_ns == original_mtime
    assert set(tmp_path.rglob("*")) == original_files
