import asyncio
import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.errors import AppError, MessageSendError
from app.models.domain import PreviewSnapshot, RecipientAggregate, RecipientSendResult, SendResult
from app.repositories.preview import PreviewRepository
from app.schemas.preview import PreviewStatus, SendResponse
from app.services.card import ReminderCardBuilder

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SendStart:
    response: SendResponse
    owner: str | None

    @property
    def should_run(self) -> bool:
        return self.owner is not None


class ReminderSender:
    def __init__(
        self,
        feishu: object,
        repository: PreviewRepository,
        card_builder: ReminderCardBuilder,
        *,
        concurrency: int = 5,
        auto_retries: int = 2,
        result_ttl_seconds: int = 604_800,
        lock_ttl_seconds: int = 600,
        timezone: str = "Asia/Shanghai",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_value: Callable[[], float] = random.random,
        metrics: object | None = None,
        task_timeout_seconds: int = 600,
    ) -> None:
        self._feishu = feishu
        self._repository = repository
        self._card_builder = card_builder
        self._concurrency = concurrency
        self._auto_retries = auto_retries
        self._result_ttl_seconds = result_ttl_seconds
        self._lock_ttl_seconds = lock_ttl_seconds
        self._timezone = ZoneInfo(timezone)
        self._sleep = sleep
        self._random_value = random_value
        self._metrics = metrics
        self._task_timeout_seconds = task_timeout_seconds
        self._recovery_tasks: set[asyncio.Task[None]] = set()

    def _now(self) -> datetime:
        return datetime.now(self._timezone)

    async def start_send(self, preview_id: str) -> SendStart:
        existing = await self._repository.get_result(preview_id)
        if existing is not None:
            return SendStart(self._response(existing), None)
        snapshot = await self._repository.get(preview_id)
        if snapshot is None or snapshot.expires_at <= self._now():
            raise AppError("PREVIEW_EXPIRED", "预览不存在或已过期，请重新预览", 410)
        if not snapshot.recipients:
            raise AppError("NO_RECIPIENTS", "当前阶段无待催项，不能发送", 409)
        owner = uuid.uuid4().hex
        claim = await self._repository.claim_send(
            preview_id,
            owner,
            self._now(),
            result_ttl_seconds=self._result_ttl_seconds,
            lock_ttl_seconds=self._lock_ttl_seconds,
        )
        if claim.result is None:
            if claim.reason == "MISSING":
                raise AppError("PREVIEW_EXPIRED", "预览不存在或已过期，请重新预览", 410)
            raise AppError("SEND_STATE_CONFLICT", "发送状态暂不可用，请稍后查询", 409)
        return SendStart(self._response(claim.result), owner if claim.claimed else None)

    async def start_retry(self, preview_id: str) -> SendStart:
        owner = uuid.uuid4().hex
        claim = await self._repository.claim_retry(
            preview_id,
            owner,
            self._now(),
            result_ttl_seconds=self._result_ttl_seconds,
            lock_ttl_seconds=self._lock_ttl_seconds,
        )
        if claim.result is None:
            if claim.reason == "EXPIRED":
                raise AppError("PREVIEW_EXPIRED", "预览已过期，不能重试", 410)
            raise AppError("PREVIEW_NOT_FOUND", "预览或发送结果不存在", 404)
        if not claim.claimed:
            if claim.reason == "LOCKED" or claim.result.status == PreviewStatus.retrying.value:
                return SendStart(self._response(claim.result), None)
            raise AppError("RETRY_NOT_ALLOWED", "当前状态不允许重试失败项", 409)
        return SendStart(self._response(claim.result), owner)

    async def recover_once(self) -> int:
        recovered = 0
        stalled = 0
        for preview_id in await self._repository.list_active():
            owner = uuid.uuid4().hex
            claim = await self._repository.claim_recovery(
                preview_id,
                owner,
                self._now(),
                lock_ttl_seconds=self._lock_ttl_seconds,
            )
            if not claim.claimed or claim.result is None:
                if (
                    claim.result is not None
                    and (self._now() - claim.result.updated_at).total_seconds()
                    > self._task_timeout_seconds
                ):
                    stalled += 1
                continue
            task = asyncio.create_task(
                self.run_send(
                    preview_id,
                    owner,
                    retry_only=claim.result.status == PreviewStatus.retrying.value,
                )
            )
            self._recovery_tasks.add(task)
            task.add_done_callback(self._recovery_tasks.discard)
            recovered += 1
        if self._metrics is not None:
            self._metrics.set_gauge("stalled_send_tasks", stalled)
        return recovered

    async def wait_for_recovered_tasks(self) -> None:
        if self._recovery_tasks:
            await asyncio.gather(*tuple(self._recovery_tasks), return_exceptions=True)

    async def run_send(self, preview_id: str, owner: str, *, retry_only: bool = False) -> None:
        try:
            result = await self._repository.get_result(preview_id)
            if result is None:
                return
            existing = {item.open_id: item for item in result.recipients}
            if retry_only:
                recipients = tuple(
                    item
                    for item in result.snapshot.recipients
                    if existing.get(item.open_id) is not None
                    and existing[item.open_id].status == "FAILED"
                )
            else:
                recipients = tuple(
                    item
                    for item in result.snapshot.recipients
                    if existing.get(item.open_id) is None
                    or existing[item.open_id].status != "SUCCESS"
                )
            semaphore = asyncio.Semaphore(self._concurrency)
            await asyncio.gather(
                *(
                    self._send_one(
                        preview_id,
                        owner,
                        result.snapshot,
                        recipient,
                        existing.get(recipient.open_id),
                        semaphore,
                    )
                    for recipient in recipients
                )
            )
            current = await self._repository.get_result(preview_id)
            if current is None:
                return
            success_count = sum(item.status == "SUCCESS" for item in current.recipients)
            failure_count = sum(item.status == "FAILED" for item in current.recipients)
            if success_count == len(current.snapshot.recipients):
                status = PreviewStatus.success.value
            elif success_count > 0 and failure_count > 0:
                status = PreviewStatus.partial_success.value
            else:
                status = PreviewStatus.failed.value
            await self._repository.finalize(
                preview_id,
                owner,
                status,
                self._now(),
                result_ttl_seconds=self._result_ttl_seconds,
            )
            if self._metrics is not None:
                metric_name = (
                    "send_task_success_total"
                    if status == PreviewStatus.success.value
                    else "send_task_failure_total"
                )
                self._metrics.increment(metric_name)
        except Exception:
            logger.exception("send orchestration failed preview_id=%s", preview_id)
            await self._repository.finalize(
                preview_id,
                owner,
                PreviewStatus.failed.value,
                self._now(),
                result_ttl_seconds=self._result_ttl_seconds,
            )

    async def _send_one(
        self,
        preview_id: str,
        owner: str,
        snapshot: PreviewSnapshot,
        recipient: RecipientAggregate,
        previous: RecipientSendResult | None,
        semaphore: asyncio.Semaphore,
    ) -> None:
        async with semaphore:
            attempts = previous.attempts if previous is not None else 0
            try:
                content = self._card_builder.build(snapshot, recipient, now=self._now())
                for retry_index in range(self._auto_retries + 1):
                    attempts += 1
                    try:
                        message_id = await self._feishu.send_card(
                            recipient.open_id,
                            content,
                            idempotency_key=str(
                                uuid.uuid5(
                                    uuid.NAMESPACE_URL,
                                    f"{preview_id}:{recipient.open_id}",
                                )
                            ),
                        )
                        result = RecipientSendResult(
                            recipient.open_id,
                            "SUCCESS",
                            attempts,
                            self._now(),
                            message_id=message_id,
                        )
                        break
                    except MessageSendError as exc:
                        if not exc.retryable or retry_index >= self._auto_retries:
                            result = RecipientSendResult(
                                recipient.open_id,
                                "FAILED",
                                attempts,
                                self._now(),
                                error_code=exc.code,
                                error_message=exc.message,
                                feishu_code=exc.feishu_code,
                            )
                            break
                        delay = (
                            exc.retry_after_seconds
                            if exc.retry_after_seconds is not None
                            else (0.5 * (2**retry_index)) + self._random_value() * 0.5
                        )
                        await self._sleep(delay)
            except MessageSendError as exc:
                result = RecipientSendResult(
                    recipient.open_id,
                    "FAILED",
                    attempts,
                    self._now(),
                    error_code=exc.code,
                    error_message=exc.message,
                    feishu_code=exc.feishu_code,
                )
            except Exception:
                logger.exception("unexpected recipient send failure preview_id=%s", preview_id)
                result = RecipientSendResult(
                    recipient.open_id,
                    "FAILED",
                    attempts,
                    self._now(),
                    error_code="INTERNAL_SEND_ERROR",
                    error_message="消息发送发生内部错误",
                )
            if result.status == "FAILED":
                logger.warning(
                    "recipient send failed preview_id=%s code=%s feishu_code=%s attempts=%s",
                    preview_id,
                    result.error_code,
                    result.feishu_code,
                    result.attempts,
                )
            if self._metrics is not None:
                self._metrics.increment(
                    "send_recipient_success_total"
                    if result.status == "SUCCESS"
                    else "send_recipient_failure_total"
                )
            await self._repository.save_recipient_result(
                preview_id,
                owner,
                result,
                result_ttl_seconds=self._result_ttl_seconds,
                lock_ttl_seconds=self._lock_ttl_seconds,
            )

    @staticmethod
    def _response(result: SendResult) -> SendResponse:
        successes = sum(item.status == "SUCCESS" for item in result.recipients)
        failures = sum(item.status == "FAILED" for item in result.recipients)
        return SendResponse(
            previewId=result.preview_id,
            status=result.status,
            successCount=successes,
            failureCount=failures,
            pendingCount=max(0, len(result.snapshot.recipients) - successes - failures),
            attemptCount=sum(item.attempts for item in result.recipients),
        )
