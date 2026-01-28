"""Synchronous Telegram message sender for stop hook."""

import httpx

from .config import config
from .markdown_v2 import convert_markdown

TELEGRAM_MAX_MESSAGE_LENGTH = 4096


def split_message(text: str, max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    """Split a message into chunks that fit Telegram's length limit.

    Tries to split on newlines when possible to preserve formatting.
    """
    if len(text) <= max_length:
        return [text]

    chunks = []
    current_chunk = ""

    for line in text.split("\n"):
        # If single line exceeds max, split it forcefully
        if len(line) > max_length:
            if current_chunk:
                chunks.append(current_chunk.rstrip("\n"))
                current_chunk = ""
            # Split long line into fixed-size pieces
            for i in range(0, len(line), max_length):
                chunks.append(line[i : i + max_length])
        elif len(current_chunk) + len(line) + 1 > max_length:
            # Current chunk is full, start a new one
            chunks.append(current_chunk.rstrip("\n"))
            current_chunk = line + "\n"
        else:
            current_chunk += line + "\n"

    if current_chunk:
        chunks.append(current_chunk.rstrip("\n"))

    return chunks


def send_telegram_message(chat_id: int, text: str) -> bool:
    """Send a message to a Telegram user.

    Handles message splitting for long messages.
    Returns True if all messages were sent successfully.
    """
    url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"

    chunks = split_message(text, max_length=3000)
    success = True

    with httpx.Client(timeout=30.0) as client:
        for chunk in chunks:
            html_chunk = convert_markdown(chunk)
            try:
                response = client.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "text": html_chunk,
                        "parse_mode": "MarkdownV2",
                    },
                )
                if not response.is_success:
                    # Retry without MarkdownV2 parsing if it fails
                    response = client.post(
                        url,
                        json={
                            "chat_id": chat_id,
                            "text": chunk,
                        },
                    )
                    if not response.is_success:
                        success = False
            except httpx.HTTPError:
                success = False

    return success
