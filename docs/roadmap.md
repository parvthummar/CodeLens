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

**The headline problem that remained.** Indexing ran inside the web process via
FastAPI `BackgroundTasks` (`api/v1/projects.py`). This was measured, not
assumed: kill the server mid-index and the project sat in `indexing`
**forever** — no retry, no reconciler, no way for a user to recover it.
**Closed by step 3**, and the fix was verified by reproducing the failure: a
`SIGKILL`'d worker leaves the project in `cloning`, and the reconciler brings it
back to `ready` on the next sweep.

---

## 2. What "good" means here

Not speed. Six properties, and the project currently fails four of them:

| Property | Now (after step 3) |
|---|---|
| Work is decoupled from the request lifecycle | ✅ the API enqueues; a separate worker indexes |
| Resource use per unit of work is bounded | ✅ chunks of 200 through describe → embed → write, LLM concurrency capped per run, worker concurrency capped per process |
| You can run N copies | ✅ `--scale worker=2`; claiming is an atomic UPDATE, verified as no double processing |
| Work survives failure | ✅ retries with backoff, terminal after 3 attempts, heartbeat reconciler for dead workers |
| Cost grows sub-linearly with data | 🟡 `content_hash` exists but nothing consumes it yet — step 4 |
| One tenant can't starve the others | 🟡 the queue serialises work now, but there is still no repo size cap, quota, or rate limit — step 6 |

Scale estimate, extrapolated from a measured 56-entity run at ~0.36 s/entity: a
Django-sized repo (~15,000 entities) would take **~1.5 hours in a single
process**, holding ~15,000 coroutines and ~15,000 × 1024 floats in memory, with
no checkpoint. That is arithmetic, not a measurement.

Step 3 removed the memory half of it — peak is now 200 entities, not 15,000,
regardless of repo size — and made the run survivable, so the wall clock is
finally worth measuring for real. Still to do.

---

## 3. The sequence

### Step 1 — Tests ✅ done

**151 tests, all passing.** 35 need no database and run in ~5 s
(`pytest -m "not db"`); the rest use Postgres and take ~3.5 min, almost entirely
network latency to Neon. Step 2 moved that onto a local container and the full
suite now runs in 38.6 s.

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

### Step 2 — Docker Compose + CI ✅ done

**Why second, and why before the worker.** The worker needs a second process,
and Compose is how you express that. It also dissolves the queue-backend
decision: if Redis is one line in a compose file, "Redis is annoying to install
on Windows" stops being an argument.

**What shipped.**

- `backend/Dockerfile` — `python:3.11-slim` matching the venv, non-root
  `appuser`, no venv inside the image, `git` installed because
  `github_service` shells out to it for every index
- `backend/docker-entrypoint.sh` — runs `alembic upgrade head`, then execs the
  command. `RUN_MIGRATIONS=false` opts a second container out, so the step 3
  worker won't race the API through alembic on startup.
- `frontend/Dockerfile` — Vite dev server with the source bind-mounted. A
  static build would bake in an API host, and the API base is still hardcoded
  in `client.js`; this becomes a two-stage build + nginx once step 6 moves it
  to a Vite env var.
- `docker-compose.yml` — `postgres` (healthchecked, published on 5432 so the
  test suite can reach it from the host), `api`, `frontend`, and `redis`
  behind a `queue` profile so it is one flag away without prejudging §5
- `backend/.env.example`, plus a `!backend/.env.example` negation in
  `.gitignore` — the existing `backend/.env.*` pattern was swallowing it
- `.gitattributes` forcing LF on `*.sh`: a Windows checkout otherwise gives the
  entrypoint CRLF endings and the container dies on the shebang with a "no such
  file or directory" that names a file that plainly exists
- `backend/ruff.toml` and `.github/workflows/ci.yml` — three jobs: backend
  (ruff + alembic + pytest against a Postgres service container), frontend
  (oxlint + build), and a job that builds both images from the compose file

