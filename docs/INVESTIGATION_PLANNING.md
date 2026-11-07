# Save an investigation plan and confirm scope

On **Cases**, select **Start investigation** to open the existing **Investigation plan** editor. Opening it writes nothing and preserves your working draft. The plan stores your question, primary purpose, public topics, source references, reviewer role, intended output, and any known baseline or observation windows. **Explore workflows** is optional guidance; choosing an example there does not change the saved plan's purpose or confirm its scope.

Follow **Plan investigation → Gather evidence → Confirm scope → Investigate findings → Review conclusions → Prepare and share briefing**. You can save an empty or partial plan before a case exists. Saving it creates no evidence case, collection job, model job, or finding decision. **Investigation plan** names this input; **Briefing** names the reviewed output.

1. Open the editor with **Start investigation**. To resume existing work, choose a plan under **Continue a saved plan** and select **Open saved plan**. The latest version is selected by default; earlier versions are under **Plan versions and imported history**. Changing either selection alone keeps your working fields unchanged. **Start another plan** is the secondary action for beginning a separate plan.
2. Choose the primary purpose, edit the fields, and select **Save plan**. This saves a new immutable revision. If another editor saved first, the save is rejected and your unsaved fields remain available. Open the newer version separately, or expand **Plan versions and imported history** and select **Copy fields into a separate plan**.
3. Follow **Gather evidence**, collect or import evidence, and save the intended case. After that case is selected, choose **Review gathered evidence for your plan**. Check the displayed plan question and case name, then select **Use this case in saved plan**. This opens the exact saved revision and copies the case's frozen source references into working fields only. Review them and select **Save plan** before attaching evidence. Collection cases use their frozen selected-source and observation manifest, including failed or empty sources. Other cases explicitly use the whole supplied evidence manifest.
4. Select **Attach collected evidence**. This creates a new revision associated with that exact saved case and evidence scope; it does not fetch material or alter evidence. The same action can attach an imported case. One plan's lineage belongs to one case; use a new draft to plan a separate case.
5. Select **Confirm scope** after checking the saved fields. Confirmation requires the purpose, public topics, exact source selection, reviewer role, and intended output. Purposes requiring a baseline also need complete, ordered baseline and observation windows with explicit UTC offsets. Unknown windows remain valid draft information.

The continuation remembers only a local unbound plan you explicitly saved or opened in this browser session. It does not choose an arbitrary plan or attach evidence automatically. A loaded or edited plan for the selected case takes priority. You can always choose a plan under **Continue a saved plan**, select **Open saved plan**, then **Use this case's sources in the plan**. If the remembered revision has changed, open the current version explicitly. Saving a new plan while an older case is selected normally leads to **Gather evidence**; the existing **Use this case's sources in the plan** action remains available when that case is the evidence you intend to use.

The compact **Plan → Evidence → Scope → Findings** guide describes saved state; a saved case is not proof of complete collection, and confirmed scope is not approval of a finding. After confirmation, **Open first finding** opens the first finding awaiting a decision. Collection-count findings use **Inspect collected material**; cases without findings retain evidence inspection. When all findings have current decisions, the next action opens the briefing preview.

A new revision needs its own confirmation. New evidence or changed saved source observations make an earlier scope require review. Adding assessment scenarios or decision history alone does not change the saved evidence scope. Scope confirmation completes only the purpose-and-scope process step; collection coverage, exact evidence checks, and human finding decisions remain separate.

## Optional activity rule

Expand the activity-rule controls and explicitly enable a rule to describe a change worth reviewing. Enter the metric, percentage threshold, direction, comparison basis, minimum baseline and current counts, and minimum coverage percentage. These fields start blank; the workshop's illustrative threshold is not a default. Count comparison needs equal-length windows; exposure-normalized comparison needs suitable saved exposure information.

Save the revision and reconfirm its scope after changing a rule. Invalid entries stay in your working draft so you can correct them. Disabling the rule and saving explicitly removes it from the new revision; earlier revisions retain their original rule. Unchanged imported rules keep their original identity, title, and threshold provenance. An edited rule is recorded as analyst-configured, which does not establish that its threshold is validated or that a signal is factual.

Choosing or saving a rule starts no evaluation, collection, or notification. Expand manual activity comparison only when it helps the investigation; it separately checks the saved evidence, windows, coverage, and exposure before it can report an available result. Missing saved coverage cannot be repaired by confirming the plan. See [the compatible-input and recovery guidance](ACTIVITY_REVIEW.md).

## Portable history

**Download case package** includes relevant immutable planning revisions and recorded confirmations in a typed, hashed sidecar. The evidence bundle and its existing fingerprint format stay unchanged. Evidence-only JSON deliberately omits planning history and media. Original version 1 case packages remain readable; readers that cannot preserve planning history reject version 2 rather than silently discarding it.

After importing a package, its recorded confirmations are origin history. Choose the imported plan under **Continue a saved plan**, expand **Plan versions and imported history**, choose the required version, and select **Open saved plan**. Select **Adopt selected version for this case** to create a new local revision. Review it and select **Confirm scope** locally. Importing alone cannot complete the local scope step or approve findings.

The handoff preview and downloads refer to one captured evidence-and-planning snapshot. Changed saved context requires refreshing the preview. Unsaved planning edits must be resolved before downloading a saved-state handoff. Downloading records neither recipient acceptance nor report submission.

## Storage and integration

Planning uses the existing SQLite workspace and connection lifecycle under the separate `monitoring_briefs` migration component, starting at version 1. It does not consume Explorer, review, or collection schema numbers. Typed draft models are separate from the existing complete `MonitoringBrief` contract. Revisions, bindings, and confirmation history are immutable; local head and active-plan pointers are not imported as authority.

Case import and planning import share one guarded database transaction. Artifact installation validates the complete package and removes files newly created by a failed import. Complete history export has explicit count and byte limits and fails visibly instead of emitting a truncated lineage. Catalog queries filter by case before pagination.

The user-facing labels preserve the existing operations and internal `MonitoringBrief` contract: **Save plan** saves a revision, **Attach collected evidence** binds that revision to the selected saved case, and **Confirm scope** records local confirmation after validation. These are separate explicit actions.

Planning does not evaluate alert rules or start a monitor. Scheduled collection is configured separately under **Gather evidence → Live sources** and refreshes source material. Manual activity comparison evaluates compatible saved windows on request; no live alert service or notifications are activated.
