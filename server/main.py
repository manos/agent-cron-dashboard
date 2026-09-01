"""
Agent Cron Dashboard — Server

A lightweight FastAPI server that exposes OpenClaw and/or Hermes Agent
cron job data via a simple REST API and serves the static frontend.

Environment variables:
    PORT               - HTTP port (default: 3000)
    OPENCLAW_CRON_DIR  - Path to OpenClaw cron directory (default: /data/cron)
    OPENCLAW_BIN       - Path to openclaw binary (default: openclaw)
    OPENCLAW_STATE_DB  - Path to openclaw.sqlite (auto-detected if unset)
    OPENCLAW_ENABLED   - Set to "false"/"0" to hide the OpenClaw backend
                          entirely, even if OPENCLAW_CRON_DIR/STATE_DB
                          resolve to real data. Default: "true".
    HERMES_CRON_DIR    - Path to a Hermes Agent cron directory
                          (~/.hermes/cron), default: /data/hermes-cron.
                          Auto-detected as absent if it doesn't look like
                          a Hermes cron dir (no jobs.json in that shape).
                          Jobs/runs from this backend are merged with
                          OpenClaw's — set only one, or both, or neither.
    HERMES_ENABLED     - Set to "false"/"0" to hide the Hermes backend
                          entirely. Default: "true".
"""

import os
import signal
import sys

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from .cron_reader import read_jobs
from .run_history import get_run_history
from . import hermes_reader


def _flag_enabled(env_name: str, default: bool = True) -> bool:
    """Parse a boolean env var ('false'/'0'/'no'/'off' => False, else default)."""
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("false", "0", "no", "off", "")


PORT = int(os.environ.get("PORT", "3000"))
CRON_DIR = os.environ.get("OPENCLAW_CRON_DIR", "/data/cron")
OPENCLAW_BIN = os.environ.get("OPENCLAW_BIN", "openclaw")
# Explicit path to openclaw.sqlite; auto-detected from OPENCLAW_CRON_DIR/../state/ when absent
STATE_DB = os.environ.get("OPENCLAW_STATE_DB", "/data/state/openclaw.sqlite") or None
# Master on/off switch for the OpenClaw backend — set OPENCLAW_ENABLED=false
# to run a Hermes-only dashboard even when OpenClaw data is still mounted.
OPENCLAW_ENABLED = _flag_enabled("OPENCLAW_ENABLED", default=True)
# Optional second cron dir for a Hermes Agent install (~/.hermes/cron). When
# set (or auto-detected — see _detect_hermes_dir below), jobs/runs from BOTH
# backends are merged so one dashboard can watch an OpenClaw AND a Hermes
# instance side by side.
HERMES_CRON_DIR = os.environ.get("HERMES_CRON_DIR", "/data/hermes-cron")
# Master on/off switch for the Hermes backend — set HERMES_ENABLED=false to
# hide it (e.g. for an OpenClaw-only dashboard).
HERMES_ENABLED = _flag_enabled("HERMES_ENABLED", default=True)

PUBLIC_DIR = os.path.join(os.path.dirname(__file__), "..", "public")

app = FastAPI(
    title="Agent Cron Dashboard",
    version="1.1.0",
    docs_url=None,  # disable Swagger UI in production
    redoc_url=None,
)

# CORS: allow any origin (self-hosted, users may proxy from another port)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["Content-Type"],
)


# No-cache for all responses — API data changes live, static assets should always be fresh
class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        return response


app.add_middleware(NoCacheStaticMiddleware)


# ─── API Routes ────────────────────────────────────────────────────────────────


def _hermes_dir() -> str | None:
    """Resolve an active Hermes cron dir, or None if disabled/not present/valid."""
    if not HERMES_ENABLED:
        return None
    return hermes_reader.detect_hermes_cron_dir(HERMES_CRON_DIR)


def _job_source_map() -> dict[str, str]:
    """Map job_id -> 'hermes' for every Hermes job (used to route by-id routes)."""
    hdir = _hermes_dir()
    if not hdir:
        return {}
    return {j["id"]: "hermes" for j in hermes_reader.read_jobs(hdir) if j.get("id")}


@app.get("/api/health")
def health():
    """Simple health check. Returns service status and config paths."""
    import time

    from .cron_reader import _resolve_state_db
    resolved_db = _resolve_state_db(STATE_DB, CRON_DIR)
    hdir = _hermes_dir()

    return {
        "status": "ok",
        "version": "1.1.0",
        "config": {
            "cronDir": CRON_DIR,
            "stateDb": resolved_db,
            "openclawBin": OPENCLAW_BIN,
            "openclawEnabled": OPENCLAW_ENABLED,
            "hermesCronDir": hdir,
            "hermesEnabled": HERMES_ENABLED,
        },
        "timestamp": int(time.time() * 1000),
    }


@app.get("/api/jobs")
def jobs():
    """
    All cron jobs with enriched schedule info and current state.
    Merges OpenClaw (SQLite/jobs.json) and Hermes (~/.hermes/cron) jobs
    when both are present so one dashboard covers either or both agents.
    """
    import time

    try:
        job_list = read_jobs(CRON_DIR, state_db=STATE_DB) if OPENCLAW_ENABLED else []
    except Exception as e:
        job_list = []
        openclaw_error = str(e)
    else:
        openclaw_error = None

    hdir = _hermes_dir()
    hermes_jobs = []
    if hdir:
        try:
            hermes_jobs = hermes_reader.read_jobs(hdir)
        except Exception as e:
            print(f"[main] Failed to read Hermes jobs: {e}")

    all_jobs = job_list + hermes_jobs
    if not all_jobs and openclaw_error:
        return {"error": "Failed to read jobs", "message": openclaw_error}

    return {
        "jobs": all_jobs,
        "count": len(all_jobs),
        "serverTime": int(time.time() * 1000),
        "serverTz": _get_server_tz(),
    }


