"""A tenant-scoped table keyed by a name only one tenant can own.

`compliance_systems`, `fairness_config` and `challenger_config` each carry a `tenant` column and are
keyed `PRIMARY KEY(model)`. A model name is not unique across tenants, so two tenants cannot both
hold a row for "JPCP" — and the writers find the row by model alone, so the second write does not
fail, it **overwrites the first and reassigns its tenant**:

    set_compliance_system("JPCP", tenant="centre-a", risk_tier="high")    # A declares it high-risk
    set_compliance_system("JPCP", tenant="centre-b", risk_tier="minimal") # B declares its own
    → one row: {"model": "JPCP", "tenant": "centre-b", "risk_tier": "minimal"}

Centre A's EU-AI-Act declaration is gone, silently. That is worse than the cross-tenant *read* this
was found next to (`test_dashboard_tenant_scoping.py`): a read leaks, this destroys.

**The real fix is a schema change** — identity `(model, tenant)` on all three, plus the helpers that
look rows up by model — and on an existing install that is a table rebuild, i.e. a **breaking**
migration through `examlops.lifecycle.migrations` + `exa upgrade apply`. Breaking migrations raise
`min_reader_format` and lock older releases out of the data, which is a release decision for the
owner rather than something to slip in. So this guard does the next best thing: it pins **exactly**
which tables have the problem, so the list cannot quietly grow while the fix is pending.

Not every key that omits the tenant is a bug. `agent_sessions(session_id)`,
`reasoning_traces(request_id)` and `virtual_keys(key_hash)` are keyed by **surrogate** identifiers —
a session id, a request id, a key hash — which are unique by construction and never collide between
tenants. The distinction this guard draws is between a *natural* key (a name a human chose, which
two tenants can both choose) and a surrogate one.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLATFORM_DB = ROOT / "platform" / "cli" / "src" / "examlops" / "platform_db.py"

#: Tenant-scoped tables whose identity key is a **natural** name, so two tenants collide.
#: Shrinks when the `(model, tenant)` migration lands. Never add to it without the same analysis.
COLLIDING: dict[str, str] = {
    "compliance_systems": "PRIMARY KEY(model) — EU-AI-Act register; B's write overwrites A's",
    "fairness_config": "PRIMARY KEY(model) — per-model fairness policy",
    "challenger_config": "PRIMARY KEY(model) — per-model challenger policy",
}

#: Keys that omit the tenant and are **fine**, because the key column is a surrogate id.
#: `suspend_snapshots.snapshot_id` is a random uuid4 hex minted per snapshot (ADR 0109), so two
#: tenants cannot collide on it.
SURROGATE_OK = {"agent_sessions", "reasoning_traces", "virtual_keys", "suspend_snapshots"}


def _tenant_tables_with_keys() -> dict[str, list[str]]:
    """`{table: [key labels]}` for tenant-scoped tables that declare an identity key."""
    text = PLATFORM_DB.read_text(encoding="utf-8")
    out: dict[str, list[str]] = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\s*\);", text, re.DOTALL):
        name, body = m.group(1), m.group(2)
        if not re.search(r"^\s*tenant\b", body, re.MULTILINE):
            continue
        keys: list[tuple[str, list[str]]] = []
        for col in re.findall(r"^\s*(\w+)\s+[A-Z]+\s+PRIMARY KEY", body, re.MULTILINE):
            keys.append((f"PRIMARY KEY({col})", [col]))
        for grp in re.findall(r"PRIMARY KEY\s*\(([^)]*)\)", body):
            keys.append((f"PRIMARY KEY({grp.strip()})", [c.strip() for c in grp.split(",")]))
        for grp in re.findall(r"UNIQUE\s*\(([^)]*)\)", body):
            keys.append((f"UNIQUE({grp.strip()})", [c.strip() for c in grp.split(",")]))
        # A surrogate row id never collides, so it is not an identity claim about a tenant's data.
        real = [(lab, cols) for lab, cols in keys if cols not in (["id"], ["rowid"])]
        if real and not any("tenant" in cols for _, cols in real):
            out[name] = [lab for lab, _ in real]
    assert out, "no tenant-scoped tables parsed at all — the DDL's shape changed, not the schema"
    return out


def test_no_new_table_is_keyed_without_its_tenant():
    found = _tenant_tables_with_keys()
    new = {k: v for k, v in found.items() if k not in COLLIDING and k not in SURROGATE_OK}
    assert not new, (
        "these tables carry a `tenant` column but are keyed without it, so two tenants cannot both "
        "hold a row and the second write collides with the first:\n  "
        + "\n  ".join(f"{k}: {', '.join(v)}" for k, v in sorted(new.items()))
        + "\nKey them `(name, tenant)`. If the key is a surrogate id that cannot collide, add the "
        "table to SURROGATE_OK with that reasoning."
    )


def test_the_recorded_collisions_are_still_real():
    """When the migration lands, these stop reproducing — delete them, so the list only shrinks."""
    found = _tenant_tables_with_keys()
    fixed = sorted(set(COLLIDING) - set(found))
    assert not fixed, (
        f"{fixed} are recorded as colliding but are now keyed with their tenant. Remove them from "
        "COLLIDING — a list that outlives the problem stops being read."
    )


def test_the_surrogate_exemptions_still_exist():
    found = _tenant_tables_with_keys()
    stale = sorted(SURROGATE_OK - set(found))
    assert not stale, f"SURROGATE_OK names tables that no longer match: {stale}"


def test_a_second_tenant_overwrites_the_first(tmp_path, monkeypatch):
    """The behaviour itself, so the cost is visible rather than inferred from a schema.

    This asserts what the platform does **today**, which is wrong. It is written as a demonstration
    on purpose: when the `(model, tenant)` migration lands, this test fails, and the person who
    lands it updates it to assert the two rows that should exist.
    """
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import data as pdb

    pdb.init_db()
    pdb.set_compliance_system("JPCP", tenant="centre-a", risk_tier="high", updated_by="alice")
    pdb.set_compliance_system("JPCP", tenant="centre-b", risk_tier="minimal", updated_by="bob")

    with pdb.get_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM compliance_systems").fetchall()]

    assert len(rows) == 1, (
        "two tenants now each have their own compliance record — the migration has landed, so "
        "rewrite this test to assert that, and clear the entry in COLLIDING"
    )
    assert rows[0]["tenant"] == "centre-b" and rows[0]["risk_tier"] == "minimal", (
        "centre-a's declaration was not overwritten in the way this documents; re-check the writer"
    )
