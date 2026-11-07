"""Environment configuration shared by the UI, CLI, worker, and model client."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path("data").resolve())
    ollama_url: str = "http://127.0.0.1:11434"
    embedding_model: str = "bge-m3"
    chat_model: str = "qwen3.5:9b"
    host: str = "127.0.0.1"
    port: int = 8051
    read_only: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "evidence.sqlite3"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(override=False)
        return cls(
            data_dir=Path(os.getenv("EVIDENCE_DATA_DIR", "data")).expanduser().resolve(),
            ollama_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/"),
            embedding_model=os.getenv("EVIDENCE_EMBEDDING_MODEL", "bge-m3"),
            chat_model=os.getenv("EVIDENCE_CHAT_MODEL", "qwen3.5:9b"),
            host=os.getenv("EVIDENCE_HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", os.getenv("EVIDENCE_PORT", "8051"))),
            read_only=os.getenv("EVIDENCE_READ_ONLY", "false").lower() in {"true", "1", "yes"},
        )
