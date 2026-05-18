import json

import pytest

from lethe.tools.telegram_tools import (
    clear_telegram_context,
    set_last_message_id,
    set_telegram_context,
    telegram_send_file_async,
    telegram_send_message_async,
)


class FakeMessage:
    def __init__(self, message_id: int):
        self.message_id = message_id


class FakeBot:
    def __init__(self):
        self.calls = []

    async def send_message(self, **kwargs):
        self.calls.append(("message", kwargs))
        return FakeMessage(777)

    async def send_photo(self, **kwargs):
        self.calls.append(("photo", kwargs))
        return FakeMessage(778)

    async def send_document(self, **kwargs):
        self.calls.append(("document", kwargs))
        return FakeMessage(779)


@pytest.fixture(autouse=True)
def telegram_context_cleanup():
    clear_telegram_context()
    yield
    clear_telegram_context()


@pytest.mark.asyncio
async def test_message_result_includes_message_chat_send_and_reply_metadata():
    bot = FakeBot()
    set_telegram_context(bot, 118958022)
    set_last_message_id(3338)

    result = json.loads(await telegram_send_message_async("hello"))

    assert result == {
        "success": True,
        "type": "message",
        "send_type": "message",
        "chat_id": 118958022,
        "message_id": 777,
        "reply_to_message_id": 3338,
        "allow_sending_without_reply": True,
    }
    assert bot.calls[0][1]["reply_to_message_id"] == 3338


@pytest.mark.asyncio
async def test_message_result_records_disabled_reply_as_null_metadata():
    bot = FakeBot()
    set_telegram_context(bot, 118958022)
    set_last_message_id(3338)

    result = json.loads(await telegram_send_message_async("hello", reply_to_message_id=-1))

    assert result["message_id"] == 777
    assert result["reply_to_message_id"] is None
    assert result["allow_sending_without_reply"] is None
    assert "reply_to_message_id" not in bot.calls[0][1]


@pytest.mark.asyncio
async def test_file_result_includes_path_filename_send_and_reply_metadata(tmp_path):
    file_path = tmp_path / "artifact.txt"
    file_path.write_text("artifact")
    bot = FakeBot()
    set_telegram_context(bot, 118958022)
    set_last_message_id(3338)

    result = json.loads(await telegram_send_file_async(str(file_path), caption="artifact", as_document=True))

    assert result == {
        "success": True,
        "type": "document",
        "send_type": "document",
        "chat_id": 118958022,
        "message_id": 779,
        "reply_to_message_id": 3338,
        "allow_sending_without_reply": True,
        "filename": "artifact.txt",
        "path": str(file_path),
    }
