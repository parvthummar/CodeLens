# Roadmap: from working prototype to defensible project

Written 2026-07-30, after the MongoDB → Postgres migration landed on `main`
(merge `d3c3aa1`).

This is the plan for the work that turns CodeLens from "the happy path works"
into something that holds up when someone competent pokes at it. It is
deliberately opinionated about **order**, because several items are cheap now
and expensive later.

---

## 1. Where the project actually stands

**What works end-to-end, verified against live services:** signup/login, adding
a repo, the indexing pipeline (`pending → cloning → indexing → ready`), semantic
search, and delete. A 56-entity repo indexes in ~29 s and search returns the
correct top hit for plain-English queries.

**What the migration already fixed:**

- Postgres owns users, projects, and parsed entities; Pinecone holds only
  vectors, keyed on `entities.id`
- Vector IDs are stable primary keys, not list positions. Re-index is an upsert
  plus a targeted delete, so it no longer remaps IDs onto different functions or
  orphans the tail.
- `source_code` lives in a Postgres `text` column — the 20 KB truncation that
  Pinecone's 40 KB metadata ceiling forced is gone
- FK `ON DELETE CASCADE` replaced hand-written cleanup; deleting a project now
  actually removes its Pinecone namespace instead of silently orphaning it
- The pipeline uses short-lived DB sessions instead of pinning a connection for
  the length of a multi-minute run
- Entity writes batch at 500 rows with a commit per batch, so a late failure
  doesn't discard earlier work

**The headline problem that remains.** Indexing still runs inside the web
process via FastAPI `BackgroundTasks` (`api/v1/projects.py`). This was measured,
not assumed: kill the server mid-index and the project sits in `indexing`
**forever** — no retry, no reconciler, no way for a user to recover it. Step 3
is what closes it.

---

## 2. What "good" means here

Not speed. Six properties, and the project currently fails four of them:

| Property | Now |
|---|---|
| Work is decoupled from the request lifecycle | ❌ indexing runs in the API process |
| Resource use per unit of work is bounded | 🟡 writes are chunked; the LLM stage fans out one coroutine per entity |
| You can run N copies | ❌ a second API instance doubles indexing load rather than sharing it |
| Work survives failure | ❌ one exception is terminal; restart strands the job |
| Cost grows sub-linearly with data | 🟡 `content_hash` exists but nothing consumes it yet |
| One tenant can't starve the others | ❌ no repo size cap, quota, or rate limit |

Scale estimate, extrapolated from a measured 56-entity run at ~0.36 s/entity: a
Django-sized repo (~15,000 entities) would take **~1.5 hours in a single
process**, holding ~15,000 coroutines and ~15,000 × 1024 floats in memory, with
no checkpoint. That is arithmetic, not a measurement — worth measuring for real
once step 3 makes the attempt survivable.

---

## 3. The sequence

### Step 1 — Tests ✅ done

**151 tests, all passing.** 35 need no database and run in ~5 s
(`pytest -m "not db"`); the rest use Postgres and take ~3.5 min, almost entirely
network latency to Neon. A local Postgres container in step 2 will cut that
sharply.

Isolation works by SAVEPOINT: the session is bound to a connection whose outer
transaction is always rolled back, and
`join_transaction_mode="create_savepoint"` turns a service's `commit()` into a
savepoint release. Production code needed no test-only branches, and the suite
leaves zero rows behind. An autouse fixture makes the OpenAI and Pinecone
clients raise, so a missing stub fails loudly instead of spending money.

Two findings worth recording:

- **`get_by_ids` had no project scoping**, so a stale or mismatched Pinecone id
  could surface another project's source. Namespaces prevented it in practice,
  but `project_id` is now a required argument. Found by writing the test.
- `pytest-asyncio` must be `>=1.0`. Before that,
  `asyncio_default_test_loop_scope` does not exist, tests and the session-scoped
  engine land on different event loops, and asyncpg fails with "another
  operation is in progress".

**Why first.** Everything after this is a refactor of working code. Tests are
what make those refactors safe, and there were none in the repo.

**Work.** A `backend/tests/` suite with `pytest` + `pytest-asyncio`. Most of the
logic already exists as throwaway verification scripts written during the
migration and needs porting, not inventing:

- `parser_service` — pure functions, highest value per line. Signature building,
  skip-dirs, syntax-error tolerance, `content_hash` derivation, methods vs
  top-level functions.
- `entity_service` — dedupe (including the `@overload` duplicate-qualname case),
  upsert identity stability across re-index, `delete_missing`.
- `auth_service` / `project_service` — happy paths plus the 400/401/404 branches.
- `search_service` — with Pinecone and OpenAI stubbed at the service boundary.
- One end-to-end test with both external APIs faked.

Use a transaction-rollback fixture so tests leave no rows behind. Aim for ~30
tests that would actually catch a regression, not coverage percentage.

**Verification.** `pytest` green locally, and deliberately breaking a known
behaviour makes a specific test fail.

**Buys.** The single most visible gap to anyone opening the repo.

---

### Step 2 — Docker Compose + CI

