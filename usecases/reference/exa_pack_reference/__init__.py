"""Entry-point shim that graduates the reference pack into an installable package.

Installing this package (``pip install -e usecases/reference``) registers the pack under the
``examlops.usecase_packs`` entry-point group, so the platform discovers it with no
``EXAMLOPS_USECASE_DIR`` set (see ``examlops.usecase`` / ADR 0094 Stage 5).

``pack_root()`` returns the pack directory that holds ``models/``, ``model_configs/``, and
``datasets/`` — for an editable install this is the live source tree.
"""

from __future__ import annotations

from pathlib import Path


def pack_root() -> str:
    """Absolute path to the pack root (the directory containing ``models/``)."""
    return str(Path(__file__).resolve().parent.parent)
