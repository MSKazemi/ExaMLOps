"""Each service reaches Postgres as its own role, once the operator gives it one (plan P3.4).

MLflow, Prefect and the dashboard all logged in as the superuser, and the dashboard kept its
tables inside MLflow's database: any one of them compromised could read or drop the others'
data. ``postgres-init`` (``postgres-init/provision.sh``) gives each an owned database and shuts
the others out; ``tests/integration/test_postgres_service_roles_live.py`` checks that on a real
Postgres. This holds Compose to it: no connection string hard-codes the superuser, nothing starts
before the roles exist, and the backup still dumps the dashboard's database once it moves.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from tests.unit._guard_deps import scan_files

D = Path(__file__).resolve().parents[2] / "platform" / "infra" / "docker-compose"
TEXT = (D / "docker-compose.yml").read_text(encoding="utf-8")
COMPOSE = yaml.safe_load(TEXT)
SCRIPT = (D / "postgres-init" / "provision.sh").read_text(encoding="utf-8")
SERVICES = {"mlflow": "MLFLOW", "orchestrator": "PREFECT", "dashboard": "DASHBOARD"}


def test_no_connection_string_hard_codes_the_superuser():
    assert not re.findall(r"postgresql(?:\+\w+)?://mlops:", TEXT)


def test_each_service_connects_with_its_own_variables():
    blob = {name: yaml.safe_dump(COMPOSE["services"][name]) for name in SERVICES}
    for name, prefix in SERVICES.items():
        assert f"${{{prefix}_DB_USER:-mlops}}" in blob[name], name
        assert f"${{{prefix}_DB_PASSWORD:-${{POSTGRES_PASSWORD:-mlops}}}}" in blob[name], name


def test_nothing_that_uses_a_role_starts_before_the_roles_exist():
    for name in SERVICES:
        depends = COMPOSE["services"][name]["depends_on"]
        assert depends["postgres-init"] == {"condition": "service_completed_successfully"}, name


def test_postgres_init_gets_every_role_and_the_script():
    init = COMPOSE["services"]["postgres-init"]
    env = init["environment"]
    for prefix in SERVICES.values():
        assert f"{prefix}_DB_USER" in env and f"{prefix}_DB_PASSWORD" in env
    assert "DASHBOARD_DB_NAME" in env
    assert "./postgres-init/provision.sh:/provision.sh:ro" in init["volumes"]
    assert init["depends_on"]["postgres"]["condition"] == "service_healthy"


def test_the_backup_follows_the_dashboards_database():
    env = COMPOSE["services"]["backup"]["environment"]
    assert "${DASHBOARD_DB_NAME:-mlflow}" in env["EXAMLOPS_BACKUP_PG_DBS"]


def test_a_repeated_database_is_dumped_once(monkeypatch):
    from examlops.backup import postgres_tier

    monkeypatch.setenv("EXAMLOPS_BACKUP_PG_DBS", "mlflow,prefect,mlflow")
    assert postgres_tier._pg_databases() == ["mlflow", "prefect"]


def test_passwords_never_reach_the_command_line():
    """psql reads them from its environment; an argument would show in `ps`."""
    assert "\\getenv password PROVISION_PASSWORD" in SCRIPT
    assert not re.search(r"psql[^\n]*-v\s+\w*pass", SCRIPT, re.I)


def test_ownership_moves_per_object_not_with_reassign_owned():
    """REASSIGN OWNED also moves every database the superuser owns, including the other services'."""
    assert "REASSIGN OWNED" not in re.sub(r"#.*", "", SCRIPT)


def test_the_variables_are_in_the_env_template():
    path = D / ".env.example"
    if not path.exists():
        pytest.skip(
            "this directory's .env.example is deliberately private "
            "(.dualgit/public.carveout) — absent on a public-only checkout"
        )
    template = path.read_text(encoding="utf-8")
    for prefix in SERVICES.values():
        assert f"{prefix}_DB_USER=" in template and f"{prefix}_DB_PASSWORD=" in template
    assert "DASHBOARD_DB_NAME=" in template


def test_the_documented_move_copies_every_dashboard_table():
    """The guide's pg_dump names the dashboard's tables; a new table left out would be lost."""
    root = D.parents[2]
    backend = root / "platform" / "services" / "dashboard" / "backend"
    tables = {"dashboard_alembic_version"}
    for source in scan_files(backend):
        if "tests" not in source.parts:
            tables |= set(
                re.findall(r"__tablename__\s*=\s*[\"']([a-z_]+)", source.read_text("utf-8"))
            )
    guide = (root / "docs" / "guides" / "production-hardening.md").read_text("utf-8")
    documented = set(re.findall(r"-t ([a-z_]+)", guide))
    assert documented == tables, (documented, tables)
