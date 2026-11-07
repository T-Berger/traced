"""Small interfaces over the workspace's existing platform collectors."""

from collections.abc import Callable
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from ...collection.models import CollectedBatch, CollectionSource

SavedPlatform = Literal["rss", "telegram", "youtube", "reddit", "x"]
Collector = Callable[[CollectionSource], CollectedBatch]


class NormalizedSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    platform: SavedPlatform
    locator: str
    url: str


class AdapterReadiness(BaseModel):
    model_config = ConfigDict(frozen=True)

    ready: bool
    message: str


class PlatformAdapter(Protocol):
    platform: str

    def collect(self, source: CollectionSource) -> CollectedBatch: ...

    def readiness(self) -> AdapterReadiness: ...
