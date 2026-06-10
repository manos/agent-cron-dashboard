# OpenClaw Cron Dashboard

A lightweight, self-hosted web dashboard for visualizing [OpenClaw](https://github.com/openclaw/openclaw) cron jobs — schedules, status, run history, and output.

![Dashboard screenshot](docs/screenshot.png)

## Features

- **24-hour schedule timeline** — see when every job fires at a glance, with the current time highlighted
- **Job cards** — schedule (human-readable), last run status, duration, next run, error count
- **Run history panel** — click any job to see its last 50 runs with full output summaries
- **Token usage** — see input/output token counts per run
- **Auto-refresh** — polls every 30 seconds; progress bar shows time until next refresh
- **Dark theme** — easy on the eyes, looks great on a monitoring screen
- **Responsive** — works on mobile and desktop
- **Zero dependencies on the frontend** — pure HTML/CSS/JS, no framework required
- **Zero-config data access** — reads directly from OpenClaw's SQLite state database; no API token or CLI binary needed

## Quick Start

### With Docker (recommended)

```bash
docker run -d \
  --name openclaw-cron-dashboard \
  -p 3000:3000 \
  -v ~/.openclaw/cron:/data/cron:ro \
  -v ~/.openclaw/state:/data/state:ro \
  ghcr.io/manos/openclaw-cron-dashboard:latest
```

Then open http://localhost:3000 in your browser.

### With Docker Compose

```bash
# Copy the example compose file
curl -O https://raw.githubusercontent.com/manos/openclaw-cron-dashboard/main/docker-compose.yml

# Edit to adjust volume paths if needed, then start:
docker compose up -d
```

### Without Docker

Requires Python 3.11+.

```bash
git clone https://github.com/manos/openclaw-cron-dashboard.git
cd openclaw-cron-dashboard
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Point at your OpenClaw data directories
export OPENCLAW_CRON_DIR=~/.openclaw/cron
export OPENCLAW_STATE_DB=~/.openclaw/state/openclaw.sqlite

uvicorn server.main:app --host 0.0.0.0 --port 3000
```

Open http://localhost:3000.

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|---|---|---|
| `PORT` | `3000` | HTTP port to listen on |
| `OPENCLAW_STATE_DB` | `/data/state/openclaw.sqlite` | Path to OpenClaw's SQLite state database. **Primary data source.** Auto-detected from `OPENCLAW_CRON_DIR/../state/` when not set. |
| `OPENCLAW_CRON_DIR` | `/data/cron` | Path to the OpenClaw cron directory. Used as a fallback for legacy `jobs.json` and JSONL run files when SQLite is unavailable. |

## Volume Mounts

| Host path | Container path | Purpose |
|---|---|---|
| `~/.openclaw/state` | `/data/state` | SQLite state database (jobs + run history) |
| `~/.openclaw/cron` | `/data/cron` | Legacy fallback: `jobs.json` and JSONL run files |

Both mounts should be **read-only** (`:ro`).

## API

The server exposes a simple REST API:

| Endpoint | Description |
|---|---|
| `GET /api/health` | Health check — returns version, config, and server timestamp |
| `GET /api/jobs` | All jobs with enriched schedule info and current state |
| `GET /api/jobs/:id/runs` | Run history for a specific job (query: `?limit=50`) |
| `GET /api/weekly-runs` | Run statuses for the current week, bucketed by job and day |

### Example

```bash
# List all jobs
curl http://localhost:3000/api/jobs | jq .

# Get run history for a job
curl "http://localhost:3000/api/jobs/396c296b-7dee-4f95-99a3-18c97f210fbd/runs" | jq .

# Health check (shows resolved DB path)
curl http://localhost:3000/api/health
```

## File Structure

```
openclaw-cron-dashboard/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── README.md
├── LICENSE
├── .gitignore
├── .dockerignore
├── server/
│   ├── __init__.py
│   ├── main.py          # FastAPI server + API routes
│   ├── cron_reader.py   # Read jobs from SQLite (or legacy jobs.json), humanize schedules
│   └── run_history.py   # Read run history from SQLite (or legacy JSONL files)
└── public/
    ├── index.html       # App shell
    ├── style.css        # Dark theme styles
    └── app.js           # Frontend logic (vanilla JS)
```

## Building from Source

```bash
# Build the Docker image
docker build -t openclaw-cron-dashboard .

# Run it
docker run -d \
  -p 3000:3000 \
  -v ~/.openclaw/cron:/data/cron:ro \
  -v ~/.openclaw/state:/data/state:ro \
  openclaw-cron-dashboard
```

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Auto-reload on file changes
OPENCLAW_CRON_DIR=~/.openclaw/cron \
OPENCLAW_STATE_DB=~/.openclaw/state/openclaw.sqlite \
uvicorn server.main:app --reload --host 0.0.0.0 --port 3000
```

## How It Works

1. **Job list** — reads the `cron_jobs` table from `openclaw.sqlite` on each request. Each row has a `job_json` column with the complete job object. No caching.
2. **Run history** — reads the `cron_run_logs` table from `openclaw.sqlite`, filtered by job ID, ordered by timestamp descending.
3. **Weekly runs** — same table, filtered to the current week, bucketed by day.
4. **Schedule humanization** — parses cron expressions server-side into readable strings like "9:30, 12:30, 15:30 on weekdays (America/New_York)".
5. **Timeline** — computes all firing times within the current day using `croniter`, returned as fractional positions [0, 1) along the day. Rendered client-side on an HTML canvas.

### Backward compatibility

Older OpenClaw versions stored jobs in `~/.openclaw/cron/jobs.json` and run history in per-job JSONL files under `~/.openclaw/cron/runs/`. When `openclaw.sqlite` is not found, the dashboard falls back to these files automatically — no configuration change needed.

## License

MIT — see [LICENSE](LICENSE).
