from datetime import datetime

import pytest

from app.models.domain import PreviewAnomalyData, PreviewSnapshot, RecipientAggregate
from app.repositories.preview import RedisPreviewRepository


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key: str, value: str, *, ex: int, nx: bool) -> bool:
        assert nx is True
        if key in self.values:
            return False
        self.values[key] = value
        self.ttls[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)


@pytest.mark.asyncio
async def test_redis_repository_round_trip_and_immutable_write() -> None:
    redis = FakeRedis()
    repository = RedisPreviewRepository(redis)
    now = datetime.fromisoformat("2026-09-24T10:00:00+08:00")
    snapshot = PreviewSnapshot(
        preview_id="pv_123",
        template_version=1,
        status="READY",
        project_name="项目",
        spreadsheet_url="https://company.feishu.cn/sheets/x?sheet=y",
        stage_name="PR1",
        deadline=now,
        source_row_count=2,
        recipients=(RecipientAggregate("ou_secret", "张三", "z***@e***.com", ("摄像头",)),),
        anomalies=(PreviewAnomalyData(4, "信号", "APPLICANT_MISSING", "未填写申请人"),),
        created_at=now,
        expires_at=now,
    )
    assert await repository.save(snapshot, 86_400)
    assert not await repository.save(snapshot, 86_400)
    assert redis.ttls["reminder:preview:pv_123"] == 86_400
    assert await repository.get("pv_123") == snapshot
    redis.values.pop("reminder:preview:pv_123")
    assert await repository.get("pv_123") is None
    assert await repository.get("missing") is None
