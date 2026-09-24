from app.clients.feishu import FeishuClient
from app.repositories.preview import PreviewRepository


class ReminderSender:
    def __init__(self, feishu: FeishuClient, repository: PreviewRepository) -> None:
        self._feishu = feishu
        self._repository = repository

    async def send(self, preview_id: str) -> None:
        del preview_id
        raise NotImplementedError

    async def retry_failures(self, preview_id: str) -> None:
        del preview_id
        raise NotImplementedError
