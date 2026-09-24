import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from redis.asyncio import Redis
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.router import api_router
from app.clients.feishu import FeishuClient
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.repositories.preview import RedisPreviewRepository
from app.services.preview import PreviewService
from app.services.sheet_parser import SheetParser

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")
logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or get_settings()
    configure_logging(active_settings.log_level)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        timeout = httpx.Timeout(
            connect=active_settings.http_connect_timeout_seconds,
            read=active_settings.http_read_timeout_seconds,
            write=active_settings.http_read_timeout_seconds,
            pool=active_settings.http_connect_timeout_seconds,
        )
        http = httpx.AsyncClient(timeout=timeout)
        redis = Redis.from_url(active_settings.redis_url, decode_responses=True)
        repository = RedisPreviewRepository(redis)
        feishu = FeishuClient(
            http,
            active_settings.feishu_app_id,
            active_settings.feishu_app_secret,
            allowed_hosts=active_settings.allowed_feishu_hosts,
            token_cache=redis,
            api_base_url=active_settings.feishu_api_base_url,
        )
        application.state.http = http
        application.state.redis = redis
        application.state.preview_service = PreviewService(
            feishu,
            SheetParser(
                timezone=active_settings.app_timezone,
                max_data_rows=active_settings.max_data_rows,
            ),
            repository,
            preview_ttl_seconds=active_settings.preview_ttl_seconds,
            max_recipients=active_settings.max_recipients,
            template_version=active_settings.template_version,
            timezone=active_settings.app_timezone,
        )
        try:
            yield
        finally:
            await http.aclose()
            await redis.aclose()

    application = FastAPI(
        title=active_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = active_settings
    application.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    application.include_router(api_router)

    @application.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "code": exc.code,
                "message": exc.message,
                "data": None,
            },
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {
                "location": ".".join(str(part) for part in error["loc"]),
                "message": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "code": "VALIDATION_ERROR",
                "message": "请求参数校验失败",
                "data": {"details": details},
            },
        )

    @application.exception_handler(StarletteHTTPException)
    async def http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) else "HTTP 请求失败"
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "code": "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR",
                "message": message,
                "data": None,
            },
            headers=exc.headers,
        )

    @application.exception_handler(Exception)
    async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unexpected application error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "code": "INTERNAL_ERROR",
                "message": "服务内部错误",
                "data": None,
            },
        )

    @application.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request=request, name="index.html")

    return application


app = create_app()
