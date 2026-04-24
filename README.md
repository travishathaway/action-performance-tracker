# Action Performance Tracker

A self-hosted GitHub Actions dashboard that collects workflow run statistics (duration, outcome) for any GitHub repository and displays them as interactive time-series charts on a GitHub Pages site.

---

## Overview

| File / Directory | Purpose |
|---|---|
| `actions-performance.toml` | Configuration: target repo, site title, etc. |
| `scripts/collect_runs.py` | Python script (run via `uv run`) that calls the GitHub API and writes `docs/data.json` |
| `.github/workflows/collect-and-deploy.yml` | Daily + on-demand workflow: installs uv, collects data, and deploys the site |
| `docs/index.html` | Single-page Plotly dashboard |
| `docs/data.json` | Auto-generated data file (committed by the workflow) |

---

## Dependencies

The collection script uses [uv](https://docs.astral.sh/uv/) as its runner. `uv` is installed in the workflow via the official `astral-sh/setup-uv` action — no manual installation is needed in CI.

The script carries its own dependency metadata ([PEP 723 inline script metadata](https://peps.python.org/pep-0723/)) at the top of `scripts/collect_runs.py`:

```python
# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "tomli>=2.0; python_version < '3.11'",
# ]
# ///
```

`uv run` reads this block, creates an isolated ephemeral environment, installs `tomli` when needed (Python < 3.11), and executes the script — no `requirements.txt` or `pyproject.toml` required.

To run the script locally:

```bash
uv run scripts/collect_runs.py
```

---

## Quick Start

### 1. Create a new repository from this template

Click **Use this template → Create a new repository** on GitHub, or copy all files into a new repo.

### 2. Edit `actions-performance.toml`

Open `actions-performance.toml` and set the repository you want to track:

```toml
[site]
title       = "My Project – CI Performance"
description = "Duration and success rates for all GitHub Actions in my-org/my-repo."

[repository]
owner = "my-org"
repo  = "my-repo"

# Optional: restrict to specific workflow files
# workflows = ["ci.yml", "release.yml"]

[collection]
# Days of history to fetch on the very first run (max ~90 before rate limits bite)
initial_lookback_days = 90
```

Commit and push this change.

### 3. Create a Personal Access Token (PAT)

The collection script needs read access to the **target repository's** Actions API.

1. Go to **GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens**.
2. Click **Generate new token**.
3. Set the resource owner to the organisation / account that owns the target repo.
4. Under *Repository access*, select the target repository (or all repositories).
5. Under *Permissions*, grant:
   - **Actions** → Read-only
6. Copy the generated token.

> **Same-repo tracking**: If this tracker repo and the target repo are the same, the built-in `GITHUB_TOKEN` has sufficient permissions and you can skip the PAT. Just remove the `DATA_COLLECTION_TOKEN` secret reference from the workflow (or leave it – it falls back gracefully).

### 4. Add the token as a repository secret

In **this** tracker repository:

1. Go to **Settings → Secrets and variables → Actions**.
2. Click **New repository secret**.
3. Name: `DATA_COLLECTION_TOKEN`
4. Value: paste the PAT from step 3.
5. Click **Add secret**.

### 5. Enable GitHub Pages

1. Go to **Settings → Pages** in this repository.
2. Under *Source*, select **GitHub Actions**.
3. Save.

### 6. Run the workflow for the first time

Go to **Actions → Collect Action Performance Data → Run workflow**.

- Leave *Force full refresh* as `false` unless you want to re-fetch everything from scratch.
- Click **Run workflow**.

The workflow will:
1. Fetch up to `initial_lookback_days` (90 by default) of historical run data from the target repository.
2. Write the results to `docs/data.json` and commit it back to `main`.
3. Deploy the `docs/` folder to GitHub Pages.

Your dashboard will be live at:

```
https://<your-github-username>.github.io/<this-repo-name>/
```

---

## Daily updates

After the initial run the workflow runs automatically every day at **02:00 UTC**. It performs an **incremental update**: only runs newer than the latest already-recorded run (with a 2-day overlap buffer) are fetched, so API calls stay well within rate limits.

---

## Configuration reference

### `[site]`

| Key | Required | Description |
|---|---|---|
| `title` | no | Dashboard heading |
| `description` | no | Sub-heading / description |

### `[repository]`

| Key | Required | Description |
|---|---|---|
| `owner` | **yes** | GitHub organisation or user name |
| `repo` | **yes** | Repository name |
| `workflows` | no | List of workflow file names to include (e.g. `["ci.yml"]`). Omit or leave empty to track **all** workflows. |

### `[collection]`

| Key | Default | Description |
|---|---|---|
| `initial_lookback_days` | `90` | How far back to fetch on the first (or full-refresh) run |

---

## Forcing a full refresh

If you want to re-fetch all data from scratch (e.g. you extended `initial_lookback_days`):

1. Go to **Actions → Collect Action Performance Data → Run workflow**.
2. Set *Force full refresh* to `true`.
3. Run.

This deletes `docs/data.json` before collecting, so all history is re-fetched.

---

## Cross-repository tracking

Because this tracker repository is separate from the repository being tracked, the `GITHUB_TOKEN` provided automatically by GitHub Actions only has access to **this** repository. You must create a PAT with `Actions: read` access to the target repository and store it as `DATA_COLLECTION_TOKEN` (see step 3 and 4 above).

---

## Dashboard features

- **Time range selector**: view the last 7 / 30 / 90 / 180 days or all-time data.
- **Metric selector**: switch between *duration* scatter plot and *run count per day* bar chart.
- **Workflow tabs**: filter all charts to a single workflow or view all at once.
- **Summary cards**: total runs, successes, failures, cancellations and average duration.
- **Outcome stacked bar chart**: daily run counts broken down by conclusion.
- **Average duration bar chart**: compare mean runtimes across workflows.

---

## Troubleshooting

### The workflow fails with "HTTP 401" or "HTTP 403"

- Check that `DATA_COLLECTION_TOKEN` is set and has not expired.
- Verify the token has `Actions: read` permission on the target repository.

### No data appears after the first run

- Confirm the `owner` and `repo` fields in `actions-performance.toml` are correct.
- Check the workflow run logs for errors from `collect_runs.py`.

### GitHub Pages shows a 404

- Ensure the Pages source is set to **GitHub Actions** (not a branch).
- Wait a minute or two after the first deploy for DNS to propagate.

### `uv` is not found locally

Install uv by following the [official instructions](https://docs.astral.sh/uv/getting-started/installation/):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Rate limits

The GitHub REST API allows 5 000 requests per hour for authenticated requests. Each paginated page of 100 runs costs one request. For a repository with many workflows and a large backlog you may hit the limit on the initial run. Reduce `initial_lookback_days` or split the initial import into multiple manual runs (with *full refresh* left as `false` so each run picks up where the last left off).
