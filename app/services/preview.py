import re
import time
import uuid
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from app.core.errors import AppError, StructuralError
from app.models.domain import (
    PreviewAnomalyData,
    PreviewSnapshot,
    RecipientAggregate,
    SendResult,
    SheetDocument,
)
from app.repositories.preview import PreviewRepository
from app.schemas.preview import (
    CreatePreviewRequest,
    PreviewAnomaly,
    PreviewResponse,
    PreviewStatus,
    RecipientPreview,
    RecipientSendResultResponse,
)
from app.services.sheet_parser import SheetParser

_EMAIL_RE = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.I,
)


class FeishuPreviewClient(Protocol):
    async def fetch_document(self, url: str) -> SheetDocument: ...

    async def batch_get_open_ids(self, emails: list[str]) -> dict[str, tuple[str, ...]]: ...


def mask_email(email: str) -> str:
    local, domain = email.split("@", 1)
    labels = domain.split(".")
    masked_domain = ".".join(
        label if index == len(labels) - 1 else f"{label[:1]}***"
        for index, label in enumerate(labels)
    )
    return f"{local[:1]}***@{masked_domain}"


class PreviewService:
    def __init__(
        self,
        feishu: FeishuPreviewClient,
        parser: SheetParser,
        repository: PreviewRepository,
        *,
        preview_ttl_seconds: int = 86_400,
        max_recipients: int = 300,
        template_version: int = 1,
        timezone: str = "Asia/Shanghai",
        metrics: object | None = None,
    ) -> None:
        self._feishu = feishu
        self._parser = parser
        self._repository = repository
        self._preview_ttl_seconds = preview_ttl_seconds
        self._max_recipients = max_recipients
        self._template_version = template_version
        self._timezone = ZoneInfo(timezone)
        self._metrics = metrics

    async def create(self, request: CreatePreviewRequest) -> PreviewResponse:
        started_at = time.perf_counter()
        source_url = str(request.spreadsheet_url)
        document = await self._feishu.fetch_document(source_url)
        parsed = self._parser.parse(document.rows, request.stage_name)
        anomalies = list(parsed.anomalies)

        valid_items = []
        emails: list[str] = []
        seen_emails: set[str] = set()
        for item in parsed.pending_items:
            if not _EMAIL_RE.fullmatch(item.applicant_email):
                anomalies.append(
                    PreviewAnomalyData(
                        item.row_number,
                        item.business_domain,
                        "APPLICANT_EMAIL_INVALID",
                        "申请人邮箱格式非法",
                    )
                )
                continue
            valid_items.append(item)
            if item.applicant_email not in seen_emails:
                seen_emails.add(item.applicant_email)
                emails.append(item.applicant_email)

        mappings = await self._feishu.batch_get_open_ids(emails) if emails else {}
        aggregate_data: OrderedDict[str, dict[str, object]] = OrderedDict()
        for item in valid_items:
            open_ids = mappings.get(item.applicant_email, ())
            if len(open_ids) != 1:
                anomalies.append(
                    PreviewAnomalyData(
                        item.row_number,
                        item.business_domain,
                        "APPLICANT_EMAIL_UNRESOLVED",
                        "申请人邮箱无法映射为本企业唯一用户",
                    )
                )
                continue
            open_id = open_ids[0]
            if open_id not in aggregate_data:
                aggregate_data[open_id] = {
                    "display_name": item.applicant_name,
                    "masked_email": mask_email(item.applicant_email),
                    "business_domains": [],
                }
            domains = aggregate_data[open_id]["business_domains"]
            assert isinstance(domains, list)
            if item.business_domain not in domains:
                domains.append(item.business_domain)

        if len(aggregate_data) > self._max_recipients:
            raise StructuralError(
                "RECIPIENT_LIMIT_EXCEEDED",
                f"不同申请人超过{self._max_recipients}人限制",
            )
        recipients = tuple(
            RecipientAggregate(
                open_id=open_id,
                display_name=str(data["display_name"]),
                masked_email=str(data["masked_email"]),
                business_domains=tuple(data["business_domains"]),  # type: ignore[arg-type]
            )
            for open_id, data in aggregate_data.items()
        )
        now = datetime.now(self._timezone)
        snapshot = PreviewSnapshot(
            preview_id=f"pv_{uuid.uuid4().hex}",
            template_version=self._template_version,
            status=PreviewStatus.ready.value,
            project_name=document.project_name,
            spreadsheet_url=source_url,
            stage_name=parsed.stage_name,
            deadline=parsed.deadline,
            source_row_count=parsed.source_row_count,
            recipients=recipients,
            anomalies=tuple(anomalies),
            created_at=now,
            expires_at=now + timedelta(seconds=self._preview_ttl_seconds),
        )
        for _ in range(3):
            if await self._repository.save(snapshot, self._preview_ttl_seconds):
                if self._metrics is not None:
                    self._metrics.increment("preview_success_total")
                    self._metrics.observe("preview_duration", time.perf_counter() - started_at)
                return self._to_response(snapshot)
            snapshot = replace(snapshot, preview_id=f"pv_{uuid.uuid4().hex}")
        raise AppError("PREVIEW_ID_COLLISION", "生成预览标识失败，请重试", 503)

    async def get(self, preview_id: str) -> PreviewResponse:
        get_result = getattr(self._repository, "get_result", None)
        result = await get_result(preview_id) if get_result is not None else None
        snapshot = result.snapshot if result is not None else await self._repository.get(preview_id)
        if snapshot is None:
            raise AppError("PREVIEW_NOT_FOUND", "预览不存在或已过期", 404)
        return self._to_response(snapshot, result)

    def _to_response(
        self, snapshot: PreviewSnapshot, result: SendResult | None = None
    ) -> PreviewResponse:
        today = datetime.now(self._timezone).date()
        deadline_date = snapshot.deadline.astimezone(self._timezone).date()
        day_delta = (deadline_date - today).days
        if day_delta == 0:
            deadline_label = "今日截止"
        elif day_delta > 0:
            deadline_label = f"剩余{day_delta}天"
        else:
            deadline_label = f"已逾期{-day_delta}天"
        send_results_by_id = (
            {item.open_id: item for item in result.recipients} if result is not None else {}
        )
        successes = sum(item.status == "SUCCESS" for item in send_results_by_id.values())
        failures = sum(item.status == "FAILED" for item in send_results_by_id.values())
        return PreviewResponse(
            previewId=snapshot.preview_id,
            status=result.status if result is not None else snapshot.status,
            createdAt=snapshot.created_at,
            expiresAt=snapshot.expires_at,
            projectName=snapshot.project_name,
            spreadsheetUrl=snapshot.spreadsheet_url,
            stageName=snapshot.stage_name,
            deadline=snapshot.deadline,
            deadlineLabel=deadline_label,
            sourceRowCount=snapshot.source_row_count,
            pendingItemCount=sum(len(item.business_domains) for item in snapshot.recipients),
            recipientCount=len(snapshot.recipients),
            anomalyCount=len(snapshot.anomalies),
            recipients=[
                RecipientPreview(
                    displayName=item.display_name,
                    maskedEmail=item.masked_email,
                    businessDomains=list(item.business_domains),
                )
                for item in snapshot.recipients
            ],
            anomalies=[
                PreviewAnomaly(
                    rowNumber=item.row_number,
                    businessDomain=item.business_domain,
                    code=item.code,
                    message=item.message,
                )
                for item in snapshot.anomalies
            ],
            successCount=successes,
            failureCount=failures,
            pendingCount=max(0, len(snapshot.recipients) - successes - failures),
            attemptCount=sum(item.attempts for item in send_results_by_id.values()),
            sendResults=[
                RecipientSendResultResponse(
                    displayName=recipient.display_name,
                    maskedEmail=recipient.masked_email,
                    status=(send_results_by_id.get(recipient.open_id).status
                            if recipient.open_id in send_results_by_id else "PENDING"),
                    attempts=(send_results_by_id.get(recipient.open_id).attempts
                              if recipient.open_id in send_results_by_id else 0),
                    messageId=self._mask_message_id(
                        send_results_by_id.get(recipient.open_id).message_id
                        if recipient.open_id in send_results_by_id else None
                    ),
                    errorCode=(send_results_by_id.get(recipient.open_id).error_code
                               if recipient.open_id in send_results_by_id else None),
                    errorMessage=(send_results_by_id.get(recipient.open_id).error_message
                                  if recipient.open_id in send_results_by_id else None),
                    updatedAt=(send_results_by_id.get(recipient.open_id).updated_at
                               if recipient.open_id in send_results_by_id else None),
                )
                for recipient in snapshot.recipients
            ],
        )

    @staticmethod
    def _mask_message_id(message_id: str | None) -> str | None:
        if not message_id:
            return None
        if len(message_id) <= 8:
            return "***"
        return f"{message_id[:4]}***{message_id[-4:]}"
