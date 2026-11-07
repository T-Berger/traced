# Domain and persistence contracts

`models.py` contains validated Pydantic records for source channels/posts/messages,
datasets, cases, pair events, pairs, analysis runs, reviews, page metadata, and case
responses. They validate types, identifiers, timezone presence, parent references,
chronology, configuration, and narrative evidence requirements. Repository methods
return these models; service methods serialize them for the existing dashboard,
CLI, importer, and export contracts. The detector uses validated frozen dataclasses
for first-arrival slots and account assignments. Algorithm accumulators and opaque
extension metadata still use maps where the keys are inherently dynamic.

Records reject attribute reassignment and use tuples for domain collections.
Extension JSON metadata remains editable in a returned instance; each read creates
fresh records, and changing them does not write to SQLite. Extra wire fields and
field presence are preserved so upgrades do not silently rewrite source provenance.

## Repository boundaries

`Store` is a compatibility facade. Its focused components are:

| Component | Responsibility |
| --- | --- |
| `persistence/database.py` | Connections, transactions, default paths, and SQLite backups. |
| `persistence/migrations.py` | Schema versions and transactional upgrades. |
| `persistence/projections.py` | Populate normalized read tables inside source/run write transactions. |
| `persistence/datasets.py` | Immutable sources, metadata, source counts, explicit materialization. |
| `persistence/runs.py` | Immutable runs, lightweight identity, explicit materialization. |
| `persistence/cases.py` | Case filters, source/account/channel pages, evidence-reference validation. |
| `persistence/evidence.py` | Pair/event pages, exact aggregates, shared content, literal FTS search. |
| `persistence/readiness.py` | Coverage and provenance facts computed in SQL. |
| `persistence/reviews.py` | Append-only review history and revision comparisons. |

Source and run writes update legacy provenance payloads and normalized projections
atomically. Importing another source invalidates its predecessor's active run;
a late analysis completion is retained but cannot activate on a different source.
Pinned historical investigations remain addressable.

## Schema upgrades

The supported SQLite `user_version` is 2. Version 1 identifies the original tables;
version 2 adds normalized source/run metadata, channels, posts, case memberships,
pairs/events, and content indexes. Original unversioned databases are recognized as
version 0 and upgraded through both versions.

An upgrade runs under `BEGIN IMMEDIATE`. Schema changes, normalized backfill,
migration history, and version changes commit together. Failure rolls everything
back, including DDL. Concurrent openers serialize the upgrade. Future schema
versions fail before schema changes. Existing source/run payloads, FTS rows,
active dataset identity, and append-only review revision IDs remain intact.
An old active-run pointer from a different source is ignored by readiness reads;
the historical run remains available through an explicit run ID.

Before upgrading a working database, use a consistent backup and schedule the first
open as maintenance. Legacy backfill materializes one existing dataset/run payload
at a time and can require substantial memory and write time. This is not a
streaming migration of arbitrary-size legacy blobs. Application tests perform all
migration and recovery checks against temporary databases.

## Bounded interactive reads

`overview`, `readiness`, `case_page`/`list_cases`, `case_detail`, review validation,
and search use normalized rows or SQL aggregates. They never decode the complete
`datasets.payload` or `runs.payload` blobs. Tests enforce that restriction with the
SQLite authorizer, rather than relying only on mocked method calls.

The default page size is 200; valid sizes are 1–500. `case_detail` returns separate
bounded pages for messages, surrounding comments, posts, accounts, channels, pairs,
and a **single global event page across the selected pair page**. Each collection
includes `total`, `limit`, `offset`, `returned`, and `has_more` in `pagination`.
It returns dataset/run metadata, not nested complete corpora or analysis results.
Exact concentration, sensitivity, and retained-pair totals describe the full case,
not the currently displayed page.

`case_page` exposes queue totals; `list_cases` retains its list return type and now
returns one bounded page. `pair_events` provides separate event paging for a chosen
pair. Shared-content presentation is capped at 20 URL and 20 text groups, 200
message references per group, and 500 account keys per URL group; exact counts and
truncation flags accompany those observations.

A regression with unchanged case evidence measured `case_detail` peak traced Python
allocations at approximately 860 KB for a 1,137-message corpus and approximately
861 KB for a 21,137-message corpus. This measures Python allocations for one read;
it excludes SQLite's native page cache, pre-existing objects, and file-system caches.
Page sizes bound row counts, not arbitrary source-text lengths or case membership
arrays. Large individual records, metadata, or cases still need their own budgets.
SQL aggregation can scan many rows even though Python does not load them all.

## Explicit complete operations and revision pinning

`run_analysis` and `snapshot` deliberately materialize a complete corpus/result.
The normalized JSONL loader still assembles an import in memory. Complete-case
export accumulates **selected case** pages and all selected pair events to preserve
evidence references; it does not load unrelated source comments. The strongest-
episode rerun checks the corpus count before materialization and declines datasets
above 10,000 comments. None of these paths establishes arbitrary large-data capacity.
The retained legacy provenance blobs and normalized projections also add disk cost.

Public investigation operations accept an optional `run_id` so a newer analysis
cannot redirect an opened review. `save_review(expected_revision=0)` means no prior
review; a positive value must equal the current saved revision. The comparison and
append occur in one write transaction. Omitting it retains the older append behavior.

`get_review`, `case_detail`, `brief_preview`, and `export_case` accept a revision pin.
Revision 0 explicitly means unreviewed. A positive revision must belong to that case
and run. Preview returns its content, run/case IDs, revision, and synthetic flag;
exporting with the same pins preserves the shown saved assessment even if another
reviewer saves a newer revision. Unsaved form drafts are owned by the UI.
