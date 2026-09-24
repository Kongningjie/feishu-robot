import httpx
import pytest
import respx

from app.clients.feishu import FeishuClient, parse_spreadsheet_url
from app.core.errors import MessageSendError, StructuralError

BASE = "https://open.feishu.cn/open-apis"
HOSTS = {"company.feishu.cn"}


class MemoryTokenCache:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, *, ex: int) -> bool:
        assert ex == 7140
        self.values[key] = value
        return True

    async def delete(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)

    async def ttl(self, key: str) -> int:
        return 7140 if key in self.values else -2


@pytest.mark.parametrize(
    "url",
    [
        "http://company.feishu.cn/sheets/abc?sheet=s1",
        "https://evil.example/sheets/abc?sheet=s1",
        "https://company.feishu.cn/docs/abc?sheet=s1",
        "https://company.feishu.cn/sheets/abc",
        "https://company.feishu.cn/sheets/abc?sheet=s1&sheet=s2",
        "https://user@company.feishu.cn/sheets/abc?sheet=s1",
    ],
)
def test_url_validation_rejects_unsafe_or_incomplete_urls(url: str) -> None:
    with pytest.raises(StructuralError):
        parse_spreadsheet_url(url, HOSTS)


def test_parse_direct_sheet_url() -> None:
    assert parse_spreadsheet_url(
        "https://company.feishu.cn/sheets/sht-token?sheet=sheet-1#fragment", HOSTS
    ) == ("sht-token", "sheet-1")


@pytest.mark.asyncio
@respx.mock
async def test_resolve_single_sheet_wiki_and_cache_token() -> None:
    respx.post(f"{BASE}/auth/v3/tenant_access_token/internal").mock(
        return_value=httpx.Response(
            200, json={"code": 0, "tenant_access_token": "token-value", "expire": 7200}
        )
    )
    respx.get(f"{BASE}/wiki/v2/spaces/get_node", params={"token": "wiki-token"}).mock(
        return_value=httpx.Response(
            200,
            json={"code": 0, "data": {"node": {"obj_type": "sheet", "obj_token": "sht"}}},
        )
    )
    respx.get(f"{BASE}/sheets/v3/spreadsheets/sht/sheets/query").mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"sheets": [{"sheet_id": "s1"}]}})
    )
    cache = MemoryTokenCache()
    async with httpx.AsyncClient() as http:
        client = FeishuClient(http, "app", "secret", allowed_hosts=HOSTS, token_cache=cache)
        reference = await client.resolve_document("https://company.feishu.cn/wiki/wiki-token")
    assert reference.spreadsheet_token == "sht"
    assert reference.sheet_id == "s1"
    assert cache.values["feishu:tenant_token"] == "token-value"


@pytest.mark.asyncio
@respx.mock
async def test_wiki_with_multiple_sheets_requires_direct_url() -> None:
    respx.post(f"{BASE}/auth/v3/tenant_access_token/internal").mock(
        return_value=httpx.Response(
            200, json={"code": 0, "tenant_access_token": "token", "expire": 7200}
        )
    )
    respx.get(f"{BASE}/wiki/v2/spaces/get_node").mock(
        return_value=httpx.Response(
            200,
            json={"code": 0, "data": {"node": {"obj_type": "sheet", "obj_token": "sht"}}},
        )
    )
    respx.get(f"{BASE}/sheets/v3/spreadsheets/sht/sheets/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"sheets": [{"sheet_id": "s1"}, {"sheet_id": "s2"}]},
            },
        )
    )
    async with httpx.AsyncClient() as http:
        client = FeishuClient(http, "app", "secret", allowed_hosts=HOSTS)
        with pytest.raises(StructuralError) as caught:
            await client.resolve_document("https://company.feishu.cn/wiki/wiki-token")
    assert caught.value.code == "MULTIPLE_SHEETS_REQUIRE_DIRECT_URL"


@pytest.mark.asyncio
@respx.mock
async def test_batch_get_ids_deduplicates_results() -> None:
    respx.post(f"{BASE}/auth/v3/tenant_access_token/internal").mock(
        return_value=httpx.Response(
            200, json={"code": 0, "tenant_access_token": "token", "expire": 7200}
        )
    )
    respx.post(f"{BASE}/contact/v3/users/batch_get_id").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "user_list": [
                        {"email": "A@example.com", "user_id": "ou_1"},
                        {"email": "a@example.com", "user_id": "ou_1"},
                    ]
                },
            },
        )
    )
    async with httpx.AsyncClient() as http:
        client = FeishuClient(http, "app", "secret", allowed_hosts=HOSTS)
        result = await client.batch_get_open_ids(["a@example.com"])
    assert result == {"a@example.com": ("ou_1",)}


