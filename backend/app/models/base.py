"""Declarative base and shared column mixins."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base for all ORM models. Alembic autogenerates from Base.metadata."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UUIDPrimaryKeyMixin:
    """UUID primary key, generated in Python rather than by the database.

    Note that `default` is evaluated during flush, not at construction, so
    `obj.id` is None until the session flushes. Callers that need the ID to
    build a dependent value (the Pinecone namespace) should pass an explicit
    `id=uuid4()` so the row can be written in a single INSERT.
    """

    # sort_order keeps the PK first in CREATE TABLE; mixin columns otherwise
    # land last, following the MRO.
    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4, sort_order=-100
    )


class TimestampMixin:
    """Timezone-aware created/updated stamps.

    `onupdate` means updated_at maintains itself on any ORM update, replacing
    the manual assignment the Mongo code had to repeat at every call site.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
        sort_order=100,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
        sort_order=101,
    )
