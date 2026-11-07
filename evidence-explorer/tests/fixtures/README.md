# Portable regression fixtures

The five files in `case_packages/` are frozen inputs for mandatory offline tests.
They were copied byte-for-byte on 2026-11-07 from the previously generated files
listed below. The originals were neither changed nor moved. Tests check the
original SHA-256 values; no regeneration, source collection, model execution, or
access to an operational workspace is required.

## Origins and limits

| Fixture | Original path relative to `evidence-explorer/` | Provenance and purpose |
| --- | --- | --- |
| `live-job.json` | `artifacts/claim-check-verification/live-job.json` | Saved completed claim-check job with public NASA Science, ESA/Webb, and ESA astronomy excerpts retrieved on 2026-11-06. Includes bounded search results, the source claim, and a saved model proposal. Used as the exact reference for the NASA record's text, hash, timestamp, and metadata. |
| `live-reviewed-case.zip` | `artifacts/claim-check-verification/live-reviewed-case.zip` | Four real-source article records, exact fragments, one saved model assessment, and two attributed verification reviews concerning the possible TWA 7 planet. The case remains `synthetic=false`; the recorded partial coverage and uncertainty remain unchanged. |
| `mixed-reviewed-reference.zip` | `artifacts/rel-07/fixtures/mixed-reviewed-reference.zip` | Synthetic browser-workflow case containing the unchanged real NASA article, one explicitly synthetic text record, and one fabricated text record whose omitted origin metadata deliberately tests the unknown-origin label. One saved verification review. The case label does not change individual record provenance. |
| `fictional-core-demo.zip` | `artifacts/valid01-demo-fixture/fictional-core-demo.zip` | Fictional Swarm discussions and separate harmless supplied-media assessments. The only media are three SVG geometric cards, including an exact duplicate. There are no real accounts, footage, or places. It starts with no reviews or scenarios. |
| `evidence-trail-reviewed-swarm.zip` | `outputs/evidence-trail-reviewed-swarm.zip` | Synthetic Swarm baseline and saved replay with fictional accounts/discussions and a demonstration review. Exercises exact saved comparison, review aliases, and reopening without recomputing the detector. |

The astronomy excerpts are real collected public-source material, not fabricated
test text. Their source URLs, public author attribution, retrieval dates, coverage
limits, and saved reviews are intentionally retained. Collection and exact
quotation do not establish scientific truth or independent corroboration. Saved
model proposals remain proposals; saved verification or demonstration reviews do
not constitute new human validation of the product or evidence.

Before promotion, the JSON contents, archive member lists, provenance fields,
source URLs, and SVG source were inspected. No credentials, tokens, cookies,
private email addresses, local absolute paths, private runtime data, or real
graphic media were found. The ZIPs contain JSON only except for the three
explicitly synthetic SVG cards. No sanitization or replacement was performed.

## Exact file identities

| Fixture | Bytes | SHA-256 |
| --- | ---: | --- |
| `live-job.json` | 79927 | `306cd0e8cef63ae74f4707aed2145fe66e3254127ba87b42731ff87278dac39e` |
| `live-reviewed-case.zip` | 13052 | `caa5908785d36a7022a71ca12ee3947ca40de89c050704eab70384c9c1200bb7` |
| `mixed-reviewed-reference.zip` | 5372 | `5305dc295f7087e6f5d95b032582717499da2b8ebc9d3a62aac62a2ca40e2c8e` |
| `fictional-core-demo.zip` | 265161 | `c1d742a78db0b5043b79be6a3a878e1406569b7ad45d46513876791daf28fd7c` |
| `evidence-trail-reviewed-swarm.zip` | 304901 | `db29d709848bfbc74e83b6a79dd99dd795e3532fccc23eed005b58bcb0379268` |

Keep these archived bytes immutable. Any deliberate replacement needs a separately
documented provenance decision and explicit test hash update; a newly generated
archive is not the same historical fixture.