**Why second, and why before the worker.** The worker needs a second process,
and Compose is how you express that. It also dissolves the queue-backend
decision: if Redis is one line in a compose file, "Redis is annoying to install
on Windows" stops being an argument.

**Work.**

- `backend/Dockerfile` (slim base, non-root user, no venv inside the image)
- `frontend/Dockerfile` or leave the frontend to `npm run dev` for now
- `docker-compose.yml` with `api`, `worker`, `postgres`, and optionally `redis`.
  A local Postgres container also removes the dependency on Neon for
  development and stops tests burning free-tier compute.
- `.env.example` committed, real `.env` still ignored
- Entrypoint runs `alembic upgrade head` before starting
- GitHub Actions: lint + `pytest` against a Postgres service container

**Verification.** `docker compose up` on a clean checkout reaches a working app.
CI green on a pull request.

**Buys.** A reviewer can run it. Right now they can't without reading the README
carefully and having their own Neon and Pinecone accounts.

---

### Step 3 — Worker queue (the big one)

**Why.** This is where most of the scalability work lives — not because a queue
is magic, but because "move the work out of the request, bound it, chunk it,
make it retryable" are four faces of the same refactor and much cheaper done
together.

**Open decision: where the queue lives.** See §5.

**Work.**

- A `worker` process that consumes jobs; the API only enqueues
- An `indexing_jobs` record with `status`, `attempts`, `run_after`,
  `last_error`, and a heartbeat column
- **Startup reconciler** — jobs claimed but not finished (stale heartbeat) get
  requeued or failed. This is the specific fix for the stranded-project bug.
- Retries with exponential backoff and a terminal failure state after N attempts
- **Stream the pipeline.** Currently `parse → describe all → embed all → write
  all`. Change to chunks of ~200 entities flowing through describe → embed →
  write, checkpointing per chunk. This is what kills the 15,000-coroutine
  problem and makes a large repo resumable rather than all-or-nothing.
- Bounded worker concurrency, so ten users adding repos queues rather than
  launching ten simultaneous clones and ~100 in-flight LLM calls
- Add `POST /projects/{id}/reindex` — needed to exercise retries, and useful
- Add a `queued` status; the frontend stepper already labels `pending` as
  "Queued" so the UI copes

**Verification.** Kill the worker mid-index; on restart the job resumes or
retries and the project reaches `ready`. Kill the API mid-index; indexing
continues unaffected. Run two workers and confirm no job is processed twice.

**Buys.** The strongest engineering story available: a measured failure, a
designed fix, and a verified result.

---

### Step 4 — Incremental re-indexing

**Why after step 3.** Re-indexing is only sensible once it's a queued, retryable
job. Phase 4 of the migration deliberately laid the groundwork.

**Work.**

- Store the cloned commit SHA on `projects`
- On re-index, diff `content_hash` per `(file_path, qualname)`: describe and
  embed **only** changed or new entities; leave unchanged rows and their vectors
  alone
- Delete removed entities and their vectors (already implemented)
- Optionally a GitHub webhook so pushes trigger a re-index
- Replace the `NOT IN` list in `entity_service.delete_missing` with a per-run
  marker column — it's currently bounded by the statement parameter limit, which
  is fine at hundreds of entities and not at tens of thousands

**Verification.** Re-index with one function changed: exactly one LLM call, one
embedding, one updated row. Report the cost reduction as a number.

**Buys.** The clearest quantifiable win in the project. "Cut re-index cost by
~98% by content-hashing entities" is a resume line with a measurement behind it.

---

### Step 5 — Retrieval evaluation, then hybrid search

**Why.** There is currently no answer to "how do you know your search is good?"
That is the question a technical interviewer will ask about a semantic search
project, and it deserves a number.

**Work.**

- A golden set of ~50 `query → expected entity` pairs over a repo you know
- A script that reports **recall@5** and **MRR**
- Establish the dense-vector-only baseline
- Then improve it and measure the delta:
  - **Hybrid search** — Postgres `tsvector` over descriptions and source for the
    keyword half, Pinecone for the dense half, fused with reciprocal rank
    fusion. Both stores are already in place; this needs no new infrastructure.
  - **Metadata filters** — `entity_type` is already stored in Pinecone metadata
    and never used. Filtering to functions-only is nearly free.
  - Optionally a cross-encoder reranker over the top ~50
  - Optionally include the signature and file path in the embedded text, not
    just the LLM description

**Verification.** The eval script prints before/after numbers for each change.

**Buys.** More than any feature. "Improved recall@5 from 0.62 to 0.84 with
hybrid retrieval and RRF" beats a longer feature list, and it demonstrates the
habit of measuring.

---

### Step 6 — Hardening

Individually small, collectively the difference between "demo" and "deployed".

