from beanie import Document, Indexed
from pydantic import EmailStr, Field
from datetime import datetime, timezone


class User(Document):
    email: Indexed(EmailStr, unique=True)
    hashed_password: str
    full_name: str | None = None
    is_active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "users"