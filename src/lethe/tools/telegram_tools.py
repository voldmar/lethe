"""Telegram tools for sending files/images.

Uses contextvars to access the bot and chat_id from the current task context.
"""

from __future__ import annotations

import json
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Optional

from lethe.reaction_transport import send_message_reaction
from lethe.telegram_turn_guard import queue_pending_reaction
from lethe.telegram_message_map import try_append_sent_message

# Context variables set by worker before tool execution
_current_bot: ContextVar[Any] = ContextVar("current_bot", default=None)
_current_chat_id: ContextVar[Optional[int]] = ContextVar("current_chat_id", default=None)
_telegram_provenance: ContextVar[dict[str, Any]] = ContextVar("telegram_provenance", default={})


def set_telegram_provenance(**provenance: Any):
    """Set optional provenance included in durable Telegram message mappings."""
    _telegram_provenance.set({k: v for k, v in provenance.items() if v is not None})


def clear_telegram_provenance():
    """Clear optional Telegram mapping provenance."""
    _telegram_provenance.set({})


def set_telegram_context(bot: Any, chat_id: int):
    """Set the Telegram context for tool execution.

    Called by worker before processing a task.
    """
    _current_bot.set(bot)
    _current_chat_id.set(chat_id)


def clear_telegram_context():
    """Clear the Telegram context after task completion."""
    _current_bot.set(None)
    _current_chat_id.set(None)
    _target_message_id.set(None)
    clear_telegram_provenance()


def _reply_fields(
    reply_to_message_id: Optional[int] = 0, allow_sending_without_reply: bool = True
) -> dict[str, Any]:
    """Build Telegram reply kwargs.

    Semantics match telegram_react(): 0/None means use the runtime-seeded
    target message when one exists. Use a negative message id to explicitly
    disable reply threading for this send.
    """
    fields: dict[str, Any] = {}
    if reply_to_message_id is not None and reply_to_message_id < 0:
        return fields

    target_message_id = reply_to_message_id or _target_message_id.get()
    if target_message_id:
        fields["reply_to_message_id"] = target_message_id
        fields["allow_sending_without_reply"] = allow_sending_without_reply
    return fields


def _telegram_send_result(
    result: Any,
    chat_id: int,
    send_type: str,
    reply_fields: dict[str, Any],
    *,
    tool: str,
    **extra: Any,
) -> str:
    """Build stable JSON result payload and durably map sent Telegram message."""
    payload: dict[str, Any] = {
        "success": True,
        "type": send_type,
        "send_type": send_type,
        "chat_id": chat_id,
        "message_id": getattr(result, "message_id", None),
        "reply_to_message_id": reply_fields.get("reply_to_message_id"),
        "allow_sending_without_reply": reply_fields.get("allow_sending_without_reply"),
    }
    payload.update(extra)

    if payload["message_id"] is not None:
        record: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": payload["message_id"],
            "type": send_type,
            "send_type": send_type,
            "reply_to_message_id": payload.get("reply_to_message_id"),
            "allow_sending_without_reply": payload.get("allow_sending_without_reply"),
            "tool": tool,
        }
        provenance = _telegram_provenance.get()
        if provenance:
            record["provenance"] = provenance
        if "filename" in payload or "path" in payload:
            record["artifact"] = {
                "filename": payload.get("filename"),
                "path": payload.get("path"),
            }
        try_append_sent_message(record)

    return json.dumps(payload)


