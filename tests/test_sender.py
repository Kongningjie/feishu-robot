import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.errors import AppError, MessageSendError
from app.models.domain import PreviewSnapshot, RecipientAggregate, RecipientSendResult, SendResult
from app.repositories.preview import SendClaim
from app.services.card import ReminderCardBuilder
from app.services.preview import PreviewService
from app.services.sender import ReminderSender

ZONE = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 24, 10, 0, tzinfo=ZONE)


def snapshot(*, count: int = 2, expires_at: datetime | None = None) -> PreviewSnapshot:
    return PreviewSnapshot(
        preview_id="pv_" + "a" * 32,
        template_version=1,
        status="READY",
        project_name="X项目",
        spreadsheet_url="https://company.feishu.cn/sheets/token?sheet=sheet",
        stage_name="PR1",
        deadline=NOW + timedelta(days=1),
        source_row_count=count,
        recipients=tuple(
            RecipientAggregate(
                f"ou_secret_{index}",
                f"申请人{index}",
                "u***@e***.com",
                (f"领域{index}", f"领域{index + 1}"),
            )
            for index in range(count)
        ),
        anomalies=(),
        created_at=NOW,
        expires_at=expires_at or NOW + timedelta(hours=24),
    )


class MemorySendRepository:
    def __init__(self, item: PreviewSnapshot | None) -> None:
        self.snapshot = item
        self.result: SendResult | None = None
        self.owner: str | None = None
        self.guard = asyncio.Lock()

    async def save(self, item, ttl_seconds):
        self.snapshot = item
        return True

    async def get(self, preview_id):
        return self.snapshot if self.snapshot and self.snapshot.preview_id == preview_id else None

    async def get_result(self, preview_id):
        return self.result if self.result and self.result.preview_id == preview_id else None

    async def list_active(self):
        if self.result and self.result.status in {"SENDING", "RETRYING"}:
            return (self.result.preview_id,)
        return ()

    async def claim_send(
        self, preview_id, owner, now, *, result_ttl_seconds, lock_ttl_seconds
    ):
        async with self.guard:
            if self.result is not None:
                return SendClaim(False, self.result, "EXISTS")
            if self.snapshot is None:
                return SendClaim(False, None, "MISSING")
            self.owner = owner
            self.result = SendResult(preview_id, "SENDING", self.snapshot, (), now, now)
            return SendClaim(True, self.result, "CLAIMED")

    async def claim_retry(
        self, preview_id, owner, now, *, result_ttl_seconds, lock_ttl_seconds
    ):
        async with self.guard:
            if self.snapshot is None:
                return SendClaim(False, self.result, "EXPIRED")
            if self.result is None:
                return SendClaim(False, None, "NO_RESULT")
            if self.result.status != "PARTIAL_SUCCESS":
                return SendClaim(False, self.result, "INVALID_STATUS")
            self.owner = owner
            self.result = replace(self.result, status="RETRYING", updated_at=now)
            return SendClaim(True, self.result, "CLAIMED")

    async def claim_recovery(self, preview_id, owner, now, *, lock_ttl_seconds):
        async with self.guard:
            if self.result is None:
                return SendClaim(False, None, "MISSING")
            if self.owner is not None:
                return SendClaim(False, self.result, "LOCKED")
            if self.result.status not in {"SENDING", "RETRYING"}:
                return SendClaim(False, self.result, "FINAL")
            self.owner = owner
            return SendClaim(True, self.result, self.result.status)

    async def save_recipient_result(
        self, preview_id, owner, result, *, result_ttl_seconds, lock_ttl_seconds
    ):
        if owner != self.owner or self.result is None:
            return False
        items = {item.open_id: item for item in self.result.recipients}
        items[result.open_id] = result
        ordered = tuple(
            items[item.open_id]
            for item in self.result.snapshot.recipients
            if item.open_id in items
        )
        self.result = replace(self.result, recipients=ordered, updated_at=result.updated_at)
        return True

    async def finalize(
        self, preview_id, owner, status, now, *, result_ttl_seconds
    ):
        if owner != self.owner or self.result is None:
            return False
        self.result = replace(self.result, status=status, updated_at=now)
        self.owner = None
        return True


