"""Per-service Postgres roles, provisioned on a real Postgres (plan P3.4, Docker).

``platform/infra/docker-compose/postgres-init/provision.sh`` is what the ``postgres-init`` Compose
service runs. This starts the platform's Postgres image on a private Docker network, creates the
objects an existing install has (tables, a serial column, an enum, a view, all owned by the
superuser, as today), runs the script, and checks what each role can do:

- MLflow, Prefect and the dashboard each own their database, including the tables the superuser
  created, so their own schema migrations can alter them;
- none of them can connect to another's database, and none is a superuser;
- a second run changes nothing it should not, and rotates a changed password;
- a dashboard given its own role on MLflow's database, or a role without a password, is refused.

Opt-in, because it needs Docker::

    EXAMLOPS_POSTGRES_ROLES_LIVE=1 .venv/bin/pytest tests/integration/test_postgres_service_roles_live.py -v
"""

from __future__ import annotations

import re
import secrets
import subprocess
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "platform" / "infra" / "docker-compose"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        __import__("os").getenv("EXAMLOPS_POSTGRES_ROLES_LIVE") != "1",
        reason="set EXAMLOPS_POSTGRES_ROLES_LIVE=1",
    ),
]

IMAGE = re.search(r"^FROM (\S+)", (COMPOSE / "Dockerfile.postgres").read_text(), re.M).group(1)
SUPER_PW = secrets.token_hex(12)


def _docker(*args: str, check: bool = True, env: dict[str, str] | None = None):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


@pytest.fixture(scope="module")
def pg():
    tag = uuid.uuid4().hex[:8]
    net, name = f"exa-pgroles-{tag}", f"exa-pgroles-db-{tag}"
    _docker("network", "create", net)
    try:
        _docker(
            "run", "-d", "--rm", "--name", name, "--network", net, "--network-alias", "postgres",
            "-e", "POSTGRES_USER=mlops", "-e", f"POSTGRES_PASSWORD={SUPER_PW}",
            "-e", "POSTGRES_DB=mlflow",
            "-v", f"{COMPOSE / 'initdb-prefect.sql'}:/docker-entrypoint-initdb.d/10-prefect.sql:ro",
            IMAGE,
        )  # fmt: skip
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = _docker(
                "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "mlops", "-d", "prefect",
                check=False,
            )  # fmt: skip
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("postgres did not start")
        env = {"net": net, "name": name}
        # What an existing install holds: every object owned by the superuser.
        _sql(env, "mlops", SUPER_PW, "mlflow", """
            CREATE TYPE legacy_stage AS ENUM ('None', 'Staging', 'Production');
            CREATE TABLE legacy_models (id SERIAL PRIMARY KEY, name TEXT, stage legacy_stage);
            CREATE VIEW model_names AS SELECT name FROM legacy_models;
            INSERT INTO legacy_models (name, stage) VALUES ('jpcp', 'Production');
            CREATE TABLE dashboard_comments (id SERIAL PRIMARY KEY, body TEXT);
        """)  # fmt: skip
        _sql(
            env,
            "mlops",
            SUPER_PW,
            "prefect",
            "CREATE TABLE legacy_flow_run (id SERIAL, state TEXT);",
        )
        # And the real schemas, migrated by the superuser as every install so far has done.
        env["mlflow_schema"] = _mlflow_store(env, "mlops", SUPER_PW)
        env["prefect_schema"] = _prefect(env, "mlops", SUPER_PW)
        yield env
    finally:
        _docker("rm", "-f", name, check=False)
        _docker("network", "rm", net, check=False)


def _sql(env, user: str, password: str, db: str, sql: str) -> subprocess.CompletedProcess:
    return _docker(
        "run", "--rm", "--network", env["net"], "-e", f"PGPASSWORD={password}", IMAGE,
        "psql", "-X", "-v", "ON_ERROR_STOP=1", "-h", "postgres", "-U", user, "-d", db, "-tAc", sql,
        check=False,
    )  # fmt: skip


def _image_present(image: str) -> bool:
    return _docker("image", "inspect", image, check=False).returncode == 0


MLFLOW_IMAGE = "examlops-mlflow:latest"
PREFECT_IMAGE = "examlops-orchestrator:latest"


def _mlflow(env, user: str, password: str, command: str) -> subprocess.CompletedProcess | None:
    """Run MLflow's own CLI against the database (its migrations, when ``command`` is db upgrade)."""
    if not _image_present(MLFLOW_IMAGE):
        return None
    uri = f"postgresql://{user}:{password}@postgres/mlflow"
    return _docker(
        "run", "--rm", "--network", env["net"], "--entrypoint", "sh", MLFLOW_IMAGE, "-c",
        f"mlflow {command} '{uri}'",
        check=False,
    )  # fmt: skip


def _mlflow_store(env, user: str, password: str) -> subprocess.CompletedProcess | None:
    """Open MLflow's tracking store: it creates its tables and applies its migrations, as the
    server does on start."""
    if not _image_present(MLFLOW_IMAGE):
        return None
    uri = f"postgresql://{user}:{password}@postgres/mlflow"
    return _docker(
        "run", "--rm", "--network", env["net"], "--entrypoint", "python", MLFLOW_IMAGE, "-c",
        f"import mlflow; mlflow.MlflowClient('{uri}').search_experiments()",
        check=False,
    )  # fmt: skip


