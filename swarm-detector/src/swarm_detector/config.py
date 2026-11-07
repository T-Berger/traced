"""Validated detector settings shared by every application entry point."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass


def positive_number(value: object, name: str) -> int | float:
    """Reject booleans and non-finite values before timing arithmetic."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be a finite positive number")
    return value


@dataclass(frozen=True, slots=True)
class DetectionConfig:
    """Timing and randomization controls; content never affects these settings."""

    window_seconds: int | float = 120
    min_families: int = 3
    randomizations: int = 20
    seed: int = 42
    swaps_per_slot: int = 10
    max_attempts_per_slot: int = 100
    max_pairs_per_post: int = 200_000

    def __post_init__(self) -> None:
        positive_number(self.window_seconds, "window_seconds")
        for name in ("min_families", "max_pairs_per_post"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("randomizations", "swaps_per_slot", "max_attempts_per_slot"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be an integer")

    @classmethod
    def from_mapping(cls, overrides: Mapping | None = None) -> DetectionConfig:
        try:
            return cls(**(overrides or {}))
        except TypeError as exc:
            raise ValueError(f"Unknown or invalid detector settings: {exc}") from exc

    def as_dict(self) -> dict:
        return asdict(self)
