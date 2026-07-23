from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # MongoDB
    mongo_uri: str
    mongo_db_name: str

    # JWT
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # OpenAI
    openai_api_key: str = ""
    openai_embedding_model: str = "text-embedding-3-small"
    openai_llm_model: str = "gpt-4o-mini"

    # Pinecone
    pinecone_api_key: str = ""
    pinecone_index_host: str = ""
    pinecone_index_name: str = "code-search"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)


settings = Settings()