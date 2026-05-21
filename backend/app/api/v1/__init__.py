"""Aggregates v1 API routers."""


from fastapi import APIRouter

from app.api.v1 import health, webhook

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(webhook.router, tags=["webhook"])
