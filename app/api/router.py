from fastapi import APIRouter

from app.api.routes import health, previews

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(previews.router, prefix="/api/v1", tags=["previews"])
