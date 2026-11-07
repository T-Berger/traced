"""Source records remain usable without importing review composition or its UI."""

import subprocess
import sys

from evidence_explorer import source_bundles, source_contracts
from evidence_explorer.review import source_browser
from evidence_explorer.review.integrations import sources


def test_source_contract_import_does_not_load_review_or_ui():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import evidence_explorer.source_contracts; "
            "assert not any(name == 'evidence_explorer.review' or "
            "name.startswith('evidence_explorer.review.') for name in sys.modules); "
            "assert 'dash' not in sys.modules",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_old_integration_imports_keep_the_same_contract_and_projection_objects():
    for name in (
        "SourceContract",
        "SourceIdentity",
        "SourceAcquisition",
        "SourceItem",
        "SourceCase",
        "SourceImport",
        "safe_source_url",
    ):
        assert getattr(sources, name) is getattr(source_contracts, name)
    assert sources.build_source_bundle is source_bundles.build_source_bundle
    assert sources.parse_source_import is source_bundles.parse_source_import
    assert sources.source_browser_data is source_browser.source_browser_data
