#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "tomli>=2.0; python_version < '3.11'",
# ]
# ///
"""
collect_runs.py – Fetch GitHub Actions workflow run data for a repository
and persist it to docs/data.json inside the repository.

Usage (called from the GitHub Actions workflow):
    uv run scripts/collect_runs.py

Environment variables (set by the workflow):
    GITHUB_TOKEN   – Personal access token or GITHUB_TOKEN secret.
    CONFIG_FILE    – Path to actions-performance.toml (default: actions-performance.toml)
    DATA_FILE      – Path to the output JSON file       (default: docs/data.json)
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]  # provided by uv on Python < 3.11


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Maximum number of times to retry a single request after a rate-limit response
# or a transient network error.
_MAX_RETRIES = 8
# Floor sleep between retries when no backoff header is present (seconds).
_RETRY_BACKOFF_BASE = 60
# Initial backoff for transient network errors (seconds); doubles each attempt.
_NETWORK_BACKOFF_BASE = 5


def _backoff_seconds(exc: HTTPError) -> float:
    """
    Return how many seconds to wait before retrying after a rate-limit response.

    GitHub documents two relevant headers:
      - ``Retry-After``      – integer seconds to wait (primary rate limit / 429)
      - ``x-ratelimit-reset`` – Unix timestamp when the quota resets (secondary / 403)

    We honour whichever is present and largest, falling back to _RETRY_BACKOFF_BASE.
    An extra 2-second buffer is added so we don't hit the reset instant exactly.
    """
    wait: float = _RETRY_BACKOFF_BASE

    retry_after = exc.headers.get("Retry-After")
    if retry_after:
        try:
            wait = max(wait, float(retry_after))
        except ValueError:
            pass

    reset_ts = exc.headers.get("x-ratelimit-reset")
    if reset_ts:
        try:
            delta = float(reset_ts) - time.time()
            if delta > 0:
                wait = max(wait, delta)
        except ValueError:
            pass

    return wait + 2  # small buffer


def _is_rate_limited(exc: HTTPError) -> bool:
    """Return True if the error looks like a GitHub rate-limit response."""
    if exc.code == 429:
        return True
    if exc.code == 403:
        # GitHub returns 403 (not 429) for secondary rate limits; the body
        # contains "rate limit" or the x-ratelimit-remaining header is "0".
        if exc.headers.get("x-ratelimit-remaining") == "0":
            return True
        try:
            body = exc.read().decode(errors="replace").lower()
            if "rate limit" in body or "rate_limit" in body:
                return True
        except Exception:
            pass
    return False


def _github_request(url: str, token: str) -> Any:
    """
    Make an authenticated GET request to the GitHub REST API and return parsed JSON.

    Retries automatically on:
      - Rate-limit responses (HTTP 429 or secondary 403): sleeps until the
        reset time indicated by response headers.
      - Transient network errors (timeouts, SSL handshake failures, connection
        resets, etc.): retries with exponential backoff starting at
        _NETWORK_BACKOFF_BASE seconds.

    Raises the underlying exception if all retries are exhausted or if the
    error is a non-retryable HTTP error (4xx other than rate limits, 5xx, …).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    network_backoff = _NETWORK_BACKOFF_BASE

    for attempt in range(1, _MAX_RETRIES + 1):
        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())

        except HTTPError as exc:
            if _is_rate_limited(exc):
                wait = _backoff_seconds(exc)
                resume_at = datetime.now(timezone.utc) + timedelta(seconds=wait)
                print(
                    f"  Rate limited (HTTP {exc.code}) on attempt {attempt}/{_MAX_RETRIES}. "
                    f"Sleeping {wait:.0f}s (until ~{resume_at.strftime('%H:%M:%S')} UTC) …",
                    flush=True,
                )
                time.sleep(wait)
                continue  # retry
            raise  # non-rate-limit HTTP error — let the caller decide

        except (URLError, OSError, http.client.HTTPException) as exc:
            # Covers: timeouts, SSL handshake failures, connection resets,
            # DNS failures, and any other transport-level error, as well as
            # http.client exceptions such as IncompleteRead, RemoteDisconnected,
            # and BadStatusLine that are raised mid-response.
            if attempt == _MAX_RETRIES:
                raise  # out of retries — propagate to caller
            print(
                f"  Network error on attempt {attempt}/{_MAX_RETRIES}: {exc}. "
                f"Retrying in {network_backoff}s …",
                flush=True,
            )
            time.sleep(network_backoff)
            network_backoff = min(network_backoff * 2, 300)  # cap at 5 minutes
            continue

    # Exhausted retries on rate-limit path — one final attempt, let it raise.
    req = Request(url, headers=headers)
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _paginate(base_url: str, token: str, params: dict[str, str | int] | None = None) -> list[dict]:
    """
    Fetch all pages of a GitHub list endpoint.

    Raises ``HTTPError`` on non-rate-limit failures so the caller can decide
    whether to skip the affected workflow or abort entirely.  Rate-limit errors
    are handled transparently inside ``_github_request``.
    """
    params = dict(params or {})
    params.setdefault("per_page", 100)
    results: list[dict] = []
    page = 1
    while True:
        params["page"] = page
        url = f"{base_url}?{urlencode(params)}"
        data = _github_request(url, token)  # raises on error
        # GitHub wraps list responses in an object with a key named after the
        # resource (e.g. {"workflow_runs": [...], "total_count": N}).
        if isinstance(data, dict):
            items = next((v for v in data.values() if isinstance(v, list)), [])
        else:
            items = data
        if not items:
            break
        results.extend(items)
        if len(items) < params["per_page"]:
            break
        page += 1
    return results


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def load_config(config_path: Path) -> dict:
    with config_path.open("rb") as fh:
        return tomllib.load(fh)