def _reply_fields(
    reply_to_message_id: int = 0, allow_sending_without_reply: bool = True
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if reply_to_message_id:
        fields["reply_to_message_id"] = reply_to_message_id
        fields["allow_sending_without_reply"] = allow_sending_without_reply
    return fields


async def telegram_send_message_async(
    text: str,
    parse_mode: str = "",
    reply_to_message_id: int = 0,
    allow_sending_without_reply: bool = True,
) -> str:
    """Send a text message to the current Telegram chat.

    Use this to send multiple separate messages instead of one long response.
    Each call sends immediately as a separate message.

    Args:
        text: Message text to send
        parse_mode: Optional parsing mode - "markdown", "html", or "" for plain text
        reply_to_message_id: Optional Telegram message id to reply to (0 falls back to the runtime-seeded target; negative disables reply threading)
        allow_sending_without_reply: Allow Telegram to send the message even if the reply target is missing

    Returns:
        JSON with success status
    """
    bot = _current_bot.get()
    chat_id = _current_chat_id.get()
    reply_fields = _reply_fields(reply_to_message_id, allow_sending_without_reply)

    if not bot or not chat_id:
        raise RuntimeError(
            "Telegram context not set. This tool can only be used during task processing."
        )

    # Map parse_mode
    pm = None
    if parse_mode.lower() == "markdown":
        pm = "MarkdownV2"
    elif parse_mode.lower() == "html":
        pm = "HTML"

    try:
        result = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=pm,
            **reply_fields,
        )
    except Exception as e:
        if pm and "parse entities" in str(e).lower():
            # Fallback to plain text on markdown parse errors
            result = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=None,
                **reply_fields,
            )
        else:
            raise

    return _telegram_send_result(
        result, chat_id, "message", reply_fields, tool="telegram_send_message"
    )


async def telegram_send_file_async(
    file_path_or_url: str,
    caption: str = "",
    as_document: bool = False,
    reply_to_message_id: int = 0,
    allow_sending_without_reply: bool = True,
) -> str:
    """Send a file or image to the current Telegram chat.

    Args:
        file_path_or_url: Local file path or URL to send
        caption: Optional caption for the file
        as_document: If True, send as document even if it's an image
        reply_to_message_id: Optional Telegram message id to reply to (0 falls back to the runtime-seeded target; negative disables reply threading)
        allow_sending_without_reply: Allow Telegram to send the message even if the reply target is missing

    Returns:
        JSON with success status
    """
    from aiogram.types import FSInputFile

    bot = _current_bot.get()
    chat_id = _current_chat_id.get()
    reply_fields = _reply_fields(reply_to_message_id, allow_sending_without_reply)

    if not bot or not chat_id:
        raise RuntimeError(
            "Telegram context not set. This tool can only be used during task processing."
        )

    # Determine source type
    is_url = file_path_or_url.startswith(("http://", "https://"))

    if is_url:
        # For URLs, pass string directly - Telegram will fetch it
        file_input = file_path_or_url
        filename = file_path_or_url.split("/")[-1].split("?")[0]
    else:
        # Local file - use FSInputFile for multipart upload
        path = Path(file_path_or_url).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        file_input = FSInputFile(path)
        filename = path.name

    # Determine file type by extension
    ext = filename.lower().split(".")[-1] if "." in filename else ""
    is_static_image = ext in ("jpg", "jpeg", "png", "webp", "bmp")
    is_gif = ext == "gif"
    is_video = ext in ("mp4", "avi", "mov", "mkv", "webm")
    is_audio = ext in ("mp3", "wav", "flac", "m4a")
    is_voice = ext == "ogg"

    # Send based on type
    # Note: GIFs should use send_animation to display properly (not send_photo)
    if is_static_image and not as_document:
        result = await bot.send_photo(
            chat_id=chat_id,
            photo=file_input,
            caption=caption or None,
            **reply_fields,
        )
        send_type = "photo"
    elif is_gif and not as_document:
        # GIFs need send_animation to animate properly in Telegram
        result = await bot.send_animation(
            chat_id=chat_id,
            animation=file_input,
            caption=caption or None,
            **reply_fields,
        )
        send_type = "animation"
    elif is_video and not as_document:
        result = await bot.send_video(
            chat_id=chat_id,
            video=file_input,
            caption=caption or None,
            **reply_fields,
        )
        send_type = "video"
    elif is_voice and not as_document:
        # OGG files sent as voice messages
        result = await bot.send_voice(
            chat_id=chat_id,
            voice=file_input,
            caption=caption or None,
            **reply_fields,
        )
        send_type = "voice"
    elif is_audio and not as_document:
        result = await bot.send_audio(
            chat_id=chat_id,
            audio=file_input,
            caption=caption or None,
            **reply_fields,
        )
        send_type = "audio"
    else:
        result = await bot.send_document(
            chat_id=chat_id,
            document=file_input,
            caption=caption or None,
            **reply_fields,
        )
        send_type = "document"

    return _telegram_send_result(
        result,
        chat_id,
        send_type,
        reply_fields,
        tool="telegram_send_file",
        filename=filename,
        path=file_path_or_url,
    )


