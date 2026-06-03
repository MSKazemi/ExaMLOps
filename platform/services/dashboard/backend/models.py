import uuid
from datetime import datetime

from database import Base
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column


class DashboardConfig(Base):
    """Per-key config row.

    Non-secret rows: ``value`` holds plaintext, ``secret_value`` is NULL.
    Secret rows: ``value`` is NULL; ``secret_value`` is NULL when unset, or a
    Fernet token (bytes) when set. ``is_secret`` distinguishes the two.

    The CHECK constraint enforces "at most one of value/secret_value is
    populated" — both NULL is legal (an unset secret); both non-NULL is not.
    """

    __tablename__ = "dashboard_config"
    __table_args__ = (
        CheckConstraint(
            "NOT (value IS NOT NULL AND secret_value IS NOT NULL)",
            name="dashboard_config_value_xor",
        ),
    )

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    secret_value: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    is_secret: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class DashboardAudit(Base):
    """Append-only audit log of admin secret writes.

    Records who (role), what (action), which key. Never the value.
    """

    __tablename__ = "dashboard_audit"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)


class ModelDocOverride(Base):
    """Admin-edited Markdown body that overrides the filesystem README.

    fs_sha is the SHA-256 of the filesystem README at the time the override
    was saved; if the filesystem README later changes, the dashboard surfaces
    a drift banner to admins.
    """

    __tablename__ = "model_doc_overrides"

    model_name: Mapped[str] = mapped_column(Text, primary_key=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    fs_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)


class ModelDocImage(Base):
    """Admin-uploaded image referenced from a model description.

    The bytes live in MinIO at object_key; this row is just metadata.
    """

    __tablename__ = "model_doc_images"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    original_name: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(127), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    uploaded_by: Mapped[str] = mapped_column(String(64), nullable=False)


Index("ix_model_doc_images_model", ModelDocImage.model_name)
