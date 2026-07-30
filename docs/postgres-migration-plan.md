# Migration Plan: MongoDB → Postgres (Neon)

**Status:** Phases 0–3 complete and verified end-to-end (commit `aa2e9f1`). Phase 4 not started.
**Branch:** `feat/postgres-migration`
**Scope:** replace MongoDB with Postgres as the metadata store as well as for user and project tables. **Pinecone is retained** as the vector store.
**Data migration required:** none — Mongo currently holds no data, so this is a hard cutover with no dual-write or backfill, but clean pinecone once.

---

## Why

1. The schema is already relational. `Project.user_id` is a hand-maintained foreign key (`models/project.py:17`) with no referential integrity or cascade, and neither model has a single nested or variable-shape field.
2. Incremental re-indexing — the biggest cost lever available — is a set-difference problem ("which of these 5,000 content hashes are new, which are gone"). That is an anti-join in SQL; in Mongo it means pulling every entity into Python and diffing in memory on every re-index.
3. Source code currently lives in Pinecone metadata, truncated to 20 KB (`indexing_service.py:46`). A vector DB is an index, not a blob store. Moving entities to Postgres removes the truncation and slims Pinecone down to what it is good at.
4. Hybrid search needs a home for the keyword half. Postgres `tsvector` provides it; Mongo's text search does not really.
5. Vector IDs are positional today — `{project_id}_{i}` where `i` is a list index (`indexing_service.py:50`). On any re-index the same ID maps to a different function, and a shrinking entity count orphans the tail forever. A Postgres `entities` table gives stable identity.

## Why Pinecone stays

Postgres-for-metadata + Pinecone-for-vectors is a standard production split, and pgvector is not required for anything in this plan:

- Hybrid search works across both stores — Pinecone returns ranked IDs, Postgres `tsvector` returns ranked IDs, and the service layer fuses them with reciprocal rank fusion. Two round trips, not a problem.
- Pinecone's free tier is far more generous for vectors than Neon's 0.5 GB is. A 1024-dim `vector` is ~4 KB; keeping vectors out of Neon is what makes the free tier comfortable rather than tight.
- Zero migration risk on the retrieval path. Search keeps working exactly as it does now.

**Accepted tradeoff:** two stores with no atomic write between them. Today that manifests as the silent orphan in `delete_project` (`project_service.py:63-69`) — Pinecone namespace deleted best-effort, exception swallowed, document deleted regardless. Mitigated below with stable IDs plus a reconciliation path, which turns it from a silent bug into a cleanup job.

pgvector remains a documented future option if the two-store split ever becomes the actual bottleneck. It is not on the critical path.

---

## Target stack

| Concern | Choice | Note |
|---|---|---|
| Driver | `asyncpg` | keeps the codebase async end-to-end |
| ORM | SQLAlchemy 2.0 async (`Mapped` / `mapped_column`) | typed declarative style |
| Migrations | Alembic | run against the **direct** (non-pooler) endpoint |
| Metadata store | Neon Postgres | pooled endpoint for the app |
| Vector store | Pinecone | unchanged |

New deps: `sqlalchemy[asyncio]>=2.0`, `asyncpg`, `alembic`, `greenlet`.
`greenlet` is listed explicitly — SQLAlchemy's async layer requires it and it is not always pulled in on Windows.

Removed at the end of Phase 2: `beanie`, `motor`, `pymongo`.

---

## Key design decisions

**Primary keys → UUID, not `bigserial`.**
`uuid4`, stored as native Postgres `uuid`. The frontend already treats IDs as opaque strings so nothing changes there, IDs stay non-enumerable, and `pinecone_namespace = str(project.id)` keeps working unchanged.

**`ProjectStatus` → `VARCHAR` + `CHECK`, not a native PG enum.**
Via `sa.Enum(ProjectStatus, native_enum=False)`. Native enums need `ALTER TYPE` to add a value, which is awkward in migrations — and this list will grow (`queued`, `stale`) once the worker queue lands.

**Sessions are passed explicitly.**
The largest mechanical change. Beanie is active-record (`Project.get()`, `project.save()`) and needs no session; SQLAlchemy does. A `get_db()` FastAPI dependency yields an `AsyncSession` per request, and every DB-touching function gains a `db: AsyncSession` first parameter.

