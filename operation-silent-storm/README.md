# Silent Storm Evidence Explorer

The local Traced Dash workbench combines Evidence Explorer, persistent Review and Chronology, and Release Diff and Cited Brief. Browse the Day 1 archive, save source-linked notes, inspect immutable revisions, compare supplied releases, and export a cited specialist snapshot. SQLite preserves review and brief history; source images stay on disk.

## Run locally

From this project folder, use Python 3.12 through uv:

```sh
make -C .. setup
uv run --locked silent-storm serve --no-import
```

Open [the local Explorer](http://127.0.0.1:8064). This starts an empty workbench. Supply your own metadata and media paths when you are ready to import a release. Image previews stay hidden until **Reveal previews** is selected. The interface includes release selection, ID search, metadata filters, pagination, clip contact sheets and a source inspector. It does not require a model or API key.

## Review and posting chronology

Open **Reviews & chronology**, or start a note from the selected Explorer frame. Enter the acting reviewer manually, link evidence, and save an observation, interpretation, uncertainty and rationale. The four states are **Draft**, **Reviewed**, **Rejected** and **Needs review**. Reviewed requires an observation, rationale and available evidence that is not excluded; it records a workflow decision rather than image authenticity. Reviewer names are manual attribution, not authenticated identities.

Each save appends an immutable SQLite revision. History and evidence references survive restart. A stale-edit conflict preserves the unsaved form and asks you to reload explicitly; another person's saved revision cannot be overwritten silently.

Source exclusions require a forked scenario, actor, reason and a current impact preview. Applying an exclusion preserves baseline evidence and history, appends an exclusion event, and marks every dependent statement Needs review in that fork. Restoring the source does not approve those statements; they require an explicit new review. The baseline rejects source-exclusion writes.

The chart is labeled **Exercise week first posted**. Choose source videos, clip groups or frames, with videos as the default. Excluded evidence appears as a subset overlay; review-state filters change statement counts only. Unknown weeks remain visible. Click a week to filter the review queue, then use **Open available evidence for selected week** to carry that week into Explorer. These counts describe supplied evidence, not events or capture chronology.

## Release Diff and Cited Brief

Open **03 Release diff & brief**. Comparison requires two separately imported releases; the Day 1 workspace does not invent a Day 2 release. Added and removed frame IDs and changed metadata describe supplied records. Hash coverage, cross-ID checksum candidates, and bounded current file-stat checks are reported separately. No comparison transfers review approval or establishes independent events.

Choose an existing review scenario and up to 12 saved statements, enter a title and acting author, and explicitly declare the evidence origin. **Preview** separates included statements from omissions. **Finalize cited snapshot** rechecks scenario/revision versions, source exclusions, and actual selected image bytes. Only current Reviewed statements with available, nonexcluded, completed-checksum evidence enter the snapshot. Other selected statements remain listed as omissions, with reasons.

Saved snapshots freeze note revisions, actors, source fingerprints, exact CSV rows, frame/clip/video IDs, original relative paths and freshly checked SHA-256 hashes. Open **Saved review history** on a snapshot to inspect the revisions captured at finalization. Every included statement has exact evidence links; a replaced image cannot be revealed as the saved citation. Later edits or exclusions leave the old snapshot inspectable as history and block a new portable package from it.

Download **Frozen Markdown**, **Frozen JSON**, or **Shared evidence package** from the same saved snapshot. Markdown links target the running local workspace. JSON can be reopened in that original workspace; it verifies the exact saved content and never imports decisions. The portable ZIP includes the selected images through the existing shared `EvidenceBundle` and package validator and can be imported in a separate Traced workspace. Specialist Reviewed/Rejected statuses remain provenance; the package creates no shared Use/Qualify/Withhold decisions.

New packages expose **Saved specialist history and context · read only** in the integrated recipient's Review screen. This separate disclosure opens retained revisions, actors, lineage and source-change history without adding supporting evidence or changing the selected source. For newly finalized briefs, the media recovery action **Inspect saved metadata** opens the exact CSV record linked to the cited frame. Earlier packages keep their saved record and link identities.

The bounded selection supports 12 notes, 12 unique frames, 20 MiB per image, 60 MiB selected media, and 2 MiB snapshot JSON. Review history is capped at 48 revisions; scenario lineage and exclusion history are also bounded, with total/truncated indicators. This is a selected assessment package, not a full-archive transfer. The editable sibling `../evidence-explorer` supplies the shared contracts and package code; no shared app, worker or model is initialized by this workbench.

## Import and checksums

Metadata import and file checksums are separate operations:

```sh
uv run --locked silent-storm import
uv run --locked silent-storm stats
uv run --locked silent-storm hash-media --limit 100
```

Run `hash-media` without `--limit` to resume all remaining media checks. Checksums establish local file identity, not authenticity. Hash coverage is explicit; unknown hashes do not imply unique or reused footage. Repeating an unchanged import is harmless; a changed metadata release needs a new release ID.

For installed wheels, set `SILENT_STORM_PROJECT` to an explicit workspace, or supply both database and dataset paths; checkout-relative defaults are not a storage location for an installed package. See the [development and release guide](../docs/PYTHON_DEVELOPMENT.md).

The default database is `data/silent-storm.sqlite3`, and the default source is `dataset/extracted/day-1/Day 1`. Override paths using `--db`, `--dataset`, or the `SILENT_STORM_DB` and `SILENT_STORM_DATASET` environment variables. Use `serve --port` to choose another local port, or `serve --no-import` to open an empty workbench without loading a release. Media routes resolve a frame ID through the database and check its path against the release root on each request.

```sh
make -C .. test-silent-storm
make -C .. lint
```

The [synthetic shared bundle](fixtures/one_way_review_bundle.json) shows the text-only handoff format.
