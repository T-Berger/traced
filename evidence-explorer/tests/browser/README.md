# Browser regression for handoff remounts

This optional test uses Node.js 22, Playwright with Chromium installed, and a
new fictional review workspace. Use a host that supports Chromium's native
sandbox. Point `TRACED_PLAYWRIGHT_MODULE` at the installed Playwright module
if it is outside Node's normal lookup paths.

From the repository root, run the setup and start the worker-off desk in
one terminal:

```sh
make setup
fixture_root="$(mktemp -d)"
printf '%s\n' "$fixture_root"
mkdir "$fixture_root/report"
uv run --locked --all-packages --all-extras --group dev python \
  evidence-explorer/tests/browser/prepare_workspace.py --data-dir "$fixture_root/workspace"
./traced --data-dir "$fixture_root/workspace" start --no-worker --port 38817
```

In a second terminal, set `fixture_root` to the printed directory and run:

```sh
node evidence-explorer/tests/browser/test_handoff_remount.mjs \
  http://127.0.0.1:38817/ evidence-explorer/tests/browser/fixture-manifest.json \
  "$fixture_root/report"
```

The report records the unclicked control remount and the exact selected comment.
Stop the fixture server when finished. The default Python suite does not start a
browser.
