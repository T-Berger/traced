"""Small, non-graphic release fixtures for Explorer acceptance checks."""

from __future__ import annotations

import base64
import csv
import hashlib
import struct
from pathlib import Path

import pytest

FRAME_FIELDS = (
    "frame_id",
    "file",
    "clip_id",
    "video_id",
    "week",
    "side",
    "image_type",
    "from_perspective",
    "to_perspective",
    "camera",
    "width_px",
    "height_px",
)
DETECTION_FIELDS = (
    "frame_id",
    "class",
    "model",
    "confidence",
    "box_x1",
    "box_y1",
    "box_x2",
    "box_y2",
    "track_id",
)

# A synthetic, uniformly gray 1-by-1 JPEG. Fixture creation needs only the standard library.
JPEG_BYTES = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAASABIAAD/4QBMRXhpZgAATU0AKgAAAAgAAYdpAAQAAAABAAAAGgAAAAAAA6AB"
    "AAMAAAABAAEAAKACAAQAAAABAAAAAaADAAQAAAABAAAAAQAAAAD/7QA4UGhvdG9zaG9wIDMuMAA4QklN"
    "BAQAAAAAAAA4QklNBCUAAAAAABDUHYzZjwCyBOmACZjs+EJ+/8AAEQgAAQABAwEiAAIRAQMRAf/EAB8A"
    "AAEFAQEBAQEBAAAAAAAAAAABAgMEBQYHCAkKC//EALUQAAIBAwMCBAMFBQQEAAABfQECAwAEEQUSITFB"
    "BhNRYQcicRQygZGhCCNCscEVUtHwJDNicoIJChYXGBkaJSYnKCkqNDU2Nzg5OkNERUZHSElKU1RVVl"
    "dYWVpjZGVmZ2hpanN0dXZ3eHl6g4SFhoeIiYqSk5SVlpeYmZqio6Slpqeoqaqys7S1tre4ubrCw8TF"
    "xsfIycrS09TV1tfY2drh4uPk5ebn6Onq8fLz9PX29/j5+v/EAB8BAAMBAQEBAQEBAQEAAAAAAAABAgME"
    "BQYHCAkKC//EALURAAIBAgQEAwQHBQQEAAECdwABAgMRBAUhMQYSQVEHYXETIjKBCBRCkaGxwQkjM1Lw"
    "FWJy0QoWJDThJfEXGBkaJicoKSo1Njc4OTpDREVGR0hJSlNUVVZXWFlaY2RlZmdoaWpzdHV2d3h5eoKD"
    "hIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uLj5OXm5+"
    "jp6vLz9PX29/j5+v/bAEMAAgICAgICAwICAwUDAwMFBgUFBQUGCAYGBgYGCAoICAgICAgKCgoKCgoKCgwM"
    "DAwMDA4ODg4ODw8PDw8PDw8PD//bAEMBAgICBAQEBwQEBxALCQsQEBAQEBAQEBAQEBAQEBAQEBAQEBAQ"
    "EBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEP/dAAQAAf/aAAwDAQACEQMRAD8A5+iiigD/2Q=="
)


def image_bytes(frame_id: str) -> bytes:
    """Add a JPEG comment so a route returning the wrong gray pixel is detectable."""
    comment = frame_id.encode("ascii")
    return (
        JPEG_BYTES[:2]
        + b"\xff\xfe"
        + struct.pack(">H", len(comment) + 2)
        + comment
        + JPEG_BYTES[2:]
    )


def write_csv(path: Path, fieldnames: tuple[str, ...], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def file_fingerprints(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def release_root(tmp_path: Path) -> Path:
    root = tmp_path / "release"
    (root / "metadata").mkdir(parents=True)
    (root / "frames").mkdir()
    specifications = [
        ("zeta", "different-file-name.jpg", "shared", "videoA", "11", "Blue Force", "RGB", "Photo"),
        ("alpha", "alpha.jpg", "shared", "videoA", "11", "Blue Force", "", "Photo"),
        ("mu", "mu.jpg", "shared", "videoB", "12", "Rusviet", "Infrared", "Photo"),
        ("literal%id", "percent.jpg", "c3", "videoC", "", "", "RGB", "Misc"),
        ("under_score", "underscore.jpg", "c4", "videoD", "13", "Other", "RGB", "NoiseEmpty"),
        ("plain", "plain.jpg", "c5", "videoE", "14", "Other", "Grayscale", "Photo"),
        ("missing", "missing.jpg", "c6", "videoF", "14", "Other", "RGB", "Photo"),
    ]
    frames = []
    for frame_id, filename, clip_id, video_id, week, side, camera, image_type in specifications:
        frames.append(
            dict(
                zip(
                    FRAME_FIELDS,
                    (
                        frame_id,
                        filename,
                        clip_id,
                        video_id,
                        week,
                        side,
                        image_type,
                        "",
                        "",
                        camera,
                        "1",
                        "1",
                    ),
                    strict=True,
                )
            )
        )
        if frame_id != "missing":
            (root / "frames" / filename).write_bytes(image_bytes(frame_id))
    write_csv(root / "metadata" / "frames.csv", FRAME_FIELDS, frames)
    detections = [
        dict(
            zip(
                DETECTION_FIELDS,
                (
                    "zeta",
                    "unclear",
                    "synthetic-fixture",
                    "0.5",
                    "0",
                    "0",
                    "1",
                    "1",
                    str(i),
                ),
                strict=True,
            )
        )
        for i in range(51)
    ]
    detections.append(
        dict(
            zip(
                DETECTION_FIELDS,
                (
                    "orphan",
                    "unclear",
                    "synthetic-fixture",
                    "0.5",
                    "0",
                    "0",
                    "1",
                    "1",
                    "",
                ),
                strict=True,
            )
        )
    )
    write_csv(root / "metadata" / "detections.csv", DETECTION_FIELDS, detections)
    return root


@pytest.fixture
def store(tmp_path: Path, release_root: Path):
    from silent_storm.ingest import import_release
    from silent_storm.storage import Store

    instance = Store(tmp_path / "explorer.sqlite")
    import_release(instance, release_root, release_id="fixture", title="Fixture")
    return instance


@pytest.fixture
def dash_app(store):
    from silent_storm.app import create_app

    return create_app(store=store)


@pytest.fixture
def client(dash_app):
    dash_app.server.config.update(TESTING=True)
    return dash_app.server.test_client()
