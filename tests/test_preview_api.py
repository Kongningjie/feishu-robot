from fastapi.testclient import TestClient

from app.api.routes.previews import get_preview_service, get_sender_service
from app.core.config import Settings
from app.main import create_app
from app.models.domain import SheetDocument, SheetReference
from app.schemas.preview import PreviewStatus, SendResponse
from app.services.preview import PreviewService
from app.services.sender import SendStart
from app.services.sheet_parser import SheetParser


class FakeFeishu:
    async def fetch_document(self, url: str) -> SheetDocument:
        return SheetDocument(
            SheetReference("token", "sheet"),
            "X项目",
            [
                ["截至时间", "", "2026/10/10", "", ""],
                ["硬件阶段", "业务领域", "PR1", "申请人", "申请人邮箱"],
                ["", "摄像头", "", "@张三", "zhang@example.com"],
            ],
        )

    async def batch_get_open_ids(self, emails: list[str]) -> dict[str, tuple[str, ...]]:
        return {"zhang@example.com": ("ou_must_not_leak",)}


class MemoryRepository:
    def __init__(self) -> None:
        self.snapshots = {}

    async def save(self, snapshot, ttl_seconds: int) -> bool:
        self.snapshots[snapshot.preview_id] = snapshot
        return True

    async def get(self, preview_id: str):
        return self.snapshots.get(preview_id)


def test_create_and_get_preview_mock_integration() -> None:
    app = create_app(
        Settings(_env_file=None, app_env="test", csrf_enabled=False, rate_limit_enabled=False)
    )
    service = PreviewService(FakeFeishu(), SheetParser(), MemoryRepository())
    app.dependency_overrides[get_preview_service] = lambda: service
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/previews",
            json={
                "spreadsheetUrl": "https://company.feishu.cn/sheets/token?sheet=sheet",
                "stageName": "PR1",
            },
        )
        preview_id = created.json()["data"]["previewId"]
        queried = client.get(f"/api/v1/previews/{preview_id}")

    assert created.status_code == 200
    assert created.json()["success"] is True
    assert queried.status_code == 200
    assert queried.json()["data"] == created.json()["data"]
    serialized = created.text
    assert "ou_must_not_leak" not in serialized
    assert "zhang@example.com" not in serialized


def test_validation_and_phase_two_routes_use_error_envelope() -> None:
    app = create_app(
        Settings(_env_file=None, app_env="test", csrf_enabled=False, rate_limit_enabled=False)
    )
    with TestClient(app) as client:
        invalid = client.post(
            "/api/v1/previews",
            json={"spreadsheetUrl": "not-a-url", "stageName": "  "},
        )
        send = client.post("/api/v1/previews/pv_123/send")
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "VALIDATION_ERROR"
    assert send.status_code == 404
    assert send.json()["code"] == "NOT_FOUND"


class StubSender:
    def __init__(self) -> None:
        self.runs: list[tuple[str, str, bool]] = []

    async def start_send(self, preview_id: str) -> SendStart:
        return SendStart(
            SendResponse(
                previewId=preview_id,
                status=PreviewStatus.sending,
                successCount=0,
                failureCount=0,
                pendingCount=1,
                attemptCount=0,
            ),
            "owner",
        )

    async def start_retry(self, preview_id: str) -> SendStart:
        return await self.start_send(preview_id)

    async def run_send(self, preview_id: str, owner: str, *, retry_only: bool = False) -> None:
        self.runs.append((preview_id, owner, retry_only))


def test_send_and_retry_routes_return_202_with_unified_envelope() -> None:
    app = create_app(
        Settings(_env_file=None, app_env="test", csrf_enabled=False, rate_limit_enabled=False)
    )
    sender = StubSender()
    app.dependency_overrides[get_sender_service] = lambda: sender
    preview_id = "pv_" + "a" * 32
    with TestClient(app) as client:
        sent = client.post(f"/api/v1/previews/{preview_id}/send")
        retried = client.post(f"/api/v1/previews/{preview_id}/retry-failures")
    assert sent.status_code == 202
    assert sent.json() == {
        "success": True,
        "code": "OK",
        "message": "success",
        "data": {
            "previewId": preview_id,
            "status": "SENDING",
            "successCount": 0,
            "failureCount": 0,
            "pendingCount": 1,
            "attemptCount": 0,
        },
    }
    assert retried.status_code == 202
    assert sender.runs == [(preview_id, "owner", False), (preview_id, "owner", True)]

    with TestClient(app) as client:
        rejected = client.post(
            f"/api/v1/previews/{preview_id}/retry-failures",
            json={"recipients": ["ou_client_must_not_choose"]},
        )
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "REQUEST_BODY_NOT_ALLOWED"
    assert set(rejected.json()) == {"success", "code", "message", "data"}