def _prefect(env, user: str, password: str) -> subprocess.CompletedProcess | None:
    """Run Prefect's schema migrations against the database."""
    if not _image_present(PREFECT_IMAGE):
        return None
    url = f"postgresql+asyncpg://{user}:{password}@postgres/prefect"
    return _docker(
        "run", "--rm", "--network", env["net"],
        "-e", f"PREFECT_API_DATABASE_CONNECTION_URL={url}", "--entrypoint", "sh", PREFECT_IMAGE,
        "-c", "prefect server database upgrade -y",
        check=False,
    )  # fmt: skip


def _provision(env, **overrides: str) -> subprocess.CompletedProcess:
    variables = {"PGHOST": "postgres", "PGUSER": "mlops", "PGPASSWORD": SUPER_PW, **overrides}
    args = [a for k, v in variables.items() for a in ("-e", f"{k}={v}")]
    return _docker(
        "run", "--rm", "--network", env["net"], *args,
        "-v", f"{COMPOSE / 'postgres-init' / 'provision.sh'}:/provision.sh:ro",
        "--entrypoint", "sh", IMAGE, "/provision.sh",
        check=False,
    )  # fmt: skip


PW = {svc: secrets.token_hex(12) for svc in ("mlflow", "prefect", "dashboard")}
ROLES = {
    "MLFLOW_DB_USER": "mlflow",
    "MLFLOW_DB_PASSWORD": PW["mlflow"],
    "PREFECT_DB_USER": "prefect",
    "PREFECT_DB_PASSWORD": PW["prefect"],
    "DASHBOARD_DB_USER": "dashboard",
    "DASHBOARD_DB_PASSWORD": PW["dashboard"],
    "DASHBOARD_DB_NAME": "dashboard",
}


@pytest.fixture(scope="module")
def provisioned(pg):
    first = _provision(pg, **ROLES)
    assert first.returncode == 0, first.stderr
    return pg


def test_each_service_owns_and_uses_its_database(provisioned):
    # MLflow's migrations alter tables the superuser created, and it keeps writing to them.
    done = _sql(provisioned, "mlflow", PW["mlflow"], "mlflow", """
        ALTER TABLE legacy_models ADD COLUMN description TEXT;
        ALTER TYPE legacy_stage ADD VALUE IF NOT EXISTS 'Archived';
        INSERT INTO legacy_models (name, stage) VALUES ('mack', 'Staging');
        CREATE OR REPLACE VIEW model_names AS SELECT name, description FROM legacy_models;
        CREATE TABLE roles_check (id SERIAL PRIMARY KEY);
        SELECT count(*) FROM legacy_models;
    """)  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "2"
    assert _sql(provisioned, "prefect", PW["prefect"], "prefect", """
        ALTER TABLE legacy_flow_run ADD COLUMN name TEXT; INSERT INTO legacy_flow_run (state) VALUES ('ok');
    """).returncode == 0  # fmt: skip
    assert (
        _sql(provisioned, "dashboard", PW["dashboard"], "dashboard", "CREATE TABLE t (i int);")
    ).returncode == 0


@pytest.mark.parametrize(
    ("role", "foreign"),
    [
        ("mlflow", "prefect"),
        ("mlflow", "dashboard"),
        ("prefect", "mlflow"),
        ("dashboard", "mlflow"),
        ("dashboard", "prefect"),
    ],
)
def test_no_service_can_connect_to_anothers_database(provisioned, role, foreign):
    refused = _sql(provisioned, role, PW[role], foreign, "SELECT 1")
    assert refused.returncode != 0
    assert "permission denied for database" in refused.stderr, refused.stderr


def test_no_service_role_is_privileged(provisioned):
    rows = _sql(provisioned, "mlops", SUPER_PW, "postgres", """
        SELECT rolname FROM pg_roles WHERE rolname IN ('mlflow', 'prefect', 'dashboard')
           AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls OR rolreplication)
    """).stdout.strip()  # fmt: skip
    assert rows == ""


def test_databases_keep_their_own_owner(provisioned):
    """REASSIGN OWNED would have handed every superuser-owned database to the last role."""
    owners = _sql(provisioned, "mlops", SUPER_PW, "postgres", """
        SELECT datname || '=' || pg_get_userbyid(datdba) FROM pg_database
         WHERE datname IN ('mlflow', 'prefect', 'dashboard', 'postgres') ORDER BY datname
    """).stdout.split()  # fmt: skip
    assert owners == ["dashboard=dashboard", "mlflow=mlflow", "postgres=mlops", "prefect=prefect"]


