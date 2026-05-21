#!/usr/bin/env python3
"""Create the Ballast project directory tree with stub files.

Re-run safely: skips files that already exist unless --force is passed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (relative_path, content). None content => skip if exists without --force
FILES: dict[str, str | None] = {}


def _register(path: str, content: str) -> None:
    FILES[path] = content


def _py_module(doc: str, extra: str = "") -> str:
    body = extra.strip()
    if body:
        body = "\n\n" + body + "\n"
    return f'"""{doc}"""\n{body}'


def _py_stub(doc: str) -> str:
    return _py_module(doc, "pass\n")


def _prompt_stub(title: str) -> str:
    return f"# {title}\n# TODO: Replace with production prompt.\n"


def build_file_registry() -> None:
    # --- Root ---
    _register(
        ".gitignore",
        """# Environment
.env

# Python
__pycache__/
*.py[cod]
*$py.class
*.egg-info/
.eggs/
dist/
build/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.venv/
venv/
env/

# Backend runtime
backend/data/
backend/logs/

# IDE
.idea/
.vscode/
*.swp
.DS_Store
""",
    )
    _register("README.md", "# Ballast\n\nPersonal AI accountability agent.\n")
    _register("PLAN.md", "# Ballast — Product Plan\n\nSee `.cursor/rules/architecture.mdc` for system design.\n")
    _register(
        ".env.example",
        """# Application
APP_ENV=development
LOG_LEVEL=INFO

# Database (SQLite path relative to backend/)
DATABASE_URL=sqlite+aiosqlite:///./data/ballast.db

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=

# Google Calendar
GOOGLE_CALENDAR_CREDENTIALS_FILE=
GOOGLE_CALENDAR_ID=primary

# LLM
OPENAI_API_KEY=
LLM_MODEL=gpt-4o-mini

# Security (reserved for future admin routes)
ADMIN_API_KEY=
""",
    )

    # --- Backend packaging ---
    _register(
        "backend/pyproject.toml",
        """[project]
name = "ballast"
version = "0.1.0"
description = "Personal AI accountability agent"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    "pydantic-settings>=2.6.0",
    "sqlalchemy[asyncio]>=2.0.36",
    "aiosqlite>=0.20.0",
    "alembic>=1.14.0",
    "httpx>=0.28.0",
    "apscheduler>=3.10.4",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.3.0",
    "pytest-asyncio>=0.24.0",
    "httpx>=0.28.0",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["."]

[tool.setuptools.packages.find]
where = ["."]
include = ["app*"]
""",
    )
    _register(
        "backend/requirements.txt",
        """fastapi>=0.115.0
uvicorn[standard]>=0.32.0
pydantic-settings>=2.6.0
sqlalchemy[asyncio]>=2.0.36
aiosqlite>=0.20.0
alembic>=1.14.0
httpx>=0.28.0
apscheduler>=3.10.4
pytest>=8.3.0
pytest-asyncio>=0.24.0
""",
    )
    _register(
        "backend/Dockerfile",
        """FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
""",
    )
    _register(
        "backend/docker-compose.yml",
        """services:
  api:
    build: .
    ports:
      - "8000:8000"
    env_file:
      - ../.env
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
""",
    )
    _register("backend/data/.gitkeep", "")
    _register("backend/logs/.gitkeep", "")

    # --- Alembic ---
    _register(
        "backend/alembic.ini",
        """[alembic]
script_location = alembic
prepend_sys_path = .
version_path_separator = os

sqlalchemy.url = driver://user:pass@localhost/dbname

