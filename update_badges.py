#!/usr/bin/env python3
"""Fetch latest llvm/offload-test-suite CI test results and generate Shields.io badge JSONs.

For each workflow linked in llvm/offload-test-suite's README.md, this script
inspects the latest completed GitHub Actions run on `main`, extracts lit test
counts from `testresults.json` artifacts (falling back to parsing lit's summary
block from the run logs), and writes:

  - `<out-dir>/<workflow-stem>.json`: Shields.io endpoint badge JSON
  - `<out-dir>/summary.json`: full structured summary for all workflows
"""

from __future__ import annotations

import argparse
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone

README_WORKFLOW_RE = re.compile(
    r"\[!\[(?P<label>.*?)\].*?\]\(https://.*?/actions/workflows/(?P<workflow>[^)]+\.yaml)\)"
)

LIT_DISCOVERED_RE = re.compile(r"Total Discovered Tests:\s*(\d+)")
LIT_SUMMARY_LINE_RE = re.compile(
    r"^\s*(?:\d{4}-\d{2}-\d{2}T\S+\s+)?"
    r"(Passed With Re-Run|Passed|Expectedly Failed|Unexpectedly Passed|"
    r"Unsupported|Excluded|Skipped|Failed|Unresolved|Timed Out)\s*:\s*(\d+)"
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def resolve_token(explicit_token: str | None) -> str | None:
    if explicit_token:
        return explicit_token
    for env_var in ("OFFLOAD_READ_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        val = os.environ.get(env_var)
        if val:
            return val
    try:
        res = subprocess.run(
            ["gh", "auth", "token"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        token = res.stdout.strip()
        if token:
            return token
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    return None


class GitHubClient:
    def __init__(self, token: str | None) -> None:
        self.token = token
        self._no_redirect_opener = urllib.request.build_opener(_NoRedirectHandler())

    def _headers(self, include_auth: bool = True) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "offload-test-suite-badges",
        }
        if include_auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get_text(self, url: str) -> str:
        req = urllib.request.Request(
            url, headers={"User-Agent": "offload-test-suite-badges"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")

    def get_json(self, url: str) -> dict:
        req = urllib.request.Request(url, headers=self._headers(include_auth=True))
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def download_zip(self, url: str) -> bytes | None:
        """Download a GitHub API zip endpoint, stripping auth across redirects."""
        req = urllib.request.Request(url, headers=self._headers(include_auth=True))
        try:
            with self._no_redirect_opener.open(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as err:
            if err.code in (301, 302, 303, 307, 308):
                location = err.headers.get("Location")
                if not location:
                    return None
                dl_req = urllib.request.Request(
                    location, headers={"User-Agent": "offload-test-suite-badges"}
                )
                with urllib.request.urlopen(dl_req, timeout=60) as resp:
                    return resp.read()
            if err.code in (403, 404, 410):
                if err.code == 403:
                    print(
                        f"Warning: 403 Forbidden fetching {url} "
                        "(cross-repo log/artifact downloads require a read-only PAT in OFFLOAD_READ_TOKEN)",
                        file=sys.stderr,
                    )
                return None
            raise


def default_label(workflow: str) -> str:
    if "-clang-" in workflow:
        return "Clang"
    if "-dxc-" in workflow:
        return "DXC"
    return pathlib.Path(workflow).stem


def parse_readme_workflows(text: str) -> list[tuple[str, str]]:
    """Return a list of (workflow_filename, label) pairs from README.md text."""
    seen: set[str] = set()
    workflows: list[tuple[str, str]] = []
    for match in README_WORKFLOW_RE.finditer(text):
        workflow = match.group("workflow")
        if workflow in seen:
            continue
        seen.add(workflow)
        label = default_label(workflow)
        workflows.append((workflow, label))
    return workflows


def parse_lit_json(data: dict) -> dict | None:
    tests = data.get("tests")
    if not isinstance(tests, list):
        return None

    counts = {"PASS": 0, "FAIL": 0, "XFAIL": 0, "XPASS": 0, "UNSUPPORTED": 0}
    for test in tests:
        code = test.get("code", "UNKNOWN")
        if code in ("PASS", "FLAKYPASS"):
            counts["PASS"] += 1
        elif code == "XFAIL":
            counts["XFAIL"] += 1
        elif code == "XPASS":
            counts["XPASS"] += 1
        elif code == "UNSUPPORTED":
            counts["UNSUPPORTED"] += 1
        else:
            counts["FAIL"] += 1

    total = len(tests)
    pass_rate = round(100.0 * counts["PASS"] / total, 1) if total > 0 else 0.0
    return {
        "total": total,
        "passed": counts["PASS"],
        "failed": counts["FAIL"],
        "xfail": counts["XFAIL"],
        "xpass": counts["XPASS"],
        "unsupported": counts["UNSUPPORTED"],
        "passRate": pass_rate,
    }


def parse_lit_summary_from_text(text: str) -> dict | None:
    """Parse lit's summary block from log text, returning the largest suite found."""
    best: dict | None = None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = LIT_DISCOVERED_RE.search(lines[i])
        if not m:
            i += 1
            continue

        total = int(m.group(1))
        counts = {"PASS": 0, "FAIL": 0, "XFAIL": 0, "XPASS": 0, "UNSUPPORTED": 0}
        j = i + 1
        while j < len(lines):
            sm = LIT_SUMMARY_LINE_RE.match(lines[j])
            if not sm:
                break
            kind, raw_count = sm.group(1), int(sm.group(2))
            if kind in ("Passed", "Passed With Re-Run"):
                counts["PASS"] += raw_count
            elif kind == "Expectedly Failed":
                counts["XFAIL"] += raw_count
            elif kind == "Unexpectedly Passed":
                counts["XPASS"] += raw_count
            elif kind in ("Unsupported", "Excluded", "Skipped"):
                counts["UNSUPPORTED"] += raw_count
            else:
                counts["FAIL"] += raw_count
            j += 1

        if total > 0 and sum(counts.values()) > 0:
            candidate = {
                "total": total,
                "passed": counts["PASS"],
                "failed": counts["FAIL"],
                "xfail": counts["XFAIL"],
                "xpass": counts["XPASS"],
                "unsupported": counts["UNSUPPORTED"],
                "passRate": round(100.0 * counts["PASS"] / total, 1),
            }
            if best is None or candidate["total"] >= best["total"]:
                best = candidate
        i = j

    return best


def fetch_latest_completed_run(
    client: GitHubClient, repo: str, workflow: str, branch: str | None
) -> dict | None:
    base = f"https://api.github.com/repos/{repo}/actions/workflows/{urllib.parse.quote(workflow)}/runs"
    params = {"status": "completed", "per_page": "5"}
    if branch:
        params["branch"] = branch
    url = f"{base}?{urllib.parse.urlencode(params)}"
    try:
        data = client.get_json(url)
    except urllib.error.HTTPError as err:
        print(f"Warning: failed to list runs for {workflow}: {err}", file=sys.stderr)
        return None

    runs = data.get("workflow_runs", [])
    for run in runs:
        if run.get("conclusion") not in ("cancelled", "skipped"):
            return run
    return runs[0] if runs else None


def extract_results_from_artifacts(
    client: GitHubClient, repo: str, run_id: int
) -> dict | None:
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100"
    try:
        data = client.get_json(url)
    except urllib.error.HTTPError:
        return None

    best: dict | None = None
    for artifact in data.get("artifacts", []):
        name = artifact.get("name", "")
        if not name.startswith("test-results-") or artifact.get("expired"):
            continue
        dl_url = artifact.get("archive_download_url")
        if not dl_url:
            continue
        raw_zip = client.download_zip(dl_url)
        if not raw_zip:
            continue
        with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
            for member in zf.namelist():
                if not member.endswith("testresults.json"):
                    continue
                try:
                    parsed = parse_lit_json(json.loads(zf.read(member).decode("utf-8")))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if parsed and (best is None or parsed["total"] >= best["total"]):
                    best = parsed
    return best


def extract_results_from_logs(
    client: GitHubClient, repo: str, run_id: int
) -> dict | None:
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/logs"
    try:
        raw_zip = client.download_zip(url)
    except urllib.error.HTTPError:
        return None
    if not raw_zip:
        return None

    best: dict | None = None
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
        for member in zf.namelist():
            if not member.endswith(".txt"):
                continue
            try:
                text = zf.read(member).decode("utf-8", errors="replace")
            except KeyError:
                continue
            parsed = parse_lit_summary_from_text(text)
            if parsed and (best is None or parsed["total"] >= best["total"]):
                best = parsed
    return best


def color_for_pass_rate(pass_rate: float) -> str:
    if pass_rate >= 90.0:
        return "brightgreen"
    if pass_rate >= 75.0:
        return "green"
    if pass_rate >= 50.0:
        return "yellowgreen"
    if pass_rate >= 25.0:
        return "yellow"
    return "orange"


def build_badge_payload(
    label: str, conclusion: str | None, results: dict | None
) -> dict:
    if conclusion is None:
        return {
            "schemaVersion": 1,
            "label": label,
            "message": "no runs",
            "color": "lightgrey",
        }

    if results is not None:
        pass_rate = results["passRate"]
        if conclusion == "success":
            return {
                "schemaVersion": 1,
                "label": label,
                "message": f"{pass_rate:.1f}%",
                "color": color_for_pass_rate(pass_rate),
            }
        return {
            "schemaVersion": 1,
            "label": label,
            "message": f"{pass_rate:.1f}% (fail)",
            "color": "red",
        }

    if conclusion == "success":
        return {
            "schemaVersion": 1,
            "label": label,
            "message": "passing",
            "color": "green",
        }
    if conclusion in ("cancelled", "skipped"):
        return {
            "schemaVersion": 1,
            "label": label,
            "message": conclusion,
            "color": "lightgrey",
        }
    return {
        "schemaVersion": 1,
        "label": label,
        "message": "build failed",
        "color": "red",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        default="llvm/offload-test-suite",
        help="Target GitHub repository in owner/name form (default: %(default)s)",
    )
    parser.add_argument(
        "--branch",
        default="main",
        help="Branch to query workflow runs for (default: %(default)s)",
    )
    parser.add_argument(
        "--readme",
        default=None,
        help="Local path or URL to README.md (default: raw README.md from --repo/--branch)",
    )
    parser.add_argument(
        "--out-dir",
        type=pathlib.Path,
        required=True,
        help="Directory to write <workflow-stem>.json badge files and summary.json",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="GitHub API token (defaults to OFFLOAD_READ_TOKEN, GITHUB_TOKEN, GH_TOKEN, or `gh auth token`)",
    )
    args = parser.parse_args(argv)

    token = resolve_token(args.token)
    if not token:
        print(
            "Warning: no GitHub token found; artifact/log downloads require authentication.",
            file=sys.stderr,
        )

    client = GitHubClient(token)
    if args.readme is None:
        readme_url = (
            f"https://raw.githubusercontent.com/{args.repo}/{args.branch}/README.md"
        )
        readme_text = client.get_text(readme_url)
    elif args.readme.startswith(("http://", "https://")):
        readme_text = client.get_text(args.readme)
    else:
        readme_text = pathlib.Path(args.readme).read_text(encoding="utf-8")

    workflows = parse_readme_workflows(readme_text)
    if not workflows:
        print("error: no workflows found in README.md", file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, dict] = {}
    generated_at = datetime.now(timezone.utc).isoformat()

    for workflow, label in workflows:
        stem = pathlib.Path(workflow).stem
        run = fetch_latest_completed_run(client, args.repo, workflow, args.branch)
        if run is None and args.branch:
            run = fetch_latest_completed_run(client, args.repo, workflow, None)

        conclusion = run.get("conclusion") if run else None
        run_id = run.get("id") if run else None
        run_url = run.get("html_url") if run else None
        updated_at = run.get("updated_at") if run else None

        results = None
        source = None
        if run_id is not None:
            results = extract_results_from_artifacts(client, args.repo, run_id)
            if results is not None:
                source = "artifact"
            else:
                results = extract_results_from_logs(client, args.repo, run_id)
                if results is not None:
                    source = "logs"

        badge = build_badge_payload(label, conclusion, results)
        badge_path = args.out_dir / f"{stem}.json"
        badge_path.write_text(json.dumps(badge, indent=2) + "\n", encoding="utf-8")

        summary[stem] = {
            "workflow": workflow,
            "label": label,
            "conclusion": conclusion,
            "runId": run_id,
            "runUrl": run_url,
            "updatedAt": updated_at,
            "source": source,
            "results": results,
            "badge": badge,
        }
        print(
            f"{stem}: {badge['message']} [{badge['color']}] "
            f"(conclusion={conclusion}, source={source})"
        )

    summary_path = args.out_dir / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "generatedAt": generated_at,
                "repository": args.repo,
                "branch": args.branch,
                "workflows": summary,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
