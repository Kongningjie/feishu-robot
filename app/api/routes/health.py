from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, Any]:
    return {
        "success": True,
        "code": "OK",
        "message": "success",
        "data": {"status": "ok"},
    }


@router.get("/ready", response_model=None)
async def ready(request: Request) -> Any:
    settings = request.app.state.settings
    missing = settings.missing_required_settings
    if missing:
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "code": "CONFIG_NOT_READY",
                "message": "必要配置未加载",
                "data": {"missing": list(missing)},
            },
        )
    try:
        await request.app.state.redis.ping()
    except Exception:
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "code": "REDIS_NOT_READY",
                "message": "Redis 不可用",
                "data": None,
            },
        )
    return {
        "success": True,
        "code": "OK",
        "message": "success",
        "data": {"status": "ready"},
    }