**The background pipeline opens its own short-lived sessions.**
`run_indexing_pipeline` cannot borrow a request-scoped session, and must not hold one open across the whole clone → LLM → embed run: that pins a pooled connection for minutes, which matters on Neon's free-tier connection budget. Short session per status write instead.

**Postgres `entities.id` becomes the Pinecone vector ID** (Phase 4). Insert into Postgres first, then upsert to Pinecone keyed on those IDs. Pinecone metadata shrinks to almost nothing since the namespace already scopes by project. Search becomes: query Pinecone for IDs + scores → `SELECT ... WHERE id = ANY(:ids)` → return in score order.

**Engine configuration for Neon specifically:**
- Normalize the URL at engine creation: `postgresql://` → `postgresql+asyncpg://`, and strip `sslmode` and `channel_binding`. `channel_binding` is libpq-only; asyncpg forwards unknown query params as server settings and errors. SSL is passed via `connect_args={"ssl": "require"}`. Keeping `.env` as the raw console string means it stays copy-pasteable.
- `pool_pre_ping=True` — Neon free tier auto-suspends compute when idle, so the first connection after a pause can be stale.
- Prepared-statement caching disabled for the pooled endpoint (PgBouncer transaction mode): `prepared_statement_cache_size=0` on the engine and `statement_cache_size=0` in `connect_args`. Without this you get intermittent `DuplicatePreparedStatementError` that looks random. **I will verify this empirically with a concurrent-request burst rather than trusting it works.**

---

## Phases

Each phase ends at a working state. Phase 3 is a checkpoint where I stop for sign-off.

### Phase 0 — Scaffolding (no behaviour change)
- Create branch `feat/postgres-migration`.
- Add deps to `requirements.txt`, install into `cr_venv`.
- New `app/db/postgres.py`: URL normalization, `create_async_engine`, `async_sessionmaker`, `get_db()` dependency.
- Add `DATABASE_URL_DIRECT` to `.env` (same host minus `-pooler`) for Alembic.
- Smoke script `backend/scripts/check_db.py`: connect, `SELECT version()`, and run a concurrent burst to prove the prepared-statement config is right.

**Verify:** smoke script connects and survives the burst. Mongo app still runs untouched.

### Phase 1 — Models + first migration
- `app/models/base.py`: `DeclarativeBase`, UUID PK mixin, `created_at`/`updated_at`.
- Rewrite `models/user.py` and `models/project.py` as SQLAlchemy models. `users.email` unique; `projects.user_id` FK → `users.id` `ON DELETE CASCADE`.
- `alembic init`, wire `env.py` to `DATABASE_URL_DIRECT` + target metadata.
- First migration: `users` and `projects`.

**Verify:** `alembic upgrade head` succeeds; tables and constraints exist in Neon.

### Phase 2 — Rewrite the data-access layer

| File | Change |
|---|---|
| `services/auth_service.py` | `signup(db, data)`, `login(db, data)`; `User.find_one` → `select(User).where(...)` |
| `services/project_service.py` | all four functions take `db`; drop `bson.ObjectId` parsing, validate UUID instead |
| `core/deps.py` | `get_current_user` gains `db: AsyncSession = Depends(get_db)` |
| `api/v1/auth.py` | both routes inject `db` |
| `api/v1/projects.py` | all five routes inject `db` |
| `services/indexing_service.py` | drop `ObjectId`; open its own short-lived sessions per write |
| `schemas/project.py` | `from_document` → `from_orm`, retargeted at the SQLAlchemy model |
| `main.py` | lifespan: replace Beanie init with engine dispose on shutdown |
| `config.py` | drop `mongo_uri` / `mongo_db_name` |

Deleted: `app/db/mongodb.py`. Removed from `.env`: `MONGO_URI`, `MONGO_DB_NAME`.

**Verify:** app imports and starts; `/health` returns ok.

### Phase 3 — CHECKPOINT: end-to-end verification
Pinecone untouched; only the metadata store has moved.

Manual pass: signup → login → dashboard loads → add project → status progresses `pending → cloning → indexing → ready` → search returns results → delete project. Plus a restart mid-index to confirm the failure mode is unchanged (still broken — the worker queue fixes that, not this migration).

**I stop here for sign-off.**

### Phase 4 — `entities` table (Postgres) + stable Pinecone IDs

