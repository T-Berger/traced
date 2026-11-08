# Mamdani evidence workspace

Build a local, read-only preview of the saved Mamdani election case. The combined page brings the mission briefing, review scenarios, relationship tree, topic clusters, timeline, and evidence index together. The plots work offline once generated.

## Build the preview

Use the existing Evidence Explorer environment for Plotly and the Traced brand assets in this repository. The renderer expects the saved artifacts from the Mamdani demo in their original layout.

```text
mamdani-demo/
  collector-11e5afc822bd4ce15ac5/
    mamdani-election-demo.zip
    dashboard-data.json
    overview-flow.json
    pattern-review.json
    notepad-platform-leads.json         (optional, generated from the pinned snapshot)
  maltego-enrichment/
    archive-cross-check/
    live-archive/
    live-describe-vetric/
    live-vetric-ballot/
    live-vetric-diwali/
    live-vetric-election-quote/          (optional)
    live-vetric-notepad-parent/          (optional)
    live-web-search/
    public-cross-check/
    collector-trace-supplement.json     (optional)
    vetric-source-import.json
    vetric-source-import.manifest.json
```

Keep the artifact directory outside the Git checkout. Case packages, collector databases, research notes, capture receipts, sidecars, and generated pages are local files. The graph builder checks input hashes and binds its result to the saved package.

Run these commands from the repository root with `demo` set to the absolute path of the saved artifact directory.

```sh
demo=/absolute/path/to/mamdani-demo
case_dir="$demo/collector-11e5afc822bd4ce15ac5"

uv run --locked --package evidence-explorer python examples/mamdani-trace/build_trace_graph.py --root "$demo"
uv run --locked --package evidence-explorer python examples/mamdani-trace/render_preview.py \
  "$case_dir/mamdani-election-demo.zip" --out "$case_dir/preview.html" --trace-alias
```

Open `preview.html` beside the ZIP. The optional `--trace-alias` flag also writes the combined page as `trace-explorer.html` to keep existing bookmarks, including `#clusters`, working.

## Follow the evidence

- Start with **Verify**, **Trace**, or **Compare** to open a guided scenario. The ballot scenario compares a claim with the saved election records. The rhetoric scenario checks who or what the wording targets. **Check input vs output** opens the saved excerpts beside the draft wording.
- The sentiment arrows compare the share of posts containing direct criticism before and from November 4. The pooled sample and the rank-spaced subset move in opposite directions. Select a segment of either window’s sentiment bar to open the exact posts behind it.
- In **Topic clusters**, expand a topic to read excerpts and each post’s role, such as stating a claim, quoting, or reporting. The labels describe what the saved wording does. A post is indented only when the collector records its parent. A post in the same conversation without a known direct parent stays at the same level. Select **Open saved wording** to inspect the cited capture.
- Select a saved post in the **Evidence workspace** to update the relationship tree, related clusters, timeline highlights, and source inspector together. The timeline counts saved posts by New York publication day and gives archive captures a separate track. The optional Election Day receipt adds the Forbes original to **Election Day quote**.

## Add optional evidence from the pinned collector snapshot

If you have the pinned SQLite snapshot, you can generate the Small Business Saturday supplement and the research-lead sidecar locally.

```sh
snapshot=/absolute/path/to/disinfo-20261107.sqlite

uv run --locked --package evidence-explorer python examples/mamdani-trace/build_trace_graph.py \
  --root "$demo" --extract-collector-supplement "$snapshot"
uv run --locked --package evidence-explorer python examples/mamdani-trace/notepad_platform_leads.py \
  "$snapshot" --out "$case_dir/notepad-platform-leads.json"
```

Both extractors verify the pinned snapshot hash and read the database in immutable mode. Run the preview renderer again after generating the sidecar. The optional notepad parent receipt adds a recovered quoted source to the research leads.

The saved sample has historical coverage gaps. Topic and criticism counts describe the selected posts. Archive pointers carry the observed replay status, and engagement counts reflect the capture time. The preview performs no provider calls and records no review decisions.
