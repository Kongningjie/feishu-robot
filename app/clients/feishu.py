import asyncio
import time
from collections import defaultdict
from collections.abc import Iterable
from typing import Any, Protocol
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from app.core.errors import StructuralError, UpstreamError
from app.models.domain import SheetDocument, SheetReference

_TOKEN_KEY = "feishu:tenant_token"
_TOKEN_INVALID_CODES = {99991661, 99991664, 99991665}


class TokenCache(Protocol):
    async def get(self, key: str) -> Any: ...

    async def set(self, key: str, value: str, *, ex: int) -> Any: ...

    async def delete(self, key: str) -> Any: ...

    async def ttl(self, key: str) -> int: ...


def parse_spreadsheet_url(url: str, allowed_hosts: set[str]) -> tuple[str, str]:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise StructuralError("INVALID_SHEET_URL", "飞书表格链接格式非法") from exc
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise StructuralError("INVALID_SHEET_URL", "飞书表格链接格式非法") from exc
    if (
        parsed.scheme != "https"
        or host not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise StructuralError("INVALID_SHEET_URL", "仅支持已配置公司域名的 HTTPS 飞书链接")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] not in {"sheets", "wiki"} or not parts[1]:
        raise StructuralError("INVALID_SHEET_URL", "仅支持 /sheets/ 或 /wiki/ 链接")
    token = parts[1]
    if parts[0] == "wiki":
        return "wiki", token
    sheet_ids = parse_qs(parsed.query, keep_blank_values=True).get("sheet", [])
    if len(sheet_ids) != 1 or not sheet_ids[0].strip():
        raise StructuralError("SHEET_ID_MISSING", "电子表格直链必须包含唯一的 sheet 参数")
    return token, sheet_ids[0].strip()


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


