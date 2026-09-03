#!/usr/bin/env python3
"""Record a production release activation in the platform's own audit chain.

The platform keeps a tamper-evident, hash-chained audit log of what it does to itself —
retrains, promotions, approvals, autopilot cycles. Deployment was the one production change
that never appeared in it. So `exa audit` could tell you that a model was promoted at 14:02
and say nothing about the release that changed underneath it at 14:00, which is exactly the
correlation an incident review needs.

Run on the deploy node by ``lxp_release.sh`` after the symlink flip, for BOTH paths:

* ``deploy``   — a new release was activated by a pipeline;
* ``rollback`` — a previous release was reactivated, by ``smoke:lxp`` automatically or by
  the manual ``rollback:lxp`` job. A rollback is a production change too, and leaving it out
  would make the log say the bad release was still running.

**Failure here must never fail a deploy.** A missing audit row is a gap in the record; a
deploy aborted over telemetry is an outage. So every failure is caught, reported loudly on
stdout so it lands in the CI job log, and exits 0. That trade is deliberate and is the
reason this is a separate script rather than an inline call.
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--action", required=True, choices=["deploy", "rollback"])
    ap.add_argument("--release", required=True, help="absolute path of the activated release")
    ap.add_argument("--sha", default="", help="commit SHA the release was built from")
    ap.add_argument("--image-tag", default="", help="image tag the release is pinned to, if any")
    ap.add_argument("--actor", default="", help="who caused it (CI user, or the operator)")
    args = ap.parse_args()

    try:
        from examlops.data.audit import write_audit_event
    except Exception as exc:  # noqa: BLE001 — any import problem is non-fatal by design
        print(f"warn: deploy not audited (examlops not importable: {exc})")
        return 0

    details = {
        "release_path": args.release,
        "commit_sha": args.sha,
        # Recorded even when empty: "this release was built on the node" is itself a fact
        # worth having in the log once some releases are pinned to registry images.
        "image_tag": args.image_tag,
        "deploy_node": os.uname().nodename,
    }

    try:
        write_audit_event(
            source="gitlab-ci",
            actor=args.actor or os.environ.get("EXAMLOPS_ACTOR") or "ci",
            action=f"release_{args.action}",
            target=args.sha or args.release,
            details=details,
        )
    except Exception as exc:  # noqa: BLE001 — see the module docstring
        print(f"warn: deploy not audited (audit write failed: {exc})")
        return 0

    print(f"audited: release_{args.action} {args.sha or args.release}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
