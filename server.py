"""iMessage MCP Server — search, read, and send iMessages from Claude Code."""

import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")
INDEX_DIR = Path(__file__).parent / "data"
INDEX_DB = INDEX_DIR / "index.db"

# Apple Core Data epoch: 2001-01-01 00:00:00 UTC
APPLE_EPOCH = 978307200  # in seconds since Unix epoch
NS = 1_000_000_000       # nanoseconds per second

mcp = FastMCP("iMessage")

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def chat_conn() -> sqlite3.Connection:
    """Read-only connection to Apple's chat.db."""
    conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def index_conn() -> sqlite3.Connection:
    """Read-write connection to our FTS5 index database."""
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(INDEX_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            rowid INTEGER PRIMARY KEY,
            chat_id INTEGER,
            handle_id TEXT,
            is_from_me INTEGER,
            date_ts INTEGER,
            service TEXT,
            text TEXT
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            text, content='messages', content_rowid='rowid',
            tokenize='porter unicode61'
        );
        CREATE TABLE IF NOT EXISTS index_state (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    conn.commit()

# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def apple_ns_to_unix(ns_val: int) -> int:
    """Convert iMessage nanosecond timestamp to Unix timestamp."""
    return (ns_val // NS) + APPLE_EPOCH


def unix_to_apple_ns(ts: int) -> int:
    """Convert Unix timestamp to iMessage nanosecond timestamp."""
    return (ts - APPLE_EPOCH) * NS


def format_ts(unix_ts: int) -> str:
    """Format Unix timestamp as ISO-like local time string."""
    return datetime.fromtimestamp(unix_ts).strftime("%Y-%m-%d %H:%M")


def parse_date(s: str) -> int | None:
    """Parse a user-supplied date string to Unix timestamp."""
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return int(datetime.strptime(s, fmt).timestamp())
        except ValueError:
            continue
    return None

# ---------------------------------------------------------------------------
# attributedBody decoder
# ---------------------------------------------------------------------------

def decode_attributed_body(blob: bytes) -> str | None:
    """Extract plain text from an NSAttributedString binary BLOB.

    The typical layout has a marker \\x01+ followed by a length byte,
    then the UTF-8 text content.
    """
    if not blob:
        return None
    try:
        # Primary method: find the \x01\x2b marker
        marker = b"\x01\x2b"
        idx = blob.find(marker)
        if idx != -1:
            start = idx + len(marker)
            if start < len(blob):
                length = blob[start]
                text_start = start + 1
                # If length byte is large enough, it might be a multi-byte length
                if length == 0 and text_start + 1 < len(blob):
                    return None
                text = blob[text_start:text_start + length].decode("utf-8", errors="replace")
                if text.strip():
                    return text.strip()

        # Fallback: look for \x01+ patterns with larger length encoding
        for m in re.finditer(rb"\x01(.)", blob):
            pos = m.end()
            length = m.group(1)[0]
            if 5 < length < 250 and pos + length <= len(blob):
                try:
                    candidate = blob[pos:pos + length].decode("utf-8", errors="strict")
                    # Heuristic: must contain some letter characters
                    if any(c.isalpha() for c in candidate) and candidate.strip():
                        return candidate.strip()
                except UnicodeDecodeError:
                    continue

        return None
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Phone normalization
# ---------------------------------------------------------------------------

def normalize_phone(phone: str) -> list[str]:
    """Return a list of phone number variants for matching."""
    cleaned = re.sub(r"[^0-9+]", "", phone)
    variants = set()

    if re.match(r"^\+1\d{10}$", cleaned):
        variants.add(cleaned)
        variants.add(cleaned[1:])   # 1XXXXXXXXXX
        variants.add(cleaned[2:])   # XXXXXXXXXX
    elif re.match(r"^1\d{10}$", cleaned):
        variants.add(f"+{cleaned}")
        variants.add(cleaned)
        variants.add(cleaned[1:])
    elif re.match(r"^\d{10}$", cleaned):
        variants.add(f"+1{cleaned}")
        variants.add(f"1{cleaned}")
        variants.add(cleaned)
    elif cleaned.startswith("+"):
        variants.add(cleaned)
    else:
        variants.add(cleaned)
        variants.add(f"+{cleaned}")

    return list(variants)

# ---------------------------------------------------------------------------
# Chat resolution
# ---------------------------------------------------------------------------

def resolve_chat(db: sqlite3.Connection, identifier: str) -> tuple[int, str] | None:
    """Resolve a chat by phone, email, chat_identifier, display_name, or ROWID.

    Returns (chat_id, display_label) or None.
    """
    # Try direct chat_identifier match first
    row = db.execute(
        "SELECT ROWID, chat_identifier, display_name FROM chat WHERE chat_identifier = ?",
        (identifier,),
    ).fetchone()
    if row:
        label = row["display_name"] or row["chat_identifier"]
        return row["ROWID"], label

    # Try display_name (group chats)
    row = db.execute(
        "SELECT ROWID, chat_identifier, display_name FROM chat WHERE display_name = ? COLLATE NOCASE",
        (identifier,),
    ).fetchone()
    if row:
        return row["ROWID"], row["display_name"]

    # Try ROWID
    if identifier.isdigit():
        row = db.execute(
            "SELECT ROWID, chat_identifier, display_name FROM chat WHERE ROWID = ?",
            (int(identifier),),
        ).fetchone()
        if row:
            label = row["display_name"] or row["chat_identifier"]
            return row["ROWID"], label

    # Try phone number variants
    for variant in normalize_phone(identifier):
        row = db.execute(
            "SELECT ROWID, chat_identifier, display_name FROM chat WHERE chat_identifier = ?",
            (variant,),
        ).fetchone()
        if row:
            label = row["display_name"] or row["chat_identifier"]
            return row["ROWID"], label

    # Try email
    if "@" in identifier:
        row = db.execute(
            "SELECT ROWID, chat_identifier, display_name FROM chat WHERE chat_identifier = ? COLLATE NOCASE",
            (identifier,),
        ).fetchone()
        if row:
            label = row["display_name"] or row["chat_identifier"]
            return row["ROWID"], label

    return None

# ---------------------------------------------------------------------------
# FTS5 Indexing
# ---------------------------------------------------------------------------

def _get_text(row) -> str | None:
    """Extract message text, decoding attributedBody if needed."""
    text = row["text"]
    if text:
        return text.strip()
    blob = row["attributedBody"]
    if blob:
        return decode_attributed_body(blob)
    return None


def build_index():
    """Build or incrementally update the FTS5 search index."""
    idb = index_conn()
    cdb = chat_conn()

    # Get last indexed rowid
    row = idb.execute("SELECT value FROM index_state WHERE key = 'last_rowid'").fetchone()
    last_rowid = int(row["value"]) if row else 0

    # Get max rowid in chat.db
    max_row = cdb.execute("SELECT MAX(ROWID) as mx FROM message").fetchone()
    max_rowid = max_row["mx"] or 0

    if last_rowid >= max_rowid:
        idb.close()
        cdb.close()
        return  # already up to date

    # Fetch new messages (skip tapbacks: associated_message_type >= 1000)
    cursor = cdb.execute("""
        SELECT m.ROWID, cmj.chat_id, h.id as handle_id,
               m.is_from_me, m.date, m.service, m.text, m.attributedBody
        FROM message m
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        WHERE m.ROWID > ?
          AND m.associated_message_type = 0
          AND m.item_type = 0
        ORDER BY m.ROWID
    """, (last_rowid,))

    batch = []
    count = 0
    for row in cursor:
        text = _get_text(row)
        if not text:
            continue
        date_ts = apple_ns_to_unix(row["date"]) if row["date"] else 0
        batch.append((
            row["ROWID"], row["chat_id"], row["handle_id"] or "",
            row["is_from_me"], date_ts, row["service"] or "", text
        ))
        count += 1
        if len(batch) >= 5000:
            _flush_batch(idb, batch)
            batch = []

    if batch:
        _flush_batch(idb, batch)

    idb.execute(
        "INSERT OR REPLACE INTO index_state VALUES ('last_rowid', ?)",
        (str(max_rowid),),
    )
    idb.execute(
        "INSERT OR REPLACE INTO index_state VALUES ('last_updated', ?)",
        (datetime.now().isoformat(),),
    )
    idb.commit()
    idb.close()
    cdb.close()


def _flush_batch(idb: sqlite3.Connection, batch: list):
    idb.executemany(
        "INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
        batch,
    )
    # Sync FTS index — these are always new rows (rowid > last_rowid)
    idb.executemany(
        "INSERT INTO messages_fts(rowid, text) VALUES(?, ?)",
        [(row[0], row[6]) for row in batch],
    )
    idb.commit()

# ---------------------------------------------------------------------------
# Message formatting helpers
# ---------------------------------------------------------------------------

def _format_message(row, cdb: sqlite3.Connection) -> str:
    """Format a single message row as a text line."""
    text = _get_text(row)
    if not text:
        if row["cache_has_attachments"]:
            text = "[attachment]"
        else:
            text = "[empty]"

    date_ts = apple_ns_to_unix(row["date"]) if row["date"] else 0
    ts = format_ts(date_ts)
    direction = "→" if row["is_from_me"] else "←"
    sender = "me" if row["is_from_me"] else (row["handle_id"] or "unknown")

    parts = [f"[{ts}] {direction} {sender}: {text}"]

    if row["cache_has_attachments"]:
        att = cdb.execute("""
            SELECT filename, mime_type FROM attachment
            INNER JOIN message_attachment_join ON attachment.ROWID = message_attachment_join.attachment_id
            WHERE message_attachment_join.message_id = ?
        """, (row["ROWID"],)).fetchall()
        if att:
            names = [a["filename"].split("/")[-1] if a["filename"] else a["mime_type"] or "file" for a in att]
            parts.append(f"  📎 {', '.join(names)}")

    return "\n".join(parts)


def _fetch_messages(cdb: sqlite3.Connection, chat_id: int,
                    date_from: int | None = None, date_to: int | None = None,
                    limit: int = 50, before_rowid: int | None = None,
                    around_rowid: int | None = None, window: int = 30) -> list:
    """Fetch messages from a chat with optional filters."""
    conditions = ["cmj.chat_id = ?"]
    params: list = [chat_id]

    if around_rowid:
        # Get window centered on a rowid
        conditions.append("m.ROWID BETWEEN ? AND ?")
        params.extend([around_rowid - window, around_rowid + window])
    else:
        if date_from:
            conditions.append("m.date >= ?")
            params.append(unix_to_apple_ns(date_from))
        if date_to:
            conditions.append("m.date <= ?")
            params.append(unix_to_apple_ns(date_to))
        if before_rowid:
            conditions.append("m.ROWID < ?")
            params.append(before_rowid)

    where = " AND ".join(conditions)

    rows = cdb.execute(f"""
        SELECT m.ROWID, m.text, m.attributedBody, m.date, m.is_from_me,
               m.cache_has_attachments, m.associated_message_type, m.item_type,
               h.id as handle_id
        FROM message m
        INNER JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE {where}
          AND m.associated_message_type = 0
          AND m.item_type = 0
        ORDER BY m.date DESC
        LIMIT ?
    """, params + [limit]).fetchall()

    return list(reversed(rows))  # chronological order

# ---------------------------------------------------------------------------
# AppleScript helpers
# ---------------------------------------------------------------------------

def _escape_applescript(s: str) -> str:
    """Escape a string for AppleScript."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _run_applescript(script: str) -> str:
    """Run an AppleScript and return stdout."""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"AppleScript error: {result.stderr.strip()}")
    return result.stdout.strip()

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def search_messages(
    query: str,
    contact: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 20,
) -> str:
    """Search all iMessages using full-text search. Supports FTS5 syntax:
    quotes for exact phrases ("dinner tomorrow"), OR for alternatives,
    - for negation. Returns matches with surrounding context (3 messages
    before/after) so you can understand the conversation flow.

    Args:
        query: Search query (FTS5 syntax supported)
        contact: Optional — filter to a specific contact (phone/email/name)
        date_from: Optional — start date (YYYY-MM-DD)
        date_to: Optional — end date (YYYY-MM-DD)
        limit: Max results to return (default 20)
    """
    build_index()
    idb = index_conn()
    cdb = chat_conn()

    # Build FTS query with optional filters
    conditions = ["messages_fts MATCH ?"]
    params: list = [query]

    if contact:
        # Resolve which handle_ids belong to this contact
        handle_ids = set()
        for variant in normalize_phone(contact):
            handle_ids.add(variant)
        if "@" in contact:
            handle_ids.add(contact)
        if handle_ids:
            placeholders = ",".join("?" for _ in handle_ids)
            conditions.append(f"m.handle_id IN ({placeholders})")
            params.extend(handle_ids)

    if date_from:
        ts = parse_date(date_from)
        if ts:
            conditions.append("m.date_ts >= ?")
            params.append(ts)
    if date_to:
        ts = parse_date(date_to)
        if ts:
            conditions.append("m.date_ts <= ?")
            params.append(ts)

    where = " AND ".join(conditions)

    hits = idb.execute(f"""
        SELECT m.rowid, m.chat_id, m.handle_id, m.is_from_me, m.date_ts,
               m.text, highlight(messages_fts, 0, '>>>', '<<<') as highlighted
        FROM messages_fts
        JOIN messages m ON m.rowid = messages_fts.rowid
        WHERE {where}
        ORDER BY m.date_ts DESC
        LIMIT ?
    """, params + [limit]).fetchall()

    if not hits:
        idb.close()
        cdb.close()
        return "No messages found."

    # For each hit, fetch surrounding context from chat.db
    output_parts = []
    for hit in hits:
        ts = format_ts(hit["date_ts"])
        direction = "→" if hit["is_from_me"] else "←"
        sender = "me" if hit["is_from_me"] else (hit["handle_id"] or "unknown")
        chat_id = hit["chat_id"]

        # Get chat info
        chat_info = ""
        if chat_id:
            crow = cdb.execute(
                "SELECT display_name, chat_identifier FROM chat WHERE ROWID = ?",
                (chat_id,),
            ).fetchone()
            if crow:
                chat_info = crow["display_name"] or crow["chat_identifier"] or ""

        # Fetch context: 3 messages before and after
        # Look up the exact nanosecond date from chat.db to avoid precision issues
        hit_rowid = hit["rowid"]
        hit_date_row = cdb.execute("SELECT date FROM message WHERE ROWID = ?", (hit_rowid,)).fetchone()
        hit_ns_date = hit_date_row["date"] if hit_date_row else 0
        if chat_id and hit_ns_date:
            context_before = cdb.execute("""
                SELECT m.ROWID, m.text, m.attributedBody, m.date, m.is_from_me,
                       m.cache_has_attachments, m.associated_message_type, m.item_type,
                       h.id as handle_id
                FROM message m
                INNER JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                LEFT JOIN handle h ON h.ROWID = m.handle_id
                WHERE cmj.chat_id = ? AND m.date < ? AND m.associated_message_type = 0 AND m.item_type = 0
                ORDER BY m.date DESC LIMIT 3
            """, (chat_id, hit_ns_date)).fetchall()

            context_after = cdb.execute("""
                SELECT m.ROWID, m.text, m.attributedBody, m.date, m.is_from_me,
                       m.cache_has_attachments, m.associated_message_type, m.item_type,
                       h.id as handle_id
                FROM message m
                INNER JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                LEFT JOIN handle h ON h.ROWID = m.handle_id
                WHERE cmj.chat_id = ? AND m.date > ? AND m.associated_message_type = 0 AND m.item_type = 0
                ORDER BY m.date ASC LIMIT 3
            """, (chat_id, hit_ns_date)).fetchall()

            context_lines = []
            for r in reversed(context_before):
                context_lines.append("  " + _format_message(r, cdb))
            context_lines.append(f"  >>> [{ts}] {direction} {sender}: {hit['highlighted']} <<<")
            for r in context_after:
                context_lines.append("  " + _format_message(r, cdb))

            header = f"— {chat_info} (chat {chat_id}) —"
            output_parts.append(header + "\n" + "\n".join(context_lines))
        else:
            output_parts.append(f"[{ts}] {direction} {sender}: {hit['highlighted']}")

    idb.close()
    cdb.close()
    return f"Found {len(hits)} results:\n\n" + "\n\n".join(output_parts)


@mcp.tool()
def list_conversations(limit: int = 30, sort_by: str = "recent") -> str:
    """List iMessage conversations with metadata.

    Args:
        limit: Max conversations to return (default 30)
        sort_by: "recent" (most recent message) or "active" (most messages)
    """
    cdb = chat_conn()

    if sort_by == "active":
        order = "msg_count DESC"
    else:
        order = "last_date DESC"

    rows = cdb.execute(f"""
        SELECT c.ROWID as chat_id, c.chat_identifier, c.display_name, c.service_name,
               c.style,
               COUNT(cmj.message_id) as msg_count,
               MAX(m.date) as last_date,
               (SELECT COALESCE(m2.text, '[media]')
                FROM message m2
                INNER JOIN chat_message_join cmj2 ON cmj2.message_id = m2.ROWID
                WHERE cmj2.chat_id = c.ROWID AND m2.associated_message_type = 0
                ORDER BY m2.date DESC LIMIT 1) as last_text
        FROM chat c
        LEFT JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
        LEFT JOIN message m ON m.ROWID = cmj.message_id
        GROUP BY c.ROWID
        HAVING msg_count > 0
        ORDER BY {order}
        LIMIT ?
    """, (limit,)).fetchall()

    lines = []
    for r in rows:
        name = r["display_name"] or r["chat_identifier"]
        is_group = r["style"] == 43
        kind = "group" if is_group else "DM"
        service = r["service_name"] or ""
        last_date = format_ts(apple_ns_to_unix(r["last_date"])) if r["last_date"] else "?"
        preview = (r["last_text"] or "")[:80]

        # Get participants for group chats
        participants = ""
        if is_group:
            handles = cdb.execute("""
                SELECT h.id FROM handle h
                INNER JOIN chat_handle_join chj ON chj.handle_id = h.ROWID
                WHERE chj.chat_id = ?
            """, (r["chat_id"],)).fetchall()
            if handles:
                participants = f"\n  participants: {', '.join(h['id'] for h in handles)}"

        lines.append(
            f"[{r['chat_id']}] {name} ({kind}, {service}, {r['msg_count']} msgs)\n"
            f"  last: {last_date} — {preview}"
            f"{participants}"
        )

    cdb.close()
    return "\n\n".join(lines) if lines else "No conversations found."


@mcp.tool()
def get_messages(
    chat: str,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
    before_rowid: int | None = None,
) -> str:
    """Get messages from a specific conversation. Supports pagination.

    Args:
        chat: Chat identifier — phone number, email, group name, chat_identifier, or chat ROWID
        date_from: Optional start date (YYYY-MM-DD)
        date_to: Optional end date (YYYY-MM-DD)
        limit: Max messages to return (default 50)
        before_rowid: For pagination — get messages before this message ROWID
    """
    cdb = chat_conn()

    resolved = resolve_chat(cdb, chat)
    if not resolved:
        cdb.close()
        return f"Could not find conversation matching '{chat}'. Use list_conversations to see available chats."

    chat_id, label = resolved

    df = parse_date(date_from) if date_from else None
    dt = parse_date(date_to) if date_to else None

    rows = _fetch_messages(cdb, chat_id, date_from=df, date_to=dt,
                           limit=limit, before_rowid=before_rowid)

    if not rows:
        cdb.close()
        return f"No messages found in '{label}'."

    lines = [f"— {label} (chat {chat_id}) — showing {len(rows)} messages —\n"]
    for r in rows:
        lines.append(_format_message(r, cdb))

    # Pagination hint
    if len(rows) == limit:
        first_rowid = rows[0]["ROWID"]
        lines.append(f"\n[more messages available — use before_rowid={first_rowid} to page back]")

    cdb.close()
    return "\n".join(lines)


@mcp.tool()
def get_unread_messages(limit: int = 50) -> str:
    """Get unread messages grouped by conversation.

    Args:
        limit: Max messages to return (default 50)
    """
    cdb = chat_conn()

    rows = cdb.execute("""
        SELECT m.ROWID, m.text, m.attributedBody, m.date, m.is_from_me,
               m.cache_has_attachments, m.associated_message_type, m.item_type,
               h.id as handle_id,
               c.ROWID as chat_id, c.display_name, c.chat_identifier
        FROM message m
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        INNER JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        INNER JOIN chat c ON c.ROWID = cmj.chat_id
        WHERE m.is_read = 0 AND m.is_from_me = 0
          AND m.is_finished = 1 AND m.is_system_message = 0
          AND m.associated_message_type = 0 AND m.item_type = 0
        ORDER BY m.date DESC
        LIMIT ?
    """, (limit,)).fetchall()

    if not rows:
        cdb.close()
        return "No unread messages."

    # Group by chat
    chats: dict[int, list] = {}
    chat_labels: dict[int, str] = {}
    for r in rows:
        cid = r["chat_id"]
        if cid not in chats:
            chats[cid] = []
            chat_labels[cid] = r["display_name"] or r["chat_identifier"]
        chats[cid].append(r)

    parts = [f"{len(rows)} unread messages in {len(chats)} conversations:\n"]
    for cid, msgs in chats.items():
        parts.append(f"— {chat_labels[cid]} ({len(msgs)} unread) —")
        for r in reversed(msgs):  # chronological
            parts.append(_format_message(r, cdb))
        parts.append("")

    cdb.close()
    return "\n".join(parts)


@mcp.tool()
def send_message(recipient: str, message: str) -> str:
    """Send an iMessage. macOS will prompt for Automation permission on first use.

    Args:
        recipient: Phone number or email of the recipient
        message: The message text to send
    """
    escaped_recipient = _escape_applescript(recipient)
    escaped_message = _escape_applescript(message)

    script = (
        'tell application "Messages"\n'
        f'  send "{escaped_message}" to buddy "{escaped_recipient}" '
        f'of (service 1 whose service type = iMessage)\n'
        'end tell'
    )

    try:
        _run_applescript(script)
        return f"Message sent to {recipient}."
    except RuntimeError as e:
        return f"Failed to send: {e}"


@mcp.tool()
def search_contacts(query: str) -> str:
    """Search macOS Contacts by name, phone, or email.

    Args:
        query: Search query (name, phone number, or email)
    """
    escaped = _escape_applescript(query.lower())
    script = f'''
tell application "Contacts"
    set output to ""
    set matchCount to 0
    repeat with p in every person
        set pName to name of p as text
        set matched to false
        if (pName as text) contains "{escaped}" then
            set matched to true
        end if
        if not matched then
            repeat with ph in phones of p
                if (value of ph as text) contains "{escaped}" then
                    set matched to true
                    exit repeat
                end if
            end repeat
        end if
        if not matched then
            repeat with em in emails of p
                if (value of em as text) contains "{escaped}" then
                    set matched to true
                    exit repeat
                end if
            end repeat
        end if
        if matched then
            set matchCount to matchCount + 1
            set output to output & pName & return
            repeat with ph in phones of p
                set output to output & "  phone: " & (value of ph as text) & return
            end repeat
            repeat with em in emails of p
                set output to output & "  email: " & (value of em as text) & return
            end repeat
            set output to output & return
            if matchCount >= 20 then exit repeat
        end if
    end repeat
    return output
end tell'''

    try:
        result = _run_applescript(script)
        return result if result else f"No contacts matching '{query}'."
    except RuntimeError as e:
        return f"Contact search failed: {e}"


@mcp.tool()
def get_conversation_context(
    chat: str,
    around_date: str | None = None,
    around_rowid: int | None = None,
    window: int = 30,
) -> str:
    """Get a window of messages centered on a point in time or message.
    Useful for understanding conversation context around a specific event.

    Args:
        chat: Chat identifier (phone, email, group name, chat ROWID)
        around_date: Center on this date (YYYY-MM-DD or YYYY-MM-DD HH:MM)
        around_rowid: Center on this message ROWID
        window: Number of messages to show (default 30, split before/after)
    """
    cdb = chat_conn()

    resolved = resolve_chat(cdb, chat)
    if not resolved:
        cdb.close()
        return f"Could not find conversation matching '{chat}'."

    chat_id, label = resolved
    half = window // 2

    if around_rowid:
        # Get messages around a specific rowid
        before = cdb.execute("""
            SELECT m.ROWID, m.text, m.attributedBody, m.date, m.is_from_me,
                   m.cache_has_attachments, m.associated_message_type, m.item_type,
                   h.id as handle_id
            FROM message m
            INNER JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            WHERE cmj.chat_id = ? AND m.ROWID <= ? AND m.associated_message_type = 0 AND m.item_type = 0
            ORDER BY m.date DESC LIMIT ?
        """, (chat_id, around_rowid, half)).fetchall()

        after = cdb.execute("""
            SELECT m.ROWID, m.text, m.attributedBody, m.date, m.is_from_me,
                   m.cache_has_attachments, m.associated_message_type, m.item_type,
                   h.id as handle_id
            FROM message m
            INNER JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            WHERE cmj.chat_id = ? AND m.ROWID > ? AND m.associated_message_type = 0 AND m.item_type = 0
            ORDER BY m.date ASC LIMIT ?
        """, (chat_id, around_rowid, half)).fetchall()

        rows = list(reversed(before)) + list(after)

    elif around_date:
        ts = parse_date(around_date)
        if not ts:
            cdb.close()
            return f"Could not parse date '{around_date}'. Use YYYY-MM-DD format."
        apple_date = unix_to_apple_ns(ts)

        before = cdb.execute("""
            SELECT m.ROWID, m.text, m.attributedBody, m.date, m.is_from_me,
                   m.cache_has_attachments, m.associated_message_type, m.item_type,
                   h.id as handle_id
            FROM message m
            INNER JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            WHERE cmj.chat_id = ? AND m.date <= ? AND m.associated_message_type = 0 AND m.item_type = 0
            ORDER BY m.date DESC LIMIT ?
        """, (chat_id, apple_date, half)).fetchall()

        after = cdb.execute("""
            SELECT m.ROWID, m.text, m.attributedBody, m.date, m.is_from_me,
                   m.cache_has_attachments, m.associated_message_type, m.item_type,
                   h.id as handle_id
            FROM message m
            INNER JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            WHERE cmj.chat_id = ? AND m.date > ? AND m.associated_message_type = 0 AND m.item_type = 0
            ORDER BY m.date ASC LIMIT ?
        """, (chat_id, apple_date, half)).fetchall()

        rows = list(reversed(before)) + list(after)
    else:
        # No anchor — just get the most recent messages
        rows = _fetch_messages(cdb, chat_id, limit=window)

    if not rows:
        cdb.close()
        return f"No messages found in '{label}'."

    lines = [f"— {label} (chat {chat_id}) — {len(rows)} messages —\n"]
    for r in rows:
        lines.append(_format_message(r, cdb))

    cdb.close()
    return "\n".join(lines)
