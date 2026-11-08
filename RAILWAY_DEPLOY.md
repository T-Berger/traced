# Deploy the Traced workbench on Railway

One Railway service runs the whole workbench, the same server as `./traced start`:

| Path | Surface |
| --- | --- |
| `/` | Review desk (cases, evidence, briefing) and the workspace worker |
| `/swarm/` | Swarm Detector |
| `/silent-storm/` | Silent Storm |
| `/health` | Health check used by Railway |

Railway builds the root `Dockerfile`, as `railway.toml` tells it to. Config as code
overrides the dashboard, so a service created with the Railpack builder still
builds the Dockerfile. The image runs gunicorn with one worker process, so the
workspace keeps one worker lifecycle, on the `PORT` that Railway injects.

## Set up the service once

1. Create the service from the GitHub repository `T-Berger/traced`, branch `main`,
   with the root directory `/`.
2. In **Settings → Deploy**, leave **Custom Start Command** empty; a custom start
   command would replace the image's own command. In **Settings → Build**, leave
   **Custom Build Command** empty.
3. In **Settings → Networking**, click **Generate Domain**. The service is not
   reachable from the internet until it has a domain.
4. Optional: add a **Volume** mounted at `/app/data` to keep cases, Swarm Detector
   runs, and Silent Storm reviews across deploys. Without one, every deploy starts
   with an empty workspace. The image takes ownership of the volume at start and
   then runs as an unprivileged user.

## Variables

No variable is required. Railway injects `PORT`.

| Variable | Effect |
| --- | --- |
| `TRACED_SEED_DEMO=1` | On first start, imports the labelled synthetic demo cases into the data directory, once |
| `OPENROUTER_API_KEY`, `EVIDENCE_REVIEW_MODEL` | Enable the OpenRouter model checks |
| `TRACED_NO_WORKER=1` | Serve the dashboards without the workspace worker |
| `TRACED_THREADS` | Request threads in the single gunicorn process (default 8) |

Variables from earlier instructions (`EVIDENCE_HOST`, `EVIDENCE_DATA_DIR=/app/data`,
`TRACED_PORT`, `EVIDENCE_READ_ONLY=false`) are harmless; the image already sets the
first two.

## Check before pushing

```sh
docker build -t traced-workbench .
docker run --rm -e PORT=8053 -p 8053:8053 traced-workbench
# open http://127.0.0.1:8053/  (also /swarm/, /silent-storm/, /health)
```

## Limits

- **There is no sign-in.** Anyone with the Railway URL can read and change the
  workspace. Keep demo data only on a public domain.
- Local datasets are not in the image. Silent Storm's day-one media and the
  Telegram archive stay unavailable, and their dashboards show that state.
- Local Ollama models are not available on Railway. Model checks need the
  OpenRouter variables above.
- Railway posts no build status to GitHub; read the build and deploy logs in the
  Railway dashboard.
