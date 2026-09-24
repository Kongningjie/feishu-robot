import json
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.errors import MessageSendError
from app.models.domain import PreviewSnapshot, RecipientAggregate


def deadline_label(deadline: datetime, now: datetime, timezone: str) -> str:
    zone = ZoneInfo(timezone)
    delta = (deadline.astimezone(zone).date() - now.astimezone(zone).date()).days
    if delta == 0:
        return "今日截止"
    if delta > 0:
        return f"剩余{delta}天"
    return f"已逾期{-delta}天"


class ReminderCardBuilder:
    def __init__(
        self,
        *,
        max_bytes: int = 30_000,
        timezone: str = "Asia/Shanghai",
    ) -> None:
        self._max_bytes = max_bytes
        self._timezone = timezone

    def build(
        self,
        snapshot: PreviewSnapshot,
        recipient: RecipientAggregate,
        *,
        now: datetime,
    ) -> str:
        deadline = snapshot.deadline.astimezone(ZoneInfo(self._timezone))
        domains = "\n".join(f"- {domain}" for domain in recipient.business_domains)
        card = {
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {
                "title": {"tag": "plain_text", "content": "样机需求填写提醒"},
                "template": "orange",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"你好，项目「{snapshot.project_name}」的 **{snapshot.stage_name}** "
                            f"阶段仍有以下业务领域尚未填写：\n{domains}\n\n"
                            f"截止时间：{deadline.strftime('%Y-%m-%d %H:%M:%S')}"
                            f"（{deadline_label(snapshot.deadline, now, self._timezone)}）\n\n"
                            "请及时打开表格完成填写，谢谢。"
                        ),
                    },
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "打开电子表格"},
                            "type": "primary",
                            "url": snapshot.spreadsheet_url,
                        }
                    ],
                },
            ],
        }
        content = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        request_body = json.dumps(
            {
                "receive_id": recipient.open_id,
                "msg_type": "interactive",
                "content": content,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(request_body.encode("utf-8")) > self._max_bytes:
            raise MessageSendError("MESSAGE_TOO_LARGE", "消息卡片超过飞书大小限制")
        return content
