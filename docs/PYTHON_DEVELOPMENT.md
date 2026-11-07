# Python development and releases

The repository is one uv workspace containing three separately installable `src/`
packages: `evidence-explorer`, `swarm-detector`, and `silent-storm` (in
`operation-silent-storm/`). Root `pyproject.toml` owns workspace membership and common
Ruff policy; each member owns its package metadata, dependencies, CLI, and tests.
Root `uv.lock` is the only dependency lock. Local package references use uv workspace
sources; published metadata uses ordinary package dependencies.

## Set up and check

Install uv **0.11.3**, the tested version declared by `tool.uv.required-version`,
using the [official installation instructions](https://docs.astral.sh/uv/getting-started/installation/).
`.python-version` selects Python 3.12, the intersection of the three packages' support.
Explorer's own Python 3.13 metadata is retained; this workspace's CI tests Python 3.12
on Linux. Install **Node.js 22**, declared in `.node-version`, for Explorer's
client-side callback regression tests. These tests execute the actual JavaScript
with `node`; no npm dependencies are required. Python application startup does
not require Node. From the repository root:

```sh
uv python install
make setup
make check
```

`make setup` performs one locked sync with all packages, extras, and development
tools into root `.venv`. Existing member `.venv` directories are left in place.
`make check` checks lock freshness and shared identity, runs Ruff lint/format checks,
the three member suites and workspace-tool tests, starts the real `./traced`
launcher against isolated temporary stores, then builds all six distributions,
then verifies their contents, rebuilds source distributions outside the workspace,
and checks clean wheel installation. Package suites run from their
own directory to preserve fixture paths and pytest settings. Tests use isolated
stores and mocked providers; they do not run a live model evaluation. Mandatory
saved-package regressions use hash-pinned `tests/fixtures` copies with documented
provenance. Silent Storm's additional local real-package check skips when its
optional generated `artifacts/selected-case.zip` is absent; synthetic recipient
checks always run.

The explicit browser gate needs the existing locked Explorer `browser` dependency
group and its matching Chromium binary. These are test tools, not application
runtime dependencies. Install them deliberately, then run the combined gate:

```sh
make browser-setup
make check-all
```

On Linux CI, `make browser-setup BROWSER_INSTALL_FLAGS=--with-deps` also installs
Chromium's system dependencies. Repeat setup after a Playwright lock update.
`make check` does not install or launch a browser. `make browser-check` starts the
real root launcher without a worker, imports the committed fictional reviewed
case into temporary storage, and checks real tabs and citation navigation while
holding genuine assessment responses. It verifies the selected tab and exactly
one visible page, then checks that saved evidence stayed unchanged. The report,
trace, request order, and any failure screenshot/DOM are saved under `dist/`.
No cached npm module or machine-specific browser path is used.

Explorer currently requires Dash below 4.2, and the workspace locks Dash 4.1.0.
With identical application code and saved evidence, Dash 4.5.0 stalled assessment
navigation in renderer loading/layout selectors; the isolated 4.1.0 comparison
completed the same transition. Keep this compatibility bound until a candidate
passes `make check-all` and the saved-case export/reopen journey.

| Command | Purpose |
| --- | --- |
| `make lint` | Ruff lint and format checks on maintained code |
| `make format` | Sort imports and format that same scope |
| `make test-explorer` | Explorer, shared review, and integration tests with Swarm installed |
| `make test-swarm` | Swarm specialist tests |
| `make test-silent-storm` | Historical-media specialist tests |
| `make test-tools` | Workspace tooling regression tests |
| `make runtime-check` | Real root startup, worker lifecycle, HTTP/assets, and CLI handoff in temporary stores |
| `make browser-setup` | Install locked test-only Playwright and its matching Chromium binary |
| `make browser-check` | Real browser navigation with delayed responses and unchanged saved evidence |
| `make check-all` | Run `make check` and the browser gate; browser setup must already be complete |
| `make locks` | Check the root lock without updating it |
| `make build` | Build all three wheels and source distributions into `dist/` |
| `make package-check` | Validate existing artifacts and install them in a disposable environment |

Ruff configuration is inherited explicitly by each member. Explorer retains its
stricter `E` rules. The maintained scope is member `src/` and `tests/`, Swarm's
deployment scripts/configuration, and root `tools/`. Archived QA scripts under
`artifacts/` and `docs/review-evidence/`, research datasets, and generated files are
outside that scope. Historical verification evidence remains unchanged.

For a focused test, keep the member directory and shared dependency selection:

```sh
uv --directory evidence-explorer run --locked --all-packages --all-extras --group dev pytest tests/integrations/test_trail_launcher.py
```

## Change dependencies

Use the member's distribution name, then review both its manifest and the root lock:

```sh
uv add --package evidence-explorer 'some-dependency>=1,<2'
uv lock --upgrade-package some-dependency
make setup
make check
```

The commands above illustrate a deliberate dependency change. Ordinary runs use
`--locked` and fail on manifest/lock drift. Do not regenerate member lockfiles or
run successive exact member-only syncs into root `.venv`; use `make setup` for the
complete development environment. The initial consolidated lock preserves prior
versions except Swarm's Gunicorn 26.2.0, which converges to Explorer-compatible
25.3.0 because Explorer's existing hosting extra requires `<26`.

## Run applications and containers

Use `./traced start` from `EDTH` for the integrated desk, with root `.venv` and
one combined worker. `./traced config` reports the selected store without opening
it; root `.env` and the Explorer fallback are explained in
[the common-root guide](PROJECT_ROOT.md). The compatibility launcher
`evidence-explorer/scripts/start-trail.sh` keeps the existing absolute
`evidence-explorer/.venv-trail` environment and its member configuration.
The standalone `run_demo.sh` uses `evidence-explorer/.venv-demo`. These launchers use
the shared lock without pruning root development dependencies. Relative
`UV_PROJECT_ENVIRONMENT` values resolve from the workspace root; use absolute paths
when selecting an application-specific environment manually. Keep the same selected
data directory when reopening existing work. No database is moved by setup.

Build Docker images from the repository root, where the shared lock lives:

```sh
docker build -f evidence-explorer/Dockerfile -t evidence-explorer .
docker build -f swarm-detector/Dockerfile -t swarm-detector:local .
```

Swarm's Compose file selects that root build context. The root `.dockerignore`
allowlist excludes operational data and secrets. These remain standalone Explorer
and Swarm images; the integrated desk uses its launcher. Source packages also use
explicit include lists, excluding local datasets, databases, and captured evidence.

## CI and release packages

[CI](../.github/workflows/ci.yml) runs on branch pushes, pull requests, and manual
dispatch. It checks the lock, identity, Ruff, and packaging tools; runs each member's
test suite in a matrix; checks the real root launcher and browser navigation; then
builds and clean-installs the distributions. Browser tooling is installed only in
the runtime job, whose failure blocks distribution and release publication. Actions
are pinned by commit. Python and uv versions come from the root configuration.

[Release delivery](../.github/workflows/release.yml) runs when a `v*` tag is pushed.
For a coordinated release, set all three member versions to the same version,
refresh `uv.lock`, complete browser setup, run `make check-all`, and tag that commit `v<version>` (currently
`v0.1.0`). CI rejects a tag that differs from any member version. Publication waits
for checks on the exact tagged commit and downloads their immutable artifact ID.
It verifies the commit and SHA-256 manifest, uploads the three wheels and three
source distributions plus `SHA256SUMS` and `BUILD_COMMIT` to a draft GitHub Release,
then publishes it. It does not rebuild the checked artifacts.

Only the publication job has repository write permission, and it does not execute
project source. The workflow uses GitHub's built-in token. Existing releases/assets
are never overwritten. An upload failure can leave a draft: inspect it and recover
deliberately before rerunning. Dependabot checks root uv dependencies and GitHub
Actions weekly.

This config delivers GitHub Release packages; it does not publish to PyPI or deploy
a live service. It becomes active when this root is committed to a GitHub repository.
Local verification is distinct from a successful GitHub Actions run or a published
release.

To install downloaded application packages, place all three wheels together and install them
together in a Python 3.12 environment (dependencies still require an index/cache):

```sh
uv venv --python 3.12
uv pip install --python .venv/bin/python ./evidence_explorer-*.whl ./swarm_detector-*.whl ./silent_storm-*.whl
```

Installed Silent Storm should use an explicit `SILENT_STORM_PROJECT`, or explicit
`--db` and `--dataset` paths; checkout-relative defaults are not an installed storage
location. Native media datasets are separate inputs, never release assets. CLI help,
safe imports, and packaged resource checks do not establish live application or
source-collection readiness.