```
entities
  id            bigserial PK        -- also the Pinecone vector ID
  project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE
  file_path     text NOT NULL
  qualname      text NOT NULL      -- "func" or "MyClass.method"
  entity_type   text NOT NULL      -- function | class | method
  signature     text
  source_code   text               -- full source, no 20 KB truncation
  description   text
  start_line    int
  end_line      int
  content_hash  text NOT NULL      -- sha256 of source_code
  created_at / updated_at
  UNIQUE (project_id, file_path, qualname)
```
- No `embedding` column — vectors stay in Pinecone.
- The `UNIQUE` constraint is the structural fix for positional vector IDs.
- `parser_service` gains `content_hash`; `qualname` maps to the existing `name` field, which already carries `Class.method` (`parser_service.py:75`), so the parser change is small.
- Pipeline writes entities to Postgres in batches (committing per batch, so a failure at 90% doesn't discard everything), then upserts vectors to Pinecone keyed on `entities.id` with minimal metadata.
- `search_service` queries Pinecone for IDs + scores, then hydrates from Postgres.
- `delete_project` keeps its Pinecone namespace delete, but `ON DELETE CASCADE` handles all Postgres rows.

**Verify:** index a small repo; row count matches parsed entity count; re-index twice and confirm no duplicate rows and no orphaned vectors.

---

## Known risks

| Risk | Handling |
|---|---|
| PgBouncer + asyncpg prepared statements | disable both caches; verify under concurrent load in Phase 0, not just a single request |
| Two stores, no atomic write | stable `entities.id` as vector ID makes Postgres↔Pinecone reconcilable; a cleanup job can find orphans by ID set difference |
| Neon idle auto-suspend | `pool_pre_ping=True`; expect a cold first query |
| Alembic through the pooler | migrations use the direct endpoint |
| Neon free tier ~0.5 GB | text-only metadata is ~1–2 KB/entity, so this is comfortable now that vectors stay in Pinecone. Confirm the quota in your dashboard. |
| Credential in chat transcript | rotate the Neon password once the migration is verified working |

## Deliberately not in this plan

Each deserves its own pass: worker queue (Redis + ARQ) to replace `BackgroundTasks`, Docker Compose + CI, a pytest suite, the retrieval eval harness, incremental re-indexing via commit SHA + content-hash diffing, and hybrid search (Postgres `tsvector` + Pinecone, fused with RRF). Phase 4 lays the groundwork for the last two.

## Rollback

Everything happens on `feat/postgres-migration`. `main` stays working on Mongo throughout, so rollback is abandoning the branch.

---

## Outcome — Phases 0–3

Verified end-to-end against live Neon (PostgreSQL 18.4). Indexed 56 entities from
this repo: `pending → cloning → indexing → ready` in ~29 s, search returned the
correct top hit for each probe query (`embed_texts` 0.687, `hash_password` 0.665,
`clone_repo` 0.554), and delete removed both the row and its Pinecone namespace.

Three things the plan got wrong, corrected in implementation:

1. **`prepared_statement_cache_size` / `prepared_statement_name_func` are DBAPI
   arguments, not engine arguments.** Passing them to `create_async_engine`
   raises `TypeError`. They belong in `connect_args`, and SQLAlchemy's documented
   PgBouncer recipe also calls for `NullPool` so prepared statements do not
   accumulate server-side. Engine config is now chosen from the host: `-pooler.`
   gets `NullPool` + unique statement names, the direct host gets `QueuePool(5)`
   with statement caching.

2. **UUID `default=` fires at flush, not at construction.** `obj.id` is `None`
   until the session flushes, so callers needing the ID up front (to build
   `pinecone_namespace`) must pass an explicit `id=uuid4()`.

3. **Omitting ORM relationships broke flush ordering.** With no dependency edge
   between mappers, SQLAlchemy sorts them alphabetically — `Project` before
   `User` — so a flush containing a new user and a new project violates the FK.
   Fixed with `Project.owner = relationship(lazy="raise")`, which restores
   ordering while still making accidental lazy loads fail loudly rather than as
   `MissingGreenlet`.

Known gap, now measured rather than assumed: killing the server mid-index leaves
the project **permanently stranded in `indexing`** — no retry, no reconciler, no
UI recovery path. Unchanged by this migration; this is the case the worker queue
is meant to close.

Outstanding: 8 orphaned Pinecone namespaces (~7.5k vectors) with ObjectId-style
names left over from the Mongo era, unreferenced by any Postgres row.