**Verification (run, not assumed).** `docker compose up --build` on an empty
volume: migrations applied, `/health` 200, frontend 200, signup → login →
authenticated `GET /projects/` round-trips through the containerised Postgres.
`git clone` confirmed working as uid 10001 inside the API container. The redis
profile starts on demand and stays out of the default `up`.

**Measured: the local Postgres is 5.4× faster for tests.** The full 151-test
suite runs in **38.6 s** against the compose container versus ~3.5 min against
Neon. Step 1 guessed this would help; it is most of the wall clock, and it also
stops the suite burning free-tier compute.

**Two judgement calls worth recording.**

- *No `worker` service yet.* The plan listed one, but there is no worker code
  until step 3, and a service that exits immediately is worse than an absent
  one. Adding it is ~10 lines against the same image with `RUN_MIGRATIONS=false`
  and a different command.
- *The lint gate is narrow on purpose.* A default ruff ruleset flagged 191
  violations, 136 of them line-length. Gating on that would have meant
  reflowing ~40 unrelated lines inside the commit that introduces CI, so the
  selection is `E4,E7,E9,F,I,W` — undefined names, unused imports, import
  order, whitespace. That left 19 auto-fixable issues, all applied; one was a
  genuinely unused import in `test_indexing_service`. Formatting is a separate
  pass. On the frontend, oxlint exits 0 on warnings, so CI pins
  `--max-warnings 5` as a ratchet against new ones rather than an unfailable
  step.

**Buys.** A reviewer can run it. Before this they couldn't without reading the
README carefully and having their own Neon and Pinecone accounts — now only the
OpenAI and Pinecone keys are still unavoidable, and everything short of
indexing and search works without them.

---

### Step 3 — Worker queue (the big one) ✅ done

**Why.** This is where most of the scalability work lives — not because a queue
is magic, but because "move the work out of the request, bound it, chunk it,
make it retryable" are four faces of the same refactor and much cheaper done
together.

**Decision: Redis + ARQ, with job state in Postgres.** §5 is settled. ARQ
delivers a job id to a worker and bounds how many run at once; that is all it
does. Everything durable — status, attempts, `run_after`, `last_error`,
heartbeat, progress — lives in an `indexing_jobs` table.

That split is what answers the objection §5 raised against Redis. Enqueue and
the DB commit *can* still diverge, but the divergence is no longer lossy: the
job row commits in the same transaction as the project row, so if the Redis
enqueue fails the row still reads `queued` and the reconciler delivers it. The
API's enqueue is deliberately allowed to fail silently for exactly this reason.
It also means the atomic DB claim, not Redis, is what guarantees a job runs
once — which is the property that actually matters and the one Redis alone
could not provide.

**What shipped.**

- `app/worker.py`, run as `arq app.worker.WorkerSettings`. The API only writes
  a job row and hands the id to Redis; `BackgroundTasks` is gone.
- `indexing_jobs` with `status`, `attempts`, `run_after`, `last_error`,
  `heartbeat_at`, and `entities_done` / `entities_total` checkpoints
- `job_service.claim` — one conditional UPDATE. A job is claimable if it is
  queued and due, or running with a stale heartbeat. Two workers handed the
  same message: exactly one gets a row.
- **Attempts increment on claim, not on failure.** A job that kills its worker
  outright never reports a failure; counting only clean failures would retry it
  forever.
- **Reconciler, at startup *and* every 30 s.** Startup alone turned out to be
  insufficient: restart quickly enough and the dead worker's heartbeat is not
  yet stale, so the startup pass correctly skips the job — and with no later
  pass, nothing ever looks at it again. That is the same stranded-job bug in a
  narrower window, so the sweep repeats.
- Exponential backoff (30 s, capped at 15 min), terminal failure after 3
  attempts
- **The pipeline streams.** `parse → describe all → embed all → write all`
  became chunks of 200 flowing through describe → embed → write → upsert, each
  chunk durable in both stores before the next starts, with a checkpoint after.
  Deletion still runs once at the end — an entity is only "missing" relative to
  the complete run.
