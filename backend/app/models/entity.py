import uuid

from sqlalchemy import (
    BigInteger,
    Computed,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.project import Project

# The keyword half of hybrid search. Weights, highest first:
#
#   A  qualname     — the strongest signal there is. Someone searching
#                     "delete_missing" means that function.
#   B  description  — the LLM's prose, which is also what the dense half embeds
#   C  file_path    — tokenises into useful words: services, job, parser
#   D  source_code  — the long tail. Weighted lowest because term frequency in a
#                     body of code is mostly noise; without this, a long
#                     function outranks a short one for repeating a word.
#
# Generated and STORED rather than computed per query: it is derived entirely
# from columns in the same row, so letting Postgres maintain it removes any
# chance of the index disagreeing with the data.
SEARCH_VECTOR_SQL = (
    "setweight(to_tsvector('english', coalesce(qualname, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(description, '')), 'B') || "
    "setweight(to_tsvector('english', coalesce(replace(file_path, '/', ' '), '')), 'C') || "
    "setweight(to_tsvector('english', coalesce(source_code, '')), 'D')"
)


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

    # Which indexing run last saw this entity in the repo. Stamped on every row
    # the run keeps — written, refreshed, or skipped as unchanged — so deletion
    # becomes "anything this project has that this run did not touch". That is a
    # two-parameter statement regardless of entity count; the previous NOT IN
    # list was bounded by the statement parameter limit.
    last_seen_run: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    # Maintained by Postgres from the columns above; never assigned in Python.
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed(SEARCH_VECTOR_SQL, persisted=True), nullable=True
    )

    # Present so the unit of work orders INSERTs after projects; lazy="raise"
    # keeps it from emitting an implicit lazy load under asyncio.
    project: Mapped[Project] = relationship(lazy="raise")

    __table_args__ = (
        # The stable identity for an entity within a project. Also what makes
        # re-indexing an upsert rather than a delete-and-reinsert.
        UniqueConstraint(
            "project_id", "file_path", "qualname", name="uq_entities_project_file_qualname"
        ),
        # GIN, not GiST: this index is read far more often than it is written,
        # and GIN is the faster of the two for lookups.
        Index("ix_entities_search_vector", "search_vector", postgresql_using="gin"),
    )

    def __repr__(self) -> str:
        return f"<Entity {self.id} {self.qualname} ({self.entity_type})>"
