from dataclasses import dataclass


@dataclass(slots=True)
class AppError(Exception):
    code: str
    message: str
    status_code: int = 400

    def __str__(self) -> str:
        return self.message


class StructuralError(AppError):
    pass


class UpstreamError(AppError):
    pass


@dataclass(slots=True)
class MessageSendError(Exception):
    code: str
    message: str
    retryable: bool = False
    retry_after_seconds: float | None = None
    feishu_code: int | None = None

    def __str__(self) -> str:
        return self.message
