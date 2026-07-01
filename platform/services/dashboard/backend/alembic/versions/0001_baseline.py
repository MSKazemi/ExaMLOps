"""baseline schema

Revision ID: c3d6457b6d8a
Revises:
Create Date: 2026-05-01

"""

from alembic import op

revision = "c3d6457b6d8a"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_config (
                key TEXT PRIMARY KEY,
                value TEXT,
                secret_value BYTEA,
                is_secret BOOLEAN NOT NULL DEFAULT FALSE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT dashboard_config_value_xor
                  CHECK (NOT (value IS NOT NULL AND secret_value IS NOT NULL))
            )
        """)
    else:
        op.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_config (
                key TEXT PRIMARY KEY,
                value TEXT,
                secret_value BLOB,
                is_secret BOOLEAN NOT NULL DEFAULT 0,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT dashboard_config_value_xor
                  CHECK (NOT (value IS NOT NULL AND secret_value IS NOT NULL))
            )
        """)

    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_audit (
                id BIGSERIAL PRIMARY KEY,
                at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                role TEXT NOT NULL,
                action TEXT NOT NULL,
                key TEXT NOT NULL
            )
        """)
    else:
        op.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                role TEXT NOT NULL,
                action TEXT NOT NULL,
                key TEXT NOT NULL
            )
        """)

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
                uploaded_by VARCHAR(64) NOT NULL,
                CONSTRAINT uq_model_doc_images_model_object UNIQUE (model_name, object_key)
            )
        """)
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_model_doc_images_model ON model_doc_images(model_name)"
        )
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
                uploaded_by VARCHAR(64) NOT NULL,
                CONSTRAINT uq_model_doc_images_model_object UNIQUE (model_name, object_key)
            )
        """)
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_model_doc_images_model ON model_doc_images(model_name)"
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS model_doc_images")
    op.execute("DROP TABLE IF EXISTS model_doc_overrides")
    op.execute("DROP TABLE IF EXISTS dashboard_audit")
    op.execute("DROP TABLE IF EXISTS dashboard_config")
