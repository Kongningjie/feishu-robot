from datetime import datetime
from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class PreviewStatus(StrEnum):
    ready = "READY"
    sending = "SENDING"
    success = "SUCCESS"
    partial_success = "PARTIAL_SUCCESS"
    failed = "FAILED"
    retrying = "RETRYING"
    expired = "EXPIRED"


class ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)


class CreatePreviewRequest(ApiModel):
    spreadsheet_url: HttpUrl = Field(alias="spreadsheetUrl")
    stage_name: str = Field(alias="stageName", min_length=1, max_length=64)

    @field_validator("stage_name")
    @classmethod
    def stage_name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("硬件阶段不能为空")
        return value


class RecipientPreview(ApiModel):
    display_name: str = Field(alias="displayName")
    masked_email: str = Field(alias="maskedEmail")
    business_domains: list[str] = Field(alias="businessDomains")


class PreviewAnomaly(ApiModel):
    row_number: int = Field(alias="rowNumber", ge=1)
    business_domain: str = Field(alias="businessDomain")
    code: str
    message: str


class PreviewResponse(ApiModel):
    preview_id: str = Field(alias="previewId")
    status: PreviewStatus
    created_at: datetime = Field(alias="createdAt")
    expires_at: datetime = Field(alias="expiresAt")
    project_name: str = Field(alias="projectName")
    spreadsheet_url: str = Field(alias="spreadsheetUrl")
    stage_name: str = Field(alias="stageName")
    deadline: datetime
    deadline_label: str = Field(alias="deadlineLabel")
    source_row_count: int = Field(alias="sourceRowCount", ge=0)
    pending_item_count: int = Field(alias="pendingItemCount", ge=0)
    recipient_count: int = Field(alias="recipientCount", ge=0)
    anomaly_count: int = Field(alias="anomalyCount", ge=0)
    recipients: list[RecipientPreview] = Field(default_factory=list)
    anomalies: list[PreviewAnomaly] = Field(default_factory=list)


T = TypeVar("T")


class ApiResponse(ApiModel, Generic[T]):
    success: bool = True
    code: str = "OK"
    message: str = "success"
    data: T


class ErrorData(ApiModel):
    details: list[dict[str, object]] | None = None


class SendResponse(ApiModel):
    preview_id: str = Field(alias="previewId")
    status: PreviewStatus
    success_count: int = Field(alias="successCount", ge=0)
    failure_count: int = Field(alias="failureCount", ge=0)
