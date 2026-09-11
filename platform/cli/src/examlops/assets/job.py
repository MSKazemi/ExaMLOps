"""Run one asset's production function inside a scheduler job (ADR 0036 clause 3).

``python -m examlops.assets.job --asset NAME --entrypoint module:qualname --upstream JSON``

This is what the ``scheduler`` orchestrator's generated ``run.sh`` executes on the cluster. It
imports the production function, calls it with the upstream versions it was built from, and exits
0 on success or non-zero on failure — nothing else. It deliberately does **not** materialize: the
submitting process waits for this job and records the version, so the job needs no access to
``platform.db`` and cannot walk the graph and submit more jobs.

Importing the function's module re-runs any ``@asset`` decorators in it. Declarations are turned
off first, so that import is a pure lookup rather than a write to a datastore the job may not
reach.

Exit codes: 0 built · 1 the production function raised · 2 bad arguments · 3 the entrypoint does
not resolve.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
import traceback
from typing import Any

# module path ':' attribute path — identifiers and dots only, nothing a shell or importer could
# read as anything else.
_ENTRYPOINT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_.]*$")


def resolve(entrypoint: str) -> Any:
    """Import ``module:qualname`` and return the callable it names."""
    module, _, qualname = entrypoint.partition(":")
    obj: Any = importlib.import_module(module)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    if not callable(obj):
        raise TypeError(f"{entrypoint} is not callable")
    return obj


def _upstream(raw: str) -> dict[str, int]:
    data = json.loads(raw)
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
        for k, v in data.items()
    ):
        raise ValueError("--upstream must be a JSON object of asset name → version")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m examlops.assets.job")
    parser.add_argument("--asset", required=True)
    parser.add_argument("--entrypoint", required=True)
    parser.add_argument("--upstream", default="{}")
    args = parser.parse_args(argv)

    if not _ENTRYPOINT.match(args.entrypoint):
        print(f"[asset-job] invalid entrypoint {args.entrypoint!r}", file=sys.stderr)
        return 2
    try:
        upstream = _upstream(args.upstream)
    except ValueError as exc:
        print(f"[asset-job] {exc}", file=sys.stderr)
        return 2

    from examlops import assets

    assets._PERSIST_DECLARATIONS = False
    try:
        fn = resolve(args.entrypoint)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        print(f"[asset-job] cannot resolve {args.entrypoint}", file=sys.stderr)
        return 3

    print(f"[asset-job] building {args.asset} via {args.entrypoint}", flush=True)
    try:
        fn(**upstream)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        print(f"[asset-job] {args.asset} failed", file=sys.stderr)
        return 1
    print(f"[asset-job] {args.asset} built", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
