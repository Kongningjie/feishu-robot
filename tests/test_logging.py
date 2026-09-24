import logging

from app.core.logging import JsonFormatter


def test_structured_log_redacts_email_and_identifiers() -> None:
    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "user zhangsan@example.com open ou_abcdefghijk token shtcnabcdefgh "
        "https://company.feishu.cn/sheets/raw-token?sheet=raw-sheet",
        (),
        None,
    )
    rendered = JsonFormatter().format(record)
    assert "zhangsan@example.com" not in rendered
    assert "ou_abcdefghijk" not in rendered
    assert "shtcnabcdefgh" not in rendered
    assert "raw-token" not in rendered
    assert "raw-sheet" not in rendered
