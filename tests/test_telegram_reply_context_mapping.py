from types import SimpleNamespace

import pytest

from lethe import telegram_reply_context
from lethe.telegram_reply_context import extract_reply_context, wrap_message_with_reply_context


@pytest.fixture(autouse=True)
def fake_mapping_lookup(monkeypatch):
    def lookup(chat_id, message_id):
        if chat_id == 118 and message_id == 777:
            return {
                "chat_id": 118,
                "message_id": 777,
                "tool": "telegram_send_file",
                "type": "document",
                "send_type": "document",
                "reply_to_message_id": 3338,
                "created_at": "2026-05-18T17:00:00+00:00",
                "provenance": {"actor_id": "cortex", "actor_name": "cortex", "session_id": "s1"},
                "artifact": {"filename": "artifact.txt", "path": "/tmp/artifact.txt"},
            }
        return None

    monkeypatch.setattr(telegram_reply_context, "try_find_sent_message", lookup)


def test_extract_reply_metadata_includes_mapped_message_when_lookup_hits():
    reply = SimpleNamespace(
        message_id=777,
        chat=SimpleNamespace(id=118),
        from_user=None,
        text="old artifact",
        content_type="text",
    )
    message = SimpleNamespace(message_id=888, reply_to_message=reply)

    metadata = extract_reply_context(reply)

    assert metadata["reply_to_mapped_message"]["tool"] == "telegram_send_file"
    assert metadata["reply_to_mapped_message"]["artifact"]["path"] == "/tmp/artifact.txt"


def test_format_reply_context_renders_mapped_target_and_provenance():
    metadata = {
        "message_id": 888,
        "reply_to_message_id": 777,
        "reply_to_chat_id": 118,
        "reply_to_mapped_message": {
            "tool": "telegram_send_file",
            "type": "document",
            "send_type": "document",
            "message_id": 777,
            "provenance": {"actor_id": "cortex", "actor_name": "cortex", "session_id": "s1"},
            "artifact": {"filename": "artifact.txt", "path": "/tmp/artifact.txt"},
        },
    }

    rendered = wrap_message_with_reply_context("User instruction", metadata)

    assert "[Telegram mapped reply target]" in rendered
    assert "- tool: telegram_send_file" in rendered
    assert "- artifact.path: /tmp/artifact.txt" in rendered
    assert "- provenance.session_id: s1" in rendered


def test_wrap_reply_context_preserves_multimodal_message_parts():
    metadata = {
        "message_id": 889,
        "reply_to_message_id": 777,
        "reply_to_chat_id": 118,
        "reply_to_content_type": "photo",
    }
    parts = [
        {"type": "text", "text": "caption text"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abc"}},
    ]

    wrapped = wrap_message_with_reply_context(parts, metadata)

    assert isinstance(wrapped, list)
    assert wrapped[0]["type"] == "text"
    assert "[Telegram reply context]" in wrapped[0]["text"]
    assert "User instruction:" in wrapped[0]["text"]
    assert wrapped[1:] == parts
