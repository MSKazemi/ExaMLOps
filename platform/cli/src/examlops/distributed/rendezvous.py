"""Rendezvous host from the scheduler's node list (ADR 0032 decision 1).

A scheduler-launched multi-node job learns its allocation only at run time — Slurm's
``$SLURM_JOB_NODELIST``, Flux's ``flux getattr hostlist`` — in the compressed hostlist syntax
(``gpu[01-04,07],login2``). torch elastic needs one reachable ``host:port`` that every node agrees on;
the first host of the allocation is the conventional choice. The generated job script runs::

    HEAD="$(python -m examlops.distributed.rendezvous "$SLURM_JOB_NODELIST")"

Only the *first* host is needed, so this parses just enough of the syntax to find it, and refuses
anything that is not a plain hostname — the value is interpolated into the torchrun command line.
"""

from __future__ import annotations

import re
import sys

_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


def first_host(hostlist: str) -> str:
    """``"gpu[07-09,11],x1"`` → ``"gpu07"``; ``"a,b"`` → ``"a"``. Raises ``ValueError`` if malformed."""
    text = (hostlist or "").strip()
    if not text:
        raise ValueError("empty node list")
    # The first top-level comma ends the first entry; commas inside [...] belong to its range.
    depth, end = 0, len(text)
    for i, ch in enumerate(text):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == "," and depth == 0:
            end = i
            break
    entry = text[:end]
    m = re.match(r"^([^\[\]]*)\[([^\]]*)\](.*)$", entry)
    if m:
        prefix, ranges, suffix = m.groups()
        first = ranges.split(",", 1)[0].split("-", 1)[0].strip()
        if not first.isdigit():
            raise ValueError(f"malformed hostlist range in {entry!r}")
        entry = f"{prefix}{first}{suffix}"
    if not _HOST_RE.match(entry):
        raise ValueError(f"not a hostname: {entry!r}")
    return entry


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m examlops.distributed.rendezvous <hostlist>", file=sys.stderr)
        return 2
    try:
        print(first_host(args[0]))
    except ValueError as exc:
        print(f"rendezvous: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
