import asyncio

import pytest

from lethe.proxy_bot import ProxyBot


@pytest.mark.asyncio
async def test_send_message_includes_reply_metadata():
    queue: asyncio.Queue = asyncio.Queue()
    bot = ProxyBot(queue)

    await bot.send_message(
        chat_id=99,
        text="hello",
        parse_mode="Markdown",
        reply_to_message_id=77,
        allow_sending_without_reply=True,
    )

    event = queue.get_nowait()
    assert event["event"] == "text"
    assert event["data"]["reply_to_message_id"] == 77
    assert event["data"]["allow_sending_without_reply"] is True


@pytest.mark.asyncio
async def test_send_document_includes_reply_metadata():
    queue: asyncio.Queue = asyncio.Queue()
    bot = ProxyBot(queue)

    await bot.send_document(
        chat_id=99,
        document="/tmp/report.pdf",
        caption="report",
        reply_to_message_id=66,
        allow_sending_without_reply=False,
    )

    event = queue.get_nowait()
    assert event["event"] == "file"
    assert event["data"]["reply_to_message_id"] == 66
    assert event["data"]["allow_sending_without_reply"] is False
