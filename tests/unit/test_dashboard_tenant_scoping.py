"""A route that reads a tenant-scoped table must scope it to the caller — a ratchet.

The platform ships a default-deny tenant filter (`capabilities.tenant_visible` and its wrappers
`assert_tenant_access` / `scope_to_tenant`, F15 R4, ADR 0057) and
`docs/guides/dashboard-auth-tenancy.md` describes it as enforced. On 2026-09-13 those functions had
**no callers at all** outside their own unit test: 62 routers, zero uses. The control existed, was
tested, was documented, and governed nothing.

Two things had to be true at once for that to happen, and both were:

1. `tenant_visible` defaulted a **missing** tenant to `"default"` on the resource but not on the
   principal — and routes are handed the raw token payload, which for a locally issued token has no
   `tenant` claim. Wiring the filter into a route therefore emptied the page, which is a strong
   reason not to wire it in.
2. Routes bound the principal to `_`, so the caller's tenant was not even in scope to filter by.

`GET /api/slo` was the proof: it selected every row of `slo_specs` with no `WHERE`, so every viewer
of every tenant saw every other tenant's SLO definitions, `sli_query` included — a Prometheus
expression carrying that tenant's metric and label names. `GET /api/secrets` did the same for secret
*paths* and `GET /api/gateway/keys` for virtual-key projects, budgets and spend.

This guard is a **ratchet**, not a pin: the count may fall and never rise. The entries still in
`UNSCOPED` are the ones not yet fixed — recorded here on purpose, because a known list is worth more
than a clean-looking suite, and "we have a tenant filter" was exactly the belief that let this last.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLATFORM_DB = ROOT / "platform" / "cli" / "src" / "examlops" / "platform_db.py"
ROUTERS = ROOT / "platform" / "services" / "dashboard" / "backend" / "routers"

#: Reads of a tenant-scoped table that are **not** scoped to the caller, with why they are still
#: here. Remove an entry when you fix it; never add one without a reason a reviewer would accept.
#: Emptied 2026-09-13 — the four it was opened with (`compliance_systems`, `fairness_config`,
#: `inference_gateway_config`, `scale_events`) are all scoped now.
UNSCOPED: dict[str, str] = {}


def _tenant_tables() -> set[str]:
    """Tables whose `CREATE TABLE` declares a `tenant` column."""
    text = PLATFORM_DB.read_text(encoding="utf-8")
    tables = {
        m.group(1)
        for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\s*\);", text, re.DOTALL)
        if re.search(r"^\s*tenant\b", m.group(2), re.MULTILINE)
    }
    assert len(tables) > 20, (
        f"only {len(tables)} tenant-scoped tables parsed — the DDL's shape changed and this guard "
        "is now reading nothing, which looks exactly like compliance"
    )
    return tables


def _unscoped_reads() -> dict[str, str]:
    """`{"<router>:<table>": evidence}` for every read this scan believes is unscoped."""
    tenant_tables = _tenant_tables()
    files = sorted(ROUTERS.glob("*.py"))
    assert files, f"no routers found under {ROUTERS} — the path is stale, not the tree clean"
    found: dict[str, str] = {}
    for py in files:
        src = py.read_text(encoding="utf-8")
        # A router that scopes anywhere is trusted for the whole file: these are small modules and
        # a per-statement claim would be guessing at which query the call belongs to.
        if "scope_to_tenant" in src or "assert_tenant_access" in src:
            continue
        for m in re.finditer(r"\bFROM\s+(\w+)|\bJOIN\s+(\w+)", src, re.IGNORECASE):
            table = m.group(1) or m.group(2)
            if table not in tenant_tables:
                continue
            window = src[max(0, m.start() - 600) : m.start() + 900]
            if re.search(r"tenant\s*=\s*[?%]|tenant=|\[.tenant.\]", window):
                continue  # the statement filters on tenant itself
            found[f"{py.name}:{table}"] = f"line {src[: m.start()].count(chr(10)) + 1}"
    return found


def test_no_new_route_reads_another_tenants_rows():
    found = _unscoped_reads()
    new = {k: v for k, v in found.items() if k not in UNSCOPED}
    assert not new, (
        "these dashboard routes read a tenant-scoped table without scoping it to the caller, so "
        "every tenant sees every other tenant's rows:\n  "
        + "\n  ".join(f"{k} ({v})" for k, v in sorted(new.items()))
        + "\nBind the principal (`principal: dict = Depends(_viewer)`) and wrap the rows in "
        "`capabilities.scope_to_tenant(principal, rows)`."
    )


def test_the_recorded_list_only_shrinks():
    """An entry that no longer reproduces has been fixed — delete it, so the count keeps falling."""
    found = _unscoped_reads()
    stale = sorted(set(UNSCOPED) - set(found))
    assert not stale, (
        f"{stale} are listed as unscoped but the scan no longer finds them. Remove them from "
        "UNSCOPED — a ratchet that is allowed to stay loose stops being one."
    )


def test_the_scan_still_recognises_an_unscoped_read(tmp_path, monkeypatch):
    """Anti-vacuity, without needing a real offender to exist.

    The first version of this asserted that the scan found *something*, which worked only while the
    codebase still had unscoped reads. Now that it has none, that assertion would have had to be
    deleted — and deleting the only thing keeping the scan honest is how a guard quietly becomes a
    function that returns an empty list. So the scan is pointed at a planted router instead: if the
    regex stops matching SQL, this fails while the real tree still looks clean.
    """
    planted = tmp_path / "routers"
    planted.mkdir()
    (planted / "planted.py").write_text(
        "async def leak(_=Depends(_viewer)):\n"
        '    return conn.execute("SELECT path, tenant FROM secrets_store ORDER BY path").fetchall()\n'
    )
    monkeypatch.setattr("tests.unit.test_dashboard_tenant_scoping.ROUTERS", planted)
    found = _unscoped_reads()
    assert "planted.py:secrets_store" in found, (
        f"the scan no longer recognises an obviously unscoped read: {found}"
    )


def test_the_real_routers_are_all_scoped():
    """The state this guard exists to hold: every read of a tenant-scoped table is scoped."""
    found = _unscoped_reads()
    assert not found, f"unscoped reads have reappeared: {sorted(found)}"


# ── the same question, asked of helper calls rather than of SQL ────────────────
#
# The scan above reads SQL, so it sees only routes that write their own `SELECT`. A route that
# reads the same rows through `examlops.data` is invisible to it — and that is not hypothetical:
# `GET /api/challenger` called `list_challenger_configs()`, whose `tenant` parameter defaults to
# `None` meaning **every tenant**, and the SQL scan passed it without comment. A helper whose
# default is "no filter" reads exactly like a helper with a safe default.


def _tenant_optional_helpers() -> dict[str, str]:
    """`examlops.data` functions where omitting `tenant` means every tenant."""
    import ast

    data = ROOT / "platform" / "cli" / "src" / "examlops" / "data"
    wide: dict[str, str] = {}
    for py in sorted(data.glob("*.py")):
        for fn in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if not isinstance(fn, ast.FunctionDef):
                continue
            a = fn.args
            defaults = {
                arg.arg: d
                for arg, d in zip(a.args[len(a.args) - len(a.defaults) :], a.defaults, strict=False)
            }
            defaults.update(
                {arg.arg: d for arg, d in zip(a.kwonlyargs, a.kw_defaults, strict=False) if d}
            )
            d = defaults.get("tenant")
            if isinstance(d, ast.Constant) and d.value is None:
                wide[fn.name] = py.name
    assert len(wide) > 5, f"only {len(wide)} tenant-optional helpers found — the scan is stale"
    return wide


def _calls_without_a_tenant(root: Path) -> list[str]:
    out: list[str] = []
    for py in sorted(root.rglob("*.py")):
        if "tests" in py.parts:
            continue
        src = py.read_text(encoding="utf-8")
        for name in _tenant_optional_helpers():
            for m in re.finditer(rf"\b{name}\s*\(", src):
                depth, end = 0, 0
                for i, ch in enumerate(src[m.start() :][:400], start=0):
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                        if depth == 0:
                            end = i
                            break
                args = src[m.start() + len(name) + 1 : m.start() + end]
                if "tenant" not in args and "scope_to_tenant" not in src:
                    out.append(f"{py.name}:{src[: m.start()].count(chr(10)) + 1}: {name}()")
    return out


def test_no_route_asks_a_helper_for_every_tenant():
    backend = ROOT / "platform" / "services" / "dashboard" / "backend"
    offenders = _calls_without_a_tenant(backend)
    assert not offenders, (
        "these call an `examlops.data` helper whose `tenant` defaults to None — which means every "
        "tenant's rows — without passing one or scoping the result:\n  " + "\n  ".join(offenders)
    )


def test_the_fixed_routes_stay_fixed():
    """The three this was found through. Named individually so a revert is unmistakable."""
    for name in ("slo.py", "secrets.py", "gateway.py"):
        src = (ROUTERS / name).read_text(encoding="utf-8")
        assert "scope_to_tenant" in src, f"{name} no longer scopes its rows to the caller's tenant"