| Item | Detail |
|---|---|
| CORS | `main.py` uses `allow_origins=["*"]` with `allow_credentials=True` — an invalid combination browsers reject, and the wrong posture anyway. Read allowed origins from settings. |
| Error leakage | `indexing_service` stores `repr(e)` and the API returns it. `git clone` stderr can contain the repo URL with embedded credentials. Store detail in logs, return a generic message. |
| Clone URL validation | A user-supplied URL goes straight to `git clone`. It's argv so there's no shell injection, but nothing server-side restricts the scheme or host — `file://` and internal addresses are reachable. The frontend regex is not validation. |
| Rate limits | None on signup, login, or search. Login is open to credential stuffing; every search is a paid embedding call. |
| Quotas | No repo size cap and no per-user project limit. One large monorepo is an unbounded bill. |
| Structured logging | `print()` throughout. Move to `logging` with request and job IDs so a failed index is debuggable after the fact. |
| Real health check | `/health` returns `{"status":"ok"}` unconditionally. It should verify Postgres and Pinecone. |
| Pagination | `list_projects` returns everything; search has no paging. |
| Pin dependencies | The DB packages are pinned; `fastapi`, `uvicorn`, `openai`, `pinecone`, `python-jose`, `passlib` and others are not. Builds aren't reproducible. |
| Auth hygiene | `login` never checks `is_active`. JWTs sit in `localStorage` with no refresh and no revocation. |
| Frontend config | The API base is hardcoded to `http://127.0.0.1:8000`; move it to a Vite env var. Polling runs every 3 s indefinitely with no backoff or cap. |
| Store consistency | Postgres and Pinecone can drift if a run dies between the two writes. IDs make it reconcilable — add a job that reports and repairs the difference. |
| Service layer purity | `project_service` raises `HTTPException`, coupling the service layer to HTTP. Raise domain errors and translate at the route. |

---

### Step 7 — Multi-language parsing (optional)

Swap `ast` for **tree-sitter** to support JavaScript, TypeScript, and Go. Also
worth extending beyond top-level definitions — the parser currently ignores
nested functions and deeply nested classes.

Deliberately last. It widens the demo without deepening the engineering, and
steps 1–5 are what earn good interview questions.

---

## 4. Prioritised defect list

Independent of the sequence above, in rough order of how bad they are:

1. Indexing in-process → stranded jobs, no retry *(step 3)*
2. LLM stage creates one coroutine per entity; nothing bounds memory by repo
   size *(step 3)*
3. No tests *(step 1)*
4. Error messages leak internal detail to the client *(step 6)*
5. Clone URL unvalidated server-side *(step 6)*
6. CORS wildcard with credentials *(step 6)*
7. No rate limits or quotas *(step 6)*
8. Re-index re-describes everything *(step 4)*
9. No way to know if retrieval is any good *(step 5)*
10. `print()` logging *(step 6)*
11. Unpinned dependencies *(step 6)*
12. `delete_missing` bounded by statement parameter limit *(step 4)*

---

## 5. Open decisions

**Where the job queue lives.** Not yet decided; deliberately deferred until
after step 2, when both options are equally runnable.

| Option | For | Against |
|---|---|---|
| **Postgres `SKIP LOCKED`** | No new infrastructure. Enqueue commits in the same transaction as the project row, so the two can never disagree. Job history is just SQL. | Worker polls the database. Less conventional than naming Celery. |
| **Redis + ARQ** | Purpose-built, less code, instant pickup. Async-native, so it fits the existing `asyncio` code. The answer interviewers expect. | Another service. Enqueue and DB commit can diverge. Upstash's free tier (~10k commands/day) does not survive ARQ's default 0.5 s poll. |
| **Redis + Celery** | Most conventional. | Poor async support; heavier than this project needs. ARQ is the better fit if Redis wins. |

Current lean: **Postgres `SKIP LOCKED`**, because transactional enqueue removes
a whole class of drift and there is already exactly one durable store. "I
evaluated Redis and chose Postgres `SKIP LOCKED` because I already had a
transactional store and didn't want two sources of truth" is a stronger
interview answer than naming the default — but this is a judgement call, not a
fact.

---

## 6. Housekeeping

- **8 orphaned Pinecone namespaces (~7,518 vectors)** with ObjectId-style names
  from the Mongo era, referenced by no Postgres row. Deleting them is
  irreversible, so it needs an explicit decision.
- **Rotate the Neon password.** It was pasted into a chat transcript during the
  migration.
- **Update the git remote.** It still points at `code_retriever`; the repo was
  renamed to `CodeLens`. Pushes currently work through GitHub's redirect.
  `git remote set-url origin https://github.com/parvthummar/CodeLens.git`
- **`CLAUDE.md` is gitignored**, so its Postgres updates exist only locally. If
  it should be shared, drop that line from `.gitignore`.
- **`feat/postgres-migration`** still exists locally and on origin; safe to
  delete now that it's merged.

---

## 7. Explicitly not doing

- **pgvector / dropping Pinecone.** Evaluated during the migration and rejected:
  Pinecone's free tier is far more generous for vectors than Neon's ~0.5 GB, and
  hybrid search works fine across both stores. Reconsider only if the two-store
  split becomes a real bottleneck.
- **Microservices.** The API/worker split is the only decomposition this earns.
- **Kubernetes.** Compose is the right ceiling here.
- **A rewrite.** The layering is sound — services separated from routes, schemas
  from models, config centralised. Every item above is additive.
