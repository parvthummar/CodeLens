import uuid

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.project import Project


class Entity(Base, TimestampMixin):
    """A parsed code entity: a top-level function, a class, or a method.

    The primary key doubles as the Pinecone vector ID, which is what makes the
    two stores reconcilable. It replaces the previous positional
    `{project_id}_{i}` scheme, where `i` was a list index — so any re-index
    remapped IDs onto different functions and orphaned the tail.
    """

    __tablename__ = "entities"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, sort_order=-100
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        sort_order=-99,
    )

    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    qualname: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False)

    signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Full source. Postgres has no practical size limit here, so the 20 KB
    # truncation the Pinecone metadata ceiling forced is no longer needed.
    source_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # sha256 of source_code, for skipping unchanged entities on re-index.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Present so the unit of work orders INSERTs after projects; lazy="raise"
    # keeps it from emitting an implicit lazy load under asyncio.
    project: Mapped[Project] = relationship(lazy="raise")

    __table_args__ = (
        # The stable identity for an entity within a project. Also what makes
        # re-indexing an upsert rather than a delete-and-reinsert.
        UniqueConstraint(
            "project_id", "file_path", "qualname", name="uq_entities_project_file_qualname"
        ),
    )

    def __repr__(self) -> str:
        return f"<Entity {self.id} {self.qualname} ({self.entity_type})>"
