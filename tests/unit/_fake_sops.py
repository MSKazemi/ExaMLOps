"""A stand-in ``sops`` executable for the SOPS backend tests (ADR 0011 clause 1).

It reproduces the parts of sops 3.x the backend relies on — argv shape, exit codes and the stderr
wording the backend classifies — against a *plaintext* JSON document, so the tests exercise the
real subprocess path without the sops/age binaries:

* ``decrypt --extract '["a"]["b"]' FILE`` → the scalar on stdout, no trailing newline; a missing key
  exits 1 with ``error truncating tree: component ['b'] not found``; no identity exits 128 with
  sops' "Failed to get the data key" text.
* ``set --value-stdin FILE '["a"]["b"]'`` → reads a JSON value from stdin (exit 7 on invalid JSON,
  the real code) and writes it into the nested document.
* ``--version`` → a version line.

The identity is modelled by ``FAKE_SOPS_IDENTITY`` (unset → decryption fails, like a missing
``SOPS_AGE_KEY_FILE``). Every argv is appended to ``FAKE_SOPS_ARGV_LOG`` when set, so a test can
prove a secret value never reached the command line.
"""

from __future__ import annotations

import json
import os
import re
import sys

_SEG = re.compile(r'\[("(?:[^"\\]|\\.)*")\]')


def _index(expr: str) -> list[str]:
    return [json.loads(m) for m in _SEG.findall(expr)]


def main(argv: list[str]) -> int:
    log = os.environ.get("FAKE_SOPS_ARGV_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(argv) + "\n")
    if argv and argv[0] == "--version":
        print("sops 3.13.3 (fake)")
        return 0
    if not argv:
        return 2
    cmd = argv[0]
    if cmd == "decrypt" and argv[1] == "--extract":
        index, path = argv[2], argv[3]
        if not os.path.exists(path):
            print(f'Error: cannot operate on non-existent file "{path}"', file=sys.stderr)
            return 100
        if os.environ.get("FAKE_SOPS_IDENTITY") != "ok":
            print("Failed to get the data key required to decrypt the SOPS file.", file=sys.stderr)
            return 128
        node = json.load(open(path, encoding="utf-8"))
        for seg in _index(index):
            if not isinstance(node, dict) or seg not in node:
                print(f"error truncating tree: component ['{seg}'] not found", file=sys.stderr)
                return 1
            node = node[seg]
        sys.stdout.write(node if isinstance(node, str) else json.dumps(node))
        return 0
    if cmd == "set" and argv[1] == "--value-stdin":
        path, index = argv[2], argv[3]
        if os.environ.get("FAKE_SOPS_IDENTITY") != "ok":
            print("Failed to get the data key required to decrypt the SOPS file.", file=sys.stderr)
            return 128
        try:
            value = json.loads(sys.stdin.read())
        except ValueError:
            print("Value for --set is not valid JSON", file=sys.stderr)
            return 7
        doc = json.load(open(path, encoding="utf-8"))
        node = doc
        segs = _index(index)
        for seg in segs[:-1]:
            node = node.setdefault(seg, {})
        node[segs[-1]] = value
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return 0
    print(f"unsupported fake sops invocation: {argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