class FakeMessageClient:
    def __init__(self, outcomes: dict[str, list[object]] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.idempotency_keys: list[str | None] = []

    async def send_card(
        self, open_id: str, content: str, *, idempotency_key: str | None = None
    ) -> str:
        assert "样机需求填写提醒" in content
        self.calls.append(open_id)
        self.idempotency_keys.append(idempotency_key)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        planned = self.outcomes.get(open_id, [])
        outcome = planned.pop(0) if planned else f"om_message_{open_id[-1]}"
        if isinstance(outcome, Exception):
            raise outcome
        return str(outcome)


def sender(repo, client, *, retries=2, concurrency=5, sleep=asyncio.sleep):
    service = ReminderSender(
        client,
        repo,
        ReminderCardBuilder(),
        concurrency=concurrency,
        auto_retries=retries,
        sleep=sleep,
        random_value=lambda: 0,
    )
    service._now = lambda: NOW  # type: ignore[method-assign]
    return service


def test_card_preserves_domain_order_link_and_rejects_oversize() -> None:
    item = snapshot(count=1)
    content = ReminderCardBuilder().build(item, item.recipients[0], now=NOW)
    assert content.index("领域0") < content.index("领域1")
    assert item.spreadsheet_url in content
    assert "剩余1天" in content
    with pytest.raises(MessageSendError) as caught:
        ReminderCardBuilder(max_bytes=10).build(item, item.recipients[0], now=NOW)
    assert caught.value.code == "MESSAGE_TOO_LARGE"


@pytest.mark.asyncio
async def test_success_is_persisted_and_duplicate_send_does_not_resend() -> None:
    repo = MemorySendRepository(snapshot())
    client = FakeMessageClient()
    service = sender(repo, client)
    second_instance = sender(repo, client)
    first, duplicate = await asyncio.gather(
        service.start_send(repo.snapshot.preview_id),
        second_instance.start_send(repo.snapshot.preview_id),
    )
    claimed = first if first.should_run else duplicate
    assert sum(item.should_run for item in (first, duplicate)) == 1
    await service.run_send(repo.snapshot.preview_id, claimed.owner)
    assert repo.result.status == "SUCCESS"
    assert len(client.calls) == 2
    after = await sender(repo, client).start_send(repo.snapshot.preview_id)
    assert not after.should_run
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_restart_recovery_only_sends_unfinished_recipient() -> None:
    item = snapshot()
    completed = RecipientSendResult(
        item.recipients[0].open_id,
        "SUCCESS",
        1,
        NOW,
        message_id="om_saved",
    )
    repo = MemorySendRepository(item)
    repo.result = SendResult(item.preview_id, "SENDING", item, (completed,), NOW, NOW)
    repo.owner = None
    client = FakeMessageClient()
    restarted = sender(repo, client)
    assert await restarted.recover_once() == 1
    await restarted.wait_for_recovered_tasks()
    assert repo.result.status == "SUCCESS"
    assert client.calls == [item.recipients[1].open_id]


@pytest.mark.asyncio
async def test_partial_failure_isolated_and_manual_retry_only_sends_failure() -> None:
    item = snapshot()
    failed_id = item.recipients[1].open_id
    client = FakeMessageClient(
        {failed_id: [MessageSendError("RECIPIENT_UNREACHABLE", "接收人不可达")]}
    )
    repo = MemorySendRepository(item)
    service = sender(repo, client)
    started = await service.start_send(item.preview_id)
    await service.run_send(item.preview_id, started.owner)
    assert repo.result.status == "PARTIAL_SUCCESS"
    assert [entry.status for entry in repo.result.recipients] == ["SUCCESS", "FAILED"]

    retry = await service.start_retry(item.preview_id)
    await service.run_send(item.preview_id, retry.owner, retry_only=True)
    assert repo.result.status == "SUCCESS"
    assert client.calls.count(item.recipients[0].open_id) == 1
    assert client.calls.count(failed_id) == 2


@pytest.mark.asyncio
async def test_retryable_error_retries_twice_and_honors_retry_after() -> None:
    item = snapshot(count=1)
    open_id = item.recipients[0].open_id
    transient = MessageSendError(
        "UPSTREAM_RATE_LIMITED", "限流", retryable=True, retry_after_seconds=2.5
    )
    client = FakeMessageClient({open_id: [transient, transient, "om_success"]})
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    repo = MemorySendRepository(item)
    service = sender(repo, client, sleep=record_sleep)
    started = await service.start_send(item.preview_id)
    await service.run_send(item.preview_id, started.owner)
    assert repo.result.status == "SUCCESS"
    assert repo.result.recipients[0].attempts == 3
    assert delays == [2.5, 2.5]
    assert len(set(client.idempotency_keys)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["UPSTREAM_TIMEOUT", "UPSTREAM_SERVER_ERROR"])
async def test_timeout_and_server_error_stop_after_two_retries(code: str) -> None:
    item = snapshot(count=1)
    open_id = item.recipients[0].open_id
    failures = [MessageSendError(code, "暂时不可用", retryable=True) for _ in range(3)]
    client = FakeMessageClient({open_id: failures})

    async def no_wait(_: float) -> None:
        return None

    repo = MemorySendRepository(item)
    service = sender(repo, client, sleep=no_wait)
    started = await service.start_send(item.preview_id)
    await service.run_send(item.preview_id, started.owner)
    assert repo.result.status == "FAILED"
    assert repo.result.recipients[0].attempts == 3
    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_sender_respects_configured_concurrency() -> None:
    item = snapshot(count=8)
    repo = MemorySendRepository(item)
    client = FakeMessageClient()
    service = sender(repo, client, concurrency=2)
    started = await service.start_send(item.preview_id)
    await service.run_send(item.preview_id, started.owner)
    assert repo.result.status == "SUCCESS"
    assert client.max_active <= 2


@pytest.mark.asyncio
async def test_sender_handles_300_recipients_without_truncation() -> None:
    item = snapshot(count=300)
    repo = MemorySendRepository(item)
    client = FakeMessageClient()
    service = sender(repo, client, concurrency=5)
    started = await service.start_send(item.preview_id)
    await service.run_send(item.preview_id, started.owner)
    assert repo.result.status == "SUCCESS"
    assert len(repo.result.recipients) == 300
    assert len(client.calls) == 300


@pytest.mark.asyncio
async def test_non_retryable_error_only_attempts_once_and_all_failed() -> None:
    item = snapshot(count=1)
    open_id = item.recipients[0].open_id
    client = FakeMessageClient(
        {open_id: [MessageSendError("MESSAGE_INVALID", "消息非法")]}
    )
    repo = MemorySendRepository(item)
    service = sender(repo, client)
    started = await service.start_send(item.preview_id)
    await service.run_send(item.preview_id, started.owner)
    assert repo.result.status == "FAILED"
    assert repo.result.recipients[0].attempts == 1
    assert len(client.calls) == 1
    with pytest.raises(AppError) as caught:
        await service.start_retry(item.preview_id)
    assert caught.value.code == "RETRY_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_expired_and_empty_preview_cannot_send() -> None:
    expired = snapshot(expires_at=NOW - timedelta(seconds=1))
    with pytest.raises(AppError) as caught:
        await sender(MemorySendRepository(expired), FakeMessageClient()).start_send(
            expired.preview_id
        )
    assert caught.value.code == "PREVIEW_EXPIRED"

    empty = replace(snapshot(), recipients=())
    with pytest.raises(AppError) as caught:
        await sender(MemorySendRepository(empty), FakeMessageClient()).start_send(empty.preview_id)
    assert caught.value.code == "NO_RECIPIENTS"


@pytest.mark.asyncio
async def test_query_result_redacts_internal_identifiers() -> None:
    item = snapshot(count=1)
    recipient_result = RecipientSendResult(
        item.recipients[0].open_id,
        "SUCCESS",
        1,
        NOW,
        message_id="om_1234567890_secret",
    )
    repo = MemorySendRepository(item)
    repo.result = SendResult(item.preview_id, "SUCCESS", item, (recipient_result,), NOW, NOW)
    service = PreviewService(object(), object(), repo)
    response = await service.get(item.preview_id)
    serialized = response.model_dump_json(by_alias=True)
    assert item.recipients[0].open_id not in serialized
    assert "om_1234567890_secret" not in serialized
    assert response.send_results[0].message_id == "om_1***cret"
