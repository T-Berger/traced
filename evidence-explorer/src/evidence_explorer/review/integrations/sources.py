"""Compatibility imports for the saved-source integration API.

Source contracts live in source_contracts, evidence projection in source_bundles,
and browser projections in review.source_browser. Existing import paths retain
the same objects while collectors no longer depend on this composition package.
"""

from ...source_bundles import (
    _LIMITATIONS as _LIMITATIONS,
)
from ...source_bundles import (
    _reject_constant as _reject_constant,
)
from ...source_bundles import (
    _unique_object as _unique_object,
)
from ...source_bundles import (
    _validate_payload as _validate_payload,
)
from ...source_bundles import (
    _validation_message as _validation_message,
)
from ...source_bundles import (
    build_source_bundle as build_source_bundle,
)
from ...source_bundles import (
    parse_source_import as parse_source_import,
)
from ...source_contracts import (
    _HOST as _HOST,
)
from ...source_contracts import (
    MAX_SOURCE_ITEMS as MAX_SOURCE_ITEMS,
)
from ...source_contracts import (
    PLATFORM_LABELS as PLATFORM_LABELS,
)
from ...source_contracts import (
    Platform as Platform,
)
from ...source_contracts import (
    SourceAcquisition as SourceAcquisition,
)
from ...source_contracts import (
    SourceCase as SourceCase,
)
from ...source_contracts import (
    SourceContract as SourceContract,
)
from ...source_contracts import (
    SourceIdentity as SourceIdentity,
)
from ...source_contracts import (
    SourceImport as SourceImport,
)
from ...source_contracts import (
    SourceItem as SourceItem,
)
from ...source_contracts import (
    _channel_key as _channel_key,
)
from ...source_contracts import (
    _digest as _digest,
)
from ...source_contracts import (
    _identity as _identity,
)
from ...source_contracts import (
    _saved_item as _saved_item,
)
from ...source_contracts import (
    _text_hash as _text_hash,
)
from ...source_contracts import (
    _timestamp as _timestamp,
)
from ...source_contracts import (
    safe_source_url as safe_source_url,
)
from ..source_browser import (
    MAX_BROWSER_ITEMS as MAX_BROWSER_ITEMS,
)
from ..source_browser import (
    _browser_record as _browser_record,
)
from ..source_browser import (
    _sort_publication as _sort_publication,
)
from ..source_browser import (
    source_browser_data as source_browser_data,
)
