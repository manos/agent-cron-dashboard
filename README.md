# Agent Cron Dashboard

A lightweight, self-hosted web dashboard for visualizing [OpenClaw](https://github.com/openclaw/openclaw) and/or [Hermes Agent](https://github.com/NousResearch/hermes-agent) cron jobs — schedules, status, run history, and output. Point it at one backend or both at once; it merges everything into a single view.

![Dashboard screenshot](docs/screenshot.png)

## Features

- **24-hour schedule timeline** — see when every job fires at a glance, with the current time highlighted
- **Job cards** — schedule (human-readable), last run status, duration, next run, error count
- **Run history panel** — click any job to see its last 50 runs with full output summaries
- **Token usage** — see input/output token counts per run (OpenClaw backend)
- **Auto-refresh** — polls every 30 seconds; progress bar shows time until next refresh
- **Dark theme** — easy on the eyes, looks great on a monitoring screen
- **Responsive** — works on mobile and desktop
- **Zero dependencies on the frontend** — pure HTML/CSS/JS, no framework required
- **Zero-config data access** — reads directly from OpenClaw's SQLite state database or Hermes's `jobs.json`/`executions.db`; no API token or CLI binary needed
- **Multi-backend** — run it against an OpenClaw install, a Hermes Agent install, or both simultaneously; jobs from each are merged into one dashboard

## Quick Start

### With Docker (recommended)

```bash
# OpenClaw only
docker run -d \
  --name agent-cron-dashboard \
  -p 3000:3000 \
  -v ~/.openclaw/cron:/data/cron:ro \
  -v ~/.openclaw/state:/data/state:ro \
  ghcr.io/manos/agent-cron-dashboard:latest

# Hermes Agent only
docker run -d \
  --name agent-cron-dashboard \
  -p 3000:3000 \
  -v ~/.hermes/cron:/data/hermes-cron:ro \
  ghcr.io/manos/agent-cron-dashboard:latest

# Both, merged into one dashboard
docker run -d \
  --name agent-cron-dashboard \
  -p 3000:3000 \
  -v ~/.openclaw/cron:/data/cron:ro \
  -v ~/.openclaw/state:/data/state:ro \
  -v ~/.hermes/cron:/data/hermes-cron:ro \
  ghcr.io/manos/agent-cron-dashboard:latest
```

Then open http://localhost:3000 in your browser.

### With Docker Compose

```bash
# Copy the example compose file
curl -O https://raw.githubusercontent.com/manos/agent-cron-dashboard/main/docker-compose.yml

# Edit to adjust volume paths for whichever backend(s) you run, then start:
docker compose up -d
```

### Without Docker

Requires Python 3.11+.

```bash
git clone https://github.com/manos/agent-cron-dashboard.git
cd agent-cron-dashboard
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Point at whichever backend(s) you run — set one or both
export OPENCLAW_CRON_DIR=~/.openclaw/cron
export OPENCLAW_STATE_DB=~/.openclaw/state/openclaw.sqlite
export HERMES_CRON_DIR=~/.hermes/cron

uvicorn server.main:app --host 0.0.0.0 --port 3000
```

Open http://localhost:3000.

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|---|---|---|
| `PORT` | `3000` | HTTP port to listen on |
| `OPENCLAW_STATE_DB` | `/data/state/openclaw.sqlite` | Path to OpenClaw's SQLite state database. **Primary OpenClaw data source.** Auto-detected from `OPENCLAW_CRON_DIR/../state/` when not set. |
| `OPENCLAW_CRON_DIR` | `/data/cron` | Path to the OpenClaw cron directory. Used as a fallback for legacy `jobs.json` and JSONL run files when SQLite is unavailable. |
| `HERMES_CRON_DIR` | `/data/hermes-cron` | Path to a Hermes Agent cron directory (`~/.hermes/cron`), containing `jobs.json` and `executions.db`. Auto-detected as absent if the path doesn't exist or doesn't look like a Hermes cron dir — safe to leave at the default if you don't run Hermes. |
| `OPENCLAW_ENABLED` | `true` | Set to `false` to hide the OpenClaw backend entirely — e.g. for a Hermes-only dashboard even if OpenClaw data is still mounted. |
| `HERMES_ENABLED` | `true` | Set to `false` to hide the Hermes backend entirely. |

Set only the variables for the backend(s) you actually run. Both can be set at once — their jobs are merged. To go Hermes-only (or OpenClaw-only) without touching mounts, just set the other backend's `*_ENABLED` to `false`.

## Volume Mounts

| Host path | Container path | Purpose |
|---|---|---|
| `~/.openclaw/state` | `/data/state` | OpenClaw SQLite state database (jobs + run history) |
| `~/.openclaw/cron` | `/data/cron` | OpenClaw legacy fallback: `jobs.json` and JSONL run files |
| `~/.hermes/cron` | `/data/hermes-cron` | Hermes Agent cron directory: `jobs.json`, `executions.db`, per-run output |

All mounts should be **read-only** (`:ro`). Omit whichever backend you don't use.

## API

The server exposes a simple REST API:

| Endpoint | Description |
|---|---|
| `GET /api/health` | Health check — returns version, config, and server timestamp |
| `GET /api/jobs` | All jobs (OpenClaw + Hermes, merged) with enriched schedule info and current state |
| `GET /api/jobs/:id/runs` | Run history for a specific job (query: `?limit=50`) |
| `GET /api/weekly-runs` | Run statuses for the current week, bucketed by job and day |

### Example

```bash
# List all jobs
curl http://localhost:3000/api/jobs | jq .

# Get run history for a job
curl "http://localhost:3000/api/jobs/396c296b-7dee-4f95-99a3-18c97f210fbd/runs" | jq .

# Health check (shows resolved DB/dir paths for both backends)
curl http://localhost:3000/api/health
```

## File Structure

```
agent-cron-dashboard/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── README.md
├── LICENSE
├── .gitignore
├── .dockerignore
├── server/
│   ├── __init__.py
│   ├── main.py           # FastAPI server + API routes; merges backends
│   ├── cron_reader.py     # Read jobs from OpenClaw SQLite (or legacy jobs.json), humanize schedules
│   ├── run_history.py     # Read OpenClaw run history from SQLite (or legacy JSONL files)
│   └── hermes_reader.py   # Read jobs + run history from a Hermes Agent cron dir
└── public/
    ├── index.html         # App shell
    ├── style.css          # Dark theme styles
    └── app.js             # Frontend logic (vanilla JS) — backend-agnostic
```

## Building from Source

```bash
# Build the Docker image
docker build -t agent-cron-dashboard .

# Run it
docker run -d \
  -p 3000:3000 \
  -v ~/.openclaw/cron:/data/cron:ro \
  -v ~/.openclaw/state:/data/state:ro \
  -v ~/.hermes/cron:/data/hermes-cron:ro \
  agent-cron-dashboard
```

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Auto-reload on file changes
OPENCLAW_CRON_DIR=~/.openclaw/cron \
OPENCLAW_STATE_DB=~/.openclaw/state/openclaw.sqlite \
HERMES_CRON_DIR=~/.hermes/cron \
uvicorn server.main:app --reload --host 0.0.0.0 --port 3000
```

## How It Works

1. **Job list** — merges two sources:
   - OpenClaw: reads the `cron_jobs` table from `openclaw.sqlite` on each request (each row has a `job_json` column with the complete job object; no caching), falling back to legacy `jobs.json` when SQLite is absent.
   - Hermes Agent: reads `~/.hermes/cron/jobs.json` (a dict with a top-level `jobs` array) on each request.
   Both are normalized into the same enriched shape before being sent to the frontend, which has no idea which backend a given job came from.
2. **Run history** — OpenClaw: reads the `cron_run_logs` table from `openclaw.sqlite`, filtered by job ID. Hermes: reads the `executions` table from `~/.hermes/cron/executions.db`, filtered by job ID, and pulls the run's saved markdown output (under `cron/output/<job_id>/`) as the summary.
3. **Weekly runs** — same sources as run history, bucketed by day for the current week.
4. **Schedule humanization** — parses cron expressions server-side into readable strings like "9:30, 12:30, 15:30 on weekdays (America/New_York)". Hermes's `interval`/`once` schedule kinds are translated to OpenClaw's `every`/`at` shapes internally so the same fraction/humanization math handles both.
5. **Timeline** — computes all firing times within the current day using `croniter`, returned as fractional positions [0, 1) along the day. Rendered client-side on an HTML canvas.

### Backward compatibility

Older OpenClaw versions stored jobs in `~/.openclaw/cron/jobs.json` and run history in per-job JSONL files under `~/.openclaw/cron/runs/`. When `openclaw.sqlite` is not found, the dashboard falls back to these files automatically — no configuration change needed.

## License

MIT — see [LICENSE](LICENSE).
