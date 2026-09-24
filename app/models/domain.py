from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class SheetReference:
    spreadsheet_token: str
    sheet_id: str


@dataclass(frozen=True, slots=True)
class SheetDocument:
    reference: SheetReference
    project_name: str
    rows: list[list[Any]]


@dataclass(frozen=True, slots=True)
class PendingItem:
    row_number: int
    business_domain: str
    applicant_name: str
    applicant_email: str


@dataclass(frozen=True, slots=True)
class PreviewAnomalyData:
    row_number: int
    business_domain: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ParsedSheet:
    stage_name: str
    deadline: datetime
    source_row_count: int
    pending_items: tuple[PendingItem, ...]
    anomalies: tuple[PreviewAnomalyData, ...]


@dataclass(frozen=True, slots=True)
class RecipientAggregate:
    open_id: str
    display_name: str
    masked_email: str
    business_domains: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreviewSnapshot:
    preview_id: str
    template_version: int
    status: str
    project_name: str
    spreadsheet_url: str
    stage_name: str
    deadline: datetime
    source_row_count: int
    recipients: tuple[RecipientAggregate, ...]
    anomalies: tuple[PreviewAnomalyData, ...]
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RecipientSendResult:
    open_id: str
    status: str
    attempts: int
    updated_at: datetime
    message_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    feishu_code: int | None = None


@dataclass(frozen=True, slots=True)
class SendResult:
    preview_id: str
    status: str
    snapshot: PreviewSnapshot
    recipients: tuple[RecipientSendResult, ...]
    started_at: datetime
    updated_at: datetime
