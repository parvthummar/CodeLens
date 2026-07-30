from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Postgres (Neon). Stored as the raw libpq URLs from the Neon console;
    # normalised to asyncpg DSNs at engine-creation time.
    # database_url      -> pooled endpoint, used by the app
    # database_url_direct -> direct endpoint, used by Alembic migrations
    database_url: str = ""
    database_url_direct: str = ""

    # JWT
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # OpenAI
    openai_api_key: str = ""
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_dimensions: int = 1024
    openai_llm_model: str = "gpt-oss-20b"

    # Pinecone
    pinecone_api_key: str = ""
    pinecone_index_host: str = ""
    pinecone_index_name: str = "code-search"

    # Queue (Redis / ARQ). Redis delivers jobs; Postgres owns their state.
    redis_url: str = "redis://localhost:6379"

    # How many indexing runs one worker executes at once. This is the ceiling on
    # simultaneous clones and, through it, on in-flight LLM calls: each run is
    # itself capped at llm_concurrency.
    worker_concurrency: int = 2
    llm_concurrency: int = 10

    # Entities per describe -> embed -> write -> upsert cycle. Bounds peak
    # memory by chunk size instead of by repo size, and gives a retry something
    # to resume from. 200 x 1024 floats is a few MB.
    indexing_chunk_size: int = 200

    # Attempts includes the first run, so 3 means "one try plus two retries".
    job_max_attempts: int = 3
    job_backoff_base_seconds: int = 30
    job_backoff_max_seconds: int = 900
    # A worker silent for this long is treated as dead and its job reclaimed.
    # Must exceed job_heartbeat_interval_seconds by a comfortable margin, or a
    # slow write reclaims a job that is still running.
    job_heartbeat_timeout_seconds: int = 120
    job_heartbeat_interval_seconds: int = 15
    job_error_max_chars: int = 2000

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)


settings = Settings()
