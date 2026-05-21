"""Application settings; loads .env via pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite+aiosqlite:///./data/ballast.db"

    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""

    google_calendar_credentials_file: str = ""
    google_calendar_id: str = "primary"

    openai_api_key: str = ""
    llm_model: str = "gpt-4o-mini"

    admin_api_key: str = ""


settings = Settings()
