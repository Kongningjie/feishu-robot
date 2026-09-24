import json
from datetime import datetime
from typing import Any, Protocol

from app.models.domain import PreviewAnomalyData, PreviewSnapshot, RecipientAggregate


class PreviewRepository(Protocol):
    async def save(self, snapshot: PreviewSnapshot, ttl_seconds: int) -> bool: ...

    async def get(self, preview_id: str) -> PreviewSnapshot | None: ...


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
                    "open_id": recipient.open_id,
                    "display_name": recipient.display_name,
                    "masked_email": recipient.masked_email,
                    "business_domains": list(recipient.business_domains),
                }
                for recipient in snapshot.recipients
            ],
            "anomalies": [
                {
                    "row_number": anomaly.row_number,
                    "business_domain": anomaly.business_domain,
                    "code": anomaly.code,
                    "message": anomaly.message,
                }
                for anomaly in snapshot.anomalies
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
                open_id=item["open_id"],
                display_name=item["display_name"],
                masked_email=item["masked_email"],
                business_domains=tuple(item["business_domains"]),
            )
            for item in payload["recipients"]
        ),
        anomalies=tuple(PreviewAnomalyData(**item) for item in payload["anomalies"]),
        created_at=datetime.fromisoformat(payload["created_at"]),
        expires_at=datetime.fromisoformat(payload["expires_at"]),
    )


class RedisPreviewRepository:
    def __init__(self, redis: Any, *, key_prefix: str = "reminder:preview:") -> None:
        self._redis = redis
        self._key_prefix = key_prefix

    def _key(self, preview_id: str) -> str:
        return f"{self._key_prefix}{preview_id}"

    async def save(self, snapshot: PreviewSnapshot, ttl_seconds: int) -> bool:
        result = await self._redis.set(
            self._key(snapshot.preview_id),
            _serialize(snapshot),
            ex=ttl_seconds,
            nx=True,
        )
        return bool(result)

    async def get(self, preview_id: str) -> PreviewSnapshot | None:
        raw = await self._redis.get(self._key(preview_id))
        return _deserialize(raw) if raw is not None else None