@app.get("/api/jobs/{job_id}/runs")
def job_runs(job_id: str, limit: int = Query(default=50, le=200, ge=1)):
    """
    Run history for a specific job. Tries the OpenClaw backend first;
    if the job isn't known there, falls back to the Hermes backend.
    """
    try:
        entries, error = ([], None)
        if OPENCLAW_ENABLED:
            entries, error = get_run_history(job_id, CRON_DIR, OPENCLAW_BIN, limit, state_db=STATE_DB)

        hdir = _hermes_dir()
        if not entries and hdir and job_id in _job_source_map():
            entries, error = hermes_reader.get_run_history(job_id, hdir, limit)

        return {
            "entries": entries,
            "count": len(entries),
            "jobId": job_id,
            "error": error,
        }
    except Exception as e:
        return {
            "error": "Failed to fetch run history",
            "message": str(e),
            "entries": [],
        }


@app.get("/api/weekly-runs")
def weekly_runs():
    """
    Run statuses for the past 7 days, bucketed by job and calendar day (UTC).
    Returns:
      {
        "days": ["2026-04-03", ...],   # 7 dates Mon→Sun of current week
        "jobs": {
          "<jobId>": {
            "2026-04-03": [{"status": "ok", "ts": 1234567890000}, ...],
            ...
          }
        }
      }
    """
    import json
    import time
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    now_utc = datetime.now(timezone.utc)
    # Build the 7-day window: today minus 6 days → today
    cutoff_dt = now_utc - timedelta(days=7)
    cutoff_ms = int(cutoff_dt.timestamp() * 1000)

    # Collect dates for current week Mon-Sun (calendar dates in UTC)
    today_date = now_utc.date()
    monday = today_date - timedelta(days=today_date.weekday())
    days = [(monday + timedelta(days=i)).isoformat() for i in range(7)]
    days_set = set(days)

    jobs_data: dict[str, dict[str, list[dict]]] = {}

    if OPENCLAW_ENABLED:
        from .run_history import _resolve_state_db
        db_path = _resolve_state_db(STATE_DB, CRON_DIR)

        if db_path:
            # ── SQLite path ────────────────────────────────────────────────
            import sqlite3 as _sqlite3
            try:
                conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                conn.row_factory = _sqlite3.Row
                rows = conn.execute(
                    "SELECT job_id, ts, status, duration_ms, summary "
                    "FROM cron_run_logs WHERE ts >= ? ORDER BY ts DESC",
                    (cutoff_ms,),
                ).fetchall()
                conn.close()

                for row in rows:
                    ts = row["ts"]
                    job_id = row["job_id"]
                    day = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                    if day not in days_set:
                        continue
                    jobs_data.setdefault(job_id, {}).setdefault(day, []).append({
                        "status": row["status"],
                        "ts": ts,
                        "durationMs": row["duration_ms"],
                        "summary": (row["summary"] or "")[:200],
                    })
            except Exception:
                pass  # fall through to JSONL below

        if not jobs_data:
            # ── Legacy fallback: JSONL run files ────────────────────────────
            runs_dir = Path(CRON_DIR) / "runs"
            if runs_dir.exists():
                try:
                    for run_file in runs_dir.iterdir():
                        if run_file.suffix != ".jsonl":
                            continue
                        try:
                            with open(run_file) as f:
                                for line in f:
                                    line = line.strip()
                                    if not line:
                                        continue
                                    try:
                                        entry = json.loads(line)
                                    except json.JSONDecodeError:
                                        continue

                                    ts = entry.get("ts") or entry.get("runAtMs")
                                    if not ts or ts < cutoff_ms:
                                        continue

                                    job_id = entry.get("jobId")
                                    if not job_id:
                                        continue

                                    day = datetime.fromtimestamp(
                                        ts / 1000, tz=timezone.utc
                                    ).strftime("%Y-%m-%d")
                                    if day not in days_set:
                                        continue

                                    summary = entry.get("summary") or ""
                                    if len(summary) > 200:
                                        summary = summary[:200] + "…"

                                    jobs_data.setdefault(job_id, {}).setdefault(day, []).append({
                                        "status": entry.get("status"),
                                        "ts": ts,
                                        "durationMs": entry.get("durationMs"),
                                        "summary": summary,
                                    })
                        except (OSError, PermissionError):
                            continue
                except (OSError, PermissionError):
                    pass

    # ── Merge in Hermes executions.db, if a Hermes cron dir is present ─────
    hdir = _hermes_dir()
    if hdir:
        try:
            hermes_jobs_data = hermes_reader.get_weekly_runs(hdir, cutoff_ms, days_set)
            for job_id, by_day in hermes_jobs_data.items():
                dest = jobs_data.setdefault(job_id, {})
                for day, entries in by_day.items():
                    dest.setdefault(day, []).extend(entries)
        except Exception as e:
            print(f"[main] Failed to read Hermes weekly runs: {e}")

    return {"days": days, "jobs": jobs_data}


# ─── Static file serving ──────────────────────────────────────────────────────

# Mount static files AFTER API routes so /api/* takes priority
app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="static")


# ─── Helpers ───────────────────────────────────────────────────────────────────


def _get_server_tz() -> str:
    """Get the server's IANA timezone string."""
    try:
        import datetime

        return str(datetime.datetime.now().astimezone().tzinfo)
    except Exception:
        return "UTC"


# ─── Graceful shutdown ─────────────────────────────────────────────────────────


def _handle_sigterm(*_):
    print("Received SIGTERM, shutting down gracefully...")
    sys.exit(0)


signal.signal(signal.SIGTERM, _handle_sigterm)
