# CLAUDE.md

## 项目开发原则

### 消息内容不做截断

历史消息（tool_use 摘要、tool_result 文本、用户/助手消息）一律保留完整内容，不在解析层做任何字符数截断。长文本的处理统一交给发送层：通过 `split_message` 按 Telegram 4096 字符限制分页，配合 inline keyboard 翻页浏览。

### 历史分页默认显示最新内容

`/history` 默认显示最后一页（最新消息），用户通过 "◀ Older" 按钮向前翻阅更早的内容。

### 遵循 Telegram Bot 最佳实践

Bot 的交互设计应参考 Telegram Bot 平台的最佳实践：优先使用 inline keyboard 而非 reply keyboard；翻页/操作通过 `edit_message_text` 原地更新而非发送新消息；callback data 保持精简以适应 64 字节限制；合理使用 `answer_callback_query` 提供即时反馈。

### 代码质量检查

每次修改代码后运行 `pyright src/ccmux/` 检查类型错误，确保 0 errors 后再提交。

### 消息格式化统一使用 MarkdownV2

所有发送到 Telegram 的消息统一使用 `parse_mode="MarkdownV2"`。通过 `telegramify-markdown` 库将标准 Markdown 转换为 Telegram MarkdownV2 格式。所有发送/编辑消息的调用都必须经过 `_safe_reply`/`_safe_edit`/`_safe_send` helper 函数，这些函数会自动完成 MarkdownV2 转换并在解析失败时 fallback 到纯文本。不要直接调用 `reply_text`/`edit_message_text`/`send_message`。

### 以 Window 为核心单位

所有逻辑（session 列表、消息发送、历史查看、通知等）均以 tmux window 为核心单位进行处理，而非以项目目录（cwd）为单位。同一个目录可以有多个 window（名称自动加后缀如 `cc:project-2`），每个 window 独立关联自己的 Claude session。

### Hook-based session tracking

窗口与 Claude Code session 的关联通过 Claude Code 的 `SessionStart`/`SessionEnd` hooks 自动维护。Hook 调用 `ccmux hook` 子命令，将 window↔session 映射写入 `~/.ccmux/session_map.json`。Monitor 循环每次 poll 时读取该文件，自动更新窗口的 session 关联（检测到 session 变更时重置 `last_msg_id`）。

用户需在 `~/.claude/settings.json` 中配置：

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [{ "type": "command", "command": "ccmux hook", "timeout": 5 }]
      }
    ]
  }
}
```
