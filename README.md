# CodeLens

A semantic code search tool that lets you search any public GitHub repository using natural language. Point it at a Python repo, wait for indexing, and ask questions like *"function that handles authentication"* or *"class for managing database connections"* — it returns the most relevant functions, classes, and methods ranked by semantic similarity.

## How It Works

1. **Connect a repo** — paste any public GitHub URL and submit
2. **Indexing runs in the background** — the backend clones the repo, parses every `.py` file using Python's `ast` module, and extracts all functions, classes, and methods
3. **LLM descriptions** — GPT-4o-mini generates a 2–3 sentence behavioural description for each code entity
4. **Storage** — entity records (source, signature, description, line numbers, content hash) are written to Postgres; their embeddings go to Pinecone under a project-specific namespace, keyed on the Postgres row id
5. **Search** — your query is embedded and ranked against Pinecone, which returns entity ids and scores; the full records are then hydrated from Postgres and returned with file path, line numbers, description, and relevance score

Only Python source files are indexed. The pipeline status (queued → cloning → indexing → ready) is shown live in the UI with 3-second polling.

## Tech Stack

### Backend
- **FastAPI** — async REST API with background tasks for the indexing pipeline
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

### Prerequisites
- Python 3.10+
- Node.js 18+
- A Postgres database (local, or a managed one such as Neon)
- OpenAI API key
- Pinecone account with an index created at **dimension 1024**

### Backend

```bash
# Create and activate a virtual environment
python -m venv backend/cr_venv
backend\cr_venv\Scripts\Activate.ps1   # Windows
# source backend/cr_venv/bin/activate  # macOS/Linux

pip install -r backend/requirements.txt
```

Create `backend/.env`:

```env
# Pooled connection, used by the app
DATABASE_URL=postgresql://user:pass@host/db?sslmode=require
# Direct connection (no PgBouncer), used by Alembic. On Neon this is the same
# host without "-pooler". Falls back to DATABASE_URL if unset.
DATABASE_URL_DIRECT=

JWT_SECRET=

OPENAI_API_KEY=
OPENAI_EMBEDDING_MODEL=
OPENAI_EMBEDDING_DIMENSIONS=
OPENAI_LLM_MODEL=

PINECONE_API_KEY=
PINECONE_INDEX_NAME=
PINECONE_INDEX_HOST=
```

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

### Frontend

```bash
cd frontend
npm install
npm run dev
# App → http://localhost:5173
```

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
| POST | `/api/v1/projects/` | Add a new project (triggers indexing) |
| GET | `/api/v1/projects/{id}` | Get project status |
| DELETE | `/api/v1/projects/{id}` | Delete project + Pinecone vectors |
| POST | `/api/v1/projects/{id}/search` | Semantic search |
