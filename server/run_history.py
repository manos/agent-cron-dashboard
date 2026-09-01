"""
run_history.py
Reads run history for a cron job directly from JSONL files on disk.
Falls back to OpenClaw CLI if direct file reading isn't available.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


# Job IDs must be UUID-like: alphanumeric and hyphens only
_VALID_JOB_ID = re.compile(r"^[a-zA-Z0-9-]+$")


def _resolve_state_db(explicit: str | None, cron_dir: str) -> str | None:
    """Mirror of cron_reader._resolve_state_db — see that module for details."""
    candidate = explicit or os.path.normpath(
        os.path.join(cron_dir, "..", "state", "openclaw.sqlite")
    )
    return candidate if os.path.exists(candidate) else None


def _get_run_history_sqlite(
    job_id: str,
    db_path: str,
    limit: int,
) -> tuple[list[dict], str | None]:
    """
    Fetch run history from the OpenClaw SQLite state database.
    Uses the ``cron_run_logs`` table; each row has an ``entry_json`` column.

    KNOWN SCHEMA DRIFT (found 2026-09-01, unverified fix): some OpenClaw
    versions don't have a ``cron_run_logs`` table at all — the closest
    analog observed was ``cron_run_receipts`` (columns: receipt_id, job_id,
    status, started_at_ms, finished_at_ms, error_text — no entry_json), but
    it was empty on the install where this was found, so there was no live
    data to confirm it's really the successor table vs. a short-lived
    lock/receipt record that gets cleared. Left unpatched rather than
    guess-fixing against untested data. If you hit this on a live OpenClaw
    install with real cron history, `PRAGMA table_info` your `cron_run_logs`
    equivalent and either add a compat branch here or open an issue.
    """
    import sqlite3 as _sqlite3

    try:
        conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = _sqlite3.Row
        rows = conn.execute(
            "SELECT entry_json FROM cron_run_logs "
            "WHERE job_id = ? ORDER BY ts DESC LIMIT ?",
            (job_id, limit),
        ).fetchall()
        conn.close()
    except Exception as e:
        return [], f"SQLite read error: {e}"

    entries: list[dict] = []
    for row in rows:
        try:
            entries.append(json.loads(row["entry_json"]))
        except (json.JSONDecodeError, KeyError):
            continue

    return [_normalize_entry(e) for e in entries], None


def get_run_history(
    job_id: str,
    cron_dir: str = "/data/cron",
    openclaw_bin: str = "openclaw",
    limit: int = 50,
    state_db: str | None = None,
) -> tuple[list[dict], str | None]:
    """
    Fetch run history for a given job ID.

    Tries the SQLite state database first (OpenClaw ≥ 0.x).  Falls back to
    reading the legacy JSONL run files in ``cron_dir/runs/``.

    Args:
        job_id:      Cron job ID (UUID-like string).
        cron_dir:    Path to the OpenClaw cron directory (``OPENCLAW_CRON_DIR``).
        openclaw_bin: Unused; kept for backward compatibility.
        limit:       Maximum number of entries to return.
        state_db:    Explicit path to ``openclaw.sqlite`` (``OPENCLAW_STATE_DB``).
                     Auto-detected from ``cron_dir/../state/`` when omitted.

    Returns:
        ``(entries, error)`` — entries is a list of normalised run dicts;
        error is a human-readable string or ``None``.
    """
    if not job_id or not isinstance(job_id, str):
        return [], "Invalid job ID"

    if not _VALID_JOB_ID.match(job_id):
        return [], "Invalid job ID format"

    db_path = _resolve_state_db(state_db, cron_dir)
    if db_path:
        return _get_run_history_sqlite(job_id, db_path, limit)

    # ── Legacy fallback: JSONL run files ──────────────────────────────────
    runs_dir = Path(cron_dir) / "runs"
    if not runs_dir.exists():
        return [], None

    entries: list[dict] = []

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
                            if entry.get("jobId") == job_id:
                                entries.append(entry)
                        except json.JSONDecodeError:
                            continue
            except (OSError, PermissionError):
                continue
    except (OSError, PermissionError) as e:
        return [], f"Cannot read runs directory: {e}"

    entries.sort(key=lambda e: e.get("ts", 0), reverse=True)
    return [_normalize_entry(e) for e in entries[:limit]], None


def _normalize_entry(entry: dict) -> dict:
    """
    Normalize a raw run entry into a consistent shape.
    Strips fields we don't need and ensures all expected fields are present.
    """
    usage = entry.get("usage")
    normalized_usage = None
    if usage:
        normalized_usage = {
            "inputTokens": usage.get("input_tokens", 0),
            "outputTokens": usage.get("output_tokens", 0),
            "totalTokens": usage.get("total_tokens", 0),
        }

    return {
        "ts": entry.get("ts"),
        "runAtMs": entry.get("runAtMs") or entry.get("ts"),
        "action": entry.get("action"),
        "status": entry.get("status"),
        "summary": entry.get("summary"),
        "durationMs": entry.get("durationMs"),
        "model": entry.get("model"),
        "provider": entry.get("provider"),
        "usage": normalized_usage,
        "delivered": entry.get("delivered"),
        "deliveryStatus": entry.get("deliveryStatus"),
        "sessionId": entry.get("sessionId"),
    }