def test_a_second_run_converges_and_rotates_a_password(provisioned):
    rotated = secrets.token_hex(12)
    again = _provision(provisioned, **{**ROLES, "MLFLOW_DB_PASSWORD": rotated})
    assert again.returncode == 0, again.stderr
    assert _sql(provisioned, "mlflow", rotated, "mlflow", "SELECT 1").returncode == 0
    assert _sql(provisioned, "mlflow", PW["mlflow"], "mlflow", "SELECT 1").returncode != 0
    # And back, so the tests after this one use the password in ROLES.
    assert _provision(provisioned, **ROLES).returncode == 0
    assert _sql(provisioned, "mlflow", PW["mlflow"], "mlflow", "SELECT 1").returncode == 0


def test_a_dashboard_role_on_mlflows_database_is_refused(provisioned):
    refused = _provision(
        provisioned, **{**ROLES, "DASHBOARD_DB_NAME": "mlflow"}
    )  # the dashboard would take MLflow's database from the mlflow role
    assert refused.returncode != 0 and "own database" in refused.stderr


def test_a_role_without_a_password_is_refused(provisioned):
    refused = _provision(provisioned, **{**ROLES, "PREFECT_DB_PASSWORD": ""})
    assert refused.returncode != 0 and "PREFECT_DB_PASSWORD must be set" in refused.stderr


def test_with_no_roles_configured_nothing_changes_hands(pg):
    """The default: every service still the superuser. Runs on its own fresh database name."""
    run = _provision(pg, DASHBOARD_DB_NAME="dashboard_default")
    assert run.returncode == 0, run.stderr
    owner = _sql(pg, "mlops", SUPER_PW, "postgres", """
        SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = 'dashboard_default'
    """).stdout.strip()  # fmt: skip
    assert owner == "mlops"  # created, because the dashboard would connect to it


def test_mlflow_runs_its_migrations_and_writes_as_its_own_role(provisioned):
    """The real MLflow schema, created by the superuser, is usable and upgradable by `mlflow`."""
    if provisioned["mlflow_schema"] is None:
        pytest.skip(f"{MLFLOW_IMAGE} is not built")
    assert provisioned["mlflow_schema"].returncode == 0, provisioned["mlflow_schema"].stderr
    upgrade = _mlflow(provisioned, "mlflow", PW["mlflow"], "db upgrade")
    assert upgrade.returncode == 0, upgrade.stderr[-2000:]
    uri = f"postgresql://mlflow:{PW['mlflow']}@postgres/mlflow"
    created = _docker(
        "run", "--rm", "--network", provisioned["net"], "--entrypoint", "python", MLFLOW_IMAGE,
        "-c", f"import mlflow; c = mlflow.MlflowClient('{uri}'); "
        "print(c.create_experiment('roles-check')); c.create_registered_model('roles-model')",
        check=False,
    )  # fmt: skip
    assert created.returncode == 0, created.stderr[-2000:]


def test_prefect_runs_its_migrations_as_its_own_role(provisioned):
    if provisioned["prefect_schema"] is None:
        pytest.skip(f"{PREFECT_IMAGE} is not built")
    assert provisioned["prefect_schema"].returncode == 0, provisioned["prefect_schema"].stderr
    upgrade = _prefect(provisioned, "prefect", PW["prefect"])
    assert upgrade.returncode == 0, upgrade.stderr[-2000:]


DASHBOARD_TABLES = (
    "dashboard_audit",
    "dashboard_config",
    "model_doc_images",
    "model_doc_overrides",
    "dashboard_alembic_version",
)


def test_moving_an_existing_dashboard_into_its_own_database(provisioned):
    """The procedure in docs/guides/production-hardening.md, step by step."""
    creates = " ".join(
        f"CREATE TABLE {t} (id SERIAL PRIMARY KEY, v TEXT); INSERT INTO {t} (v) VALUES ('kept');"
        for t in DASHBOARD_TABLES
    )
    assert _sql(provisioned, "mlops", SUPER_PW, "mlflow", creates).returncode == 0
    moved = {**ROLES, "DASHBOARD_DB_USER": "dashboard2", "DASHBOARD_DB_NAME": "dashboard_moved"}
    assert _provision(provisioned, **moved).returncode == 0  # creates the database
    tables = " ".join(f"-t {t}" for t in DASHBOARD_TABLES)
    copy = _docker(
        "run", "--rm", "--network", provisioned["net"], "-e", f"PGPASSWORD={SUPER_PW}",
        "--entrypoint", "sh", IMAGE, "-c",
        f"pg_dump -h postgres -U mlops -d mlflow --no-owner --no-privileges {tables}"
        " | psql -v ON_ERROR_STOP=1 -h postgres -U mlops -d dashboard_moved",
        check=False,
    )  # fmt: skip
    assert copy.returncode == 0, copy.stderr
    assert _provision(provisioned, **moved).returncode == 0  # hands the copies to the role
    read = _sql(provisioned, "dashboard2", PW["dashboard"], "dashboard_moved", """
        INSERT INTO dashboard_config (v) VALUES ('new');
        SELECT count(*) FROM dashboard_config;
    """)  # fmt: skip
    assert read.returncode == 0, read.stderr
    assert read.stdout.strip().splitlines()[-1] == "2"
    assert _sql(provisioned, "dashboard2", PW["dashboard"], "mlflow", "SELECT 1").returncode != 0
