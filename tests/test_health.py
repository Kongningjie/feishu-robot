from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


class HealthyRedis:
    async def ping(self) -> bool:
        return True


def test_live_uses_response_envelope() -> None:
    app = create_app(Settings(_env_file=None, app_env="test"))
    with TestClient(app) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "code": "OK",
        "message": "success",
        "data": {"status": "ok"},
    }


def test_ready_rejects_missing_required_configuration() -> None:
    app = create_app(
        Settings(_env_file=None, app_env="test", feishu_app_id="", feishu_app_secret="")
    )
    with TestClient(app) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["code"] == "CONFIG_NOT_READY"


def test_ready_checks_redis() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        feishu_app_id="app-id",
        feishu_app_secret="secret",
        feishu_allowed_hosts="example.feishu.cn",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.redis = HealthyRedis()
        response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["data"] == {"status": "ready"}
