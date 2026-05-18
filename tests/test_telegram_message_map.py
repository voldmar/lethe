import json

from lethe.telegram_message_map import append_sent_message, try_append_sent_message


def test_append_sent_message_writes_jsonl_with_created_at(tmp_path):
    path = tmp_path / "state" / "telegram_messages.jsonl"

    written = append_sent_message({"chat_id": 1, "message_id": 2, "tool": "telegram_send_message"}, path=path)

    assert written == path
    rows = path.read_text().splitlines()
    assert len(rows) == 1
    data = json.loads(rows[0])
    assert data["chat_id"] == 1
    assert data["message_id"] == 2
    assert data["tool"] == "telegram_send_message"
    assert "created_at" in data


def test_try_append_sent_message_is_best_effort(tmp_path):
    # Directory path cannot be opened as an appendable file.
    assert try_append_sent_message({"chat_id": 1, "message_id": 2}, path=tmp_path) is None
from lethe.telegram_message_map import find_sent_message


def test_find_sent_message_returns_newest_matching_record(tmp_path):
    path = tmp_path / "telegram_messages.jsonl"
    append_sent_message({"chat_id": 1, "message_id": 2, "tool": "old"}, path=path)
    append_sent_message({"chat_id": 1, "message_id": 3, "tool": "other"}, path=path)
    append_sent_message({"chat_id": 1, "message_id": 2, "tool": "new"}, path=path)

    assert find_sent_message(1, 2, path=path)["tool"] == "new"
    assert find_sent_message(1, 404, path=path) is None
