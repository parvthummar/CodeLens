from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.v1.router import v1_router
from app.db.postgres import dispose_engine, get_engine
from app.services import queue_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    # Fail fast on a bad DATABASE_URL rather than surfacing it on first request.
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    print("[OK] Postgres reachable")

    # Redis is checked but not required to start. Enqueue failures are already
    # recoverable — the job row is the record and the worker reconciles it — so
    # refusing to serve login and search because the queue is down would be a
    # worse outage than the one being reported.
    try:
        pool = await queue_service.get_pool()
        await pool.ping()
        print("[OK] Redis reachable")
    except Exception as e:
        print(f"[WARN] Redis unreachable ({e!r}); new indexing jobs will wait "
              "for the worker's reconciler")

    yield

    await queue_service.close_pool()
    await dispose_engine()
    print("[STOP] Postgres connection pool closed")


app = FastAPI(
    title="Code Search API",
    description="Search codebases using natural language",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — allow everything during development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount versioned API router
app.include_router(v1_router)


@app.get("/health", tags=["Health"])
async def health_check():
    """Basic liveness probe."""
    return {"status": "ok"}
