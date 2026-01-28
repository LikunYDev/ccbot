"""Convert standard Markdown to Telegram MarkdownV2 format."""

import telegramify_markdown


def convert_markdown(text: str) -> str:
    """Convert standard Markdown to Telegram MarkdownV2 format."""
    return telegramify_markdown.markdownify(
        text,
        normalize_whitespace=False,
    )
