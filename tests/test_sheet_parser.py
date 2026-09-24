from datetime import datetime

import pytest

from app.core.errors import StructuralError
from app.services.sheet_parser import SheetParser, is_blank


def make_rows(*data_rows: list[object], deadline: object = "2026/10/10") -> list[list[object]]:
    return [
        ["截至时间", "", deadline, "", ""],
        ["硬件阶段", "业务领域", "PR1", "申请人", "申请人邮箱"],
        *data_rows,
    ]


@pytest.mark.parametrize("value", [None, "", "   ", {"value": ""}, {"result": "  "}])
def test_blank_values(value: object) -> None:
    assert is_blank(value)


@pytest.mark.parametrize("value", [0, "0", "无需求", "不适用", {"value": "完成"}])
def test_non_blank_values(value: object) -> None:
    assert not is_blank(value)


def test_parser_matches_stage_and_isolates_row_errors() -> None:
    rows = make_rows(
        ["", "摄像头", "", "@张三", " Zhang@Example.com "],
        ["", "信号", 0, "@李四", "li@example.com"],
        [
            "",
            "传感器",
            {"value": ""},
            [
                {"type": "mention", "text": "@甲", "token": "1"},
                {"type": "mention", "text": "@乙", "token": "2"},
            ],
            "two@example.com",
        ],
        ["部门", "", "", "", ""],
        ["", "音频", "", "", "missing@example.com"],
        ["", "电池", "", "@王五", ""],
    )
    parsed = SheetParser().parse(rows, " pr1 ")
    assert parsed.stage_name == "PR1"
    assert parsed.source_row_count == 5
    assert parsed.deadline == datetime.fromisoformat("2026-10-10T23:59:59+08:00")
    assert [
        (item.row_number, item.business_domain, item.applicant_email)
        for item in parsed.pending_items
    ] == [(3, "摄像头", "zhang@example.com")]
    assert [item.code for item in parsed.anomalies] == [
        "APPLICANT_MULTIPLE",
        "APPLICANT_MISSING",
        "APPLICANT_EMAIL_MISSING",
    ]


@pytest.mark.parametrize(
    ("rows", "stage", "code"),
    [
        (
            [["截至时间", "", "2026-01-01"], ["硬件阶段", "业务领域", "PR1"]],
            "PR1",
            "HEADER_MISSING",
        ),
        (make_rows([], deadline="bad-date"), "PR1", "DEADLINE_INVALID"),
        (make_rows([]), "PR2", "STAGE_NOT_FOUND"),
    ],
)
def test_structural_errors(rows: list[list[object]], stage: str, code: str) -> None:
    with pytest.raises(StructuralError) as caught:
        SheetParser().parse(rows, stage)
    assert caught.value.code == code


def test_stage_ambiguous() -> None:
    rows = [
        ["截至时间", "", "2026-01-01", "2026-01-02", "", ""],
        ["硬件阶段", "业务领域", "PR1", "pr1", "申请人", "申请人邮箱"],
    ]
    with pytest.raises(StructuralError) as caught:
        SheetParser().parse(rows, "PR1")
    assert caught.value.code == "STAGE_AMBIGUOUS"


def test_duplicated_critical_header_is_rejected() -> None:
    rows = make_rows(["业务领域", "摄像头", "", "@张三", "a@example.com"])
    with pytest.raises(StructuralError) as caught:
        SheetParser().parse(rows, "PR1")
    assert caught.value.code == "HEADER_DUPLICATED"


def test_row_limit_accepts_300_and_rejects_301() -> None:
    row = ["", "领域", "已填写", "@张三", "a@example.com"]
    assert SheetParser().parse(make_rows(*([row] * 300)), "PR1").source_row_count == 300
    with pytest.raises(StructuralError) as caught:
        SheetParser().parse(make_rows(*([row] * 301)), "PR1")
    assert caught.value.code == "ROW_LIMIT_EXCEEDED"