@pytest.mark.asyncio
@respx.mock
async def test_fetch_direct_sheet_reads_metadata_and_effective_values() -> None:
    respx.post(f"{BASE}/auth/v3/tenant_access_token/internal").mock(
        return_value=httpx.Response(
            200, json={"code": 0, "tenant_access_token": "token", "expire": 7200}
        )
    )
    respx.get(f"{BASE}/sheets/v3/spreadsheets/sht/sheets/query").mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"sheets": [{"sheet_id": "s1"}]}})
    )
    respx.get(f"{BASE}/sheets/v3/spreadsheets/sht").mock(
        return_value=httpx.Response(
            200, json={"code": 0, "data": {"spreadsheet": {"title": "项目表"}}}
        )
    )
    respx.get(f"{BASE}/sheets/v2/spreadsheets/sht/values/s1").mock(
        return_value=httpx.Response(
            200,
            json={"code": 0, "data": {"valueRange": {"values": [["有效数据"]]}}},
        )
    )
    async with httpx.AsyncClient() as http:
        client = FeishuClient(http, "app", "secret", allowed_hosts=HOSTS)
        document = await client.fetch_document("https://company.feishu.cn/sheets/sht?sheet=s1")
    assert document.project_name == "项目表"
    assert document.rows == [["有效数据"]]


@pytest.mark.asyncio
@respx.mock
async def test_feishu_permission_error_is_normalized() -> None:
    respx.post(f"{BASE}/auth/v3/tenant_access_token/internal").mock(
        return_value=httpx.Response(
            200, json={"code": 0, "tenant_access_token": "token", "expire": 7200}
        )
    )
    respx.get(f"{BASE}/sheets/v3/spreadsheets/sht/sheets/query").mock(
        return_value=httpx.Response(403, json={"code": 999, "msg": "forbidden"})
    )
    async with httpx.AsyncClient() as http:
        client = FeishuClient(http, "app", "secret", allowed_hosts=HOSTS)
        with pytest.raises(StructuralError) as caught:
            await client.fetch_document("https://company.feishu.cn/sheets/sht?sheet=s1")
    assert caught.value.code == "SHEET_ACCESS_DENIED"


@pytest.mark.asyncio
@respx.mock
async def test_expired_token_is_refreshed_and_request_replayed_once() -> None:
    token_route = respx.post(f"{BASE}/auth/v3/tenant_access_token/internal").mock(
        side_effect=[
            httpx.Response(200, json={"code": 0, "tenant_access_token": "old", "expire": 7200}),
            httpx.Response(200, json={"code": 0, "tenant_access_token": "new", "expire": 7200}),
        ]
    )
    sheet_route = respx.get(f"{BASE}/sheets/v3/spreadsheets/sht/sheets/query").mock(
        side_effect=[
            httpx.Response(200, json={"code": 99991661, "msg": "token invalid"}),
            httpx.Response(200, json={"code": 0, "data": {"sheets": [{"sheet_id": "s1"}]}}),
        ]
    )
    async with httpx.AsyncClient() as http:
        client = FeishuClient(http, "app", "secret", allowed_hosts=HOSTS)
        sheets = await client._list_sheets("sht")
    assert token_route.call_count == 2
    assert sheet_route.call_count == 2
    assert sheets == [{"sheet_id": "s1"}]


@pytest.mark.asyncio
@respx.mock
async def test_send_card_uses_open_id_and_returns_message_id() -> None:
    respx.post(f"{BASE}/auth/v3/tenant_access_token/internal").mock(
        return_value=httpx.Response(
            200, json={"code": 0, "tenant_access_token": "token", "expire": 7200}
        )
    )
    route = respx.post(
        f"{BASE}/im/v1/messages", params={"receive_id_type": "open_id"}
    ).mock(return_value=httpx.Response(200, json={"code": 0, "data": {"message_id": "om_1"}}))
    async with httpx.AsyncClient() as http:
        client = FeishuClient(http, "app", "secret", allowed_hosts=HOSTS)
        message_id = await client.send_card("ou_secret", "{\"schema\":\"2.0\"}")
    assert message_id == "om_1"
    assert route.calls.last.request.content
    assert b'ou_secret' in route.calls.last.request.content


@pytest.mark.asyncio
@respx.mock
async def test_send_card_classifies_rate_limit_and_retry_after() -> None:
    respx.post(f"{BASE}/auth/v3/tenant_access_token/internal").mock(
        return_value=httpx.Response(
            200, json={"code": 0, "tenant_access_token": "token", "expire": 7200}
        )
    )
    respx.post(f"{BASE}/im/v1/messages").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "3"}, json={"code": 1})
    )
    async with httpx.AsyncClient() as http:
        client = FeishuClient(http, "app", "secret", allowed_hosts=HOSTS)
        with pytest.raises(MessageSendError) as caught:
            await client.send_card("ou_secret", "{}")
    assert caught.value.code == "UPSTREAM_RATE_LIMITED"
    assert caught.value.retryable is True
    assert caught.value.retry_after_seconds == 3


@pytest.mark.asyncio
@respx.mock
async def test_send_card_does_not_retry_permission_error() -> None:
    respx.post(f"{BASE}/auth/v3/tenant_access_token/internal").mock(
        return_value=httpx.Response(
            200, json={"code": 0, "tenant_access_token": "token", "expire": 7200}
        )
    )
    respx.post(f"{BASE}/im/v1/messages").mock(
        return_value=httpx.Response(403, json={"code": 230006, "msg": "forbidden"})
    )
    async with httpx.AsyncClient() as http:
        client = FeishuClient(http, "app", "secret", allowed_hosts=HOSTS)
        with pytest.raises(MessageSendError) as caught:
            await client.send_card("ou_secret", "{}")
    assert caught.value.code == "MESSAGE_PERMISSION_DENIED"
    assert caught.value.retryable is False
