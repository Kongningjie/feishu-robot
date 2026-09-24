from app.core.errors import StructuralError
from app.models.domain import SheetDocument, SheetReference
from app.schemas.preview import CreatePreviewRequest
from app.services.preview import PreviewService
from app.services.sheet_parser import SheetParser


class FakeFeishu:
    def __init__(self, rows: list[list[object]], mappings: dict[str, tuple[str, ...]]) -> None:
        self.rows = rows
        self.mappings = mappings
        self.requested_emails: list[str] = []

    async def fetch_document(self, url: str) -> SheetDocument:
        return SheetDocument(SheetReference("token", "sheet"), "X项目", self.rows)

    async def batch_get_open_ids(self, emails: list[str]) -> dict[str, tuple[str, ...]]:
        self.requested_emails = emails
        return self.mappings


class MemoryRepository:
    def __init__(self) -> None:
        self.snapshots = {}
        self.ttl: int | None = None

    async def save(self, snapshot, ttl_seconds: int) -> bool:
        if snapshot.preview_id in self.snapshots:
            return False
        self.snapshots[snapshot.preview_id] = snapshot
        self.ttl = ttl_seconds
        return True

    async def get(self, preview_id: str):
        return self.snapshots.get(preview_id)


def rows_for_service() -> list[list[object]]:
    return [
        ["截至时间", "", "2026/10/10", "", ""],
        ["硬件阶段", "业务领域", "PR1", "申请人", "申请人邮箱"],
        ["", "摄像头", "", "@张三", " ZHANG@example.com "],
        ["", "传感器", "", "@张三", "zhang@example.com"],
        ["", "摄像头", "", "@张三", "zhang@example.com"],
        ["", "信号", "", "@李四", "not-an-email"],
        ["", "音频", "", "@王五", "wang@example.com"],
    ]


async def test_service_maps_once_aggregates_in_order_and_redacts_response() -> None:
    feishu = FakeFeishu(rows_for_service(), {"zhang@example.com": ("ou_secret",)})
    repository = MemoryRepository()
    service = PreviewService(feishu, SheetParser(), repository)
    response = await service.create(
        CreatePreviewRequest(
            spreadsheetUrl="https://company.feishu.cn/sheets/token?sheet=sheet",
            stageName="pr1",
        )
    )

    assert feishu.requested_emails == ["zhang@example.com", "wang@example.com"]
    assert response.recipient_count == 1
    assert response.pending_item_count == 2
    assert response.recipients[0].business_domains == ["摄像头", "传感器"]
    assert response.recipients[0].masked_email == "z***@e***.com"
    assert [item.code for item in response.anomalies] == [
        "APPLICANT_EMAIL_INVALID",
        "APPLICANT_EMAIL_UNRESOLVED",
    ]
    public_json = response.model_dump_json(by_alias=True)
    assert "ou_secret" not in public_json
    assert "zhang@example.com" not in public_json
    assert repository.ttl == 86_400
    saved = repository.snapshots[response.preview_id]
    assert saved.recipients[0].open_id == "ou_secret"
    assert "zhang@example.com" not in repr(saved)


async def test_zero_recipients_is_a_valid_empty_preview() -> None:
    rows = rows_for_service()[:2] + [["", "摄像头", "完成", "@张三", "z@example.com"]]
    service = PreviewService(FakeFeishu(rows, {}), SheetParser(), MemoryRepository())
    response = await service.create(
        CreatePreviewRequest(
            spreadsheetUrl="https://company.feishu.cn/sheets/token?sheet=sheet",
            stageName="PR1",
        )
    )
    assert response.recipient_count == 0
    assert response.pending_item_count == 0
    assert response.status == "READY"


async def test_recipient_limit_is_structural_error() -> None:
    rows = [
        ["截至时间", "", "2026/10/10", "", ""],
        ["硬件阶段", "业务领域", "PR1", "申请人", "申请人邮箱"],
        ["", "A", "", "@甲", "a@example.com"],
        ["", "B", "", "@乙", "b@example.com"],
    ]
    service = PreviewService(
        FakeFeishu(rows, {"a@example.com": ("ou_1",), "b@example.com": ("ou_2",)}),
        SheetParser(),
        MemoryRepository(),
        max_recipients=1,
    )
    try:
        await service.create(
            CreatePreviewRequest(
                spreadsheetUrl="https://company.feishu.cn/sheets/token?sheet=sheet",
                stageName="PR1",
            )
        )
    except StructuralError as exc:
        assert exc.code == "RECIPIENT_LIMIT_EXCEEDED"
    else:
        raise AssertionError("expected recipient limit error")