def load_existing_data(data_path: Path) -> dict:
    if data_path.exists():
        with data_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    return {"meta": {}, "workflows": {}}


def save_data(data_path: Path, data: dict) -> None:
    data_path.parent.mkdir(parents=True, exist_ok=True)
    with data_path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    print(f"Saved data to {data_path}")


def _run_to_record(run: dict) -> dict:
    """Convert a raw API workflow run object to a compact record."""
    started = run.get("run_started_at") or run.get("created_at")
    updated = run.get("updated_at")

    duration_seconds: int | None = None
    if started and updated:
        try:
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            t0 = datetime.strptime(started, fmt)
            t1 = datetime.strptime(updated, fmt)
            duration_seconds = max(0, int((t1 - t0).total_seconds()))
        except ValueError:
            pass

    conclusion = run.get("conclusion")  # success | failure | cancelled | skipped | None
    if conclusion is None:
        conclusion = "in_progress"

    return {
        "id": run["id"],
        "run_number": run.get("run_number"),
        "event": run.get("event"),
        "status": run.get("status"),
        "conclusion": conclusion,
        "started_at": started,
        "updated_at": updated,
        "duration_seconds": duration_seconds,
        "html_url": run.get("html_url"),
        "head_branch": run.get("head_branch"),
        "head_sha": run.get("head_sha"),
        # Populated later for successful runs via _fetch_skipped_jobs_count().
        # None means "not yet fetched"; 0 means "fetched, none skipped".
        "skipped_jobs": None,
    }


def _fetch_skipped_jobs_count(run_id: int, base_api: str, token: str) -> int:
    """
    Return the number of jobs with conclusion == 'skipped' for a workflow run.

    Uses _paginate so rate-limit retries are handled transparently.
    """
    jobs = _paginate(f"{base_api}/actions/runs/{run_id}/jobs", token)
    return sum(1 for j in jobs if j.get("conclusion") == "skipped")


