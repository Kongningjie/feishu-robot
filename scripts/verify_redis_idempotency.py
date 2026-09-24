"""在两个应用容器中依次执行，用于验证共享 Redis 的原子发送抢占。"""

import asyncio
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from redis.asyncio import Redis

from app.models.domain import PreviewSnapshot, RecipientAggregate
from app.repositories.preview import RedisPreviewRepository

PREVIEW_ID = "pv_ffffffffffffffffffffffffffffffff"
ZONE = ZoneInfo("Asia/Shanghai")


async def run() -> None:
    mode = os.environ["VERIFY_MODE"]
    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    repository = RedisPreviewRepository(redis)
    now = datetime.now(ZONE)
    if mode == "cleanup":
        await redis.delete(
            f"reminder:preview:{PREVIEW_ID}",
            f"reminder:result:{PREVIEW_ID}",
            f"reminder:result:{PREVIEW_ID}:recipients",
            f"reminder:lock:{PREVIEW_ID}",
        )
        await redis.srem("reminder:active", PREVIEW_ID)
        output = {"mode": mode, "success": True}
    elif mode == "seed":
        snapshot = PreviewSnapshot(
            preview_id=PREVIEW_ID,
            template_version=1,
            status="READY",
            project_name="Mock双实例验收",
            spreadsheet_url="https://company.feishu.cn/sheets/mock?sheet=mock",
            stage_name="PR1",
            deadline=now + timedelta(days=1),
            source_row_count=1,
            recipients=(RecipientAggregate("ou_mock", "Mock用户", "m***@e***.com", ("领域",)),),
            anomalies=(),
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )
        saved = await repository.save(snapshot, 3600)
        claim = await repository.claim_send(
            PREVIEW_ID,
            "instance-one",
            now,
            result_ttl_seconds=3600,
            lock_ttl_seconds=60,
        )
        output = {"mode": mode, "saved": saved, "claimed": claim.claimed}
        if not saved or not claim.claimed:
            raise SystemExit(1)
    elif mode == "compete":
        claim = await repository.claim_send(
            PREVIEW_ID,
            "instance-two",
            now,
            result_ttl_seconds=3600,
            lock_ttl_seconds=60,
        )
        output = {"mode": mode, "claimed": claim.claimed, "reason": claim.reason}
        if claim.claimed or claim.reason != "EXISTS":
            raise SystemExit(1)
    else:
        raise SystemExit(f"unsupported VERIFY_MODE: {mode}")
    print(json.dumps(output, ensure_ascii=False))
    await redis.aclose()


asyncio.run(run())
