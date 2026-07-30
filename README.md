# CodeLens

A semantic code search tool that lets you search any public GitHub repository using natural language. Point it at a Python repo, wait for indexing, and ask questions like *"function that handles authentication"* or *"class for managing database connections"* — it returns the most relevant functions, classes, and methods ranked by semantic similarity.

## How It Works

1. **Connect a repo** — paste any public GitHub URL and submit
2. **Indexing runs in a separate worker** — the API writes a job row and returns; a worker process clones the repo, parses every `.py` file using Python's `ast` module, and extracts all functions, classes, and methods
3. **LLM descriptions** — GPT-4o-mini generates a 2–3 sentence behavioural description for each code entity
4. **Storage** — entity records (source, signature, description, line numbers, content hash) are written to Postgres; their embeddings go to Pinecone under a project-specific namespace, keyed on the Postgres row id
5. **Search** — your query is embedded and ranked against Pinecone, which returns entity ids and scores; the full records are then hydrated from Postgres and returned with file path, line numbers, description, and relevance score

Only Python source files are indexed. The pipeline status (queued → cloning → indexing → ready) is shown live in the UI with 3-second polling.

Entities flow through describe → embed → write in chunks of 200 rather than being staged whole, so peak memory is bounded by the chunk size instead of by repository size, and each chunk is durable in both stores before the next one starts. A failed run is retried with exponential backoff; a worker that dies mid-run stops writing its heartbeat, and a reconciler requeues whatever it was holding.

## Tech Stack

### Backend
- **FastAPI** — async REST API; it enqueues indexing work and never runs it
- **ARQ + Redis** — job delivery to the worker. Job *state* (attempts, backoff, heartbeat, progress) lives in Postgres, so an undelivered job is recoverable and claiming is an atomic UPDATE rather than a Redis pop
- **Postgres** (SQLAlchemy 2.0 async + asyncpg, Alembic migrations) — users, projects, and parsed code entities
- **Pinecone** — vector database for the embeddings; entity records live in Postgres, so Pinecone holds only vectors keyed on `entities.id`
- **OpenAI API** — `gpt-4o-mini` for description generation, `text-embedding-3-small` for embeddings
- **Python AST** — parses source files without executing them
- **JWT + bcrypt** — authentication with `python-jose` and `passlib`

### Frontend
- **React 19** + **Vite 8** + **React Router v7**
- **PrismJS** — syntax-highlighted source code view in search results
- Dark glassmorphism design system with CSS variables, backdrop-filter blur, and gradient accents

## Getting Started

The quickest path is Docker Compose, which brings up Postgres, the API and the
frontend together. Running the pieces directly on your machine is documented
below it.

Either way you need an **OpenAI API key** and a **Pinecone index created at
dimension 1024** — those are external services with no local substitute. Signup,
login and project CRUD work without them; indexing and search do not.

### Docker Compose

```bash
cp backend/.env.example backend/.env
# Fill in JWT_SECRET, OPENAI_API_KEY and PINECONE_API_KEY.
# Leave the DATABASE_URL lines alone — compose points them at the postgres
# container regardless.

docker compose up --build
```

- Frontend → http://localhost:5173
- API → http://127.0.0.1:8000 (docs at `/docs`)
- Postgres → `localhost:5432`, user/password/database all `codelens`
- Redis → `localhost:6379`; the `worker` service consumes from it

Run more than one worker with `docker compose up -d --scale worker=2`. Jobs are
claimed with an atomic UPDATE in Postgres, so extra workers share the queue
rather than duplicating work.

Migrations run automatically from the API container's entrypoint, so a clean
checkout reaches a working app in one command. Postgres data persists in the
`postgres_data` volume; `docker compose down -v` resets it.

The frontend container runs the Vite dev server with the source bind-mounted,
so edits hot-reload. Backend changes need `docker compose up --build api`.

A Redis service is defined but not started by default — `docker compose
--profile queue up` enables it. It is there for the worker-queue work in
[the roadmap](docs/roadmap.md), which has not picked a queue backend yet.

### Running without Docker

Prerequisites: Python 3.11, Node.js 18+, and a Postgres database (local, or a
managed one such as Neon).

#### Backend

```bash
# Create and activate a virtual environment
python -m venv backend/cr_venv
backend\cr_venv\Scripts\Activate.ps1   # Windows
# source backend/cr_venv/bin/activate  # macOS/Linux

pip install -r backend/requirements.txt
```

Create `backend/.env` by copying `backend/.env.example`, which documents every
key including the pooled-vs-direct database URL split that Alembic needs.

Create the schema, then run the API:

```bash
cd backend

# Optional: verify the database connection and pooling config first
python scripts/check_db.py

# Apply migrations (required — the app does not create tables itself)
alembic upgrade head

uvicorn app.main:app --reload
# API → http://127.0.0.1:8000
# Docs → http://127.0.0.1:8000/docs
```

#### Frontend

```bash
cd frontend
npm install
npm run dev
# App → http://localhost:5173
```

### Running the worker without Docker

```bash
cd backend
arq app.worker.WorkerSettings
```

Needs `REDIS_URL` pointing at a running Redis. Without a worker the API still
serves everything except indexing — projects sit in `queued` until one starts,
at which point its reconciler picks them up.

## Tests and Lint

The suite is 228 tests. 35 of them need no database, and none need Redis:

```bash
cd backend
pip install -r requirements.txt -r requirements-dev.txt

pytest -m "not db"   # ~5 s, no database
pytest               # everything; needs DATABASE_URL_DIRECT and a migrated schema
ruff check .
```

Against the compose Postgres, `DATABASE_URL_DIRECT` in `backend/.env` already
points at `localhost:5432`, so `docker compose up -d postgres` is enough to run
the full suite from the host. Tests roll back everything they write.

```bash
cd frontend
npm run lint     # oxlint, not ESLint
npm run build
```

CI (`.github/workflows/ci.yml`) runs all of the above on every push and pull
request, with Postgres as a service container, plus a job that builds both
Docker images.

## Project Structure

```
backend/
  alembic/        migrations
  scripts/        check_db.py connection smoke test
  app/
    api/v1/       auth + project routes
    services/     github, parser, llm, embedding, pinecone, entity, indexing, search
    models/       SQLAlchemy models (User, Project, Entity)
    db/           async engine, session factory, get_db dependency
    core/         JWT, bcrypt, FastAPI dependencies

frontend/src/
  pages/          Login, Signup, Dashboard, AddProject, ProjectDetail, Search
  components/     Navbar, GlassCard, CodeBlock, StatusBadge, Modal, ProtectedRoute
  context/        AuthContext (JWT + email persisted in localStorage)
  api/            fetch wrapper with auto 401 handling
```

## API Overview

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/auth/signup` | Register a new user |
| POST | `/api/v1/auth/login` | Login, returns JWT |
| GET | `/api/v1/projects/` | List user's projects |
| POST | `/api/v1/projects/` | Add a new project (queues indexing) |
| GET | `/api/v1/projects/{id}` | Get project status |
| POST | `/api/v1/projects/{id}/reindex` | Queue another indexing run (409 if one is already active) |
| DELETE | `/api/v1/projects/{id}` | Delete project + Pinecone vectors |
| POST | `/api/v1/projects/{id}/search` | Semantic search |
