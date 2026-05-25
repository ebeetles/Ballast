"""Application settings; loads .env via pydantic-settings."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> backend/ -> repo root
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _BACKEND_ROOT.parent


def _discover_env_files() -> tuple[str, ...]:
    """Find .env whether uvicorn is started from backend/ or repo root."""
    candidates = (
        Path.cwd() / ".env",
        _BACKEND_ROOT / ".env",
        _REPO_ROOT / ".env",
    )
    found = tuple(str(p) for p in candidates if p.is_file())
    return found if found else (".env",)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_discover_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite+aiosqlite:///./data/ballast.db"

    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""

    google_calendar_credentials_file: str = ""
    google_calendar_id: str = "primary"
    # IANA timezone for scheduling when user.timezone is unset or still UTC.
    default_user_timezone: str = "America/Los_Angeles"

    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # Intent classification — fast and cheap, haiku is sufficient.
    router_model: str = "claude-haiku-4-5-20251001"
    # Cognitive loop and onboarding — needs full reasoning and persona.
    cognitive_model: str = "claude-sonnet-4-5-20250929"
    # Lightweight reflection tasks (nightly jobs, insight scoring).
    reflection_model: str = "claude-haiku-4-5-20251001"

    # Deprecated: kept so any external code referencing llm_model still works.
    llm_model: str = "claude-haiku-4-5-20251001"

    admin_api_key: str = ""
    api_key: str = ""


settings = Settings()
