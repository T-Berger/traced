"""Local paths and runtime configuration; importing this module creates nothing."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    db_path: Path
    dataset_root: Path
    host: str = "127.0.0.1"
    port: int = 8064

    @classmethod
    def from_env(cls) -> AppConfig:
        project = Path(__file__).resolve().parents[2]
        root = Path(os.environ.get("SILENT_STORM_PROJECT", str(project))).expanduser().resolve()
        db_path = (
            Path(os.environ.get("SILENT_STORM_DB", str(root / "data" / "silent-storm.sqlite3")))
            .expanduser()
            .resolve()
        )
        dataset = (
            Path(
                os.environ.get("SILENT_STORM_DATASET", str(root / "dataset/extracted/day-1/Day 1"))
            )
            .expanduser()
            .resolve()
        )
        return cls(db_path=db_path, dataset_root=dataset)
