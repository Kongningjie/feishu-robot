from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request

from app.schemas.preview import ApiResponse, CreatePreviewRequest, PreviewResponse
from app.services.preview import PreviewService

router = APIRouter()


def get_preview_service(request: Request) -> PreviewService:
    return request.app.state.preview_service


PreviewServiceDependency = Annotated[PreviewService, Depends(get_preview_service)]


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
