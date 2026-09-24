from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "local"
    app_name: str = "feishu-sample-reminder"
    app_timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"
    allowed_hosts: str = "localhost,127.0.0.1,testserver"
    csrf_enabled: bool = True
    csrf_cookie_secure: bool = False
    rate_limit_enabled: bool = True
    rate_limit_requests: int = Field(default=60, gt=0)
    rate_limit_window_seconds: int = Field(default=60, gt=0)

    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_allowed_hosts: str = "ccn9vqxcon1p.feishu.cn,transsioner.feishu.cn"

    redis_url: str = "redis://localhost:6379/0"
    preview_ttl_seconds: int = Field(default=86_400, gt=0)
    result_ttl_seconds: int = Field(default=604_800, gt=0)
    max_data_rows: int = Field(default=300, gt=0)
    max_recipients: int = Field(default=300, gt=0)
    send_concurrency: int = Field(default=5, gt=0)
    send_auto_retries: int = Field(default=2, ge=0)
    send_lock_ttl_seconds: int = Field(default=600, gt=0)
    recovery_scan_seconds: int = Field(default=30, gt=0)
    send_task_timeout_seconds: int = Field(default=600, gt=0)
    message_max_bytes: int = Field(default=30_000, gt=0)
    http_connect_timeout_seconds: float = Field(default=3, gt=0)
    http_read_timeout_seconds: float = Field(default=10, gt=0)
    template_version: int = Field(default=1, gt=0)
    feishu_api_base_url: str = "https://open.feishu.cn/open-apis"

    @property
    def allowed_feishu_hosts(self) -> set[str]:
        return {
            host.strip().lower() for host in self.feishu_allowed_hosts.split(",") if host.strip()
        }

    @property
    def allowed_http_hosts(self) -> list[str]:
        return [host.strip() for host in self.allowed_hosts.split(",") if host.strip()]

    @property
    def missing_required_settings(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.feishu_app_id.strip():
            missing.append("FEISHU_APP_ID")
        if not self.feishu_app_secret.strip():
            missing.append("FEISHU_APP_SECRET")
        if not self.allowed_feishu_hosts:
            missing.append("FEISHU_ALLOWED_HOSTS")
        if not self.redis_url.strip():
            missing.append("REDIS_URL")
        return tuple(missing)


@lru_cache
def get_settings() -> Settings:
    return Settings()
