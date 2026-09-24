from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from app.core.errors import StructuralError
from app.models.domain import ParsedSheet, PendingItem, PreviewAnomalyData

_HEADERS = ("截至时间", "硬件阶段", "业务领域", "申请人", "申请人邮箱")


def _computed_value(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("value", "result", "text"):
            if key in value:
                return value[key]
    return value


def is_blank(value: Any) -> bool:
    value = _computed_value(value)
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def _text(value: Any) -> str:
    value = _computed_value(value)
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "".join(_text(part) for part in value).strip()
    return str(value).strip()


def _cell(row: list[Any], index: int) -> Any:
    return row[index] if index < len(row) else None


def _parse_applicant(value: Any) -> tuple[str, int]:
    if is_blank(value):
        return "", 0
    values = value if isinstance(value, list) else [value]
    mentions: list[str] = []
    for part in values:
        if isinstance(part, dict):
            kind = str(part.get("type", "")).lower()
            if kind in {"mention", "user", "person"} or "token" in part:
                name = _text(part.get("text") or part.get("name") or part.get("value"))
                mentions.append(name.removeprefix("@").strip())
        elif isinstance(part, str) and part.strip():
            mentions.append(part.removeprefix("@").strip())
    mentions = [name for name in mentions if name]
    return (mentions[0] if mentions else "", len(mentions))


def _parse_deadline(value: Any, timezone: ZoneInfo) -> datetime:
    value = _computed_value(value)
    parsed: datetime | None = None
    date_only = False
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time(23, 59, 59))
        date_only = True
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError
        normalized = raw.replace("年", "-").replace("月", "-").replace("日", "")
        try:
            parsed = datetime.fromisoformat(normalized.replace("/", "-"))
            date_only = "T" not in normalized and " " not in normalized and ":" not in normalized
        except ValueError:
            for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    parsed = datetime.strptime(normalized.replace("/", "-"), pattern)
                    break
                except ValueError:
                    continue
    if parsed is None:
        raise ValueError
    if date_only:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=0)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


class SheetParser:
    """按冻结模板定位字段并抽取目标阶段未填记录。"""

    def __init__(self, *, timezone: str = "Asia/Shanghai", max_data_rows: int = 300) -> None:
        self._timezone = ZoneInfo(timezone)
        self._max_data_rows = max_data_rows

    def parse(self, rows: list[list[Any]], stage_name: str) -> ParsedSheet:
        positions: dict[str, list[tuple[int, int]]] = {header: [] for header in _HEADERS}
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row):
                normalized = _text(value)
                if normalized in positions:
                    positions[normalized].append((row_index, column_index))

        for header, matches in positions.items():
            if not matches:
                raise StructuralError("HEADER_MISSING", f"缺少关键表头：{header}")
            if len(matches) > 1:
                raise StructuralError("HEADER_DUPLICATED", f"关键表头重复：{header}")

        header_row_index, _ = positions["硬件阶段"][0]
        header_row = rows[header_row_index]
        required_columns: dict[str, int] = {}
        for name in ("业务领域", "申请人", "申请人邮箱"):
            row_matches = [i for i, value in enumerate(header_row) if _text(value) == name]
            if len(row_matches) != 1:
                raise StructuralError("HEADER_MISSING", f"{name}必须位于硬件阶段表头行")
            required_columns[name] = row_matches[0]

        normalized_stage = stage_name.strip().casefold()
        stage_columns = [
            index
            for index, value in enumerate(header_row)
            if _text(value).casefold() == normalized_stage
        ]
        if not stage_columns:
            raise StructuralError("STAGE_NOT_FOUND", "目标硬件阶段不存在")
        if len(stage_columns) > 1:
            raise StructuralError("STAGE_AMBIGUOUS", "目标硬件阶段匹配不唯一")
        stage_column = stage_columns[0]
        matched_stage_name = _text(header_row[stage_column])

        deadline_row_index, _ = positions["截至时间"][0]
        try:
            deadline = _parse_deadline(
                _cell(rows[deadline_row_index], stage_column), self._timezone
            )
        except (ValueError, TypeError, OverflowError) as exc:
            raise StructuralError("DEADLINE_INVALID", "目标阶段的截止时间缺失或非法") from exc

        business_column = required_columns["业务领域"]
        applicant_column = required_columns["申请人"]
        email_column = required_columns["申请人邮箱"]
        data_rows: list[tuple[int, list[Any], str]] = []
        for row_index in range(header_row_index + 1, len(rows)):
            row = rows[row_index]
            business_domain = _text(_cell(row, business_column))
            if business_domain:
                data_rows.append((row_index, row, business_domain))
        if len(data_rows) > self._max_data_rows:
            raise StructuralError(
                "ROW_LIMIT_EXCEEDED", f"业务数据行超过{self._max_data_rows}行限制"
            )

        pending: list[PendingItem] = []
        anomalies: list[PreviewAnomalyData] = []
        for row_index, row, business_domain in data_rows:
            if not is_blank(_cell(row, stage_column)):
                continue
            row_number = row_index + 1
            applicant_name, applicant_count = _parse_applicant(_cell(row, applicant_column))
            email = _text(_cell(row, email_column)).strip().casefold()
            if applicant_count == 0:
                anomalies.append(
                    PreviewAnomalyData(
                        row_number, business_domain, "APPLICANT_MISSING", "未填写申请人"
                    )
                )
                continue
            if applicant_count > 1:
                anomalies.append(
                    PreviewAnomalyData(
                        row_number,
                        business_domain,
                        "APPLICANT_MULTIPLE",
                        "一个业务领域对应多个申请人",
                    )
                )
                continue
            if not email:
                anomalies.append(
                    PreviewAnomalyData(
                        row_number, business_domain, "APPLICANT_EMAIL_MISSING", "未填写申请人邮箱"
                    )
                )
                continue
            pending.append(PendingItem(row_number, business_domain, applicant_name, email))

        return ParsedSheet(
            stage_name=matched_stage_name,
            deadline=deadline,
            source_row_count=len(data_rows),
            pending_items=tuple(pending),
            anomalies=tuple(anomalies),
        )
