"""extend dashboard_config and add model doc tables

Revision ID: 0002
Revises: c3d6457b6d8a
Create Date: 2026-05-01

Upgrades the schema from the initial deploy (c3d6457b6d8a) to the full
model-documentation schema:
  - dashboard_config gains secret_value, is_secret, and the XOR check;
    value becomes nullable.
  - model_doc_overrides and model_doc_images are created.
"""

from alembic import op

revision = "0002"
down_revision = "c3d6457b6d8a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ── dashboard_config: add secret columns, relax value NOT NULL ────────────
    if is_pg:
        op.execute("""
            ALTER TABLE dashboard_config
              ADD COLUMN IF NOT EXISTS secret_value BYTEA,
              ADD COLUMN IF NOT EXISTS is_secret BOOLEAN NOT NULL DEFAULT FALSE,
              ALTER COLUMN value DROP NOT NULL
        """)
        # Add XOR constraint only if it doesn't already exist
        op.execute("""
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'dashboard_config_value_xor'
                  AND conrelid = 'dashboard_config'::regclass
              ) THEN
                ALTER TABLE dashboard_config
                  ADD CONSTRAINT dashboard_config_value_xor
                    CHECK (NOT (value IS NOT NULL AND secret_value IS NOT NULL));
              END IF;
            END $$
        """)
    else:
        # SQLite: recreate table (SQLite doesn't support ADD COLUMN with constraints)
        op.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_config_new (
                key TEXT PRIMARY KEY,
                value TEXT,
                secret_value BLOB,
                is_secret BOOLEAN NOT NULL DEFAULT 0,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT dashboard_config_value_xor
                  CHECK (NOT (value IS NOT NULL AND secret_value IS NOT NULL))
            )
        """)
        op.execute("""
            INSERT INTO dashboard_config_new (key, value, is_secret, updated_at)
            SELECT key, value, 0, updated_at FROM dashboard_config
        """)
        op.execute("DROP TABLE dashboard_config")
        op.execute("ALTER TABLE dashboard_config_new RENAME TO dashboard_config")

    # ── model_doc_overrides ───────────────────────────────────────────────────
    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS model_doc_overrides (
                model_name TEXT PRIMARY KEY,
                body TEXT NOT NULL,
                fs_sha VARCHAR(64) NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_by VARCHAR(64) NOT NULL
            )
        """)
    else:
        op.execute("""
            CREATE TABLE IF NOT EXISTS model_doc_overrides (
                model_name TEXT PRIMARY KEY,
                body TEXT NOT NULL,
                fs_sha VARCHAR(64) NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_by VARCHAR(64) NOT NULL
            )
        """)

    # ── model_doc_images ──────────────────────────────────────────────────────
    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS model_doc_images (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                model_name TEXT NOT NULL,
                object_key TEXT NOT NULL,
                original_name TEXT NOT NULL,
                content_type VARCHAR(127) NOT NULL,
                size_bytes BIGINT NOT NULL,
                uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                uploaded_by VARCHAR(64) NOT NULL
            )
        """)
    else:
        op.execute("""
            CREATE TABLE IF NOT EXISTS model_doc_images (
                id VARCHAR(36) PRIMARY KEY,
                model_name TEXT NOT NULL,
                object_key TEXT NOT NULL,
                original_name TEXT NOT NULL,
                content_type VARCHAR(127) NOT NULL,
                size_bytes BIGINT NOT NULL,
                uploaded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                uploaded_by VARCHAR(64) NOT NULL
            )
        """)

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_model_doc_images_model ON model_doc_images(model_name)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS model_doc_images")
    op.execute("DROP TABLE IF EXISTS model_doc_overrides")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            ALTER TABLE dashboard_config
              DROP COLUMN IF EXISTS secret_value,
              DROP COLUMN IF EXISTS is_secret,
              ALTER COLUMN value SET NOT NULL
        """)
