"""One helper, no imports — deliberately a leaf module.

``platform_db`` imports the per-domain modules and ``data/__init__`` imports ``platform_db``,
so anything the domain modules need at import time cannot live in either of those without a
cycle. This file is where such a helper goes.
"""

from __future__ import annotations

from typing import Any


def last_insert_id(cur: Any) -> int:
    """The row id sqlite assigned to the INSERT just executed on ``cur``.

    ``sqlite3.Cursor.lastrowid`` is ``int | None`` because it is only meaningful after an
    INSERT — after a SELECT or a DDL statement it is ``None``. Every caller here has just
    run an INSERT, so the ``None`` is impossible; saying that once, out loud, is better than
    each caller casting it away and leaving a ``None`` to surface later as a row id.
    """
    rowid = cur.lastrowid
    if rowid is None:  # pragma: no cover - unreachable after an INSERT
        raise RuntimeError("INSERT returned no row id — the cursor did not run an INSERT")
    return int(rowid)
