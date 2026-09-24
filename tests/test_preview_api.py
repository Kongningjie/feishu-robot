from fastapi.testclient import TestClient

from app.api.routes.previews import get_preview_service
from app.core.config import Settings
from app.main import create_app
from app.models.domain import SheetDocument, SheetReference
from app.services.preview import PreviewService
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
    app = create_app(Settings(_env_file=None))
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
    app = create_app(Settings(_env_file=None))
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