[post_write_hooks]

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
""",
    )
    _register("backend/alembic/script.py.mako", None)  # use alembic init template if missing
    _register("backend/alembic/versions/.gitkeep", "")

    # --- App root ---
    _register("backend/app/__init__.py", _py_module("Ballast application package."))
    _register(
        "backend/app/main.py",
        _py_module(
            "Composition root: FastAPI app, router registration, scheduler lifecycle.",
            '''from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1 import api_router
from app.scheduler.engine import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield
    stop_scheduler()


def create_app() -> FastAPI:
    app = FastAPI(title="Ballast", lifespan=lifespan)
    app.include_router(api_router, prefix="/api/v1")
    return app


app = create_app()
''',
        ),
    )

    # --- API ---
    _register(
        "backend/app/api/v1/__init__.py",
        _py_module(
            "Aggregates v1 API routers.",
            '''from fastapi import APIRouter

from app.api.v1 import health, webhook

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(webhook.router, tags=["webhook"])
''',
        ),
    )
    _register(
        "backend/app/api/v1/webhook.py",
        _py_module(
            "Telegram webhook endpoint. Delegates to dispatcher only.",
            '''from fastapi import APIRouter, Request

from app.telegram.dispatcher import handle_update

router = APIRouter()


@router.post("/webhook/telegram")
async def telegram_webhook(request: Request) -> dict[str, str]:
    update = await request.json()
    await handle_update(update)
    return {"status": "ok"}
''',
        ),
    )
    _register(
        "backend/app/api/v1/health.py",
        _py_module(
            "Health check endpoint.",
            '''from fastapi import APIRouter

from app.api.v1.schemas.health import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")
''',
        ),
    )
    _register("backend/app/api/v1/schemas/__init__.py", _py_module("Pydantic schemas for API v1."))
    _register(
        "backend/app/api/v1/schemas/health.py",
        _py_module(
            "Health check schemas.",
            '''from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
''',
        ),
    )
    _register(
        "backend/app/api/v1/schemas/webhook.py",
        _py_module("Telegram webhook payload schemas.", "pass\n"),
    )
    _register(
        "backend/app/api/v1/schemas/common.py",
        _py_module("Shared API types.", "pass\n"),
    )

    # --- Core ---
    for name, doc in [
        ("config.py", "Application settings; loads .env via pydantic-settings."),
        ("logging.py", "Structured logger setup."),
        ("security.py", "API key validation for future admin routes."),
        ("exceptions.py", "Custom application exception classes."),
    ]:
        _register(f"backend/app/core/{name}", _py_stub(doc))

    # --- DB ---
    _register("backend/app/db/session.py", _py_stub("Async SQLAlchemy session factory."))
    _register("backend/app/db/base.py", _py_stub("Declarative base and base CRUD class."))
    _register(
        "backend/app/db/models/__init__.py",
        _py_module(
            "ORM models; re-exported for Alembic.",
            '''from app.db.models.insight import Insight
from app.db.models.task import Task
from app.db.models.time_debt import TimeDebt
from app.db.models.user import User

__all__ = ["User", "Task", "TimeDebt", "Insight"]
''',
        ),
    )
    for model, doc in [
        ("user.py", "User model with onboarding_status, onboarding_step, and goals fields."),
        ("task.py", "Task model."),
        ("time_debt.py", "Time debt model."),
        ("insight.py", "Insight model. Written only via insight_engine."),
    ]:
        _register(f"backend/app/db/models/{model}", _py_stub(doc))

    # --- Services ---
    for svc, doc in [
        ("task_service.py", "Task CRUD and scheduling logic."),
        ("debt_service.py", "All time debt mutations."),
        ("user_service.py", "User management; goals and preferences for north_star."),
        ("schedule_service.py", "Schedule proposal and commit domain logic."),
        ("onboarding_service.py", "Onboarding persistence: goals, preferences, initial tasks."),
    ]:
        _register(f"backend/app/services/{svc}", _py_stub(doc))

    # --- Agent ---
    for name, doc in [
        ("router.py", "Intent classification only."),
        ("cognitive_loop.py", "ReAct reasoning loop for general_chat."),
        ("context_assembler.py", "Builds LLM prompt context from all sources."),
        ("response_formatter.py", "Formats agent output for Telegram."),
    ]:
        _register(f"backend/app/agent/{name}", _py_stub(doc))
    _register("backend/app/agent/tools/__init__.py", _py_module("Agent tools invoked by cognitive_loop."))
    _register("backend/app/agent/tools/task_tools.py", _py_stub("Task tools for cognitive_loop."))
    for prompt, title in [
        ("system.txt", "System"),
        ("intent_router.txt", "Intent router"),
        ("cognitive_loop.txt", "Cognitive loop"),
        ("onboarding.txt", "Onboarding"),
    ]:
        _register(f"backend/app/agent/prompts/{prompt}", _prompt_stub(title))

    # --- Memory ---
    _register("backend/app/memory/north_star.py", _py_stub("Layer 1 goal context via user_service."))
    _register("backend/app/memory/insight_engine.py", _py_stub("Layer 2 insights; sole writer to insights table."))

    # --- Calendar ---
    for name, doc in [
        ("gcal_client.py", "Google Calendar API client."),
        ("overlap_checker.py", "Calendar conflict detection."),
        ("slot_finder.py", "Free slot discovery."),
    ]:
        _register(f"backend/app/calendar/{name}", _py_stub(doc))

    # --- Scheduler ---
    _register(
        "backend/app/scheduler/engine.py",
        _py_module(
            "APScheduler setup; started from main.py lifespan.",
            '''def start_scheduler() -> None:
    """Register and start scheduled jobs."""
    pass


def stop_scheduler() -> None:
    """Shut down the scheduler."""
    pass
''',
        ),
    )
    for job, doc in [
        ("heartbeat.py", "Heartbeat job; orchestrates services/agent only."),
        ("proof_of_work.py", "Proof of work job; orchestrates services/agent only."),
        ("nightly_reflection.py", "Nightly reflection; calls insight_engine only for insight writes."),
    ]:
        _register(f"backend/app/scheduler/jobs/{job}", _py_stub(doc))

    # --- Telegram ---
    _register("backend/app/telegram/client.py", _py_stub("Telegram bot API wrapper and signature verification."))
    _register(
        "backend/app/telegram/dispatcher.py",
        _py_module(
            "Routes updates: onboarding gate, then agent/router, then handler.",
            '''async def handle_update(update: dict) -> None:
    """Entry point from webhook. Checks onboarding, then routes intent."""
    pass
''',
        ),
    )
    for handler, doc in [
        ("onboarding.py", "Multi-turn onboarding; uses onboarding.txt and onboarding_service."),
        ("push_task.py", "Thin handler for push_task intent."),
        ("complete_task.py", "Thin handler for complete_task intent."),
        ("add_task.py", "Thin handler for add_task intent."),
        ("general_chat.py", "Delegates to cognitive_loop."),
    ]:
        _register(f"backend/app/telegram/handlers/{handler}", _py_stub(doc))

    # --- Tests ---
    _register("backend/tests/conftest.py", _py_module("Shared pytest fixtures.", "pass\n"))
    _register("backend/tests/unit/services/.gitkeep", "")
    _register("backend/tests/unit/agent/.gitkeep", "")
    _register("backend/tests/unit/calendar/.gitkeep", "")
    _register("backend/tests/unit/memory/.gitkeep", "")
    _register("backend/tests/integration/api/.gitkeep", "")
    _register("backend/tests/integration/telegram/.gitkeep", "")
    _register("backend/tests/integration/scheduler/.gitkeep", "")

    test_files = [
        "backend/tests/unit/services/test_task_service.py",
        "backend/tests/unit/services/test_debt_service.py",
        "backend/tests/unit/services/test_user_service.py",
        "backend/tests/unit/services/test_schedule_service.py",
        "backend/tests/unit/services/test_onboarding_service.py",
        "backend/tests/unit/agent/test_router.py",
        "backend/tests/unit/agent/test_cognitive_loop.py",
        "backend/tests/unit/agent/test_context_assembler.py",
        "backend/tests/unit/agent/test_task_tools.py",
        "backend/tests/integration/api/test_webhook.py",
        "backend/tests/integration/api/test_health.py",
        "backend/tests/integration/telegram/test_dispatcher.py",
        "backend/tests/integration/telegram/test_onboarding_flow.py",
        "backend/tests/integration/scheduler/test_jobs.py",
    ]
    for path in test_files:
        _register(path, _py_module(f"Tests for {path.split('/')[-1]}.", "pass\n"))

    # --- Raycast placeholder ---
    _register("raycast-extension/.gitkeep", "")

    # --- Cursor rules (stubs; populated by scaffold or separate step) ---
    _register(".cursor/rules/.gitkeep", None)


def write_files(force: bool) -> tuple[int, int, int]:
    created = skipped = errors = 0
    for rel_path, content in FILES.items():
        path = ROOT / rel_path
        if content is None:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
                created += 1
            else:
                skipped += 1
            continue
        if path.exists() and not force:
            skipped += 1
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            created += 1
        except OSError as exc:
            print(f"ERROR: {rel_path}: {exc}", file=sys.stderr)
            errors += 1
    return created, skipped, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Overwrite existing files")
    args = parser.parse_args()
    build_file_registry()
    created, skipped, errors = write_files(args.force)
    print(f"Scaffold complete: {created} created, {skipped} skipped, {errors} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
