#!/usr/bin/env python3
"""
Send a failure notification webhook when a GitLab CI pipeline fails.
Called by notify:failure job in .gitlab-ci.yml.

Set NOTIFICATION_WEBHOOK_URL as a masked CI/CD variable.
Supports Slack-compatible incoming webhook format.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

webhook_url = os.environ.get("NOTIFICATION_WEBHOOK_URL", "")
if not webhook_url:
    print("NOTIFICATION_WEBHOOK_URL not set — skipping notification")
    sys.exit(0)

pipeline_url = os.environ.get("CI_PIPELINE_URL", "unknown")
pipeline_id  = os.environ.get("CI_PIPELINE_ID", "?")
job_name     = os.environ.get("CI_JOB_NAME", "unknown")
branch       = os.environ.get("CI_COMMIT_BRANCH", "unknown")
commit_sha   = os.environ.get("CI_COMMIT_SHA", "unknown")[:8]
commit_msg   = os.environ.get("CI_COMMIT_MESSAGE", "").split("\n")[0][:120]
project      = os.environ.get("CI_PROJECT_NAME", "ExaMLOps")

payload = json.dumps({
    "text": (
        f":red_circle: *{project}* — pipeline #{pipeline_id} FAILED on `{branch}`\n"
        f"*Failed job:* `{job_name}`\n"
        f"*Commit:* `{commit_sha}` — {commit_msg}\n"
        f"*Pipeline:* {pipeline_url}"
    )
}).encode()

req = urllib.request.Request(
    webhook_url,
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(f"Notification sent: HTTP {resp.status}", flush=True)
except urllib.error.URLError as exc:
    print(f"Failed to send notification: {exc}", file=sys.stderr)
    sys.exit(0)  # never fail the pipeline on a notification error
