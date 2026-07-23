from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db.mongodb import init_db, close_db
from app.api.v1.router import v1_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    await init_db()
    print("[OK] MongoDB connected & Beanie initialised")
    yield
    await close_db()
    print("[STOP] MongoDB connection closed")


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
