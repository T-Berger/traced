# Mamdani evidence workspace

Build a read-only companion for the saved Mamdani election case. The tree, cluster map and timeline use the collector snapshot, the native case package and separately saved Maltego and X capture receipts. The plots work offline once generated.

Choose a saved post to update the relationship tree, related clusters, timeline highlights and source inspector together. The timeline counts saved collector posts by New York publication day and gives archive captures a separate track. The evidence index resolves exact excerpts and preserves the selected trace when opening a record.

The renderer uses the existing Evidence Explorer environment for Plotly and the Traced brand assets in this repository. The example expects the saved artifact directory from the Mamdani demo, with its original relative layout:

```text
mamdani-demo/
  collector-11e5afc822bd4ce15ac5/
    mamdani-election-demo.zip
    dashboard-data.json
    overview-flow.json
    pattern-review.json
  maltego-enrichment/
    archive-cross-check/
    live-archive/
    live-describe-vetric/
    live-vetric-ballot/
    live-vetric-diwali/
    live-vetric-election-quote/         (optional)
    live-web-search/
    public-cross-check/
    collector-trace-supplement.json    (optional)
    vetric-source-import.json
    vetric-source-import.manifest.json
```

Keep this artifact directory outside the Git checkout. The original receipts, response bodies, saved case and generated pages are local inputs and outputs. The graph builder checks input hashes and binds its result to the saved package.

From the repository root, set `demo` to the absolute path of the saved artifact directory and run:

```sh
demo=/absolute/path/to/mamdani-demo
case_dir="$demo/collector-11e5afc822bd4ce15ac5"

uv run --locked --package evidence-explorer python examples/mamdani-trace/build_trace_graph.py --root "$demo"
uv run --locked --package evidence-explorer python examples/mamdani-trace/trace_explorer.py "$case_dir"
uv run --locked --package evidence-explorer python examples/mamdani-trace/render_preview.py "$case_dir/mamdani-election-demo.zip"
```

Open `preview.html` beside the ZIP and choose **Open evidence workspace** from the case overview. Select **Election Day quote** and expand **Read linked source · Forbes original** to compare the saved wording. Select **Timeline → Exact dated records** to see where the quote and original fall around November 4. The Forbes original requires the optional Election Day receipt.

The optional collector supplement provides the Small Business Saturday trace. When you have the pinned SQLite snapshot, pass `--extract-collector-supplement /path/to/disinfo-20261107.sqlite` to the graph builder along with `--root` to generate the bounded supplement. The builder verifies the snapshot hash and reads it in immutable mode.

The case sample has historical coverage gaps. Archive pointers carry the observed replay status, and capture-time metrics remain snapshots. The companion performs no provider calls and records no review decisions.