def collect(config: dict, existing: dict, token: str, data_path: Path) -> dict:
    """
    Fetch workflow run data and merge it into *existing*.

    Progress is flushed to *data_path* after every workflow so that a failure
    (network error, unhandled exception, CI timeout) can be resumed simply by
    re-running the script — already-collected workflows are skipped because
    their runs will already satisfy the incremental cutoff.
    """
    repo_cfg = config["repository"]
    owner = repo_cfg["owner"]
    repo = repo_cfg["repo"]
    workflow_filter: list[str] = repo_cfg.get("workflows", [])
    lookback_days: int = config.get("collection", {}).get("initial_lookback_days", 90)

    base_api = f"https://api.github.com/repos/{owner}/{repo}"

    # --- Determine the earliest date we need data for -----------------------
    # Computed once from *existing* so that the cutoff is stable even as we
    # add new workflows during this run.
    existing_workflows: dict[str, dict] = existing.get("workflows", {})

    latest_known_ts: datetime | None = None
    for wf_data in existing_workflows.values():
        for run in wf_data.get("runs", []):
            ts_str = run.get("started_at")
            if ts_str:
                try:
                    ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    if latest_known_ts is None or ts > latest_known_ts:
                        latest_known_ts = ts
                except ValueError:
                    pass

    now = datetime.now(timezone.utc)
    if latest_known_ts is not None:
        # Overlap by 2 days to catch anything that finished late.
        since = latest_known_ts - timedelta(days=2)
        print(f"Incremental mode: fetching runs since {since.date()} (latest known: {latest_known_ts.date()})")
    else:
        since = now - timedelta(days=lookback_days)
        print(f"Initial mode: fetching runs for the past {lookback_days} days (since {since.date()})")

    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- Fetch all workflows -------------------------------------------------
    print(f"Fetching workflow definitions for {owner}/{repo} …")
    try:
        all_workflows = _paginate(f"{base_api}/actions/workflows", token)
    except HTTPError as exc:
        print(f"ERROR: Could not fetch workflow list (HTTP {exc.code}): {exc.reason}", file=sys.stderr)
        raise

    # Filter if the user specified a workflow allowlist
    if workflow_filter:
        all_workflows = [
            w for w in all_workflows
            if Path(w.get("path", "")).name in workflow_filter
        ]
        print(f"Filtered to {len(all_workflows)} workflow(s): {workflow_filter}")
    else:
        print(f"Tracking {len(all_workflows)} workflow(s)")

    # Build the mutable result dict seeded with everything we already have.
    # We update it in-place and flush to disk after each workflow so that any
    # subsequent re-run can pick up where we left off.
    site_cfg = config.get("site", {})
    result: dict = {
        "meta": {
            "owner": owner,
            "repo": repo,
            "title": site_cfg.get("title", f"{owner}/{repo} Action Performance"),
            "description": site_cfg.get("description", ""),
            "last_updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "workflows": dict(existing_workflows),  # start from what we already have
    }

    # --- Collect runs per workflow -------------------------------------------
    total = len(all_workflows)
    failed_workflows: list[str] = []

    for idx, wf in enumerate(all_workflows, start=1):
        wf_id = str(wf["id"])
        wf_name = wf.get("name", wf_id)
        wf_file = Path(wf.get("path", "")).name

        print(f"  [{idx}/{total}] '{wf_name}' ({wf_file}) …", end=" ", flush=True)

        try:
            raw_runs = _paginate(
                f"{base_api}/actions/workflows/{wf_id}/runs",
                token,
                {"created": f">={since_str}", "status": "completed"},
            )
            # Also grab in-progress runs so we don't miss them on the next pass.
            raw_runs += _paginate(
                f"{base_api}/actions/workflows/{wf_id}/runs",
                token,
                {"created": f">={since_str}", "status": "in_progress"},
            )
        except HTTPError as exc:
            # Non-retryable error for this specific workflow.  Record it, keep
            # going, and report a summary at the end so the operator knows which
            # workflows need attention on a re-run.
            print(f"SKIPPED (HTTP {exc.code}: {exc.reason})", flush=True)
            failed_workflows.append(f"{wf_name} ({wf_file}): HTTP {exc.code}")
            continue

        print(f"{len(raw_runs)} runs fetched", flush=True)

        new_runs_by_id: dict[int, dict] = {r["id"]: _run_to_record(r) for r in raw_runs}

        # Merge with existing runs for this workflow
        existing_wf = existing_workflows.get(wf_id, {})
        existing_runs_by_id: dict[int, dict] = {r["id"]: r for r in existing_wf.get("runs", [])}

        # New / updated records overwrite old ones; keep the rest
        merged = {**existing_runs_by_id, **new_runs_by_id}

        # --- Fetch skipped-job counts for successful runs that need it -------
        # A run needs fetching when: conclusion is "success" AND skipped_jobs
        # is None (i.e. never fetched, including runs just converted from raw).
        needs_job_fetch = [
            r for r in merged.values()
            if r.get("conclusion") == "success" and r.get("skipped_jobs") is None
        ]
        if needs_job_fetch:
            print(
                f"    Fetching job details for {len(needs_job_fetch)} successful run(s) …",
                flush=True,
            )
        for run_record in needs_job_fetch:
            try:
                count = _fetch_skipped_jobs_count(run_record["id"], base_api, token)
                merged[run_record["id"]]["skipped_jobs"] = count
            except HTTPError as exc:
                # Non-fatal: leave skipped_jobs as None; it will be retried next run.
                print(
                    f"    WARNING: Could not fetch jobs for run {run_record['id']} "
                    f"(HTTP {exc.code}) – will retry next collection.",
                    file=sys.stderr,
                )

        # Sort by started_at ascending
        sorted_runs = sorted(
            merged.values(),
            key=lambda r: r.get("started_at") or "",
        )

        result["workflows"][wf_id] = {
            "id": wf_id,
            "name": wf_name,
            "file": wf_file,
            "path": wf.get("path"),
            "state": wf.get("state"),
            "runs": sorted_runs,
        }

        # --- Flush to disk after every workflow so progress is never lost ----
        save_data(data_path, result)

    if failed_workflows:
        print(
            f"\nWARNING: {len(failed_workflows)} workflow(s) could not be fetched and were skipped:\n"
            + "\n".join(f"  • {w}" for w in failed_workflows),
            file=sys.stderr,
        )
        print("Re-run the script to retry the skipped workflows.", file=sys.stderr)

    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("ERROR: GITHUB_TOKEN environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    config_path = Path(os.environ.get("CONFIG_FILE", "actions-performance.toml"))
    data_path = Path(os.environ.get("DATA_FILE", "docs/data.json"))

    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    config = load_config(config_path)
    existing = load_existing_data(data_path)
    collect(config, existing, token, data_path)
    print("Done.")


if __name__ == "__main__":
    main()
