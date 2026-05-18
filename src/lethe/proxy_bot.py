"""ProxyBot — duck-type substitute for aiogram.Bot that enqueues SSE events.

Used in API mode so telegram tools work without a real Telegram connection.
Events are drained by the HTTP API and streamed to the gateway via SSE.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class MockMessage:
    """Minimal stand-in for aiogram.types.Message."""
    message_id: int


class ProxyBot:
    """Duck-type aiogram.Bot that captures Telegram API calls as SSE events."""

    def __init__(self, event_queue: asyncio.Queue):
        self._queue = event_queue
        self._message_counter = 0

    def _next_message_id(self) -> int:
        self._message_counter += 1
        return self._message_counter

    async def _put(self, event: dict):
        await self._queue.put(event)

    def _reply_fields(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        reply_to_message_id = kwargs.get("reply_to_message_id")
        if reply_to_message_id:
            fields["reply_to_message_id"] = reply_to_message_id
            if "allow_sending_without_reply" in kwargs:
                fields["allow_sending_without_reply"] = kwargs.get("allow_sending_without_reply")
        return fields

    # --- aiogram.Bot interface used by telegram_tools.py ---

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: Optional[str] = None,
        **kwargs,
    ) -> MockMessage:
        mid = self._next_message_id()
        reply_fields = self._reply_fields(kwargs)
        await self._put({
            "event": "text",
            "data": {"content": text, "parse_mode": parse_mode, "message_id": mid, **reply_fields},
        })
        return MockMessage(message_id=mid)

    async def send_photo(
        self,
        chat_id: int,
        photo: Any,
        caption: Optional[str] = None,
        **kwargs,
    ) -> MockMessage:
        mid = self._next_message_id()
        reply_fields = self._reply_fields(kwargs)
        await self._put({
            "event": "file",
            "data": {"type": "photo", "path": _resolve_path(photo), "caption": caption or "", "message_id": mid, **reply_fields},
        })
        return MockMessage(message_id=mid)

    async def send_animation(
        self,
        chat_id: int,
        animation: Any,
        caption: Optional[str] = None,
        **kwargs,
    ) -> MockMessage:
        mid = self._next_message_id()
        reply_fields = self._reply_fields(kwargs)
        await self._put({
            "event": "file",
            "data": {"type": "animation", "path": _resolve_path(animation), "caption": caption or "", "message_id": mid, **reply_fields},
        })
        return MockMessage(message_id=mid)

    async def send_video(
        self,
        chat_id: int,
        video: Any,
        caption: Optional[str] = None,
        **kwargs,
    ) -> MockMessage:
        mid = self._next_message_id()
        reply_fields = self._reply_fields(kwargs)
        await self._put({
            "event": "file",
            "data": {"type": "video", "path": _resolve_path(video), "caption": caption or "", "message_id": mid, **reply_fields},
        })
        return MockMessage(message_id=mid)

    async def send_voice(
        self,
        chat_id: int,
        voice: Any,
        caption: Optional[str] = None,
        **kwargs,
    ) -> MockMessage:
        mid = self._next_message_id()
        reply_fields = self._reply_fields(kwargs)
        await self._put({
            "event": "file",
            "data": {"type": "voice", "path": _resolve_path(voice), "caption": caption or "", "message_id": mid, **reply_fields},
        })
        return MockMessage(message_id=mid)

    async def send_audio(
        self,
        chat_id: int,
        audio: Any,
        caption: Optional[str] = None,
        **kwargs,
    ) -> MockMessage:
        mid = self._next_message_id()
        reply_fields = self._reply_fields(kwargs)
        await self._put({
            "event": "file",
            "data": {"type": "audio", "path": _resolve_path(audio), "caption": caption or "", "message_id": mid, **reply_fields},
        })
        return MockMessage(message_id=mid)

    async def send_document(
        self,
        chat_id: int,
        document: Any,
        caption: Optional[str] = None,
        **kwargs,
    ) -> MockMessage:
        mid = self._next_message_id()
        reply_fields = self._reply_fields(kwargs)
        await self._put({
            "event": "file",
            "data": {"type": "document", "path": _resolve_path(document), "caption": caption or "", "message_id": mid, **reply_fields},
        })
        return MockMessage(message_id=mid)

    async def send_chat_action(self, chat_id: int, action: Any, **kwargs):
        await self._put({"event": "typing", "data": {}})

    async def set_message_reaction(
        self,
        chat_id: int,
        message_id: int,
        reaction: list,
        **kwargs,
    ):
        emoji = ""
        if reaction:
            emoji = getattr(reaction[0], "emoji", str(reaction[0]))
        await self._put({
            "event": "reaction",
            "data": {"emoji": emoji, "message_id": message_id},
        })


def _resolve_path(file_input: Any) -> str:
    """Extract a filesystem path from an aiogram FSInputFile or string."""
    # FSInputFile has a .path attribute
    if hasattr(file_input, "path"):
        return str(file_input.path)
    # URL string
    if isinstance(file_input, str):
        return file_input
    return str(file_input)
