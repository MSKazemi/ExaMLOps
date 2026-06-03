#!/usr/bin/env python3
"""
ci/notify_model_changes.py

Stdlib-only script that detects which ExaMLOps models changed between two git
SHAs and notifies the Control Plane via POST /api/changes.

Usage (called from GitHub Actions):
    python ci/notify_model_changes.py \\
        --before "${{ github.event.before }}" \\
        --after  "${{ github.sha }}" \\
        --commit-msg "${{ github.event.head_commit.message }}"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Patterns that identify model-related source files
# ---------------------------------------------------------------------------
MODEL_FILE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^modelzoo/seanergys_modelzoo/models/tasks/[^/]+\.py$"),
    re.compile(r"^pipelines/model_configs/[^/]+_config\.py$"),
    re.compile(r"^pipelines/model_registry\.yaml$"),
]

REGISTRY_YAML = "pipelines/model_registry.yaml"

# Matches:  model_id = "JPCP"   or   model_id: ClassVar[str] = "JPCP"
MODEL_ID_RE = re.compile(r'model_id\s*(?::\s*ClassVar\[str\])?\s*=\s*"([^"]+)"')


def _is_all_zeros(sha: str) -> bool:
    return sha == "0" * len(sha)


def _git(*args: str) -> str:
    """Run a git command and return stripped stdout."""
    result = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def get_changed_files(before: str, after: str) -> list[str]:
    """Return files changed between *before* and *after* SHAs."""
    if _is_all_zeros(before):
        # First push — no previous commit; list files added in the latest commit.
        output = _git(
            "log",
            "--diff-filter=A",
            "--name-only",
            "--format=",
            "-1",
            after,
        )
    else:
        output = _git("diff", "--name-only", f"{before}..{after}")
    return [line for line in output.splitlines() if line]


def filter_model_files(changed: list[str]) -> list[str]:
    """Keep only files that match any MODEL_FILE_PATTERNS."""
    matched: list[str] = []
    for path in changed:
        for pattern in MODEL_FILE_PATTERNS:
            if pattern.match(path):
                matched.append(path)
                break
    return matched


def extract_model_id(file_path: str) -> str | None:
    """
    Read *file_path* from disk and return the value of the first
    ``model_id = "..."`` assignment found, or None.
    """
    try:
        text = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = MODEL_ID_RE.search(text)
    return match.group(1) if match else None


def collect_model_ids(model_files: list[str]) -> tuple[list[str], bool]:
    """
    Scan each matched file for a model_id.

    Returns:
        (model_ids, registry_changed)
        where registry_changed is True when model_registry.yaml is in the list.
    """
    ids: list[str] = []
    registry_changed = REGISTRY_YAML in model_files

    for fpath in model_files:
        if fpath == REGISTRY_YAML:
            continue  # handled separately
        mid = extract_model_id(fpath)
        if mid and mid not in ids:
            ids.append(mid)

    return ids, registry_changed


def post_notification(
    url: str,
    token: str,
    model_ids: list[str],
    commit_sha: str,
    commit_msg: str,
    changed_files: list[str],
) -> int:
    """
    POST to {url}/api/changes.  Returns the HTTP status code.
    Raises urllib.error.URLError on connection problems.
    """
    payload = json.dumps(
        {
            "model_ids": model_ids,
            "commit_sha": commit_sha,
            "commit_msg": commit_msg,
            "changed_files": changed_files,
        }
    ).encode()

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    endpoint = url.rstrip("/") + "/api/changes"
    req = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode(errors="replace")
            status = resp.status
            print(f"Control plane responded {status}: {body}")
            return status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        print(f"Control plane HTTP error {exc.code}: {body}", file=sys.stderr)
        return exc.code


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect changed ExaMLOps models and notify the Control Plane."
    )
    parser.add_argument("--before", required=True, help="Previous commit SHA (or all-zeros on first push)")
    parser.add_argument("--after", required=True, help="Current commit SHA")
    parser.add_argument("--commit-msg", default="", help="Commit message")
    parser.add_argument(
        "--control-plane-url",
        default=os.environ.get("CONTROL_PLANE_URL", "http://localhost:18002"),
        help="Control Plane base URL (env: CONTROL_PLANE_URL)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("CONTROL_PLANE_TOKEN", ""),
        help="Bearer token for the Control Plane (env: CONTROL_PLANE_TOKEN)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # 1. Discover changed files
    try:
        changed_files = get_changed_files(args.before, args.after)
    except subprocess.CalledProcessError as exc:
        print(f"git command failed: {exc.stderr}", file=sys.stderr)
        return 1

    # 2. Filter to model-related files only
    model_files = filter_model_files(changed_files)

    if not model_files:
        print("No model changes detected, skipping notification.")
        return 0

    # 3. Extract model_ids from the matched files
    model_ids, registry_changed = collect_model_ids(model_files)

    # 4. If only the registry YAML changed (no individual model files with model_id)
    if not model_ids and registry_changed:
        model_ids = ["__registry__"]

    if not model_ids:
        print("No model changes detected, skipping notification.")
        return 0

    print(f"Detected model changes: {model_ids}")
    print(f"Changed files: {model_files}")

    # 5. POST to the control plane
    try:
        status = post_notification(
            url=args.control_plane_url,
            token=args.token,
            model_ids=model_ids,
            commit_sha=args.after,
            commit_msg=args.commit_msg,
            changed_files=model_files,
        )
    except urllib.error.URLError as exc:
        print(
            f"Warning: could not reach control plane at {args.control_plane_url}: {exc.reason}. "
            "Skipping notification.",
            file=sys.stderr,
        )
        return 0

    return 0 if status in (200, 201) else 1


if __name__ == "__main__":
    sys.exit(main())
