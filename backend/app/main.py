"""Composition root: FastAPI app, router registration, scheduler lifecycle."""


from contextlib import asynccontextmanager

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
