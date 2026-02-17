# iMessage MCP Server

An MCP server that gives Claude rich access to your iMessage history — search, browse conversations, read messages, and send texts. Works with **Claude Code** and **Claude Desktop** on macOS.

Built with [FastMCP](https://github.com/jlowin/fastmcp). Single file, no project setup, runs directly with `uv`.

## What you get

| Tool | Description |
|------|-------------|
| `search_messages` | Full-text search across all conversations with surrounding context |
| `list_conversations` | Browse conversations sorted by recent or most active |
| `get_messages` | Read messages from a specific chat with pagination |
| `get_unread_messages` | Unread inbox grouped by conversation |
| `send_message` | Send an iMessage via AppleScript |
| `search_contacts` | Search macOS Contacts by name, phone, or email |
| `get_conversation_context` | Get a window of messages around a point in time |

Search is powered by a **FTS5 full-text index** with porter stemming — initial build takes ~5 seconds for 750k messages, then incremental updates are instant. The index is stored separately and never touches Apple's database.

## Prerequisites

- **macOS** (uses iMessage's `chat.db` and AppleScript)
- **Full Disk Access** for your terminal app (System Settings → Privacy & Security → Full Disk Access)
- [**uv**](https://docs.astral.sh/uv/) — `brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`

## Setup

### Claude Code

Add to `~/.claude/mcp.json` (create if it doesn't exist):

```json
{
  "mcpServers": {
    "imessage": {
      "command": "uv",
      "args": ["run", "--with", "fastmcp", "fastmcp", "run",
               "/ABSOLUTE/PATH/TO/imessage-mcp/server.py"]
    }
  }
}
```

Then restart Claude Code.

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "imessage": {
      "command": "uv",
      "args": ["run", "--with", "fastmcp", "fastmcp", "run",
               "/ABSOLUTE/PATH/TO/imessage-mcp/server.py"]
    }
  }
}
```

Then restart Claude Desktop.

> Replace `/ABSOLUTE/PATH/TO/imessage-mcp/server.py` with the actual path where you cloned this repo.

## How it works

- **Read-only** access to `~/Library/Messages/chat.db` — Apple's iMessage database is never modified
- A separate FTS5 search index is created at `data/index.db` within the project directory
- Sending messages uses AppleScript (`osascript`) — macOS will prompt for Automation permission on first use
- Contact search also uses AppleScript via Contacts.app
- Reactions/tapbacks are filtered out of the index (no value in searching "Loved an image")

## Example queries

Once set up, you can ask Claude things like:

- "What restaurants have people recommended to me?"
- "Search my messages for 'flight confirmation'"
- "Show me my conversation with [name] from last week"
- "What are my unread messages?"
- "Send [contact] a message saying I'll be 10 minutes late"

## Privacy

This server runs **locally on your machine**. Your messages are never sent anywhere except to Claude's API as part of your conversation (same as pasting text into any Claude chat). The FTS5 index is stored locally in the `data/` directory.

## License

MIT
