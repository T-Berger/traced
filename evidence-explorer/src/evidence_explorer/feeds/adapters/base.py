"""Provider wrappers dispatch to existing collectors; no HTTP or auth is duplicated."""

from ...collection.models import CollectedBatch, CollectionFailure, CollectionSource
from ...ingestion.models import CollectionError
from .contracts import AdapterReadiness, Collector
from .locators import source_readiness


class SourceAdapterError(CollectionError):
    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: int | None = None,
        code: str = "platform_collection_failed",
        url: str = "",
    ):
        super().__init__(message, code=code, url=url)
        self.retry_after_seconds = (
            min(retry_after_seconds, 86400)
            if type(retry_after_seconds) is int and retry_after_seconds > 0
            else None
        )


class RegistryAdapter:
    platform: str = ""

    def __init__(self, collector: Collector | None = None):
        self._collector = collector

    def readiness(self) -> AdapterReadiness:
        return source_readiness(self.platform)

    def collect(self, source: CollectionSource) -> CollectedBatch:
        if source.platform != self.platform:
            raise SourceAdapterError("The selected adapter does not match the saved source.")
        try:
            if self._collector is not None:
                return self._collector(source)
            # Resolve dynamically so the existing registry stays the single dispatch
            # boundary and offline HTTP fixtures can replace it without network access.
            from ...collection import registry

            return registry.collect(source)
        except CollectionFailure as exc:
            raise SourceAdapterError(
                str(exc), retry_after_seconds=exc.retry_after_seconds
            ) from None
        except SourceAdapterError:
            raise
        except Exception:
            # Unexpected client failures can contain tokens or request headers.
            # Only existing providers' deliberately user-safe failures cross this boundary.
            raise SourceAdapterError(
                "The platform source could not be collected. Check its connection and retry."
            ) from None
