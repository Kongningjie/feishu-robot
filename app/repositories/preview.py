import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from app.models.domain import (
    PreviewAnomalyData,
    PreviewSnapshot,
    RecipientAggregate,
    RecipientSendResult,
    SendResult,
)


@dataclass(frozen=True, slots=True)
class SendClaim:
    claimed: bool
    result: SendResult | None
    reason: str


class PreviewRepository(Protocol):
    async def save(self, snapshot: PreviewSnapshot, ttl_seconds: int) -> bool: ...
    async def get(self, preview_id: str) -> PreviewSnapshot | None: ...
    async def get_result(self, preview_id: str) -> SendResult | None: ...
    async def list_active(self) -> tuple[str, ...]: ...

    async def claim_send(
        self, preview_id: str, owner: str, now: datetime, *,
        result_ttl_seconds: int, lock_ttl_seconds: int,
    ) -> SendClaim: ...

    async def claim_retry(
        self, preview_id: str, owner: str, now: datetime, *,
        result_ttl_seconds: int, lock_ttl_seconds: int,
    ) -> SendClaim: ...

    async def claim_recovery(
        self, preview_id: str, owner: str, now: datetime, *, lock_ttl_seconds: int,
    ) -> SendClaim: ...

    async def save_recipient_result(
        self, preview_id: str, owner: str, result: RecipientSendResult, *,
        result_ttl_seconds: int, lock_ttl_seconds: int,
    ) -> bool: ...

    async def finalize(
        self, preview_id: str, owner: str, status: str, now: datetime, *,
        result_ttl_seconds: int,
    ) -> bool: ...


