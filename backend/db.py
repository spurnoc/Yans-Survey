"""
SPUR Survey — Turso database persistence via HTTP API.

All Turso DB functions extracted from main.py. Uses a shared module-level
httpx.AsyncClient for connection pooling — all Turso calls reuse the same
TCP connection.
"""
from __future__ import annotations

import os
import json
import logging

import httpx

logger = logging.getLogger(__name__)

# ── Config (read from environment at import time, matching main.py) ──
TURSO_DB_URL = os.getenv("TURSO_DB_URL", "")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")


# ── Shared httpx.AsyncClient for connection pooling ──────────────
_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Return a shared module-level httpx.AsyncClient (lazy-init).

    All Turso HTTP calls reuse the same client (and TCP connection) for
    free connection pooling. Re-created automatically if closed.
    """
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30.0)
    return _http_client


def _turso_url() -> str:
    """Convert the Turso libsql URL to the HTTP API URL."""
    url = TURSO_DB_URL
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    return url.rstrip("/") + "/v2/pipeline"


async def _turso_request(sql: str, args: list = None) -> dict:
    """Execute a SQL statement via the Turso HTTP API and return raw JSON (async).
    Args should be a list of dicts like {"type": "text", "value": "hello"}.
    The value is ALWAYS a string, even for integers.
    """
    stmt = {"sql": sql}
    if args:
        # Ensure all values are strings
        stmt["args"] = [{"type": a.get("type", "text"), "value": str(a["value"])} for a in args]
    body = {"requests": [{"type": "execute", "stmt": stmt}]}
    client = _get_client()
    resp = await client.post(
        _turso_url(),
        json=body,
        headers={
            "Authorization": f"Bearer {TURSO_AUTH_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    if resp.status_code != 200:
        raise Exception(f"Turso API error: {resp.status_code} {resp.text[:300]}")
    return resp.json()


async def _turso_execute(sql: str, args: list = None):
    """Execute a SQL statement via the Turso HTTP API (async).
    Args should be a list of dicts like {"type": "text", "value": "hello"}.
    The value is ALWAYS a string, even for integers.
    """
    return await _turso_request(sql, args)


async def _turso_query(sql: str, args: list = None) -> list[dict]:
    """Execute a SELECT and return rows as dicts (async)."""
    data = await _turso_request(sql, args)
    results = data.get("results", [])
    if not results:
        return []
    result = results[0]
    if "error" in result:
        raise Exception(f"Turso SQL error: {result['error']['message']}")
    resp_data = result.get("response", {}).get("result", {})
    rows_raw = resp_data.get("rows", [])
    cols = [c["name"] for c in resp_data.get("cols", [])]
    rows = []
    for row in rows_raw:
        values = []
        for v in row:
            if isinstance(v, dict):
                val = v.get("value")
                # Try to convert numeric strings back to int or float
                if val and val.lstrip('-').replace('.', '').isdigit():
                    try:
                        num = float(val)
                        val = int(num) if num == int(num) else num
                    except (ValueError, TypeError):
                        pass
                values.append(val)
            else:
                values.append(v)
        rows.append(dict(zip(cols, values)))
    return rows


async def init_db():
    await _turso_execute("""
    CREATE TABLE IF NOT EXISTS survey_sessions (
        session_id TEXT PRIMARY KEY,
        conversation TEXT DEFAULT '[]',
        q_index INTEGER DEFAULT 0,
        probe_count INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    )
    """)
    await _turso_execute("""
    CREATE TABLE IF NOT EXISTS survey_profiles (
        session_id TEXT PRIMARY KEY,
        profile TEXT DEFAULT '',
        updated_at TEXT DEFAULT (datetime('now'))
    )
    """)
    await _turso_execute("""
    CREATE TABLE IF NOT EXISTS daily_checkins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        conversation TEXT DEFAULT '[]',
        stress_points TEXT DEFAULT '[]',
        wins TEXT DEFAULT '[]',
        priorities TEXT DEFAULT '[]',
        summary TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    )
    """)
    # Add summary column if it doesn't exist (for already-deployed databases)
    try:
        await _turso_execute("ALTER TABLE daily_checkins ADD COLUMN summary TEXT DEFAULT ''")
    except Exception as e:
        logger.debug("init_db: summary column already exists: %s", e)
    await _turso_execute("""
    CREATE TABLE IF NOT EXISTS card_priorities (
        session_id TEXT PRIMARY KEY,
        ordered_cards TEXT DEFAULT '[]',
        last_updated_by TEXT DEFAULT 'onboarding',
        updated_at TEXT DEFAULT (datetime('now'))
    )
    """)
    await _turso_execute("""
    CREATE TABLE IF NOT EXISTS business_profiles (
        session_id TEXT PRIMARY KEY,
        profile_data TEXT DEFAULT '{}',
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    )
    """)


async def _load_session(session_id: str) -> dict | None:
    """SELECT only — returns None if session not found. No INSERT."""
    rows = await _turso_query(
        "SELECT * FROM survey_sessions WHERE session_id=?",
        [{"type": "text", "value": session_id}]
    )
    if rows:
        row = rows[0]
        return {
            "session_id": session_id,
            "conversation": json.loads(row.get("conversation") or "[]"),
            "q_index": int(row.get("q_index") or 0),
            "probe_count": int(row.get("probe_count") or 0),
        }
    return None


async def _load_or_create_session(session_id: str) -> dict:
    """SELECT, if not found INSERT + return new. Used by POST endpoints only."""
    sess = await _load_session(session_id)
    if sess is not None:
        return sess
    await _turso_execute(
        "INSERT INTO survey_sessions (session_id) VALUES (?)",
        [{"type": "text", "value": session_id}]
    )
    return {
        "session_id": session_id,
        "conversation": [],
        "q_index": 0,
        "probe_count": 0,
    }


async def _save_session(sess: dict):
    await _turso_execute(
        """INSERT OR REPLACE INTO survey_sessions (session_id, conversation, q_index, probe_count, updated_at)
           VALUES (?, ?, ?, ?, datetime('now'))""",
        [
            {"type": "text", "value": sess["session_id"]},
            {"type": "text", "value": json.dumps(sess["conversation"])},
            {"type": "integer", "value": sess["q_index"]},
            {"type": "integer", "value": sess["probe_count"]},
        ]
    )


async def _reset_session(session_id: str):
    await _turso_execute(
        """INSERT OR REPLACE INTO survey_sessions (session_id, conversation, q_index, probe_count, updated_at)
           VALUES (?, '[]', 0, 0, datetime('now'))""",
        [{"type": "text", "value": session_id}]
    )
    await _turso_execute(
        """INSERT OR REPLACE INTO survey_profiles (session_id, profile, updated_at)
           VALUES (?, '', datetime('now'))""",
        [{"type": "text", "value": session_id}]
    )


async def _load_profile(session_id: str) -> str:
    rows = await _turso_query(
        "SELECT profile FROM survey_profiles WHERE session_id=?",
        [{"type": "text", "value": session_id}]
    )
    if rows and rows[0].get("profile"):
        return rows[0]["profile"]
    return ""


async def _save_profile(session_id: str, content: str):
    await _turso_execute(
        """INSERT OR REPLACE INTO survey_profiles (session_id, profile, updated_at)
           VALUES (?, ?, datetime('now'))""",
        [
            {"type": "text", "value": session_id},
            {"type": "text", "value": content},
        ]
    )


# ── Daily check-in helpers ───────────────────────────────────────
async def _has_completed_onboarding(session_id: str) -> bool:
    """Check if this session has completed the onboarding survey."""
    # Import locally to avoid circular import (prompts imports from db for
    # _detect_business_type_from_session, but db needs _get_questions_for_type
    # and _detect_business_type from prompts for onboarding completion check).
    from prompts import _detect_business_type, _get_questions_for_type

    sess = await _load_session(session_id)
    if not sess:
        return False
    # Use business-type-aware question count
    active_questions = _get_questions_for_type(_detect_business_type(sess.get("conversation", [])))
    return sess["q_index"] >= len(active_questions)


async def _get_latest_checkin(session_id: str) -> dict | None:
    """Get the most recent daily check-in for a session."""
    rows = await _turso_query(
        "SELECT * FROM daily_checkins WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
        [{"type": "text", "value": session_id}]
    )
    if rows:
        row = rows[0]
        return {
            "id": row.get("id"),
            "conversation": json.loads(row.get("conversation") or "[]"),
            "stress_points": json.loads(row.get("stress_points") or "[]"),
            "wins": json.loads(row.get("wins") or "[]"),
            "priorities": json.loads(row.get("priorities") or "[]"),
            "summary": row.get("summary") or "",
            "created_at": row.get("created_at"),
        }
    return None


async def _save_checkin(session_id: str, conversation: list, stress_points: list, wins: list, priorities: list, summary: str = ""):
    """Save a daily check-in to Turso."""
    await _turso_execute(
        """INSERT INTO daily_checkins (session_id, conversation, stress_points, wins, priorities, summary, created_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        [
            {"type": "text", "value": session_id},
            {"type": "text", "value": json.dumps(conversation)},
            {"type": "text", "value": json.dumps(stress_points)},
            {"type": "text", "value": json.dumps(wins)},
            {"type": "text", "value": json.dumps(priorities)},
            {"type": "text", "value": summary},
        ]
    )


