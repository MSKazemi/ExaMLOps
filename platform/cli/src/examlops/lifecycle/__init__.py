"""Instance lifecycle — the seams between the core, the deployment and the instance data (ADR 0128).

* :mod:`~examlops.lifecycle.datadir` — the instance-data layer: one optional data root
  (``EXAMLOPS_DATA_DIR``), its layout, and an inventory of every place user data lives.
* :mod:`~examlops.lifecycle.dataformat` / :mod:`~examlops.lifecycle.migrations` — the data-format
  stamp carried by every datastore, the compatibility rule a release applies before it opens
  the data, and ordered data migrations.
* :mod:`~examlops.lifecycle.upgrade` — plan and apply an upgrade, with a backup first.
* :mod:`~examlops.lifecycle.modules` — the site feature profile: which modules a centre runs,
  enforced by the CLI, the dashboard, Compose and Helm.

Every submodule imports lazily from the rest of the platform, so ``platform_db`` can call into
the stamp without an import cycle.
"""

from __future__ import annotations

__all__ = ["datadir", "dataformat", "migrations", "modules", "upgrade"]
