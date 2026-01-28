"""JSONL transcript parser for Claude Code session files.

Parses Claude Code session JSONL files and extracts message content.
Format reference: https://github.com/desis123/claude-code-viewer
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class ParsedMessage:
    """Parsed message from a transcript."""

    message_type: str  # "user", "assistant", "tool_use", "tool_result", etc.
    role: str | None  # "user" or "assistant"
    text: str  # Extracted text content
    tool_name: str | None = None  # For tool_use messages
    raw: dict | None = None  # Original data


@dataclass
class ParsedEntry:
    """A single parsed message entry ready for display."""

    role: str  # "user" | "assistant"
    text: str  # Already formatted text
    content_type: str  # "text" | "thinking" | "tool_use" | "tool_result" | "local_command"


class TranscriptParser:
    """Parser for Claude Code JSONL session files."""

    @staticmethod
    def parse_line(line: str) -> dict | None:
        """Parse a single JSONL line.

        Args:
            line: A single line from the JSONL file

        Returns:
            Parsed dict or None if line is empty/invalid
        """
        line = line.strip()
        if not line:
            return None

        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def get_message_type(data: dict) -> str | None:
        """Get the message type from parsed data.

        Returns:
            Message type: "user", "assistant", "file-history-snapshot", etc.
        """
        return data.get("type")

    @staticmethod
    def is_assistant_message(data: dict) -> bool:
        """Check if this is an assistant message."""
        return data.get("type") == "assistant"

    @staticmethod
    def is_user_message(data: dict) -> bool:
        """Check if this is a user message."""
        return data.get("type") == "user"

    @staticmethod
    def parse_structured_content(content_list: list[Any]) -> str:
        """Parse structured content array into a string representation.

        Handles text, tool_use, tool_result, and thinking blocks.

        Args:
            content_list: List of content blocks

        Returns:
            Combined string representation
        """
        if not isinstance(content_list, list):
            if isinstance(content_list, str):
                return content_list
            return ""

        parts = []
        for item in content_list:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                item_type = item.get("type", "")

                if item_type == "text":
                    text = item.get("text", "")
                    if text:
                        parts.append(text)

                elif item_type == "thinking":
                    # Skip thinking blocks by default
                    pass

                elif item_type == "tool_use":
                    tool_name = item.get("name", "unknown")
                    parts.append(f"[Tool: {tool_name}]")

                elif item_type == "tool_result":
                    # Skip tool results by default
                    pass

        return "\n".join(parts)

    @staticmethod
    def extract_text_only(content_list: list[Any]) -> str:
        """Extract only text content from structured content.

        This is used for Telegram notifications where we only want
        the actual text response, not tool calls or thinking.

        Args:
            content_list: List of content blocks

        Returns:
            Combined text content only
        """
        if not isinstance(content_list, list):
            if isinstance(content_list, str):
                return content_list
            return ""

        texts = []
        for item in content_list:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    text = item.get("text", "")
                    if text:
                        texts.append(text)

        return "\n".join(texts)

    _RE_COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>")
    _RE_LOCAL_STDOUT = re.compile(r"<local-command-stdout>(.*?)</local-command-stdout>", re.DOTALL)
    _RE_SYSTEM_TAGS = re.compile(r"<(bash-input|bash-stdout|bash-stderr|local-command-caveat|system-reminder)")

    @staticmethod
    def format_tool_use_summary(name: str, input_data: dict | Any) -> str:
        """Format a tool_use block into a brief summary line.

        Args:
            name: Tool name (e.g. "Read", "Write", "Bash")
            input_data: The tool input dict

        Returns:
            Formatted string like "🔧 Read: /path/to/file.py"
        """
        if not isinstance(input_data, dict):
            return f"🔧 {name}"

        # Pick a meaningful short summary based on tool name
        summary = ""
        if name in ("Read", "Glob"):
            summary = input_data.get("file_path") or input_data.get("pattern", "")
        elif name == "Write":
            summary = input_data.get("file_path", "")
        elif name in ("Edit", "NotebookEdit"):
            summary = input_data.get("file_path") or input_data.get("notebook_path", "")
        elif name == "Bash":
            summary = input_data.get("command", "")
        elif name == "Grep":
            summary = input_data.get("pattern", "")
        elif name == "Task":
            summary = input_data.get("description", "")
        elif name == "WebFetch":
            summary = input_data.get("url", "")
        elif name == "WebSearch":
            summary = input_data.get("query", "")
        else:
            # Generic: show first string value
            for v in input_data.values():
                if isinstance(v, str) and v:
                    summary = v
                    break

        if summary:
            return f"🔧 **{name}** `{summary}`"
        return f"🔧 **{name}**"

    @staticmethod
    def extract_tool_result_text(content: list | Any) -> str:
        """Extract text from a tool_result content block."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    t = item.get("text", "")
                    if t:
                        parts.append(t)
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
        return ""

    @classmethod
    def parse_message(cls, data: dict) -> ParsedMessage | None:
        """Parse a message entry from the JSONL data.

        Args:
            data: Parsed JSON dict from a JSONL line

        Returns:
            ParsedMessage or None if not a parseable message
        """
        msg_type = cls.get_message_type(data)

        if msg_type not in ("user", "assistant"):
            return None

        message = data.get("message", {})
        role = message.get("role")
        content = message.get("content", "")

        if isinstance(content, list):
            text = cls.extract_text_only(content)
        else:
            text = str(content) if content else ""

        # Detect local command responses in user messages.
        # These are rendered as bot replies: "❯ /cmd\n  ⎿  output"
        if msg_type == "user" and text:
            stdout_match = cls._RE_LOCAL_STDOUT.search(text)
            if stdout_match:
                stdout = stdout_match.group(1).strip()
                cmd_match = cls._RE_COMMAND_NAME.search(text)
                cmd = cmd_match.group(1) if cmd_match else None
                return ParsedMessage(
                    message_type="local_command",
                    role="assistant",
                    text=stdout,
                    tool_name=cmd,  # reuse field for command name
                    raw=data,
                )
            # Pure command invocation (no stdout) — carry command name
            cmd_match = cls._RE_COMMAND_NAME.search(text)
            if cmd_match:
                return ParsedMessage(
                    message_type="local_command_invoke",
                    role=None,
                    text="",
                    tool_name=cmd_match.group(1),
                    raw=data,
                )

        return ParsedMessage(
            message_type=msg_type,
            role=role,
            text=text,
            raw=data,
        )

    @classmethod
    def extract_assistant_text(cls, data: dict) -> str | None:
        """Extract text content from an assistant message.

        This is a convenience method for getting just the text
        from an assistant message, suitable for notifications.
        Filters out "(no content)" placeholder text.

        Args:
            data: Parsed JSON dict from a JSONL line

        Returns:
            Text content or None if not an assistant message
        """
        if not cls.is_assistant_message(data):
            return None

        message = data.get("message", {})
        content = message.get("content", [])

        text = cls.extract_text_only(content)
        # Filter out "(no content)" placeholder
        if text and text.strip() == "(no content)":
            return None
        return text

    @classmethod
    def extract_assistant_content(cls, data: dict) -> tuple[str, str] | None:
        """Extract content and its type from an assistant message.

        Returns:
            (text, content_type) where content_type is "text" or "thinking",
            or None if not an assistant message or no content.
        """
        if not cls.is_assistant_message(data):
            return None

        message = data.get("message", {})
        content = message.get("content", [])
        if not isinstance(content, list):
            return None

        # Check what types of content blocks are present
        has_thinking = False
        has_text = False
        thinking_text = ""
        text_text = ""

        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "thinking":
                t = item.get("thinking", "")
                if t:
                    has_thinking = True
                    thinking_text = t
            elif item.get("type") == "text":
                t = item.get("text", "")
                if t and t.strip() != "(no content)":
                    has_text = True
                    text_text = t

        if has_text:
            return (text_text, "text")
        if has_thinking:
            return (thinking_text, "thinking")
        return None

    @staticmethod
    def get_session_id(data: dict) -> str | None:
        """Extract session ID from message data."""
        return data.get("sessionId")

    @staticmethod
    def get_cwd(data: dict) -> str | None:
        """Extract working directory (cwd) from message data."""
        return data.get("cwd")

    @staticmethod
    def get_timestamp(data: dict) -> str | None:
        """Extract timestamp from message data."""
        return data.get("timestamp")

    @staticmethod
    def get_uuid(data: dict) -> str | None:
        """Extract message UUID from message data."""
        return data.get("uuid")

    @staticmethod
    def _format_expandable_quote(text: str) -> str:
        """Format text as a Telegram expandable blockquote.

        Uses the MarkdownV2 expandable blockquote syntax:
        each line prefixed with '>' and the last line ending with '||'.
        """
        lines = text.split("\n")
        quoted = "\n".join(f">{line}" for line in lines)
        quoted += "||"
        return quoted

    @classmethod
    def parse_entries(cls, entries: list[dict]) -> list[ParsedEntry]:
        """Parse a list of JSONL entries into a flat list of display-ready messages.

        This is the shared core logic used by both get_recent_messages (history)
        and check_for_updates (monitor).

        Args:
            entries: List of parsed JSONL dicts (already filtered through parse_line)

        Returns:
            List of ParsedEntry with formatted text
        """
        result: list[ParsedEntry] = []
        last_cmd_name: str | None = None
        # Pending tool_use blocks from the last assistant message, keyed by id
        pending_tools: dict[str, str] = {}  # tool_use_id -> formatted summary

        for data in entries:
            msg_type = cls.get_message_type(data)
            if msg_type not in ("user", "assistant"):
                continue

            message = data.get("message", {})
            content = message.get("content", "")
            if not isinstance(content, list):
                content = [{"type": "text", "text": str(content)}] if content else []

            parsed = cls.parse_message(data)

            # Handle local command messages first
            if parsed:
                if parsed.message_type == "local_command_invoke":
                    last_cmd_name = parsed.tool_name
                    continue
                if parsed.message_type == "local_command":
                    cmd = parsed.tool_name or last_cmd_name or ""
                    text = parsed.text
                    if cmd:
                        if "\n" in text:
                            formatted = f"❯ `{cmd}`\n```\n{text}\n```"
                        else:
                            formatted = f"❯ `{cmd}`\n`{text}`"
                    else:
                        if "\n" in text:
                            formatted = f"```\n{text}\n```"
                        else:
                            formatted = f"`{text}`"
                    result.append(ParsedEntry(
                        role="assistant",
                        text=formatted,
                        content_type="local_command",
                    ))
                    last_cmd_name = None
                    continue
            last_cmd_name = None

            if msg_type == "assistant":
                # Flush any pending tools that didn't get results
                for tool_summary in pending_tools.values():
                    result.append(ParsedEntry(
                        role="assistant", text=tool_summary, content_type="tool_use",
                    ))
                pending_tools = {}

                # Process content blocks
                has_text = False
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")

                    if btype == "text":
                        t = block.get("text", "").strip()
                        if t and t != "(no content)":
                            result.append(ParsedEntry(
                                role="assistant", text=t, content_type="text",
                            ))
                            has_text = True

                    elif btype == "tool_use":
                        tool_id = block.get("id", "")
                        name = block.get("name", "unknown")
                        inp = block.get("input", {})
                        summary = cls.format_tool_use_summary(name, inp)
                        if tool_id:
                            pending_tools[tool_id] = summary
                        else:
                            result.append(ParsedEntry(
                                role="assistant", text=summary, content_type="tool_use",
                            ))

                    elif btype == "thinking":
                        thinking_text = block.get("thinking", "")
                        if thinking_text:
                            quoted = cls._format_expandable_quote(thinking_text)
                            result.append(ParsedEntry(
                                role="assistant", text=f"💭\n{quoted}", content_type="thinking",
                            ))
                        elif not has_text:
                            result.append(ParsedEntry(
                                role="assistant", text="💭 (thinking)", content_type="thinking",
                            ))

            elif msg_type == "user":
                # Check for tool_result blocks and merge with pending tools
                user_text_parts: list[str] = []

                for block in content:
                    if not isinstance(block, dict):
                        if isinstance(block, str) and block.strip():
                            user_text_parts.append(block.strip())
                        continue
                    btype = block.get("type", "")

                    if btype == "tool_result":
                        tool_use_id = block.get("tool_use_id", "")
                        result_content = block.get("content", "")
                        result_text = cls.extract_tool_result_text(result_content)
                        tool_summary = pending_tools.pop(tool_use_id, None)
                        if tool_summary:
                            entry_text = tool_summary
                            if result_text:
                                entry_text += "\n" + cls._format_expandable_quote(result_text)
                            result.append(ParsedEntry(
                                role="assistant", text=entry_text, content_type="tool_result",
                            ))
                        elif result_text:
                            result.append(ParsedEntry(
                                role="assistant",
                                text=cls._format_expandable_quote(result_text),
                                content_type="tool_result",
                            ))

                    elif btype == "text":
                        t = block.get("text", "").strip()
                        if t and not cls._RE_SYSTEM_TAGS.search(t):
                            user_text_parts.append(t)

                # Flush remaining pending tools
                for tool_summary in pending_tools.values():
                    result.append(ParsedEntry(
                        role="assistant", text=tool_summary, content_type="tool_use",
                    ))
                pending_tools = {}

                # Add user text if present (skip if message was only tool_results)
                if user_text_parts:
                    combined = "\n".join(user_text_parts)
                    # Skip if it looks like local command XML
                    if not cls._RE_LOCAL_STDOUT.search(combined) and \
                       not cls._RE_COMMAND_NAME.search(combined):
                        result.append(ParsedEntry(
                            role="user", text=combined, content_type="text",
                        ))

        # Flush any remaining pending tools at end
        for tool_summary in pending_tools.values():
            result.append(ParsedEntry(
                role="assistant", text=tool_summary, content_type="tool_use",
            ))

        # Strip whitespace
        for entry in result:
            entry.text = entry.text.strip()

        return result
