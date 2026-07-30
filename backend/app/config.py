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

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)


settings = Settings()
