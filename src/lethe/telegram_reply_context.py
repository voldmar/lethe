from __future__ import annotations

from typing import Any

from lethe.telegram_message_map import try_find_sent_message


def _reply_from_summary(reply_from: Any) -> dict[str, Any]:
    if not reply_from:
        return {}

    summary: dict[str, Any] = {}
    user_id = getattr(reply_from, "id", None)
    if user_id is not None:
        summary["id"] = user_id
    is_bot = getattr(reply_from, "is_bot", None)
    if is_bot is not None:
        summary["is_bot"] = is_bot
    username = getattr(reply_from, "username", None)
    if username:
        summary["username"] = username
    first_name = getattr(reply_from, "first_name", None)
    if first_name:
        summary["first_name"] = first_name
    last_name = getattr(reply_from, "last_name", None)
    if last_name:
        summary["last_name"] = last_name
    return summary


def _infer_content_type(reply_to_message: Any) -> str:
    explicit = getattr(reply_to_message, "content_type", None)
    if explicit:
        return str(explicit)

    if getattr(reply_to_message, "text", None):
        return "text"
    if getattr(reply_to_message, "photo", None):
        return "photo"
    if getattr(reply_to_message, "document", None):
        return "document"
    if getattr(reply_to_message, "voice", None):
        return "voice"
    if getattr(reply_to_message, "audio", None):
        return "audio"
    if getattr(reply_to_message, "video", None):
        return "video"
    if getattr(reply_to_message, "sticker", None):
        return "sticker"
    if getattr(reply_to_message, "caption", None):
        return "caption"
    return "unknown"


def extract_reply_context(reply_to_message: Any) -> dict[str, Any]:
    if not reply_to_message:
        return {}

    context: dict[str, Any] = {}
    reply_message_id = getattr(reply_to_message, "message_id", None)
    if reply_message_id is not None:
        context["reply_to_message_id"] = reply_message_id
        context["reply_message_id"] = reply_message_id

    reply_chat = getattr(reply_to_message, "chat", None)
    reply_chat_id = getattr(reply_chat, "id", None)
    if reply_chat_id is not None:
        context["reply_to_chat_id"] = reply_chat_id

    if reply_message_id is not None and reply_chat_id is not None:
        mapped = try_find_sent_message(reply_chat_id, reply_message_id)
        if mapped:
            context["reply_to_mapped_message"] = mapped

    reply_date = getattr(reply_to_message, "date", None)
    if reply_date is not None:
        if hasattr(reply_date, "isoformat"):
            context["reply_to_date"] = reply_date.isoformat()
        else:
            context["reply_to_date"] = str(reply_date)

    reply_from = getattr(reply_to_message, "from_user", None)
    reply_from_summary = _reply_from_summary(reply_from)
    if reply_from_summary:
        context["reply_to_from"] = reply_from_summary

    text = getattr(reply_to_message, "text", None)
    caption = getattr(reply_to_message, "caption", None)
    if text:
        context["reply_to_text"] = text
    if caption:
        context["reply_to_caption"] = caption

    context["reply_to_content_type"] = _infer_content_type(reply_to_message)
    return context


def _format_value(value: Any) -> str:
    if isinstance(value, str):
        return value.replace("\n", "\\n")
    return str(value)


def _render_reply_context(metadata: dict[str, Any]) -> str:
    reply_to_message_id = metadata.get("reply_to_message_id")
    lines = ["[Telegram reply context]"]
    current_message_id = metadata.get("message_id")
    if current_message_id:
        lines.append(f"- current_message_id: {_format_value(current_message_id)}")
    lines.append(f"- reply_to_message_id: {_format_value(reply_to_message_id)}")

    reply_to_chat_id = metadata.get("reply_to_chat_id")
    if reply_to_chat_id is not None:
        lines.append(f"- reply_to_chat_id: {_format_value(reply_to_chat_id)}")

    reply_to_content_type = metadata.get("reply_to_content_type")
    if reply_to_content_type:
        lines.append(f"- reply_to_content_type: {_format_value(reply_to_content_type)}")

    reply_to_from = metadata.get("reply_to_from") or {}
    if reply_to_from:
        actor_bits = []
        username = reply_to_from.get("username")
        if username:
            actor_bits.append(f"@{username}")
        name_bits = [reply_to_from.get("first_name"), reply_to_from.get("last_name")]
        actor_name = " ".join(part for part in name_bits if part)
        if actor_name:
            actor_bits.append(actor_name)
        actor_id = reply_to_from.get("id")
        if actor_id is not None:
            actor_bits.append(f"id={actor_id}")
        if actor_bits:
            lines.append(f"- reply_to_from: {', '.join(actor_bits)}")

    reply_to_text = metadata.get("reply_to_text")
    if reply_to_text:
        lines.append(f"- reply_to_text: {_format_value(reply_to_text)}")

    reply_to_caption = metadata.get("reply_to_caption")
    if reply_to_caption:
        lines.append(f"- reply_to_caption: {_format_value(reply_to_caption)}")

    mapped = metadata.get("reply_to_mapped_message") or {}
    if mapped:
        lines.append("[Telegram mapped reply target]")
        for key in (
            "tool",
            "type",
            "send_type",
            "chat_id",
            "message_id",
            "reply_to_message_id",
            "created_at",
        ):
            value = mapped.get(key)
            if value is not None:
                lines.append(f"- {key}: {_format_value(value)}")
        provenance = mapped.get("provenance") or {}
        for key in ("actor_id", "actor_name", "session_id", "turn_id"):
            value = provenance.get(key)
            if value is not None:
                lines.append(f"- provenance.{key}: {_format_value(value)}")
        artifact = mapped.get("artifact") or {}
        for key in ("filename", "path"):
            value = artifact.get(key)
            if value:
                lines.append(f"- artifact.{key}: {_format_value(value)}")
        lines.append("[/Telegram mapped reply target]")

    lines.append("[/Telegram reply context]")
    return "\n".join(lines)


def wrap_message_with_reply_context(message: Any, metadata: dict[str, Any]) -> Any:
    reply_to_message_id = metadata.get("reply_to_message_id")
    if not reply_to_message_id:
        return message

    rendered_context = _render_reply_context(metadata)
    if isinstance(message, list):
        return [
            {"type": "text", "text": rendered_context + "\n\nUser instruction:"},
            *message,
        ]

    return rendered_context + f"\n\nUser instruction:\n{message}"