class FeishuClient:
    """阶段 1 所需的飞书 OpenAPI 异步客户端；不包含任何消息发送能力。"""

    def __init__(
        self,
        http: httpx.AsyncClient,
        app_id: str,
        app_secret: str,
        *,
        allowed_hosts: set[str],
        token_cache: TokenCache | None = None,
        api_base_url: str = "https://open.feishu.cn/open-apis",
    ) -> None:
        self._http = http
        self._app_id = app_id
        self._app_secret = app_secret
        self._allowed_hosts = allowed_hosts
        self._token_cache = token_cache
        self._api_base_url = api_base_url.rstrip("/")
        self._token_lock = asyncio.Lock()
        self._local_token: str | None = None
        self._local_token_expires_at = 0.0

    def _has_valid_local_token(self) -> bool:
        return bool(self._local_token and time.monotonic() < self._local_token_expires_at)

    async def _get_token(self, *, force_refresh: bool = False) -> str:
        if force_refresh:
            self._local_token = None
            self._local_token_expires_at = 0.0
            if self._token_cache is not None:
                await self._token_cache.delete(_TOKEN_KEY)
        if self._has_valid_local_token():
            assert self._local_token is not None
            return self._local_token
        if self._token_cache is not None:
            cached = await self._token_cache.get(_TOKEN_KEY)
            if cached:
                self._local_token = cached.decode() if isinstance(cached, bytes) else str(cached)
                ttl = await self._token_cache.ttl(_TOKEN_KEY)
                self._local_token_expires_at = time.monotonic() + max(1, ttl)
                return self._local_token
        async with self._token_lock:
            if self._has_valid_local_token():
                assert self._local_token is not None
                return self._local_token
            try:
                response = await self._http.post(
                    f"{self._api_base_url}/auth/v3/tenant_access_token/internal",
                    json={"app_id": self._app_id, "app_secret": self._app_secret},
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise UpstreamError("UPSTREAM_TIMEOUT", "获取飞书访问令牌超时", 504) from exc
            except (httpx.HTTPError, ValueError) as exc:
                raise UpstreamError("TOKEN_REFRESH_FAILED", "获取飞书访问令牌失败", 502) from exc
            if payload.get("code", 0) != 0 or not payload.get("tenant_access_token"):
                raise UpstreamError("TOKEN_REFRESH_FAILED", "获取飞书访问令牌失败", 502)
            token = str(payload["tenant_access_token"])
            ttl = max(1, int(payload.get("expire", 7200)) - 60)
            self._local_token = token
            self._local_token_expires_at = time.monotonic() + ttl
            if self._token_cache is not None:
                await self._token_cache.set(_TOKEN_KEY, token, ex=ttl)
            return token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        retried: bool = False,
    ) -> dict[str, Any]:
        token = await self._get_token()
        try:
            response = await self._http.request(
                method,
                f"{self._api_base_url}{path}",
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.TimeoutException as exc:
            raise UpstreamError("UPSTREAM_TIMEOUT", "飞书接口请求超时", 504) from exc
        except httpx.NetworkError as exc:
            raise UpstreamError("FEISHU_UPSTREAM_ERROR", "飞书接口暂时不可用", 502) from exc

        if response.status_code == 429:
            raise UpstreamError("UPSTREAM_RATE_LIMITED", "飞书接口请求过于频繁", 503)
        if response.status_code >= 500:
            raise UpstreamError("FEISHU_UPSTREAM_ERROR", "飞书接口暂时不可用", 502)
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError("FEISHU_UPSTREAM_ERROR", "飞书接口返回无效数据", 502) from exc
        code = int(payload.get("code", 0))
        if code in _TOKEN_INVALID_CODES and not retried:
            await self._get_token(force_refresh=True)
            return await self._request(method, path, params=params, json=json, retried=True)
        if response.status_code in {401, 403}:
            raise StructuralError("SHEET_ACCESS_DENIED", "应用无权访问该飞书资源", 403)
        if response.status_code == 404:
            raise StructuralError("SHEET_NOT_FOUND", "表格或工作表不存在", 404)
        if code != 0:
            message = str(payload.get("msg", "")).lower()
            if "permission" in message or "forbidden" in message:
                raise StructuralError("SHEET_ACCESS_DENIED", "应用无权访问该飞书资源", 403)
            if "not found" in message:
                raise StructuralError("SHEET_NOT_FOUND", "表格或工作表不存在", 404)
            raise UpstreamError("FEISHU_UPSTREAM_ERROR", "飞书接口返回业务错误", 502)
        return payload

    async def resolve_document(self, url: str) -> SheetReference:
        token, sheet_id = parse_spreadsheet_url(url, self._allowed_hosts)
        if token != "wiki":
            return SheetReference(token, sheet_id)
        wiki_token = sheet_id
        payload = await self._request(
            "GET", "/wiki/v2/spaces/get_node", params={"token": wiki_token}
        )
        node = payload.get("data", {}).get("node", {})
        if node.get("obj_type") != "sheet" or not node.get("obj_token"):
            raise StructuralError("WIKI_NODE_INVALID", "wiki 节点不是普通电子表格")
        spreadsheet_token = str(node["obj_token"])
        sheets = await self._list_sheets(spreadsheet_token)
        if len(sheets) != 1:
            if len(sheets) > 1:
                raise StructuralError(
                    "MULTIPLE_SHEETS_REQUIRE_DIRECT_URL",
                    "wiki 底层电子表格含多个页签，请使用包含 sheet_id 的直链",
                )
            raise StructuralError("SHEET_NOT_FOUND", "电子表格中没有可用工作表", 404)
        return SheetReference(spreadsheet_token, str(sheets[0]["sheet_id"]))

    async def _list_sheets(self, spreadsheet_token: str) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET", f"/sheets/v3/spreadsheets/{quote(spreadsheet_token)}/sheets/query"
        )
        return list(payload.get("data", {}).get("sheets", []))

    async def _project_name(self, spreadsheet_token: str) -> str:
        payload = await self._request("GET", f"/sheets/v3/spreadsheets/{quote(spreadsheet_token)}")
        spreadsheet = payload.get("data", {}).get("spreadsheet", {})
        return str(spreadsheet.get("title") or "未命名电子表格")

    async def read_sheet(self, spreadsheet_token: str, sheet_id: str) -> list[list[Any]]:
        payload = await self._request(
            "GET",
            f"/sheets/v2/spreadsheets/{quote(spreadsheet_token)}/values/{quote(sheet_id)}",
            params={
                "valueRenderOption": "FormattedValue",
                "dateTimeRenderOption": "FormattedString",
            },
        )
        values = payload.get("data", {}).get("valueRange", {}).get("values", [])
        if not isinstance(values, list):
            raise UpstreamError("FEISHU_UPSTREAM_ERROR", "飞书表格数据格式无效", 502)
        return values

    async def fetch_document(self, url: str) -> SheetDocument:
        reference = await self.resolve_document(url)
        sheets = await self._list_sheets(reference.spreadsheet_token)
        if not any(str(sheet.get("sheet_id")) == reference.sheet_id for sheet in sheets):
            raise StructuralError("SHEET_NOT_FOUND", "URL 指定的工作表不存在", 404)
        project_name, rows = await asyncio.gather(
            self._project_name(reference.spreadsheet_token),
            self.read_sheet(reference.spreadsheet_token, reference.sheet_id),
        )
        return SheetDocument(reference, project_name, rows)

    async def batch_get_open_ids(self, emails: list[str]) -> dict[str, tuple[str, ...]]:
        mapped: dict[str, list[str]] = defaultdict(list)
        for chunk in _chunks(emails, 50):
            payload = await self._request(
                "POST",
                "/contact/v3/users/batch_get_id",
                params={"user_id_type": "open_id"},
                json={"emails": chunk, "include_resigned": False},
            )
            for user in payload.get("data", {}).get("user_list", []):
                email = str(user.get("email", "")).strip().casefold()
                open_id = str(user.get("user_id", "")).strip()
                if email and open_id and open_id not in mapped[email]:
                    mapped[email].append(open_id)
        return {email: tuple(open_ids) for email, open_ids in mapped.items()}