- **The pipeline raises instead of swallowing.** Classifying a failure as
  transient or terminal is the worker's job. Swallowing every exception is what
  made a rate limit indistinguishable from a repo that will never parse.
- `worker_concurrency` (default 2) bounds simultaneous clones and, through
  them, in-flight LLM calls
- `POST /projects/{id}/reindex`, guarded with 409 against double-queueing the
  most expensive operation in the app
- `queued` status end to end, including the frontend stepper; legacy `pending`
  rows normalise to it

**Verification — run against the compose stack, not asserted.** Using
`github.com/mdn/content`: 260 MB, ~13 s to clone, and zero `.py` files, so the
pipeline runs its full course with a realistically long job and no paid calls
at all.

| Roadmap criterion | Result |
|---|---|
| Kill the worker mid-index | `SIGKILL` during clone left the project in `cloning` with a running job — the old bug, reproduced. Worker restarted, reconciler requeued it, `attempt=2`, project reached `ready`. |
| Kill the API mid-index | `docker compose stop api`; health check unreachable (`000`), indexing continued and the project reached `ready`. |
| Two workers, no double processing | Six jobs, two workers: all six `succeeded` with `attempts=1`, and the intersection of the two workers' claim logs was empty. |

Across the whole session: 9 job rows, 10 total claims. The extra claim is the
worker that got killed — exactly one retry, exactly where one was expected.

**Tests: 151 → 228.** The new ones cover claim contention, backoff, terminal
failure, the reconciler's three populations, chunk boundaries, and that an
earlier chunk survives a later failure. None of them need a running Redis —
`conftest` makes the client raise, so a missing stub fails loudly.

**Still open.** A retry re-describes from the beginning. Per-chunk commits mean
completed chunks are already durable, but nothing yet *skips* them on the next
attempt — that is step 4's `content_hash` diff, which is what makes a resumed
run cheap rather than merely correct.

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
| Error leakage | Fixed for indexing in step 3: the detail goes to `indexing_jobs.last_error` and the logs, and the client gets a generic message — `git clone` stderr can contain the repo URL with embedded credentials. The remaining routes still return raw `HTTPException` detail. |
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

1. ~~Indexing in-process → stranded jobs, no retry~~ ✅ *(step 3)*
2. ~~LLM stage creates one coroutine per entity; nothing bounds memory by repo
   size~~ ✅ *(step 3)*
3. ~~No tests~~ ✅ *(step 1)*
4. Error messages leak internal detail to the client *(step 6)* — the indexing
   path is fixed (the client now gets a generic message and the detail stays in
   `indexing_jobs.last_error`); the other routes still leak
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

**Where the job queue lives.** ✅ Decided in step 3: **Redis + ARQ for delivery,
Postgres for job state.**

| Option | For | Against |
|---|---|---|
| **Postgres `SKIP LOCKED`** | No new infrastructure. Enqueue commits in the same transaction as the project row, so the two can never disagree. Job history is just SQL. | Worker polls the database. Less conventional than naming Celery. |
| **Redis + ARQ** ✅ | Purpose-built, less code, instant pickup. Async-native, so it fits the existing `asyncio` code. The answer interviewers expect. | Another service. Enqueue and DB commit can diverge. Upstash's free tier (~10k commands/day) does not survive ARQ's default 0.5 s poll. |
| **Redis + Celery** | Most conventional. | Poor async support; heavier than this project needs. ARQ is the better fit if Redis wins. |

The "enqueue and DB commit can diverge" objection turned out to be answerable
rather than disqualifying, and answering it produced a better design than
either option alone. The job row commits with the project row, so divergence
means at worst an undelivered job — which the reconciler repairs — never a lost
one. And because claiming is an atomic Postgres UPDATE rather than a Redis pop,
the at-most-once guarantee survives duplicate delivery, a Redis flush, or two
workers racing.

The honest framing: this is a hybrid, and the hybrid exists because Redis on
its own could not be trusted with the durable record. Pure `SKIP LOCKED` would
have been less machinery for the same guarantees, at the cost of polling.

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
