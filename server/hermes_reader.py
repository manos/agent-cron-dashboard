"""
hermes_reader.py
Reads cron job data from a Hermes Agent installation (~/.hermes/cron/).

Hermes stores jobs in ``cron/jobs.json`` (a dict with a top-level ``jobs``
list — NOT the OpenClaw array/`{jobs:[...]}` shape, though similar) and run
history in ``cron/executions.db`` (SQLite, table ``executions``), with the
actual per-run output saved as markdown under ``cron/output/<job_id>/``.

This module normalizes both into the same shape ``cron_reader.py`` /
``run_history.py`` produce for OpenClaw, so the rest of the dashboard
(``main.py`` API routes, the frontend) is completely backend-agnostic.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


def detect_hermes_cron_dir(explicit: str | None) -> str | None:
    """
    Return a Hermes cron directory if one is present, else None.

    A directory counts as a "Hermes" cron dir when it has a ``jobs.json``
    whose top-level shape is ``{"jobs": [...], ...}`` (Hermes) rather than
    a bare list or ``{"jobs": [...]}`` without the rest of Hermes's job
    fields (``id``, ``next_run_at``, ``last_status`` at the top level of
    each job, not nested under ``state``).
    """
    if not explicit:
        return None
    jobs_path = os.path.join(explicit, "jobs.json")
    if not os.path.exists(jobs_path):
        return None
    try:
        with open(jobs_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    jobs = data.get("jobs") or []
    if jobs and isinstance(jobs[0], dict) and "next_run_at" in jobs[0]:
        return explicit
    # Empty jobs list — still treat as Hermes if executions.db exists
    if not jobs and os.path.exists(os.path.join(explicit, "executions.db")):
        return explicit
    return None


def read_jobs(cron_dir: str) -> list[dict]:
    """Read and enrich all jobs from a Hermes ``cron/jobs.json``."""
    jobs_path = os.path.join(cron_dir, "jobs.json")
    try:
        with open(jobs_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[hermes_reader] Failed to read/parse jobs.json: {e}")
        return []

    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    return [_enrich_job(job) for job in jobs]


def _enrich_job(job: dict) -> dict:
    """Enrich a raw Hermes job object into the shared dashboard shape."""
    schedule = job.get("schedule") or {}
    last_status = job.get("last_status")
    consecutive_errors = job.get("failure_streak") or 0

    return {
        "id": job.get("id"),
        "name": job.get("name") or job.get("id"),
        "agentId": None,
        "enabled": job.get("enabled", True),
        "sessionTarget": None,
        "schedule": {
            "kind": schedule.get("kind"),
            "expr": schedule.get("expr"),
            "tz": schedule.get("tz"),
            "everyMs": (schedule.get("minutes") or 0) * 60_000
            if schedule.get("kind") == "interval"
            else None,
            "at": schedule.get("run_at") if schedule.get("kind") == "once" else None,
        },
        "scheduleHuman": _humanize_schedule(schedule),
        "firingFractions": _compute_firing_fractions(schedule),
        "weeklyFiringFractions": compute_weekly_fractions(schedule),
        "model": job.get("model") or job.get("model_snapshot"),
        "prompt": job.get("prompt"),
        # Full job definition as stored in jobs.json, so the detail panel can
        # show exactly how the cron is configured (Charlie 2026-11-21). Runtime
        # bookkeeping fields stay in; the UI groups them.
        "config": job,
        "channel": (job.get("origin") or {}).get("platform"),
        "deliveryMode": job.get("deliver"),
        "state": {
            "nextRunAtMs": _iso_to_ms(job.get("next_run_at")),
            "lastRunAtMs": _iso_to_ms(job.get("last_run_at")),
            "lastRunStatus": last_status,
            "consecutiveErrors": consecutive_errors,
            "lastDurationMs": None,
            "lastDeliveryStatus": job.get("last_delivery_error") and "error" or None,
        },
    }


def _iso_to_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


# ─── Schedule Humanization (Hermes schedule shapes) ────────────────────────


def _humanize_schedule(schedule: dict) -> str:
    kind = schedule.get("kind")
    display = schedule.get("display")
    if display:
        return display
    if kind == "cron":
        return schedule.get("expr") or "Invalid cron"
    if kind == "interval":
        minutes = schedule.get("minutes") or 0
        return f"Every {minutes} minute{'s' if minutes != 1 else ''}"
    if kind == "once":
        return f"Once at {schedule.get('run_at', '?')}"
    return kind or "Unknown"


# ─── Firing Fractions (reuses OpenClaw dashboard's cron-fraction math) ─────
#
# We delegate to cron_reader's cron/every fraction helpers where the
# schedule shapes line up (a "cron" kind is identical between backends).
# The "interval"/"once" kinds are Hermes-specific names for what
# cron_reader calls "every"/"at", so we translate the schedule dict first.


def _to_cron_reader_schedule(schedule: dict) -> dict:
    kind = schedule.get("kind")
    if kind == "cron":
        return {"kind": "cron", "expr": schedule.get("expr"), "tz": schedule.get("tz")}
    if kind == "interval":
        minutes = schedule.get("minutes") or 0
        return {"kind": "every", "everyMs": minutes * 60_000}
    if kind == "once":
        return {"kind": "at", "at": schedule.get("run_at")}
    return {}


def _compute_firing_fractions(schedule: dict) -> list[float]:
    from .cron_reader import _compute_firing_fractions as _base

    return _base(_to_cron_reader_schedule(schedule))


def compute_weekly_fractions(schedule: dict) -> dict[str, list[float]]:
    from .cron_reader import compute_weekly_fractions as _base

    return _base(_to_cron_reader_schedule(schedule))


# ─── Run History ────────────────────────────────────────────────────────────

_VALID_JOB_ID = re.compile(r"^[a-zA-Z0-9-]+$")

# Maps Hermes's executions.db `status` values to the dashboard's shared
# vocabulary ("ok" | "error" | "timeout" | anything else passed through).
_STATUS_MAP = {
    "completed": "ok",
    "failed": "error",
    "unknown": "error",
    "claimed": "pending",
    "running": "pending",
}


def get_run_history(
    job_id: str,
    cron_dir: str,
    limit: int = 50,
) -> tuple[list[dict], str | None]:
    """
    Fetch run history for a Hermes job from ``executions.db``, enriched
    with the saved markdown output (summary) from ``cron/output/<id>/``.
    """
    if not job_id or not isinstance(job_id, str) or not _VALID_JOB_ID.match(job_id):
        return [], "Invalid job ID format"

    db_path = os.path.join(cron_dir, "executions.db")
    if not os.path.exists(db_path):
        return [], None

    try:
        # immutable=1 (not just mode=ro): the DB may be on a read-only bind
        # mount (Docker :ro volume). Plain mode=ro still tries to take a
        # SHARED lock / probe for a hot journal, which needs write access to
        # the *directory* — that fails with "unable to open database file"
        # on a read-only mount even though the .db file itself is readable.
        # immutable=1 tells SQLite the file won't change underneath us and
        # skips all locking. Found live 2026-09-01: dashboard container had
        # the mount read-only and every Hermes run-history query 500'd.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM executions WHERE job_id = ? "
            "ORDER BY claimed_at DESC LIMIT ?",
            (job_id, limit),
        ).fetchall()
        conn.close()
    except Exception as e:
        return [], f"SQLite read error: {e}"

    output_dir = Path(cron_dir) / "output" / job_id
    entries = [_normalize_entry(dict(row), output_dir) for row in rows]
    return entries, None


def _normalize_entry(entry: dict, output_dir: Path) -> dict:
    ts = _iso_to_ms(entry.get("finished_at") or entry.get("started_at") or entry.get("claimed_at"))
    raw_status = entry.get("status") or ""
    status = _STATUS_MAP.get(raw_status, raw_status)

    duration_ms = None
    if entry.get("started_at") and entry.get("finished_at"):
        try:
            start = datetime.fromisoformat(entry["started_at"])
            end = datetime.fromisoformat(entry["finished_at"])
            duration_ms = int((end - start).total_seconds() * 1000)
        except (ValueError, TypeError):
            pass

    summary = entry.get("error")
    if not summary:
        summary = _find_output_summary(output_dir, ts)

    return {
        "ts": ts,
        "runAtMs": ts,
        "action": "finished",
        "status": status,
        "summary": summary,
        "durationMs": duration_ms,
        "model": None,
        "provider": None,
        "usage": None,
        "delivered": None,
        "deliveryStatus": None,
        "sessionId": entry.get("process_id"),
    }


def _find_output_summary(output_dir: Path, ts_ms: int | None) -> str | None:
    """
    Best-effort: find the saved output .md file closest to this run's
    timestamp and extract the "## Response" section as the summary.

    If the response is exactly "[SILENT]" (the standard cron convention for
    "nothing to report, suppress delivery"), that alone is useless in the
    dashboard -- the whole point of checking here instead of Discord is to
    see what the underlying script actually found even when nothing was
    delivered. Fall back to the "## Script Output" block in that case so a
    silent run still shows real data (e.g. "NONE", or a raw transaction dump)
    instead of just the literal string "[SILENT]". (Charlie 2026-09-09.)
    """
    if not output_dir.exists() or ts_ms is None:
        return None
    try:
        target = datetime.fromtimestamp(ts_ms / 1000)
        best_path = None
        best_diff = None
        for f in output_dir.glob("*.md"):
            # filenames look like 2026-09-01_13-34-54.md
            try:
                stamp = datetime.strptime(f.stem, "%Y-%m-%d_%H-%M-%S")
            except ValueError:
                continue
            diff = abs((stamp - target).total_seconds())
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_path = f
        if best_path is None or best_diff is None or best_diff > 120:
            return None
        text = best_path.read_text(errors="ignore")
        marker = "## Response"
        idx = text.find(marker)
        if idx == -1:
            return text[:200]
        response = text[idx + len(marker):].strip()
        if response.strip() == "[SILENT]":
            script_marker = "## Script Output"
            s_idx = text.find(script_marker)
            if s_idx != -1:
                # Script output is a fenced code block; grab its contents.
                block = text[s_idx + len(script_marker):idx]
                fence_start = block.find("```")
                if fence_start != -1:
                    fence_end = block.find("```", fence_start + 3)
                    if fence_end != -1:
                        script_text = block[fence_start + 3:fence_end].strip()
                        if script_text:
                            return f"[SILENT] (script output: {script_text[:180]})"
            return response[:200]
        return response[:200]
    except Exception:
        return None


def get_weekly_runs(cron_dir: str, cutoff_ms: int, days_set: set[str]) -> dict[str, dict[str, list[dict]]]:
    """Same shape as main.py's /api/weekly-runs, sourced from executions.db."""
    db_path = os.path.join(cron_dir, "executions.db")
    jobs_data: dict[str, dict[str, list[dict]]] = {}
    if not os.path.exists(db_path):
        return jobs_data

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM executions WHERE claimed_at IS NOT NULL "
            "ORDER BY claimed_at DESC"
        ).fetchall()
        conn.close()
    except Exception:
        return jobs_data

    for row in rows:
        entry = dict(row)
        ts = _iso_to_ms(entry.get("finished_at") or entry.get("started_at") or entry.get("claimed_at"))
        if not ts or ts < cutoff_ms:
            continue
        day = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
        if day not in days_set:
            continue
        job_id = entry.get("job_id")
        if not job_id:
            continue
        output_dir = Path(cron_dir) / "output" / job_id
        raw_status = entry.get("status") or ""
        status = _STATUS_MAP.get(raw_status, raw_status)
        duration_ms = None
        if entry.get("started_at") and entry.get("finished_at"):
            try:
                start = datetime.fromisoformat(entry["started_at"])
                end = datetime.fromisoformat(entry["finished_at"])
                duration_ms = int((end - start).total_seconds() * 1000)
            except (ValueError, TypeError):
                pass
        summary = entry.get("error") or _find_output_summary(output_dir, ts) or ""
        jobs_data.setdefault(job_id, {}).setdefault(day, []).append({
            "status": status,
            "ts": ts,
            "durationMs": duration_ms,
            "summary": (summary or "")[:200],
        })

    return jobs_data
