# Review category evidence over time

Traced keeps model suggestions, human review and platform response separate. This
workflow describes the saved sample; it does not decide whether a platform broke
a rule or law, establish whole-platform coverage, or submit a report.

1. Save or import an evidence case and confirm its investigation plan. In Review,
   open a finding and saved source. Under **Propose categories for saved records**,
   enter the target, select 1–20 saved text records, and queue the local Ollama
   job. The shared worker must be running. All selected records belong to one
   run; failure on one record saves no partial run.
2. When the job finishes, refresh the case and choose the new assessment. Inspect
   the exact cited text and use the existing review controls to record Use,
   Qualify or Withhold for each finding. These are assessments of proposals, not
   automatic violation verdicts. A result with no candidates is not a negative
   classification.
3. On **Compare**, open **Review saved category findings over time**, choose the
   exact target and record kinds, and inspect the monthly observed counts and
   linked evidence. Counts are per saved record ID in the selected assessment;
   categories may overlap. Unknown coverage and undated records remain visible.
   Do not combine separate assessments or count sources as independent people.
   To include this selected scope in a draft dossier, open **Briefing** and select
   **Include selected category scope from Compare**. Select the separate Swarm
   option only when its saved runs should appear as unreviewed behavioral context.
   Withheld, unreviewed and excluded category details are omitted from that dossier.
4. For a manual change comparison, save and locally confirm a plan with explicit
   windows, a rule for the exact category and target, and an immutable bounded
   sample coverage declaration. The **Compare reviewed category findings in saved
   windows** panel shows the exact metric name required. Each in-window record
   needs an exact, current Use or Withhold decision; missing, conflicting or
   qualified reviews leave the result unavailable. Results count findings
   selected for Use, not verified violations. No threshold or severity hierarchy
   is supplied automatically. See [activity review](ACTIVITY_REVIEW.md).
5. On **Alerts**, an analyst may select an analytics subject and up to 20 candidates,
   explicitly choose a different saved case record for each and enter a target.
   Compare them before saving. The saved text and source URL must match, and
   captured analytics material and referenced context
   must pass the snapshot checks. **Save proposals for review** rechecks the
   selection and creates a separate assessment without copying any decision.
   Refresh the case and select that assessment to review it. The shared case
   records analytics IDs and hashes, not the external capture bytes; retain the
   original analytics evidence separately.

The Alerts database is a separate, explicitly configured source. A missing
connection or unresolved source stays unavailable. Notices, later observations,
and removal or platform-action evidence require independent saved documentation;
the current workflow does not calculate a platform response rate or latency.
Existing Swarm findings may provide related behavioral context but do not prove
automation, intent, a violation or an enforcement outcome. The merged Theory
pipeline remains unavailable for account analysis until its identity, coverage
and evidence-handoff requirements are met.

**Policies** offers the merged reference guide, starting with general guidance.
Its case examples are supplied planning material rather than verified findings.
Check the linked official sources and dates before applying a policy.

The fictional example in Compare is separate from the selected case and cannot
establish a threshold or real-world result. Local model jobs and Alerts exports
are explicit actions; no cloud model call, notification or report submission is
triggered by opening this workflow.

## Import a pinned collector snapshot

For a collector SQLite snapshot, first verify its SHA-256 and choose a new case
ID and a new, empty workspace directory. Run the importer with the selected source
case slug, expected hash, source path and data directory:

```sh
python -m evidence_explorer.compliance_review.collector_snapshot \
  --source /path/to/disinfo.sqlite --sha256 VERIFIED_SHA256 \
  --source-case CASE_SLUG --case-id NEW_CASE_ID --data-dir /path/to/new-workspace
```

The command verifies the snapshot before and after reading, copies it into the
new workspace, and preserves the original separately. It imports at most 10,000
saved items with exact source locators and derived hashes. The case package
contains saved text and selected provenance; full raw rows and media metadata
remain in the retained source snapshot. Media URLs do not imply saved media
bytes. Unknown coverage, classifications and human decisions remain unknown.
The full source snapshot is not embedded in the downloadable case package.