# Sync version for tool registration (never actually called - async handler is used)
def _is_tool(func):
    """Decorator to mark a function as a tool."""
    func._is_tool = True
    return func


@_is_tool
def telegram_send_message(
    text: str,
    parse_mode: str = "",
    reply_to_message_id: int = 0,
    allow_sending_without_reply: bool = True,
) -> str:
    """Send an EXTRA message to the user during a long task.

    IMPORTANT: Your final text response is sent automatically — do NOT use this
    for your main reply. Only use this to send updates WHILE working on a task
    (e.g., "Starting analysis..." before a long operation).
    Do NOT send the same content multiple times. Do NOT retry if a message sent.

    Args:
        text: Message text to send
        parse_mode: Optional - "markdown", "html", or "" for plain text
        reply_to_message_id: Optional Telegram message id to reply to (0 falls back to the runtime-seeded target; negative disables reply threading)
        allow_sending_without_reply: Allow Telegram to send the message even if the reply target is missing

    Returns:
        JSON with success status and message_id
    """
    raise Exception("Client-side execution required")


@_is_tool
def telegram_send_file(
    file_path_or_url: str,
    caption: str = "",
    as_document: bool = False,
    reply_to_message_id: int = 0,
    allow_sending_without_reply: bool = True,
) -> str:
    """Send a file or image to the current Telegram chat.

    Supports local files and URLs. Automatically detects file type:
    - Images (jpg, png, gif, webp): sent as photos
    - Videos (mp4, mov, etc): sent as videos
    - Audio (mp3, ogg, etc): sent as audio
    - Other files: sent as documents

    Args:
        file_path_or_url: Local file path (e.g., "/tmp/chart.png") or URL (e.g., "https://example.com/image.jpg")
        caption: Optional caption to display with the file
        as_document: If True, send as document even if it's an image/video (preserves original quality)
        reply_to_message_id: Optional Telegram message id to reply to (0 falls back to the runtime-seeded target; negative disables reply threading)
        allow_sending_without_reply: Allow Telegram to send the message even if the reply target is missing

    Returns:
        JSON with success status and message details
    """
    raise Exception("Client-side execution required")


# Context variable for the selected reaction target (seeded by runtime)
_target_message_id: ContextVar[Optional[int]] = ContextVar("target_message_id", default=None)


def set_last_message_id(message_id: int):
    """Seed the selected target message ID for reaction support."""
    _target_message_id.set(message_id)


async def telegram_react_async(emoji: str = "👍", message_id: int = 0) -> str:
    """React to the user's last message with an emoji.

    Args:
        emoji: Emoji to react with (e.g., "👍", "❤️", "😂", "🔥", "👀")
        message_id: Optional message ID to react to. Use 0 to fall back to the
            runtime-seeded target message.

    Returns:
        JSON with success status
    """
    bot = _current_bot.get()
    chat_id = _current_chat_id.get()
    target_message_id = message_id or _target_message_id.get()

    if not bot or not chat_id or not target_message_id:
        raise RuntimeError("Telegram context not set or no message to react to.")

    if queue_pending_reaction(bot, chat_id, target_message_id, emoji):
        return json.dumps(
            {
                "success": True,
                "queued": True,
                "emoji": emoji,
                "message_id": target_message_id,
            }
        )

    await send_message_reaction(bot, chat_id, target_message_id, emoji)

    return json.dumps(
        {
            "success": True,
            "emoji": emoji,
            "message_id": target_message_id,
        }
    )


@_is_tool
def telegram_react(emoji: str = "👍", message_id: int = 0) -> str:
    """React to the user's last message with an emoji.

    Use this sparingly to acknowledge emotionally meaningful moments, not every message.
    Common emojis: 👍 (ok/approve), ❤️ (love), 😂 (funny), 🔥 (impressive),
    👀 (interesting), 🤔 (thinking), ✅ (done), 🎉 (celebrate)

    Args:
        emoji: Emoji to react with
        message_id: Optional message ID to react to. Use 0 to fall back to the
            runtime-seeded target message.

    Returns:
        JSON with success status
    """
    raise Exception("Client-side execution required")
