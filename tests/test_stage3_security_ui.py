from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

BASE_DIR = Path(__file__).resolve().parents[1]


class CounterRedis:
    def __init__(self) -> None:
        self.value = 0

    async def eval(self, script, key_count, key, window):
        self.value += 1
        return self.value


def test_csrf_requires_cookie_header_pair_and_uses_envelope() -> None:
    app = create_app(
        Settings(_env_file=None, app_env="test", csrf_enabled=True, rate_limit_enabled=False)
    )
    with TestClient(app) as client:
        page = client.get("/")
        rejected = client.post(
            "/api/v1/previews",
            json={"spreadsheetUrl": "invalid", "stageName": ""},
        )
        token = client.cookies["csrf_token"]
        accepted_by_csrf = client.post(
            "/api/v1/previews",
            headers={"X-CSRF-Token": token},
            json={"spreadsheetUrl": "invalid", "stageName": ""},
        )
    assert page.status_code == 200
    assert rejected.status_code == 403
    assert rejected.json() == {
        "success": False,
        "code": "CSRF_VALIDATION_FAILED",
        "message": "CSRF 校验失败",
        "data": None,
    }
    assert accepted_by_csrf.status_code == 422
    assert accepted_by_csrf.json()["code"] == "VALIDATION_ERROR"


def test_redis_rate_limit_and_request_id() -> None:
    app = create_app(
        Settings(
            _env_file=None,
            app_env="test",
            csrf_enabled=False,
            rate_limit_enabled=True,
            rate_limit_requests=1,
        )
    )
    with TestClient(app) as client:
        app.state.redis = CounterRedis()
        first = client.get("/api/v1/previews/pv_bad", headers={"X-Request-ID": "test-1"})
        second = client.get("/api/v1/previews/pv_bad")
    assert first.status_code == 422
    assert first.headers["X-Request-ID"] == "test-1"
    assert second.status_code == 429
    assert second.json()["code"] == "RATE_LIMITED"
    assert set(second.json()) == {"success", "code", "message", "data"}


def test_metrics_and_security_headers_use_safe_contract() -> None:
    app = create_app(
        Settings(_env_file=None, app_env="test", csrf_enabled=False, rate_limit_enabled=False)
    )
    with TestClient(app) as client:
        response = client.get("/internal/metrics")
    assert response.status_code == 200
    assert response.json()["success"] is True
    counters = response.json()["data"]["counters"]
    assert "preview_success_total" in counters
    assert "send_recipient_failure_total" in counters
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_untrusted_http_host_is_rejected() -> None:
    app = create_app(
        Settings(_env_file=None, app_env="test", csrf_enabled=False, rate_limit_enabled=False)
    )
    with TestClient(app) as client:
        response = client.get("/", headers={"Host": "evil.example"})
    assert response.status_code == 400


def test_frontend_contains_read_only_flow_without_identity_fields() -> None:
    template = (BASE_DIR / "app/templates/index.html").read_text(encoding="utf-8")
    script = (BASE_DIR / "app/static/app.js").read_text(encoding="utf-8")
    assert "confirm-dialog" in template
    assert "retry-button" in template
    assert "send-progress" in template
    assert "sessionStorage" in script
    assert "retry-failures" in script
    assert "setTimeout(refreshCurrent, 2000)" in script
    assert "innerHTML" not in script
    assert "open_id" not in template
    assert "open_id" not in script
