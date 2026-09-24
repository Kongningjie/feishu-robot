import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from app.core.observability import preview_id_context, request_id_context

_EMAIL_RE = re.compile(r"(?i)\b([a-z0-9._%+-])[a-z0-9._%+-]*@([a-z0-9-])[a-z0-9.-]*\.[a-z]{2,}\b")
_TOKEN_RE = re.compile(r"(?i)\b(?:t-|ou_|shtcn|wikcn)[a-z0-9_-]{6,}\b")
_SHEET_QUERY_RE = re.compile(r"(?i)([?&]sheet=)[^&#\s]+")
_DOCUMENT_PATH_RE = re.compile(r"(?i)(/(?:sheets|wiki)/)[^/?#\s]+")


def redact(value: str) -> str:
    value = _EMAIL_RE.sub(r"\1***@\2***", value)
    value = _SHEET_QUERY_RE.sub(r"\1***", value)
    value = _DOCUMENT_PATH_RE.sub(r"\1***", value)
    return _TOKEN_RE.sub("***", value)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
            "requestId": request_id_context.get(),
            "previewId": preview_id_context.get(),
        }
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