async def _get_card_priorities(session_id: str) -> list:
    """Get the current card priority ordering for a session."""
    rows = await _turso_query(
        "SELECT ordered_cards FROM card_priorities WHERE session_id=?",
        [{"type": "text", "value": session_id}]
    )
    if rows and rows[0].get("ordered_cards"):
        return json.loads(rows[0]["ordered_cards"])
    return []


async def _save_card_priorities(session_id: str, ordered_cards: list, updated_by: str = "checkin"):
    """Save or update card priorities for a session."""
    await _turso_execute(
        """INSERT OR REPLACE INTO card_priorities (session_id, ordered_cards, last_updated_by, updated_at)
           VALUES (?, ?, ?, datetime('now'))""",
        [
            {"type": "text", "value": session_id},
            {"type": "text", "value": json.dumps(ordered_cards)},
            {"type": "text", "value": updated_by},
        ]
    )


async def _load_business_profile(session_id: str) -> dict | None:
    """Load the business profile from Turso."""
    try:
        rows = await _turso_query(
            "SELECT profile_data FROM business_profiles WHERE session_id=?",
            [{"type": "text", "value": session_id}]
        )
        if rows and rows[0].get("profile_data"):
            return json.loads(rows[0]["profile_data"])
    except Exception as e:
        logger.debug("_load_business_profile failed: %s", e)
    return None


async def _save_business_profile(session_id: str, profile_data: dict):
    """Save the business profile (card selection + config) to Turso."""
    await _turso_execute(
        """INSERT OR REPLACE INTO business_profiles (session_id, profile_data, updated_at)
           VALUES (?, ?, datetime('now'))""",
        [
            {"type": "text", "value": session_id},
            {"type": "text", "value": json.dumps(profile_data)},
        ]
    )


async def _detect_business_type_from_session(session_id: str) -> str:
    """Load the onboarding conversation and detect business type."""
    # Import locally to avoid circular import
    from prompts import _detect_business_type

    try:
        sess = await _load_session(session_id)
        if sess is None:
            return "Other"
        return _detect_business_type(sess.get("conversation", []))
    except Exception as e:
        logger.debug("_detect_business_type_from_session failed: %s", e)
        return "Other"
