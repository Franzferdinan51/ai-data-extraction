#!/usr/bin/env python3
"""
extract_hermes.py - Extract conversation history from Hermes Agent

Hermes Agent (https://hermes-agent.nousresearch.com) stores all sessions
in a SQLite database at:
  - Windows: %LOCALAPPDATA%\hermes\state.db
  - macOS:   ~/Library/Application Support/hermes/state.db
  - Linux:   ~/.local/share/hermes/state.db

Schema (relevant tables):
  - sessions: id, source, user_id, model, system_prompt, started_at, ended_at,
              message_count, tool_call_count, input_tokens, output_tokens,
              cwd, billing_provider, billing_mode, estimated_cost_usd, title
  - messages: session_id, role, content, tool_calls, tool_name, timestamp,
              token_count, finish_reason, reasoning_content, codex_reasoning_items

Output: JSONL matching the same schema as other extract_* scripts in this
toolkit, so the merged dataset is uniform.

Author: Duckets <https://github.com/Franzferdinan51>
"""

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------

def find_hermes_db():
    """Find Hermes Agent state.db in standard locations."""
    candidates = []

    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            candidates.append(Path(local_app) / "hermes" / "state.db")
        candidates.append(Path.home() / "AppData" / "Local" / "hermes" / "state.db")
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "hermes" / "state.db")
    else:
        candidates.append(Path.home() / ".local" / "share" / "hermes" / "state.db")
        xdg = os.environ.get("XDG_DATA_HOME", "")
        if xdg:
            candidates.append(Path(xdg) / "hermes" / "state.db")

    found = []
    for path in candidates:
        if path.exists() and path.is_file():
            found.append(path)
    return found


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_from_db(db_path, output_path, include_system=False, min_messages=2):
    """
    Extract sessions from a Hermes state.db.

    Args:
        db_path: Path to state.db
        output_path: Where to write the JSONL
        include_system: Include the system prompt as a first message (default False)
        min_messages: Skip sessions with fewer than this many messages
    """
    print(f"  Opening {db_path} (read-only)...")
    # URI mode = read-only, avoids SQLite locked errors
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row

    # Verify schema (sanity check)
    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "messages" not in tables or "sessions" not in tables:
        print(f"  ❌ Skipping {db_path} - missing required tables (messages/sessions)")
        conn.close()
        return 0

    # Get all non-archived sessions
    sessions = conn.execute(
        """
        SELECT * FROM sessions
        WHERE archived = 0
        ORDER BY started_at ASC
        """
    ).fetchall()

    written = 0
    with open(output_path, "w", encoding="utf-8") as out:
        for sess in sessions:
            msgs = conn.execute(
                """
                SELECT role, content, tool_calls, tool_name, timestamp,
                       token_count, finish_reason, reasoning_content,
                       codex_reasoning_items, codex_message_items
                FROM messages
                WHERE session_id = ? AND active = 1
                ORDER BY id ASC
                """,
                (sess["id"],),
            ).fetchall()

            if len(msgs) < min_messages:
                continue

            conversation = []
            if include_system and sess["system_prompt"]:
                conversation.append({
                    "role": "system",
                    "content": sess["system_prompt"],
                    "timestamp": sess["started_at"],
                })

            for m in msgs:
                msg = {
                    "role": m["role"],
                    "content": m["content"] or "",
                    "timestamp": m["timestamp"],
                }
                if m["tool_calls"]:
                    try:
                        msg["tool_calls"] = json.loads(m["tool_calls"])
                    except (json.JSONDecodeError, TypeError):
                        msg["tool_calls_raw"] = m["tool_calls"]
                if m["tool_name"]:
                    msg["tool_name"] = m["tool_name"]
                if m["token_count"]:
                    msg["token_count"] = m["token_count"]
                if m["reasoning_content"]:
                    msg["reasoning"] = m["reasoning_content"]
                if m["codex_message_items"]:
                    try:
                        msg["codex_items"] = json.loads(m["codex_message_items"])
                    except (json.JSONDecodeError, TypeError):
                        msg["codex_items_raw"] = m["codex_message_items"]
                conversation.append(msg)

            # Build the conversation record matching the toolkit's output schema
            record = {
                "messages": conversation,
                "source": f"hermes-{sess['source'] or 'unknown'}",
                "name": sess["title"] or f"hermes-{sess['id'][:8]}",
                "session_id": sess["id"],
                "model": sess["model"],
                "platform": sess["source"],
                "user_id": sess["user_id"],
                "cwd": sess["cwd"],
                "created_at": int(sess["started_at"] * 1000) if sess["started_at"] else None,
                "ended_at": int(sess["ended_at"] * 1000) if sess["ended_at"] else None,
                "message_count": sess["message_count"],
                "tool_call_count": sess["tool_call_count"],
                "input_tokens": sess["input_tokens"],
                "output_tokens": sess["output_tokens"],
                "estimated_cost_usd": sess["estimated_cost_usd"],
                "extracted_at": datetime.utcnow().isoformat() + "Z",
            }

            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1

    conn.close()
    return written


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Extract conversation history from Hermes Agent (state.db)"
    )
    parser.add_argument(
        "--output-dir",
        default="extracted_data",
        help="Output directory (default: extracted_data)",
    )
    parser.add_argument(
        "--include-system",
        action="store_true",
        help="Include the system prompt as a first message",
    )
    parser.add_argument(
        "--min-messages",
        type=int,
        default=2,
        help="Skip sessions with fewer than N messages (default: 2)",
    )
    parser.add_argument(
        "--db",
        help="Explicit path to state.db (skips auto-discovery)",
    )
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(exist_ok=True)

    if args.db:
        dbs = [Path(args.db)]
    else:
        dbs = find_hermes_db()

    if not dbs:
        print("❌ No Hermes state.db found in standard locations.")
        print("   Searched:")
        if sys.platform == "win32":
            print("   - %LOCALAPPDATA%\\hermes\\state.db")
        elif sys.platform == "darwin":
            print("   - ~/Library/Application Support/hermes/state.db")
        else:
            print("   - ~/.local/share/hermes/state.db")
        print("   Use --db to point to a custom location.")
        sys.exit(1)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    total = 0
    for db in dbs:
        output = out_dir / f"hermes_conversations_{timestamp}.jsonl"
        try:
            count = extract_from_db(
                db, output,
                include_system=args.include_system,
                min_messages=args.min_messages,
            )
            print(f"  ✅ {db} -> {output}  ({count} conversations)")
            total += count
        except Exception as e:
            print(f"  ❌ Failed on {db}: {e}")

    print(f"\n📊 Total: {total} conversations across {len(dbs)} database(s)")


if __name__ == "__main__":
    main()
