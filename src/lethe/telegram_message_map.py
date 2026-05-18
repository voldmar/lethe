"""Append-only durable mapping for outbound Telegram messages.

The map is intentionally small and boring: JSONL under the Lethe workspace.
It lets later inbound reply handling resolve a Telegram message id back to the
local tool/artifact context that created it.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lethe.paths import workspace_dir

logger = logging.getLogger(__name__)


def default_message_map_path() -> Path:
    """Return the default append-only Telegram message map path."""
    return workspace_dir() / "state" / "telegram_messages.jsonl"


def append_sent_message(record: dict[str, Any], path: Path | None = None) -> Path:
    """Append one sent Telegram message mapping record as JSONL."""
    target = path or default_message_map_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = dict(record)
    payload.setdefault("created_at", datetime.now(UTC).isoformat())

    with target.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        f.write("\n")

    return target


def try_append_sent_message(record: dict[str, Any], path: Path | None = None) -> Path | None:
    """Best-effort append; never let mapping failures break Telegram sends."""
    try:
        return append_sent_message(record, path=path)
    except Exception:
        logger.warning("Failed to append Telegram sent-message mapping", exc_info=True)
        return None


def find_sent_message(chat_id: int, message_id: int, path: Path | None = None) -> dict[str, Any] | None:
    """Find the newest mapping record for a Telegram chat/message id."""
    target = path or default_message_map_path()
    if not target.exists():
        return None

    found: dict[str, Any] | None = None
    with target.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed Telegram message map line")
                continue
            if record.get("chat_id") == chat_id and record.get("message_id") == message_id:
                found = record
    return found


def try_find_sent_message(chat_id: int, message_id: int, path: Path | None = None) -> dict[str, Any] | None:
    """Best-effort lookup; never let mapping failures break inbound context."""
    try:
        return find_sent_message(chat_id, message_id, path=path)
    except Exception:
        logger.warning("Failed to lookup Telegram sent-message mapping", exc_info=True)
        return None