def _serialize(snapshot: PreviewSnapshot) -> str:
    return json.dumps(
        {
            "preview_id": snapshot.preview_id,
            "template_version": snapshot.template_version,
            "status": snapshot.status,
            "project_name": snapshot.project_name,
            "spreadsheet_url": snapshot.spreadsheet_url,
            "stage_name": snapshot.stage_name,
            "deadline": snapshot.deadline.isoformat(),
            "source_row_count": snapshot.source_row_count,
            "recipients": [
                {
                    "open_id": item.open_id,
                    "display_name": item.display_name,
                    "masked_email": item.masked_email,
                    "business_domains": list(item.business_domains),
                }
                for item in snapshot.recipients
            ],
            "anomalies": [
                {
                    "row_number": item.row_number,
                    "business_domain": item.business_domain,
                    "code": item.code,
                    "message": item.message,
                }
                for item in snapshot.anomalies
            ],
            "created_at": snapshot.created_at.isoformat(),
            "expires_at": snapshot.expires_at.isoformat(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _deserialize(raw: str | bytes) -> PreviewSnapshot:
    payload = json.loads(raw)
    return PreviewSnapshot(
        preview_id=payload["preview_id"],
        template_version=payload["template_version"],
        status=payload["status"],
        project_name=payload["project_name"],
        spreadsheet_url=payload["spreadsheet_url"],
        stage_name=payload["stage_name"],
        deadline=datetime.fromisoformat(payload["deadline"]),
        source_row_count=payload["source_row_count"],
        recipients=tuple(
            RecipientAggregate(
                open_id=item["open_id"], display_name=item["display_name"],
                masked_email=item["masked_email"],
                business_domains=tuple(item["business_domains"]),
            )
            for item in payload["recipients"]
        ),
        anomalies=tuple(PreviewAnomalyData(**item) for item in payload["anomalies"]),
        created_at=datetime.fromisoformat(payload["created_at"]),
        expires_at=datetime.fromisoformat(payload["expires_at"]),
    )


def _serialize_recipient(result: RecipientSendResult) -> str:
    return json.dumps(
        {
            "open_id": result.open_id,
            "status": result.status,
            "attempts": result.attempts,
            "updated_at": result.updated_at.isoformat(),
            "message_id": result.message_id,
            "error_code": result.error_code,
            "error_message": result.error_message,
            "feishu_code": result.feishu_code,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _deserialize_recipient(raw: str | bytes) -> RecipientSendResult:
    payload = json.loads(raw)
    payload["updated_at"] = datetime.fromisoformat(payload["updated_at"])
    return RecipientSendResult(**payload)


_CLAIM_SEND = """
if redis.call('EXISTS', KEYS[2]) == 1 then return {0, 'EXISTS'} end
local snapshot = redis.call('GET', KEYS[1])
if not snapshot then return {-1, 'MISSING'} end
if not redis.call('SET', KEYS[3], ARGV[1], 'NX', 'EX', ARGV[2]) then
  return {0, 'LOCKED'}
end
redis.call('HSET', KEYS[2], 'preview_id', ARGV[3], 'status', 'SENDING',
  'snapshot', snapshot, 'started_at', ARGV[4], 'updated_at', ARGV[4])
redis.call('EXPIRE', KEYS[2], ARGV[5])
redis.call('EXPIRE', KEYS[4], ARGV[5])
redis.call('SADD', KEYS[5], ARGV[3])
return {1, 'CLAIMED'}
"""

_CLAIM_RETRY = """
if redis.call('EXISTS', KEYS[1]) == 0 then return {-1, 'EXPIRED'} end
if redis.call('EXISTS', KEYS[2]) == 0 then return {-2, 'NO_RESULT'} end
if redis.call('HGET', KEYS[2], 'status') ~= 'PARTIAL_SUCCESS' then
  return {0, 'INVALID_STATUS'}
end
if not redis.call('SET', KEYS[3], ARGV[1], 'NX', 'EX', ARGV[2]) then
  return {0, 'LOCKED'}
end
redis.call('HSET', KEYS[2], 'status', 'RETRYING', 'updated_at', ARGV[3])
redis.call('EXPIRE', KEYS[2], ARGV[4])
redis.call('EXPIRE', KEYS[4], ARGV[4])
redis.call('SADD', KEYS[5], ARGV[5])
return {1, 'CLAIMED'}
"""

_CLAIM_RECOVERY = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  redis.call('SREM', KEYS[3], ARGV[3])
  return {-1, 'MISSING'}
end
local status = redis.call('HGET', KEYS[1], 'status')
if status ~= 'SENDING' and status ~= 'RETRYING' then
  redis.call('SREM', KEYS[3], ARGV[3])
  return {0, 'FINAL'}
end
if not redis.call('SET', KEYS[2], ARGV[1], 'NX', 'EX', ARGV[2]) then
  return {0, 'LOCKED'}
end
redis.call('HSET', KEYS[1], 'updated_at', ARGV[4])
return {1, status}
"""

_SAVE_RECIPIENT = """
if redis.call('GET', KEYS[3]) ~= ARGV[1] then return 0 end
redis.call('HSET', KEYS[2], ARGV[2], ARGV[3])
redis.call('HSET', KEYS[1], 'updated_at', ARGV[4])
redis.call('EXPIRE', KEYS[1], ARGV[5])
redis.call('EXPIRE', KEYS[2], ARGV[5])
redis.call('EXPIRE', KEYS[3], ARGV[6])
return 1
"""

_FINALIZE = """
if redis.call('GET', KEYS[2]) ~= ARGV[1] then return 0 end
redis.call('HSET', KEYS[1], 'status', ARGV[2], 'updated_at', ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[4])
redis.call('EXPIRE', KEYS[3], ARGV[4])
redis.call('DEL', KEYS[2])
redis.call('SREM', KEYS[4], ARGV[5])
return 1
"""


class RedisPreviewRepository:
    def __init__(self, redis: Any, *, key_prefix: str = "reminder:") -> None:
        self._redis = redis
        self._key_prefix = key_prefix

    def _preview_key(self, preview_id: str) -> str:
        return f"{self._key_prefix}preview:{preview_id}"

    def _result_key(self, preview_id: str) -> str:
        return f"{self._key_prefix}result:{preview_id}"

    def _recipients_key(self, preview_id: str) -> str:
        return f"{self._key_prefix}result:{preview_id}:recipients"

    def _lock_key(self, preview_id: str) -> str:
        return f"{self._key_prefix}lock:{preview_id}"

    def _active_key(self) -> str:
        return f"{self._key_prefix}active"

    async def save(self, snapshot: PreviewSnapshot, ttl_seconds: int) -> bool:
        result = await self._redis.set(
            self._preview_key(snapshot.preview_id), _serialize(snapshot), ex=ttl_seconds, nx=True
        )
        return bool(result)

    async def get(self, preview_id: str) -> PreviewSnapshot | None:
        raw = await self._redis.get(self._preview_key(preview_id))
        return _deserialize(raw) if raw is not None else None

    async def get_result(self, preview_id: str) -> SendResult | None:
        raw_result = await self._redis.hgetall(self._result_key(preview_id))
        if not raw_result:
            return None
        raw_recipients = await self._redis.hgetall(self._recipients_key(preview_id))
        recipients_by_id = {
            open_id: _deserialize_recipient(value) for open_id, value in raw_recipients.items()
        }
        snapshot = _deserialize(raw_result["snapshot"])
        recipients = tuple(
            recipients_by_id[item.open_id]
            for item in snapshot.recipients
            if item.open_id in recipients_by_id
        )
        return SendResult(
            preview_id=preview_id,
            status=raw_result["status"],
            snapshot=snapshot,
            recipients=recipients,
            started_at=datetime.fromisoformat(raw_result["started_at"]),
            updated_at=datetime.fromisoformat(raw_result["updated_at"]),
        )

    async def list_active(self) -> tuple[str, ...]:
        values = await self._redis.smembers(self._active_key())
        return tuple(sorted(str(value) for value in values))

    async def claim_send(
        self, preview_id: str, owner: str, now: datetime, *,
        result_ttl_seconds: int, lock_ttl_seconds: int,
    ) -> SendClaim:
        response = await self._redis.eval(
            _CLAIM_SEND, 5, self._preview_key(preview_id), self._result_key(preview_id),
            self._lock_key(preview_id), self._recipients_key(preview_id), self._active_key(), owner,
            lock_ttl_seconds, preview_id, now.isoformat(), result_ttl_seconds,
        )
        reason = response[1].decode() if isinstance(response[1], bytes) else str(response[1])
        return SendClaim(int(response[0]) == 1, await self.get_result(preview_id), reason)

    async def claim_retry(
        self, preview_id: str, owner: str, now: datetime, *,
        result_ttl_seconds: int, lock_ttl_seconds: int,
    ) -> SendClaim:
        response = await self._redis.eval(
            _CLAIM_RETRY, 5, self._preview_key(preview_id), self._result_key(preview_id),
            self._lock_key(preview_id), self._recipients_key(preview_id), self._active_key(), owner,
            lock_ttl_seconds, now.isoformat(), result_ttl_seconds, preview_id,
        )
        reason = response[1].decode() if isinstance(response[1], bytes) else str(response[1])
        return SendClaim(int(response[0]) == 1, await self.get_result(preview_id), reason)

    async def claim_recovery(
        self, preview_id: str, owner: str, now: datetime, *, lock_ttl_seconds: int,
    ) -> SendClaim:
        response = await self._redis.eval(
            _CLAIM_RECOVERY, 3, self._result_key(preview_id), self._lock_key(preview_id),
            self._active_key(), owner, lock_ttl_seconds, preview_id, now.isoformat(),
        )
        reason = response[1].decode() if isinstance(response[1], bytes) else str(response[1])
        return SendClaim(int(response[0]) == 1, await self.get_result(preview_id), reason)

    async def save_recipient_result(
        self, preview_id: str, owner: str, result: RecipientSendResult, *,
        result_ttl_seconds: int, lock_ttl_seconds: int,
    ) -> bool:
        saved = await self._redis.eval(
            _SAVE_RECIPIENT, 3, self._result_key(preview_id),
            self._recipients_key(preview_id), self._lock_key(preview_id), owner,
            result.open_id, _serialize_recipient(result), result.updated_at.isoformat(),
            result_ttl_seconds, lock_ttl_seconds,
        )
        return bool(saved)

    async def finalize(
        self, preview_id: str, owner: str, status: str, now: datetime, *,
        result_ttl_seconds: int,
    ) -> bool:
        finalized = await self._redis.eval(
            _FINALIZE, 4, self._result_key(preview_id), self._lock_key(preview_id),
            self._recipients_key(preview_id), self._active_key(), owner, status,
            now.isoformat(), result_ttl_seconds, preview_id,
        )
        return bool(finalized)
