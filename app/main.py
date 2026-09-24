import asyncio
import logging
import re
import time
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
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.router import api_router
from app.clients.feishu import FeishuClient
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.core.observability import MetricRegistry, preview_id_context, request_id_context
from app.core.security import (
    consume_rate_limit,
    csrf_is_valid,
    new_csrf_token,
    request_id,
)
from app.repositories.preview import RedisPreviewRepository
from app.services.card import ReminderCardBuilder
from app.services.preview import PreviewService
from app.services.sender import ReminderSender
from app.services.sheet_parser import SheetParser

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")
logger = logging.getLogger(__name__)
_PREVIEW_PATH_RE = re.compile(r"/previews/(pv_[0-9a-f]{32})(?:/|$)")


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
            metrics=application.state.metrics,
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
            metrics=application.state.metrics,
        )
        application.state.sender_service = ReminderSender(
            feishu,
            repository,
            ReminderCardBuilder(
                max_bytes=active_settings.message_max_bytes,
                timezone=active_settings.app_timezone,
            ),
            concurrency=active_settings.send_concurrency,
            auto_retries=active_settings.send_auto_retries,
            result_ttl_seconds=active_settings.result_ttl_seconds,
            lock_ttl_seconds=active_settings.send_lock_ttl_seconds,
            timezone=active_settings.app_timezone,
            metrics=application.state.metrics,
            task_timeout_seconds=active_settings.send_task_timeout_seconds,
        )
        stop_recovery = asyncio.Event()

        async def recovery_loop() -> None:
            while not stop_recovery.is_set():
                try:
                    recovered = await application.state.sender_service.recover_once()
                    if recovered:
                        logger.info("recovered interrupted send tasks count=%s", recovered)
                except Exception:
                    application.state.metrics.increment("redis_errors_total")
                    logger.exception("send recovery scan failed")
                try:
                    await asyncio.wait_for(
                        stop_recovery.wait(), timeout=active_settings.recovery_scan_seconds
                    )
                except TimeoutError:
                    continue

        recovery_task = (
            asyncio.create_task(recovery_loop()) if active_settings.app_env != "test" else None
        )
        try:
            yield
        finally:
            stop_recovery.set()
            if recovery_task is not None:
                await recovery_task
            await application.state.sender_service.wait_for_recovered_tasks()
            await http.aclose()
            await redis.aclose()

    application = FastAPI(
        title=active_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = active_settings
    application.state.metrics = MetricRegistry()
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=active_settings.allowed_http_hosts or ["localhost"],
    )
    application.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    application.include_router(api_router)

    @application.middleware("http")
    async def request_controls(request: Request, call_next):
        started_at = time.perf_counter()
        current_request_id = request_id(request.headers.get("X-Request-ID"))
        match = _PREVIEW_PATH_RE.search(request.url.path)
        current_preview_id = match.group(1) if match else "-"
        request_token = request_id_context.set(current_request_id)
        preview_token = preview_id_context.set(current_preview_id)
        metrics: MetricRegistry = request.app.state.metrics

        def error_response(status_code: int, code: str, message: str) -> JSONResponse:
            response = JSONResponse(
                status_code=status_code,
                content={"success": False, "code": code, "message": message, "data": None},
                headers={"X-Request-ID": current_request_id},
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response

        try:
            if (
                active_settings.csrf_enabled
                and request.url.path.startswith("/api/")
                and request.method in {"POST", "PUT", "PATCH", "DELETE"}
                and not csrf_is_valid(request)
            ):
                metrics.increment("csrf_rejected_total")
                return error_response(403, "CSRF_VALIDATION_FAILED", "CSRF 校验失败")
            if active_settings.rate_limit_enabled and request.url.path.startswith("/api/"):
                try:
                    allowed, _ = await consume_rate_limit(
                        request.app.state.redis,
                        request,
                        requests=active_settings.rate_limit_requests,
                        window_seconds=active_settings.rate_limit_window_seconds,
                    )
                except Exception:
                    metrics.increment("redis_errors_total")
                    logger.exception("rate limiter redis failure")
                    return error_response(503, "RATE_LIMIT_UNAVAILABLE", "接口限流服务不可用")
                if not allowed:
                    metrics.increment("rate_limit_rejected_total")
                    return error_response(429, "RATE_LIMITED", "请求过于频繁，请稍后重试")
            response = await call_next(request)
            response.headers["X-Request-ID"] = current_request_id
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'self'; script-src 'self'; "
                "img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            )
            metrics.increment("http_requests_total")
            if response.status_code >= 400:
                metrics.increment("http_errors_total")
            return response
        finally:
            duration = time.perf_counter() - started_at
            metrics.observe("http_request_duration", duration)
            logger.info(
                "request completed method=%s path=%s duration_ms=%.2f",
                request.method,
                request.url.path,
                duration * 1000,
            )
            request_id_context.reset(request_token)
            preview_id_context.reset(preview_token)

    @application.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        if request.method == "POST" and request.url.path == "/api/v1/previews":
            request.app.state.metrics.increment("preview_failure_total")
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
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        if request.method == "POST" and request.url.path == "/api/v1/previews":
            request.app.state.metrics.increment("preview_failure_total")
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
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        if request.method == "POST" and request.url.path == "/api/v1/previews":
            request.app.state.metrics.increment("preview_failure_total")
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
        csrf_token = new_csrf_token()
        response = templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"csrf_token": csrf_token},
        )
        response.set_cookie(
            "csrf_token",
            csrf_token,
            secure=active_settings.csrf_cookie_secure,
            httponly=False,
            samesite="strict",
            max_age=3600,
        )
        return response

    @application.get("/internal/metrics", include_in_schema=False)
    async def metrics(request: Request) -> dict[str, object]:
        return {
            "success": True,
            "code": "OK",
            "message": "success",
            "data": request.app.state.metrics.snapshot(),
        }

    return application


app = create_app()
