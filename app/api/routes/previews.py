import re
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Request, status

from app.core.errors import AppError
from app.schemas.preview import ApiResponse, CreatePreviewRequest, PreviewResponse, SendResponse
from app.services.preview import PreviewService
from app.services.sender import ReminderSender

router = APIRouter()


def get_preview_service(request: Request) -> PreviewService:
    return request.app.state.preview_service


PreviewServiceDependency = Annotated[PreviewService, Depends(get_preview_service)]


def get_sender_service(request: Request) -> ReminderSender:
    return request.app.state.sender_service


SenderServiceDependency = Annotated[ReminderSender, Depends(get_sender_service)]
_PREVIEW_ID_RE = re.compile(r"^pv_[0-9a-f]{32}$")


def _validate_preview_id(preview_id: str) -> None:
    if not _PREVIEW_ID_RE.fullmatch(preview_id):
        raise AppError("NOT_FOUND", "资源不存在", 404)


async def _reject_request_body(request: Request) -> None:
    if (await request.body()).strip():
        raise AppError("REQUEST_BODY_NOT_ALLOWED", "该接口不接收请求体", 422)


@router.post("/previews", response_model=ApiResponse[PreviewResponse])
async def create_preview(
    request: CreatePreviewRequest,
    service: PreviewServiceDependency,
) -> ApiResponse[PreviewResponse]:
    return ApiResponse(data=await service.create(request))


@router.get("/previews/{preview_id}", response_model=ApiResponse[PreviewResponse])
async def get_preview(
    preview_id: Annotated[str, Path(pattern=r"^pv_[0-9a-f]{32}$")],
    service: PreviewServiceDependency,
) -> ApiResponse[PreviewResponse]:
    return ApiResponse(data=await service.get(preview_id))


@router.post(
    "/previews/{preview_id}/send",
    response_model=ApiResponse[SendResponse],
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_preview(
    preview_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    service: SenderServiceDependency,
) -> ApiResponse[SendResponse]:
    _validate_preview_id(preview_id)
    await _reject_request_body(request)
    started = await service.start_send(preview_id)
    if started.should_run:
        background_tasks.add_task(service.run_send, preview_id, started.owner)
    return ApiResponse(data=started.response)


@router.post(
    "/previews/{preview_id}/retry-failures",
    response_model=ApiResponse[SendResponse],
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_preview_failures(
    preview_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    service: SenderServiceDependency,
) -> ApiResponse[SendResponse]:
    _validate_preview_id(preview_id)
    await _reject_request_body(request)
    started = await service.start_retry(preview_id)
    if started.should_run:
        background_tasks.add_task(service.run_send, preview_id, started.owner, retry_only=True)
    return ApiResponse(data=started.response)
