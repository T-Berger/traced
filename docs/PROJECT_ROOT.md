# Common project root

Open `EDTH/` as the project and run `./traced start`. This root command uses the
existing root uv workspace, `.venv`, and locked dependencies for all three Python
packages. It dispatches to the existing integrated Evidence Trail application;
evidence services, specialist packages, and the combined worker retain their
existing responsibilities. No database, dataset, package, or repository is moved.

## Commands

```sh
./traced config                   # Report paths; do not open a database
./traced start                    # Shared desk and one combined worker on port 8053
./traced start --port 8055         # Choose another port
./traced start --no-worker        # Desk only; do not start another worker
./traced review --help            # Existing review CLI
./traced review cases             # Open the selected store and list cases
```

`./traced` with no command means `start`. Startup uses the existing uv version and
Python constraints; it synchronizes the complete root environment with the root
lock, without updating dependency versions. It does not seed demonstration data.
The entry point resolves its own location, so invoking `/path/to/EDTH/traced`
works from another directory. Import/export file arguments resolve from `EDTH`.

Stop an older desk with `Ctrl+C` in its terminal before replacing it on the same
port. Running services retain the code loaded at startup. Starting on another
port does not refresh the old process. Keep one combined worker for the selected
workspace; `--no-worker` is available for an additional desk when appropriate.

## Configuration and saved data

Run `./traced config` before switching entry points to inspect the exact selected
database. It reports only paths and the source of the data-directory selection;
it neither opens nor initializes the store and does not print provider secrets.

If desired, copy the root `.env.example` to root `.env`, then uncomment only the
settings you want to override. The example leaves everything commented so copying
it alone does not replace existing Explorer settings. Settings precedence is:

1. Process environment.
2. Root `EDTH/.env`.
3. Legacy `EDTH/evidence-explorer/.env` for remaining settings.
4. Existing application defaults.

The explicit root `--data-dir` option overrides all data-directory settings.
Data path resolution preserves existing process and Explorer settings:

| Data-directory selection | Relative-path base |
| --- | --- |
| `./traced --data-dir PATH ...` | `EDTH/` |
| Process `EVIDENCE_DATA_DIR` | `EDTH/evidence-explorer/` (legacy compatibility) |
| Root `.env` `EVIDENCE_DATA_DIR` | `EDTH/` |
| Explorer `.env` `EVIDENCE_DATA_DIR` | `EDTH/evidence-explorer/` |
| No selection | `EDTH/evidence-explorer/data/` |

Absolute paths are used as supplied, and `~` expands to the user's home directory.
Empty selections fail instead of silently opening a different database. The
resolved path is passed to the existing application and worker as one absolute
`EVIDENCE_DATA_DIR`. `PYTHON_DOTENV_DISABLED=1` disables both configuration files;
subsequent application imports never discover additional parent/caller `.env` files.

For the earlier Trail store, select it explicitly without moving it:

```sh
./traced --data-dir ./evidence-explorer/data/evidence-trail config
./traced --data-dir ./evidence-explorer/data/evidence-trail start
```

Root `start` uses `EVIDENCE_HOST` (default `127.0.0.1`) and `TRACED_PORT` (default
`8053`), with `--host` and `--port` overriding those settings. Explorer's separate
`EVIDENCE_PORT` remains its own dashboard setting. Model and provider settings use
their existing names, documented in `evidence-explorer/.env.example`.

The older `evidence-explorer/scripts/start-trail.sh` remains a compatibility entry
point with its existing member configuration and dedicated environment. Use the
root command when selecting root `.env` configuration. Specialist databases and
media datasets keep their separate paths; selecting the common workbench does
not merge specialist stores. Standalone containers retain their existing startup
contracts.

## Development and ownership

Use `make setup`, `make check`, and the focused targets in
[Python development](PYTHON_DEVELOPMENT.md) from `EDTH`. From another directory,
use `make -C /path/to/EDTH TARGET`.
