# offload-test-suite-badges

Automated Shields.io endpoint badge generator for [`llvm/offload-test-suite`](https://github.com/llvm/offload-test-suite), hosted in a personal public repository so it consumes zero LLVM organization GitHub Actions minutes.

## How It Works

1. `.github/workflows/update-badges.yaml` runs hourly (and on manual `workflow_dispatch`).
2. `update_badges.py` reads the workflow table from `llvm/offload-test-suite`'s `README.md`, queries the latest completed `main` run for each workflow, and extracts `PASS / total` from `lit` test output (`testresults.json` artifact if present, falling back to `lit`'s summary in the run logs).
3. Generated Shields.io endpoint JSON files (`<workflow-stem>.json` and `summary.json`) are published to the orphan [`badges`](https://github.com/farzonl/offload-test-suite-badges/tree/badges) branch.
4. `llvm/offload-test-suite`'s `README.md` renders each badge via:
   ```
   https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Ffarzonl%2Foffload-test-suite-badges%2Fbadges%2F<workflow-stem>.json
   ```

## Authentication Note (`OFFLOAD_READ_TOKEN`)

GitHub's API requires authentication to download Actions logs and artifacts across repositories (a repo's default `GITHUB_TOKEN` is scoped only to that repository). Set a read-only personal access token (no write permissions needed) as the repository secret **`OFFLOAD_READ_TOKEN`** in **Settings → Secrets and variables → Actions** so the hourly workflow can download `llvm/offload-test-suite` run logs.
