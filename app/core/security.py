import hashlib
import hmac
import re
import secrets
from typing import Any

from fastapi import Request

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_RATE_LIMIT_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return current
"""


def request_id(value: str | None) -> str:
    if value and _REQUEST_ID_RE.fullmatch(value):
        return value
    return secrets.token_hex(16)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_is_valid(request: Request) -> bool:
    cookie = request.cookies.get("csrf_token", "")
    header = request.headers.get("X-CSRF-Token", "")
    return bool(cookie and header and hmac.compare_digest(cookie, header))


async def consume_rate_limit(
    redis: Any,
    request: Request,
    *,
    requests: int,
    window_seconds: int,
) -> tuple[bool, int]:
    client_value = request.client.host if request.client else "unknown"
    digest = hashlib.sha256(client_value.encode("utf-8")).hexdigest()[:24]
    key = f"reminder:rate:{digest}"
    current = int(await redis.eval(_RATE_LIMIT_SCRIPT, 1, key, window_seconds))
    return current <= requests, current
