#!/bin/sh
# Per-service Postgres roles (plan P3.4): each service logs in as its own role, owns its own
# database, and cannot connect to anyone else's. Run by the `postgres-init` Compose service as the
# superuser, after Postgres is healthy and before MLflow, Prefect and the dashboard start.
#
# Idempotent: every run converges roles, passwords (rotation = change the variable and re-run),
# database ownership and connect rights. It works on a new volume and on an existing one, where
# the superuser created every table: those tables are handed to the service role, because MLflow's
# and Prefect's own schema migrations must own what they alter.
#
# Opt-in per service: a service whose *_DB_USER is the superuser (the default) is left as it was.
#
#   MLFLOW_DB_USER / MLFLOW_DB_PASSWORD          database mlflow
#   PREFECT_DB_USER / PREFECT_DB_PASSWORD        database prefect
#   DASHBOARD_DB_USER / DASHBOARD_DB_PASSWORD    database DASHBOARD_DB_NAME (default mlflow)
#
# Passwords reach psql through the environment (\getenv), never argv, so `ps` does not show them.
set -eu

SUPERUSER="${PGUSER:?PGUSER (the Postgres superuser) is required}"
DASHBOARD_DB_NAME="${DASHBOARD_DB_NAME:-mlflow}"

psql_super() {
    psql -v ON_ERROR_STOP=1 -X -q "$@"
}

fail() {
    echo "postgres-init: $*" >&2
    exit 1
}

# provision <database> <role> <password-variable-name>
provision() {
    db="$1" role="$2" pw_var="$3"
    if [ "$role" = "$SUPERUSER" ]; then
        # Still make sure the database exists: a dashboard moved to its own database connects to it.
        PROVISION_DB="$db" psql_super -d postgres <<'SQL'
\getenv db PROVISION_DB
SELECT format('CREATE DATABASE %I', :'db')
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db') \gexec
SQL
        echo "postgres-init: $db stays with the superuser (set its *_DB_USER to give it its own role)"
        return 0
    fi
    eval "pw=\${$pw_var:-}"
    [ -n "$pw" ] || fail "$pw_var must be set when the $db database has its own role ($role)"

    # Role and password. CREATE ROLE has no IF NOT EXISTS; \gexec runs it only when missing.
    PROVISION_ROLE="$role" PROVISION_PASSWORD="$pw" psql_super -d postgres <<'SQL'
\getenv role PROVISION_ROLE
\getenv password PROVISION_PASSWORD
SELECT format('CREATE ROLE %I', :'role')
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'role') \gexec
ALTER ROLE :"role" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD :'password';
SQL

    # The database, owned by the role. An existing one changes owner; nobody else may connect.
    PROVISION_ROLE="$role" PROVISION_DB="$db" psql_super -d postgres <<'SQL'
\getenv role PROVISION_ROLE
\getenv db PROVISION_DB
SELECT format('CREATE DATABASE %I OWNER %I', :'db', :'role')
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db') \gexec
ALTER DATABASE :"db" OWNER TO :"role";
REVOKE CONNECT, TEMPORARY ON DATABASE :"db" FROM PUBLIC;
SQL

    # Everything inside it. Per object, not REASSIGN OWNED: that also hands over every *database*
    # the superuser owns, which would give this role the other services' databases.
    PROVISION_ROLE="$role" psql_super -d "$db" <<'SQL'
\getenv role PROVISION_ROLE
SELECT format('ALTER SCHEMA %I OWNER TO %I', nspname, :'role')
  FROM pg_namespace
 WHERE nspname NOT LIKE 'pg\_%' AND nspname <> 'information_schema'
   AND pg_get_userbyid(nspowner) <> :'role' \gexec
SELECT format('ALTER %s %I.%I OWNER TO %I',
              CASE c.relkind WHEN 'r' THEN 'TABLE' WHEN 'p' THEN 'TABLE'
                             WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                             WHEN 'S' THEN 'SEQUENCE' WHEN 'f' THEN 'FOREIGN TABLE' END,
              n.nspname, c.relname, :'role')
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
   AND n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
   AND pg_get_userbyid(c.relowner) <> :'role'
   -- A sequence owned by a table column moves with its table.
   AND NOT (c.relkind = 'S' AND EXISTS (SELECT FROM pg_depend d
            WHERE d.objid = c.oid AND d.deptype IN ('a', 'i'))) \gexec
SELECT format('ALTER TYPE %I.%I OWNER TO %I', n.nspname, t.typname, :'role')
  FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
 WHERE t.typtype IN ('e', 'd', 'r', 'm')
   AND n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
   AND pg_get_userbyid(t.typowner) <> :'role' \gexec
SELECT format('ALTER ROUTINE %I.%I(%s) OWNER TO %I',
              n.nspname, p.proname, pg_get_function_identity_arguments(p.oid), :'role')
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
   AND NOT EXISTS (SELECT FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
   AND pg_get_userbyid(p.proowner) <> :'role' \gexec
SQL
    echo "postgres-init: $db is owned by $role"
}

# One database, one owner: two services given different roles on the same database would each
# take it from the other. The dashboard shares MLflow's database only while it runs as the superuser.
if [ "$DASHBOARD_DB_NAME" = "mlflow" ] && [ "${DASHBOARD_DB_USER:-$SUPERUSER}" != "$SUPERUSER" ] \
    && [ "${DASHBOARD_DB_USER}" != "${MLFLOW_DB_USER:-$SUPERUSER}" ]; then
    fail "the dashboard needs its own database for its own role: set DASHBOARD_DB_NAME=dashboard"
fi
provision mlflow "${MLFLOW_DB_USER:-$SUPERUSER}" MLFLOW_DB_PASSWORD
provision prefect "${PREFECT_DB_USER:-$SUPERUSER}" PREFECT_DB_PASSWORD
if [ "$DASHBOARD_DB_NAME" != "mlflow" ]; then
    provision "$DASHBOARD_DB_NAME" "${DASHBOARD_DB_USER:-$SUPERUSER}" DASHBOARD_DB_PASSWORD
fi
echo "postgres-init: done"
