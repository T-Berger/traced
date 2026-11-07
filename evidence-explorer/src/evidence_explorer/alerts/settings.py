"""Filing standard: when a labeled post is ready for a takedown report. Saved in the workspace."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

CATEGORIES = ("disinformation", "racism", "hate_speech", "harassment")
CATEGORY_LABELS = {
    "disinformation": "Disinformation",
    "racism": "Racism",
    "hate_speech": "Hate speech",
    "harassment": "Harassment",
}
SETTINGS_FILE = "alert_settings.json"


class CategoryStandard(BaseModel):
    """label_score: the post is a violation. filing_score: confident enough to file."""

    model_config = ConfigDict(extra="forbid")

    label_score: float = Field(0.6, ge=0, le=1)
    filing_score: float = Field(0.8, ge=0, le=1)

    @model_validator(mode="after")
    def _filing_not_below_label(self):
        if self.filing_score < self.label_score:
            raise ValueError("the filing score must be at least the violation score")
        return self


class AlertSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 2
    categories: dict[str, CategoryStandard] = Field(
        default_factory=lambda: {c: CategoryStandard() for c in CATEGORIES}
    )
    # An account is reported (not only its posts) once it has this many posts ready to file.
    account_ready_posts: int = Field(2, ge=1)
    # Posts at or above this reach are marked high reach and listed first.
    high_reach_views: int = Field(1000, ge=0)
    # Require an independent archive copy (e.g. Wayback Machine) before a post is ready.
    require_archive_copy: bool = False
    # Report preparer, printed on the evidence report; stays in this workspace.
    notifier_name: str = Field("", max_length=200)
    notifier_email: str = Field("", max_length=200)
    notifier_organisation: str = Field("", max_length=200)

    @model_validator(mode="after")
    def _all_categories(self):
        if set(self.categories) != set(CATEGORIES):
            raise ValueError(f"categories must be exactly {', '.join(CATEGORIES)}")
        return self

    @classmethod
    def defaults(cls) -> AlertSettings:
        return cls()


class AlertSettingsStore:
    """JSON file in the workspace data directory; missing or unreadable falls back to defaults."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / SETTINGS_FILE

    def load(self) -> tuple[AlertSettings, str | None]:
        if not self.path.exists():
            return AlertSettings.defaults(), None
        try:
            return AlertSettings.model_validate_json(self.path.read_text("utf-8")), None
        except (OSError, ValueError, ValidationError):
            return (
                AlertSettings.defaults(),
                "The saved filing standard could not be read; defaults are in use.",
            )

    def save(self, settings: AlertSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(settings.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".alert_settings.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def reset(self) -> AlertSettings:
        self.path.unlink(missing_ok=True)
        return AlertSettings.defaults()
