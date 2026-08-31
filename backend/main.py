"""
SPUR Survey — Daily 15 Business Survey conversational survey.

Standalone FastAPI app. Chat-style adaptive survey with SSE streaming.
- Per-browser sessions via session_id, persisted to Turso via HTTP API
- AI generates dynamic multiple-choice options based on conversation context
- Behavioral analysis runs after each answer, stored in DB
- Findings are fed back into the system prompt so the AI adapts in real-time
"""
from __future__ import annotations

import os, json, time, re, asyncio, logging
import hashlib, hmac, secrets, base64
from typing import Optional
from datetime import datetime, timezone

import httpx
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

# V3-7: retain references to background tasks to prevent GC from cancelling them
_background_tasks: set = set()

from fastapi import FastAPI, HTTPException, Header, Body, Depends, Request
from fastapi.responses import StreamingResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
import pathlib

# ── Module imports for connection pooling ────────────────────────
try:
    from db import _get_client as _get_db_client
    from llm import _get_client as _get_llm_client
    _use_pooled_clients = True
except ImportError:
    _use_pooled_clients = False

@asynccontextmanager
async def lifespan(app):
    """Application lifespan: initialize the database on startup."""
    await init_db()
    yield

app = FastAPI(title="SPUR Survey", lifespan=lifespan)

# Security headers middleware
@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
        "font-src 'self' https://fonts.gstatic.com; "
        "connect-src 'self'; "
        "img-src 'self' data:"
    )
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://daily15.spurnoc.com",
        "https://daily15.spurnoc.com",
        "http://localhost:8080",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SPUR_API_BASE = os.getenv("SPUR_API_BASE", "https://ai.spuric.com/v1")
SPUR_DEMO_API_KEY = os.getenv("SPUR_DEMO_API_KEY", "")
SURVEY_MODEL = os.getenv("SURVEY_MODEL", "spur-glm-5-2")
ANALYSIS_MODEL = os.getenv("ANALYSIS_MODEL", "spur-glm-air")
TURSO_DB_URL = os.getenv("TURSO_DB_URL", "")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")

# Email settings (V3-12: no hardcoded fallbacks — email is skipped if unset)
SMTP_HOST = os.getenv("SMTP_HOST", "mail.spuric.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")

# ── Auth config ──────────────────────────────────────────────────
# Secret used to sign session tokens. Falls back to a random per-process
# value if AUTH_SECRET is not set (tokens won't survive restart in that case).
AUTH_SECRET = os.getenv("AUTH_SECRET", "")
if not AUTH_SECRET:
    AUTH_SECRET = secrets.token_hex(32)
    logger.warning("AUTH_SECRET env var not set — using random per-process secret. Tokens will not survive restart.")

# Password hashing parameters (PBKDF2-HMAC-SHA256)
_PBKDF2_ITERATIONS = 100_000
_SALT_BYTES = 16
_TOKEN_TTL_SECONDS = 30 * 24 * 3600  # 30 days

# ── Business-type-aware questions ───────────────────────────────
# Q1 is always business type selection. Q2-Q13 adapt based on answer.

BUSINESS_TYPES = [
    "Restaurant/Cafe", "Salon/Spa/Barber", "Plumber/Electrician/HVAC",
    "Retail/Boutique", "Gym/Fitness Studio", "Landscaping/Lawn Care",
    "Auto Repair/Detailing", "Cleaning Service", "Photography/Video",
    "Real Estate", "Other"
]

# Universal questions used for ALL business types (Q6-Q13)
UNIVERSAL_QUESTIONS = [
    {"id": 6, "text": "If it gave you a bad suggestion once, would that turn you off the whole thing?", "type": "choice", "tag": "trust"},
    {"id": 7, "text": "Would you trust a suggestion like 'call this customer back today' coming from a screen?", "type": "choice", "tag": "ai_trust"},
    {"id": 8, "text": "Is there a decision you make regularly where you'd want a second opinion?", "type": "text", "tag": "second_opinion"},
    {"id": 9, "text": "What's the one thing about running your business that's been bugging you lately?", "type": "text", "tag": "pain_point"},
    {"id": 10, "text": "Walk me through the last time you looked something up on your phone or a website. Was it easy?", "type": "text", "tag": "tech_comfort"},
    {"id": 11, "text": "When you check something online, is it on your phone or a computer? Quick check, or do you sit down for it?", "type": "text", "tag": "habits"},
    {"id": 12, "text": "If everything — revenue, customers, schedule, staff — was on one screen at once, does that help or feel like a lot?", "type": "choice", "tag": "density"},
    {"id": 13, "text": "When something's confusing on a screen, what do you usually do?", "type": "choice", "tag": "ux_reaction"},
]

# Shared Q5 — appended to every business type's Q2-Q4
Q5 = {"id": 5, "text": "Would you rather this just show you what happened, or tell you what to do next?", "type": "choice", "tag": "proactive"}

# Business-type-specific questions for Q2-Q4 (Q5 is shared above)
QUESTIONS_BY_TYPE = {
    "Restaurant/Cafe": [
        {"id": 2, "text": "When a customer says something nice, a bad review comes in, or a big catering order lands, what happens next?", "type": "text", "tag": "reactions"},
        {"id": 3, "text": "If I asked you right now how many catering orders you did last month, could you actually find that, or is it more of a guess?", "type": "choice", "tag": "catering_data"},
        {"id": 4, "text": "When something goes wrong in the shop, do you usually already know why, or are you guessing?", "type": "choice", "tag": "problem_solving"},
    ],
    "Salon/Spa/Barber": [
        {"id": 2, "text": "How do you handle bookings right now? Phone, an app, walk-ins, or a mix?", "type": "text", "tag": "booking_method"},
        {"id": 3, "text": "How often do you deal with no-shows or last-minute cancellations?", "type": "choice", "tag": "no_shows"},
        {"id": 4, "text": "Do you track when clients are due for a return visit, or is it on them to come back?", "type": "choice", "tag": "retention"},
    ],
    "Plumber/Electrician/HVAC": [
        {"id": 2, "text": "How do you keep track of jobs right now? Notebook, app, whiteboard, or in your head?", "type": "text", "tag": "job_tracking"},
        {"id": 3, "text": "When you quote a job, do you know what's outstanding and what's been paid, or is it a scramble to figure out?", "type": "choice", "tag": "invoices"},
        {"id": 4, "text": "Do you know which jobs need parts on order, or is that mostly in your head?", "type": "choice", "tag": "parts_tracking"},
    ],
    "Retail/Boutique": [
        {"id": 2, "text": "How do you know what's selling and what's sitting on the shelves?", "type": "text", "tag": "sales_tracking"},
        {"id": 3, "text": "Do you get alerts when stock is low, or do you find out when a customer asks for something you don't have?", "type": "choice", "tag": "stock_alerts"},
        {"id": 4, "text": "When something goes wrong in the shop, do you usually already know why, or are you guessing?", "type": "choice", "tag": "problem_solving"},
    ],
    "Gym/Fitness Studio": [
        {"id": 2, "text": "How do you track memberships right now? A system, spreadsheet, or paper?", "type": "text", "tag": "membership_tracking"},
        {"id": 3, "text": "Do you know which members haven't been in lately, or do you only notice when they cancel?", "type": "choice", "tag": "churn"},
        {"id": 4, "text": "When a class is half empty, do you usually know why, or is it a guess?", "type": "choice", "tag": "attendance"},
    ],
    "Landscaping/Lawn Care": [
        {"id": 2, "text": "How do you plan your route each day? Is it mapped out or mostly in your head?", "type": "text", "tag": "routing"},
        {"id": 3, "text": "Do you track which clients are on recurring schedules vs one-offs?", "type": "choice", "tag": "recurring"},
        {"id": 4, "text": "When equipment breaks down, how big of a deal is that for your day?", "type": "choice", "tag": "equipment"},
    ],
    "Auto Repair/Detailing": [
        {"id": 2, "text": "How do you track what cars are in the shop and what stage they're at?", "type": "text", "tag": "job_board"},
        {"id": 3, "text": "Do you know what parts are on order vs in stock, or is it a scramble?", "type": "choice", "tag": "parts"},
        {"id": 4, "text": "How do you handle customer history — do you know what you did last time they came in?", "type": "choice", "tag": "customer_history"},
    ],
    "Cleaning Service": [
        {"id": 2, "text": "How do you schedule your clients each day? Is it mapped out or flexible?", "type": "text", "tag": "scheduling"},
        {"id": 3, "text": "Do you track which clients are recurring vs one-time?", "type": "choice", "tag": "recurring"},
        {"id": 4, "text": "When something goes wrong on a job, do you usually already know why?", "type": "choice", "tag": "problem_solving"},
    ],
    "Photography/Video": [
        {"id": 2, "text": "How do you track what shoots are booked, what's being edited, and what's delivered?", "type": "text", "tag": "project_pipeline"},
        {"id": 3, "text": "Do you know which clients haven't booked again, or is it out of sight, out of mind?", "type": "choice", "tag": "retention"},
        {"id": 4, "text": "When you're juggling multiple projects, what slips through the cracks?", "type": "choice", "tag": "bottlenecks"},
    ],
    "Real Estate": [
        {"id": 2, "text": "How do you track your listings and leads right now?", "type": "text", "tag": "pipeline"},
        {"id": 3, "text": "Do you know which leads have gone cold, or is it hard to tell?", "type": "choice", "tag": "lead_tracking"},
        {"id": 4, "text": "When a deal falls through, do you usually know why?", "type": "choice", "tag": "deal_loss"},
    ],
    "Other": [
        {"id": 2, "text": "How do you keep track of your customers and revenue right now?", "type": "text", "tag": "tracking"},
        {"id": 3, "text": "When a customer comes back, do you know when they were last in, or is it a guess?", "type": "choice", "tag": "retention"},
        {"id": 4, "text": "When something goes wrong, do you usually already know why, or are you guessing?", "type": "choice", "tag": "problem_solving"},
    ],
}

# Q1 is always the business type question
Q1 = {"id": 1, "text": "What kind of business do you run?", "type": "choice", "tag": "business_type"}

def _get_questions_for_type(business_type: str) -> list:
    """Get the full 13-question set for a specific business type."""
    type_questions = QUESTIONS_BY_TYPE.get(business_type, QUESTIONS_BY_TYPE["Other"])
    return [Q1] + type_questions + [Q5] + UNIVERSAL_QUESTIONS

def _detect_business_type(conversation: list) -> str:
    """Detect business type from the first answer in the conversation."""
    if not conversation:
        return "Other"
    first_answer = ""
    for msg in conversation:
        if msg["role"] == "user":
            first_answer = msg["content"].lower()
            break
    for btype in BUSINESS_TYPES:
        if btype.lower() in first_answer:
            return btype
    # Fuzzy match — use word-boundary matching to avoid false positives (V3-6).
    # E.g. 'shop' should not match 'photoshop', 'detail' should not match 'details'.
    if re.search(r'\b(restaurant|cafe|food)\b', first_answer):
        return "Restaurant/Cafe"
    if re.search(r'\b(salon|barber|spa)\b', first_answer):
        return "Salon/Spa/Barber"
    if re.search(r'\b(plumb|electric|hvac)\w*\b', first_answer):
        return "Plumber/Electrician/HVAC"
    if re.search(r'\b(retail|boutique)\b', first_answer) or re.search(r'\b(shop|store)\b', first_answer):
        return "Retail/Boutique"
    if re.search(r'\b(gym|fitness|studio)\b', first_answer):
        return "Gym/Fitness Studio"
    if re.search(r'\b(landscap|lawn)\w*\b', first_answer):
        return "Landscaping/Lawn Care"
    if re.search(r'\b(auto|repair|detail)\w*\b', first_answer) or re.search(r'\b(auto shop|repair shop)\b', first_answer):
        return "Auto Repair/Detailing"
    if re.search(r'\bclean\w*\b', first_answer):
        return "Cleaning Service"
    if re.search(r'\b(photo|video)\w*\b', first_answer):
        return "Photography/Video"
    if re.search(r'\b(real estate|realty)\b', first_answer):
        return "Real Estate"
    return "Other"


async def _detect_business_type_from_session(session_id: str) -> str:
    """Load the onboarding conversation and detect business type."""
    try:
        sess = await _load_session(session_id)
        if sess is None:
            return "Other"
        return _detect_business_type(sess.get("conversation", []))
    except Exception as e:
        logger.debug("_detect_business_type_from_session failed: %s", e)
        return "Other"


# ── Turso HTTP API persistence ───────────────────────────────────
def _turso_url():
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
    if _use_pooled_clients:
        client = _get_db_client()
        resp = await client.post(
            _turso_url(),
            json=body,
            headers={
                "Authorization": f"Bearer {TURSO_AUTH_TOKEN}",
                "Content-Type": "application/json",
            },
        )
    else:
        async with httpx.AsyncClient(timeout=30.0) as client:
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
        # Only swallow the "duplicate column" error — re-raise anything else (EDGE-4)
        if 'duplicate column' not in str(e).lower():
            raise
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
    await _turso_execute("""
    CREATE TABLE IF NOT EXISTS checkin_sessions (
        session_id TEXT PRIMARY KEY,
        messages TEXT DEFAULT '[]',
        step INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT (datetime('now'))
    )
    """)

    # ── Auth: users table ────────────────────────────────────────
    await _turso_execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """)

    # ── Auth: link survey_sessions to users via user_id column ───
    # ALTER TABLE ... ADD COLUMN is idempotent-safe: we swallow the
    # "duplicate column" error for already-migrated databases.
    try:
        await _turso_execute("ALTER TABLE survey_sessions ADD COLUMN user_id TEXT")
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            raise
        logger.debug("init_db: survey_sessions.user_id column already exists: %s", e)


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


# ── Auth: password hashing & token management ────────────────────
def _hash_password(password: str) -> str:
    """Hash a password using PBKDF2-HMAC-SHA256 with a random salt.
    Returns 'pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>'."""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def _verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password against a stored hash."""
    try:
        parts = stored_hash.split("$")
        if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
            return False
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected = bytes.fromhex(parts[3])
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


def _create_token(user_id: str) -> str:
    """Create a signed session token.
    Format: user_id:timestamp:hmac_sha256(user_id:timestamp, secret)
    All base64url-encoded for URL safety.
    """
    ts = str(int(time.time()))
    msg = f"{user_id}:{ts}"
    sig = hmac.new(AUTH_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    payload = base64.urlsafe_b64encode(msg.encode()).decode().rstrip("=")
    return f"{payload}.{sig}"


def _verify_token(token: str) -> dict | None:
    """Verify a session token. Returns {'user_id', 'issued_at'} or None.
    Tokens expire after _TOKEN_TTL_SECONDS.
    """
    if not token or "." not in token:
        return None
    parts = token.split(".", 1)
    if len(parts) != 2:
        return None
    payload_b64, sig = parts
    # Restore padding
    padding = 4 - (len(payload_b64) % 4)
    if padding != 4:
        payload_b64 += "=" * padding
    try:
        msg = base64.urlsafe_b64decode(payload_b64).decode()
    except Exception:
        return None
    expected_sig = hmac.new(AUTH_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None
    # Parse user_id:timestamp
    if ":" not in msg:
        return None
    user_id, ts_str = msg.rsplit(":", 1)
    try:
        issued_at = int(ts_str)
    except ValueError:
        return None
    # Check expiry
    if time.time() - issued_at > _TOKEN_TTL_SECONDS:
        return None
    return {"user_id": user_id, "issued_at": issued_at}


async def _get_user_by_email(email: str) -> dict | None:
    """Look up a user by email. Returns row dict or None."""
    rows = await _turso_query(
        "SELECT user_id, email, password_hash, created_at FROM users WHERE email=?",
        [{"type": "text", "value": email.lower().strip()}]
    )
    if rows:
        return rows[0]
    return None


async def _get_user_by_id(user_id: str) -> dict | None:
    """Look up a user by user_id. Returns row dict or None."""
    rows = await _turso_query(
        "SELECT user_id, email, created_at FROM users WHERE user_id=?",
        [{"type": "text", "value": user_id}]
    )
    if rows:
        return rows[0]
    return None


async def _link_session_to_user(session_id: str, user_id: str):
    """Associate a survey_session with the authenticated user."""
    try:
        await _turso_execute(
            "UPDATE survey_sessions SET user_id=? WHERE session_id=?",
            [
                {"type": "text", "value": user_id},
                {"type": "text", "value": session_id},
            ]
        )
    except Exception as e:
        logger.debug("_link_session_to_user failed: %s", e)


# ── Auth: dependency for protected endpoints ─────────────────────
# Public endpoints that do NOT require auth
_PUBLIC_PATHS = {
    "/api/health",
    "/api/auth/register",
    "/api/auth/login",
    "/api/survey/questions",
}


def _is_public_path(path: str) -> bool:
    """Check if a path is public (no auth required)."""
    if path in _PUBLIC_PATHS:
        return True
    # /api/survey/reset is auth-required when it's a POST, but we allow
    # it as a public path only for GET (which doesn't exist). POST reset
    # will be handled by the dependency check below.
    if path == "/api/survey/reset":
        return False
    return False


async def _auth_dependency(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> dict:
    """FastAPI dependency: validates the Bearer token and returns user info.
    Raises 401 if the token is missing or invalid.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required. Provide a Bearer token.")
    token = authorization[7:]  # strip "Bearer "
    token_data = _verify_token(token)
    if token_data is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    user = await _get_user_by_id(token_data["user_id"])
    if user is None:
        raise HTTPException(status_code=401, detail="User not found.")
    return {
        "user_id": user["user_id"],
        "email": user["email"],
        "created_at": user.get("created_at"),
    }


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Middleware that enforces auth on protected API endpoints.
    Public endpoints (health, auth, questions) bypass auth.
    Protected survey endpoints require a valid Bearer token.
    """
    path = request.url.path

    # Only enforce auth on /api/ paths
    if not path.startswith("/api/"):
        response = await call_next(request)
        return response

    # Public endpoints skip auth
    if _is_public_path(path):
        response = await call_next(request)
        return response

    # All other /api/ endpoints require auth
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(
            {"detail": "Authentication required. Provide a Bearer token."},
            status_code=401,
        )
    token = auth_header[7:]
    token_data = _verify_token(token)
    if token_data is None:
        return JSONResponse(
            {"detail": "Invalid or expired token."},
            status_code=401,
        )
    # Attach user_id to request state for downstream handlers
    request.state.user_id = token_data["user_id"]
    response = await call_next(request)
    return response


def _get_state(sess: dict) -> dict:
    active_questions = _get_questions_for_type(_detect_business_type(sess.get("conversation", [])))
    return {
        "q_index": sess["q_index"],
        "total_questions": len(active_questions),
        "probe_count": sess["probe_count"],
        "conversation": sess["conversation"],
        "current_question": active_questions[sess["q_index"]] if sess["q_index"] < len(active_questions) else None,
    }


# ── Daily check-in helpers ───────────────────────────────────────
async def _has_completed_onboarding(session_id: str) -> bool:
    """Check if this session has completed the onboarding survey."""
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


# ── Check-in system prompt ───────────────────────────────────────
CHECKIN_QUESTIONS_BY_TYPE = {
    "Restaurant/Cafe": [
        "Ask how their day is going. Keep it casual.",
        "Ask what's on their plate today — what's the main thing they're dealing with?",
        "Ask if anything is stressing them out right now. If they mentioned stress last time, ask how that went.",
        "Ask if they had any wins since last time — anything go well?",
        "Wrap up naturally. Tell them you've noted their priorities and the dashboard is ready.",
    ],
    "Salon/Spa/Barber": [
        "Ask how their day is going. Keep it casual.",
        "Ask how bookings are looking today — busy, slow, or about right?",
        "Ask if they've had any no-shows or cancellations today.",
        "Ask if they had any wins since last time — great clients, good feedback?",
        "Wrap up naturally. Tell them you've noted their priorities and the dashboard is ready.",
    ],
    "Plumber/Electrician/HVAC": [
        "Ask how their day is going. Keep it casual.",
        "Ask what jobs they've got on today — anything tricky?",
        "Ask if anything is stressing them out — parts delays, customer callbacks?",
        "Ask if they had any wins since last time — jobs completed, good referrals?",
        "Wrap up naturally. Tell them you've noted their priorities and the dashboard is ready.",
    ],
    "Retail/Boutique": [
        "Ask how their day is going. Keep it casual.",
        "Ask how foot traffic has been today — busy or quiet?",
        "Ask if anything is stressing them out — stock issues, slow sellers?",
        "Ask if they had any wins since last time — good sales, new customers?",
        "Wrap up naturally. Tell them you've noted their priorities and the dashboard is ready.",
    ],
    "Gym/Fitness Studio": [
        "Ask how their day is going. Keep it casual.",
        "Ask how class attendance has been today — full or quiet?",
        "Ask if anything is stressing them out — member cancellations, staffing?",
        "Ask if they had any wins since last time — new signups, great classes?",
        "Wrap up naturally. Tell them you've noted their priorities and the dashboard is ready.",
    ],
    "Landscaping/Lawn Care": [
        "Ask how their day is going. Keep it casual.",
        "Ask how many jobs are on the schedule today — is it a full day?",
        "Ask if anything is stressing them out — weather, equipment, client issues?",
        "Ask if they had any wins since last time — new clients, referrals, good feedback?",
        "Wrap up naturally. Tell them you've noted their priorities and the dashboard is ready.",
    ],
    "Auto Repair/Detailing": [
        "Ask how their day is going. Keep it casual.",
        "Ask how many cars are in the shop today — what stage are they at?",
        "Ask if anything is stressing them out — parts delays, difficult customers, backlog?",
        "Ask if they had any wins since last time — big jobs done, repeat customers, good reviews?",
        "Wrap up naturally. Tell them you've noted their priorities and the dashboard is ready.",
    ],
    "Cleaning Service": [
        "Ask how their day is going. Keep it casual.",
        "Ask how many jobs they have today — is it a packed schedule?",
        "Ask if anything is stressing them out — staffing, supplies, last-minute add-ons?",
        "Ask if they had any wins since last time — new clients, referrals, good feedback?",
        "Wrap up naturally. Tell them you've noted their priorities and the dashboard is ready.",
    ],
    "Photography/Video": [
        "Ask how their day is going. Keep it casual.",
        "Ask what they're working on today — shoots, editing, client deliverables?",
        "Ask if anything is stressing them out — deadlines, client communication, backlog?",
        "Ask if they had any wins since last time — new bookings, great shots, happy clients?",
        "Wrap up naturally. Tell them you've noted their priorities and the dashboard is ready.",
    ],
    "Real Estate": [
        "Ask how their day is going. Keep it casual.",
        "Ask what's on their plate today — showings, listings, client calls?",
        "Ask if anything is stressing them out — deals falling through, slow market, leads going cold?",
        "Ask if they had any wins since last time — new listings, closed deals, good referrals?",
        "Wrap up naturally. Tell them you've noted their priorities and the dashboard is ready.",
    ],
    "Other": [
        "Ask how their day is going. Keep it casual.",
        "Ask what's on their plate today — what's the main thing they're dealing with?",
        "Ask if anything is stressing them out right now. If they mentioned stress last time, ask how that went.",
        "Ask if they had any wins since last time — anything go well?",
        "Wrap up naturally. Tell them you've noted their priorities and the dashboard is ready.",
    ],
}

DEFAULT_CHECKIN = [
    "Ask how their day is going. Keep it casual.",
    "Ask what's on their plate today — what's the main thing they're dealing with?",
    "Ask if anything is stressing them out right now. If they mentioned stress last time, ask how that went.",
    "Ask if they had any wins since last time — anything go well?",
    "Wrap up naturally. Tell them you've noted their priorities and the dashboard is ready.",
]


async def _build_checkin_prompt(session_id: str, conversation: list, checkin_step: int) -> str:
    # Load business profile (behavioral) for context
    profile = await _load_profile(session_id)
    profile_section = ""
    if profile:
        if len(profile) > 800:
            profile = profile[-800:]
        profile_section = f"\nBEHAVIORAL PROFILE (how this person communicates and thinks):\n{profile}\n"

    # Load onboarding answers for context
    sess_rows = await _turso_query(
        "SELECT conversation FROM survey_sessions WHERE session_id=?",
        [{"type": "text", "value": session_id}]
    )
    onboarding_summary = ""
    if sess_rows and sess_rows[0].get("conversation"):
        conv = json.loads(sess_rows[0]["conversation"])
        answers = [m["content"] for m in conv if m["role"] == "user"]
        onboarding_summary = "What they told us during onboarding:\n" + "\n".join(f"- {a[:100]}" for a in answers[:5]) + "\n"

    # Load last check-in for continuity
    last_checkin = await _get_latest_checkin(session_id)
    last_checkin_section = ""
    if last_checkin:
        last_stress = last_checkin["stress_points"]
        last_wins = last_checkin["wins"]
        if last_stress:
            last_checkin_section += f"\nLast check-in stress points: {', '.join(last_stress)}\n"
        if last_wins:
            last_checkin_section += f"Last check-in wins: {', '.join(last_wins)}\n"
        last_checkin_section += f"(Last check-in was on {last_checkin['created_at']})\n"

    # Business-type-specific check-in questions (defined at module level)
    # Reuse the onboarding conversation already loaded above (avoid a redundant
    # _load_session / Turso round-trip — V3-1/V3-3).
    business_type = _detect_business_type(conv) if conv else "Other"

    CHECKIN_QUESTIONS = CHECKIN_QUESTIONS_BY_TYPE.get(business_type, DEFAULT_CHECKIN)

    current_q = CHECKIN_QUESTIONS[min(checkin_step, len(CHECKIN_QUESTIONS) - 1)]

    return f"""You are doing a quick daily check-in with a small business owner. This is NOT the onboarding survey — they already did that. This is a 2-minute conversation to understand what's on their mind today.

RULES:
1. Be casual and warm. Like a friend checking in, not a survey.
2. Keep your responses SHORT — one or two sentences max.
3. React to what they say before moving on.
4. If they mention something stressing them, acknowledge it.
5. Reference previous check-ins if relevant (e.g., "How did that staffing thing work out?").
6. Don't ask more than one question at a time.

{profile_section}

{onboarding_summary}

{last_checkin_section}

CURRENT CHECK-IN STEP {checkin_step}:
{current_q}

Respond naturally. One reaction + one question. Keep it real."""


class ChatRequest(BaseModel):
    answer: str
    session_id: str

    @field_validator("answer")
    @classmethod
    def validate_answer(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("answer must not be empty")
        if len(v) > 5000:
            raise ValueError("answer must be 5000 characters or fewer")
        return v

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, v: str) -> str:
        if not v:
            raise ValueError("session_id must not be empty")
        if len(v) > 100:
            raise ValueError("session_id must be 100 characters or fewer")
        if not re.match(r"^[a-zA-Z0-9_-]+$", v):
            raise ValueError("session_id may only contain alphanumeric characters, hyphens, and underscores")
        return v


def _validate_session_id_param(session_id: str) -> str:
    """Validate session_id query parameter. Returns validated id or raises HTTPException(422)."""
    if not session_id:
        raise HTTPException(status_code=422, detail="session_id must not be empty")
    if len(session_id) > 100:
        raise HTTPException(status_code=422, detail="session_id must be 100 characters or fewer")
    if not re.match(r"^[a-zA-Z0-9_-]+$", session_id):
        raise HTTPException(status_code=422, detail="session_id may only contain alphanumeric characters, hyphens, and underscores")
    return session_id


# ── Rate limiter (in-memory, per session_id) ─────────────────────
_RATE_LIMIT_WINDOW = 60          # seconds
_RATE_LIMIT_MAX_REQUESTS = 20   # per window
_rate_limit_store: dict[str, list[float]] = {}
_rate_limit_lock = asyncio.Lock()


async def _check_rate_limit(session_id: str) -> bool:
    """Return True if request is within the rate limit, False if exceeded."""
    now = time.time()
    window_start = now - _RATE_LIMIT_WINDOW

    expired_keys: list[str] = []
    async with _rate_limit_lock:
        # Get or create the timestamp list for this session
        timestamps = _rate_limit_store.get(session_id, [])

        # Drop timestamps outside the window
        timestamps = [t for t in timestamps if t > window_start]

        if len(timestamps) >= _RATE_LIMIT_MAX_REQUESTS:
            _rate_limit_store[session_id] = timestamps  # still update cleaned list
            return False

        timestamps.append(now)
        _rate_limit_store[session_id] = timestamps

        # Collect expired keys to clean OUTSIDE the lock (V3-2)
        if len(_rate_limit_store) > 1000:
            expired_keys = [k for k, ts in _rate_limit_store.items()
                           if not any(t > window_start for t in ts)]

    # Delete expired keys outside the lock to avoid O(N) cleanup under lock
    for k in expired_keys:
        async with _rate_limit_lock:
            # Re-check under lock to avoid deleting a key that was just updated
            ts_list = _rate_limit_store.get(k, [])
            if ts_list and not any(t > window_start for t in ts_list):
                del _rate_limit_store[k]

    return True


# ── Profile / behavioral analysis ─────────────────────────────────
def _build_analysis_prompt(question_text: str, answer: str, existing_profile: str) -> list[dict]:
    sys_msg = (
        "You are a behavioral analyst. You analyze survey responses to build a running "
        "profile of the respondent — how they learn, how they perceive things, and how they function. "
        "This profile will be passed to other AI agents who will use it to tailor their interactions with this person.\n\n"
        "Analyze the respondent's answer for signals about:\n"
        "- Communication style: formal/casual, verbose/terse, abstract/concrete\n"
        "- Learning style: visual, auditory, kinesthetic, reading/writing; does he learn by doing, by seeing, by hearing?\n"
        "- Decision-making: intuitive vs analytical, gut vs data-driven, fast vs deliberate\n"
        "- Tech comfort: what tools he uses naturally, what he avoids, how he interacts with screens\n"
        "- Trust patterns: who/what he trusts, what makes him skeptical, what builds credibility\n"
        "- Emotional patterns: what excites him, what frustrates him, what he's proud of, what worries him\n"
        "- Work style: organized vs ad-hoc, proactive vs reactive, delegation vs hands-on\n"
        "- Perception: how he frames problems, what he notices first, what he overlooks\n\n"
        "The current profile so far is below. If it's empty, this is the first answer. "
        "Update it with any NEW findings from this answer. Don't repeat what's already there. "
        "If an answer doesn't reveal anything new, just return the existing profile unchanged.\n\n"
        "Respond with the FULL updated profile in markdown format. Use these sections:\n"
        "## Communication Style\n## Learning Style\n## Decision-Making\n## Tech Comfort\n"
        "## Trust Patterns\n## Emotional Patterns\n## Work Style\n## Perception\n"
        "## Key Findings for Other Agents\n\n"
        "Keep each section to 1-3 bullet points. Be specific — cite what he actually said. "
        "The last section 'Key Findings for Other Agents' should be practical, actionable notes "
        "that another AI agent could use to tailor how it interacts with this person."
    )
    user_msg = (
        f"QUESTION ASKED: {question_text}\n\n"
        f"ANSWER: {answer}\n\n"
    )
    if existing_profile:
        user_msg += f"EXISTING PROFILE:\n{existing_profile}\n\n"
        user_msg += "Update the profile with any new findings. Return the FULL updated profile."
    else:
        user_msg += "This is the first answer. Start the profile from scratch."

    return [
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": user_msg},
    ]


def _extract_llm_content(data: dict) -> str:
    """Extract text content from an LLM chat completion response.
    Falls back to 'reasoning' if 'content' is empty."""
    msg = data["choices"][0]["message"]
    return msg.get("content") or msg.get("reasoning") or ""


def _extract_json_from_llm(content: str) -> dict | None:
    """Extract and parse a JSON object from LLM response text.
    Returns None if no JSON found or parse fails."""
    json_match = re.search(r'\{.*\}', content, re.DOTALL)
    if not json_match:
        return None
    try:
        return json.loads(json_match.group())
    except json.JSONDecodeError:
        return None


async def _spur_chat_completion(messages, model, temperature=0.6, max_tokens=1000, stream=False, timeout=30.0) -> httpx.Response:
    """Send a chat completion request to the SPUR API. Returns the httpx.Response."""
    if _use_pooled_clients:
        client = _get_llm_client()
        resp = await client.post(
            f"{SPUR_API_BASE}/chat/completions",
            json={
                "model": model,
                "messages": messages,
                "stream": stream,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            headers={
                "Authorization": f"Bearer {SPUR_DEMO_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
    else:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{SPUR_API_BASE}/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": stream,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                headers={
                    "Authorization": f"Bearer {SPUR_DEMO_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
    return resp


def _append_recent_context(messages: list[dict], conversation: list[dict], window: int = 4) -> list[dict]:
    """Append the last N messages from conversation to messages list."""
    recent = conversation[-window:]
    for msg in recent:
        if msg["role"] == "user":
            messages.append({"role": "user", "content": msg["content"]})
        else:
            messages.append({"role": "assistant", "content": msg["content"]})
    return messages


async def _run_analysis(question_text: str, answer: str, session_id: str):
    """Run behavioral analysis and save to DB. Best-effort, non-blocking."""
    try:
        existing = await _load_profile(session_id)
        messages = _build_analysis_prompt(question_text, answer, existing)
        resp = await _spur_chat_completion(messages, ANALYSIS_MODEL, temperature=0.3, max_tokens=600)
        if resp.status_code != 200:
            return
        data = resp.json()
        content = _extract_llm_content(data)
        if content:
            await _save_profile(session_id, content.strip())
    except Exception as e:
        logger.debug("_run_analysis failed: %s", e)  # analysis is best-effort, don't block the survey


# ── Email transcript on survey completion ───────────────────────
async def _send_transcript_email(sess: dict):
    """Send the survey transcript via email. Best-effort."""
    # V3-12: don't send if email is not configured — no hardcoded fallbacks
    if not EMAIL_TO or not SMTP_USER:
        logger.info("Email not sent - EMAIL_TO or SMTP_USER not configured")
        return
    try:
        conv = sess["conversation"]
        # Build readable transcript
        lines = ["BUSINESS SURVEY — TRANSCRIPT", "=" * 40, ""]
        for msg in conv:
            if msg["role"] == "assistant":
                lines.append(f"AI: {msg['content']}")
            else:
                lines.append(f"Owner: {msg['content']}")
            lines.append("")

        # Attach behavioral profile if exists
        profile = await _load_profile(sess["session_id"])
        if profile:
            lines.append("=" * 40)
            lines.append("BEHAVIORAL PROFILE")
            lines.append("=" * 40)
            lines.append(profile)

        transcript_text = "\n".join(lines)

        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = EMAIL_TO
        msg["Subject"] = f"Business Survey — Session Complete ({sess['session_id'][:12]})"

        msg.attach(MIMEText(transcript_text, "plain"))

        # Run synchronous SMTP in a thread to avoid blocking the event loop
        await asyncio.to_thread(_send_email_sync, msg)
    except Exception as e:
        logger.debug("_send_transcript_email failed: %s", e)  # best-effort — don't break the survey


def _send_email_sync(msg: MIMEMultipart):
    """Synchronous SMTP send helper (called via asyncio.to_thread)."""
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())


# ── System prompt builder ────────────────────────────────────────
async def _build_system_prompt(sess: dict, answered_q_id: int, answered_q_text: str, target_q_index: int) -> str:
    # Detect business type and get the right questions
    business_type = _detect_business_type(sess.get("conversation", []))
    active_questions = _get_questions_for_type(business_type)

    target_q = active_questions[target_q_index] if target_q_index < len(active_questions) else None

    if not target_q:
        return (
            "You are conducting a conversational survey with a small business owner. "
            "The survey is now complete. Thank them naturally and say something genuine about what they shared."
        )

    target_q_text = target_q["text"]
    target_q_type = target_q["type"]
    target_q_id = target_q["id"]

    if target_q_type == "choice":
        choice_instruction = (
            f"\nQuestion #{target_q_id} is a multiple-choice question. "
            f'The topic is: "{target_q_text}"\n'
            "Generate 3-5 answer choices natural to how the business owner has been talking. "
            "Make them specific and concrete. Mix in a 'something else' or 'not sure' option.\n"
            "IMPORTANT: Do NOT say the choices out loud. Just ask the question naturally. "
            "After your response, on a SEPARATE line at the very end, put ONLY:\n"
            "CHOICES: [option 1] | [option 2] | [option 3]\n"
            "This line is invisible to the user."
        )
    else:
        choice_instruction = ""

    # Load behavioral profile if it exists
    profile = await _load_profile(sess["session_id"])
    profile_section = ""
    if profile:
        if len(profile) > 1500:
            profile = profile[-800:]
        profile_section = (
            "\nBEHAVIORAL PROFILE (what you've learned about the business owner so far — adapt your questioning style accordingly):\n"
            f"{profile}\n"
        )

    # Build list of questions already asked
    asked_questions = []
    for i in range(target_q_index):
        if i < len(active_questions):
            asked_questions.append(f"Q{active_questions[i]['id']}: {active_questions[i]['text']}")
    asked_list = "\n".join(asked_questions) if asked_questions else "None yet"

    return (
        f"""You are conducting a conversational survey with a small business owner ({business_type}). You're having a real conversation — one question at a time, react to their answers like a normal person would, then move on.

CRITICAL RULES:
1. You are NOT a robot. React to what he says. "Got it." "That makes sense." "Honestly, that's smart." Be real but brief — one short sentence max.
2. Keep everything SHORT. Your reaction + the next question should be 2-3 sentences total. This is a conversation, not an essay.
3. When asking the next question, don't just read it verbatim. Rephrase it naturally to fit the conversation. Keep the meaning, change the words.
4. NEVER repeat a question you've already asked. The questions already covered are listed below. Do NOT ask about those topics again.

QUESTIONS ALREADY ASKED (do NOT repeat these):
{asked_list}

CURRENT STATE:
- Just answered question #{answered_q_id}: {answered_q_text}
- You must now ask question #{target_q_id}: {target_q_text}
- Question type: {target_q_type}

Respond as plain text. Just the reaction and the question. No markers, no JSON, no formatting.
{choice_instruction}
{profile_section}"""
    )


def _parse_response(text: str) -> tuple[str, list[str]]:
    """Extract CHOICES marker from AI response. Returns (clean_text, choices_list)."""
    choices = []
    choices_match = re.search(r'^CHOICES:\s*(.+)', text, re.MULTILINE | re.IGNORECASE)
    if choices_match:
        choices_str = choices_match.group(1).strip()
        choices = [c.strip() for c in choices_str.split('|') if c.strip()]

    clean_text = re.sub(r'^CHOICES:\s*.+', '', text, flags=re.MULTILINE | re.IGNORECASE)
    clean_text = re.sub(r'\n{3,}', '\n\n', clean_text).strip()
    return clean_text, choices


# ── Shared SSE LLM streaming helper ──────────────────────────────
async def _stream_llm_response(messages: list[dict], model: str, max_tokens: int):
    """Stream an LLM chat completion via SSE.

    Yields SSE ``data:`` lines containing JSON payloads:
      - ``{"content": "<chunk>"}`` for each streamed token
      - ``{"error": "<message>"}`` on failure

    Implements the reasoning-model fallback: if streaming yields no
    ``content`` deltas (some models only emit ``reasoning``), retries
    as a non-streaming request and emits the full text in 3-char chunks.
    """
    if not SPUR_DEMO_API_KEY:
        yield f"data: {json.dumps({'error': 'No API key configured'})}\n\n"
        return

    try:
        if _use_pooled_clients:
            client = _get_llm_client()
            _ctx = client.stream(
                "POST",
                f"{SPUR_API_BASE}/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": True,
                    "temperature": 0.6,
                    "max_tokens": max_tokens,
                },
                headers={
                    "Authorization": f"Bearer {SPUR_DEMO_API_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(90.0),
            )
        else:
            client = httpx.AsyncClient(timeout=httpx.Timeout(90.0))
            await client.__aenter__()
            _ctx = client.stream(
                "POST",
                f"{SPUR_API_BASE}/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": True,
                    "temperature": 0.6,
                    "max_tokens": max_tokens,
                },
                headers={
                    "Authorization": f"Bearer {SPUR_DEMO_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
        async with _ctx as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield f"data: {json.dumps({'error': body.decode(errors='replace')[:200]})}\n\n"
                    return  # can't return value from async generator

                got_content = False
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    if line.strip() == "data: [DONE]":
                        break
                    try:
                        chunk = json.loads(line[6:])
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content")
                        if content:
                            got_content = True
                            yield f"data: {json.dumps({'content': content})}\n\n"
                    except (json.JSONDecodeError, IndexError):
                        continue

                # Edge case: reasoning model returned only reasoning, no content
                if not got_content:
                    resp2 = await _spur_chat_completion(messages, model, max_tokens=max_tokens, timeout=90.0)
                    if resp2.status_code == 200:
                        fallback_text = _extract_llm_content(resp2.json())
                        if fallback_text:
                            for i in range(0, len(fallback_text), 3):
                                yield f"data: {json.dumps({'content': fallback_text[i:i+3]})}\n\n"

    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)[:200]})}\n\n"


# ── Auth endpoints ────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not v or "@" not in v or "." not in v:
            raise ValueError("A valid email address is required")
        if len(v) > 254:
            raise ValueError("Email must be 254 characters or fewer")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if not v or len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if len(v) > 128:
            raise ValueError("Password must be 128 characters or fewer")
        return v


class LoginRequest(BaseModel):
    email: str
    password: str


@app.post("/api/auth/register")
async def register(req: RegisterRequest):
    """Register a new user. Returns a session token."""
    # Check if email is already registered
    existing = await _get_user_by_email(req.email)
    if existing is not None:
        return JSONResponse(
            {"detail": "An account with this email already exists. Try logging in."},
            status_code=409,
        )

    user_id = "u-" + secrets.token_hex(12)
    password_hash = _hash_password(req.password)

    try:
        await _turso_execute(
            """INSERT INTO users (user_id, email, password_hash) VALUES (?, ?, ?)""",
            [
                {"type": "text", "value": user_id},
                {"type": "text", "value": req.email},
                {"type": "text", "value": password_hash},
            ]
        )
    except Exception as e:
        if "unique" in str(e).lower():
            return JSONResponse(
                {"detail": "An account with this email already exists. Try logging in."},
                status_code=409,
            )
        raise

    token = _create_token(user_id)
    return {
        "token": token,
        "user": {"user_id": user_id, "email": req.email},
    }


@app.post("/api/auth/login")
async def login(req: LoginRequest):
    """Log in an existing user. Returns a session token."""
    email = req.email.strip().lower()
    user = await _get_user_by_email(email)
    if user is None:
        return JSONResponse(
            {"detail": "Invalid email or password."},
            status_code=401,
        )

    if not _verify_password(req.password, user.get("password_hash", "")):
        return JSONResponse(
            {"detail": "Invalid email or password."},
            status_code=401,
        )

    token = _create_token(user["user_id"])
    return {
        "token": token,
        "user": {"user_id": user["user_id"], "email": user["email"]},
    }


@app.get("/api/auth/me")
async def get_me(user: dict = Depends(_auth_dependency)):
    """Return the current user's info from the token."""
    return {"user": user}


@app.post("/api/survey/chat")
async def chat(req: ChatRequest, request: Request):
    if not await _check_rate_limit(req.session_id):
        return JSONResponse(
            {"error": "Rate limit exceeded. Please slow down."},
            status_code=429,
        )

    sess = await _load_or_create_session(req.session_id)

    # Link this session to the authenticated user (from auth middleware)
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        await _link_session_to_user(req.session_id, user_id)

    # Detect business type and get the right questions for this session
    business_type = _detect_business_type(sess.get("conversation", []))
    active_questions = _get_questions_for_type(business_type)

    if sess["q_index"] >= len(active_questions):
        return JSONResponse({"done": True, "message": "Survey already complete."})

    current_q = active_questions[sess["q_index"]]
    current_q_text = current_q["text"]

    sess["conversation"].append({"role": "user", "content": req.answer})

    # Fire behavioral analysis in the background (V3-7: retain task reference)
    task = asyncio.create_task(_run_analysis(current_q_text, req.answer, sess["session_id"]))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    answered_q_id = current_q["id"]
    answered_q_text = current_q_text
    target_q_index = sess["q_index"] + 1

    system_prompt = await _build_system_prompt(sess, answered_q_id, answered_q_text, target_q_index)
    messages = [{"role": "system", "content": system_prompt}]

    # Send last 4 messages for context
    _append_recent_context(messages, sess["conversation"], window=4)

    if len(sess["conversation"]) == 1:
        first_q = active_questions[0]
        messages.insert(1, {"role": "assistant", "content": first_q["text"]})

    # Tell the AI to react + ask the next question (rephrased)
    if target_q_index < len(active_questions):
        target_q = active_questions[target_q_index]
        messages.append({"role": "user", "content": (
            f"React to my answer in one short sentence, then ask me this survey question "
            f"(rephrase it naturally to fit our conversation — keep the meaning, change the words):\n"
            f"\"{target_q['text']}\""
        )})

    async def sse_stream():
        # Stream the LLM response via the shared helper
        gen = _stream_llm_response(messages, SURVEY_MODEL, max_tokens=1200)
        full_response = ""
        async for chunk in gen:
            if chunk.startswith("data: {") and '"content"' in chunk:
                try:
                    full_response += json.loads(chunk[6:])["content"]
                except (json.JSONDecodeError, KeyError):
                    pass  # V3-5: malformed JSON that passes startswith check
            yield chunk

        # Parse choices from the response
        clean_text, choices = _parse_response(full_response)

        # ALWAYS advance
        sess["q_index"] = target_q_index
        sess["probe_count"] = 0

        sess["conversation"].append({"role": "assistant", "content": clean_text})

        # Save to Turso via HTTP API
        try:
            await _save_session(sess)
        except Exception as save_err:
            yield f"data: {json.dumps({'error': f'Save failed: {str(save_err)[:200]}'})}\n\n"

        state = _get_state(sess)
        # Use total_questions from state (already computed) instead of
        # re-detecting business type and re-querying question count (V3-9)
        is_done = sess['q_index'] >= state['total_questions']
        yield f"data: {json.dumps({'state': state, 'choices': choices, 'done': is_done})}\n\n"

        # Send transcript email when survey completes
        if is_done:
            try:
                await _send_transcript_email(sess)
            except Exception as e:
                logger.debug("send_transcript_email failed: %s", e)
            # Run card selection engine in background (V3-7: retain task reference)
            try:
                task = asyncio.create_task(_run_card_selection(sess["session_id"]))
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
            except Exception as e:
                logger.debug("create_task _run_card_selection failed: %s", e)

        yield "data: [DONE]\n\n"

    return StreamingResponse(sse_stream(), media_type="text/event-stream")


# ── Card selection engine ────────────────────────────────────────
AVAILABLE_CARDS = [
    {"id": "sales", "name": "Sales Tracker", "description": "Daily revenue, POS summary, trend vs yesterday"},
    {"id": "reviews", "name": "Review Monitor", "description": "Google/TripAdvisor reviews, sentiment, pending replies"},
    {"id": "social", "name": "Social Pulse", "description": "Instagram/Facebook engagement, posting cadence"},
    {"id": "catering", "name": "Catering Pipeline", "description": "Open quotes, follow-ups, pipeline value"},
    {"id": "inventory", "name": "Inventory Tracker", "description": "Supply schedule, low stock alerts"},
    {"id": "staff", "name": "Staff & Labor", "description": "Staff costs, labor expenses, total hours"},
    {"id": "expenses", "name": "Expense Tracker", "description": "Food costs, overhead, margins"},
    {"id": "checklist", "name": "Daily Checklist", "description": "Morning routine, prep list, priorities"},
    {"id": "goals", "name": "Goal Tracker", "description": "Monthly targets, progress bars"},
    {"id": "stress", "name": "Wellbeing", "description": "Daily check-in prompt, stress trends"},
    {"id": "contacts", "name": "Customer Contacts", "description": "Email/phone list builder"},
    {"id": "decisions", "name": "Decision Helper", "description": "Second opinion for recurring decisions"},
    {"id": "appointments", "name": "Appointments", "description": "Appointment calendar, no-shows, schedule gaps"},
    {"id": "pipeline", "name": "Job Pipeline", "description": "Kanban: scheduled, in-progress, completed, waiting parts"},
    {"id": "retention", "name": "Client Retention", "description": "Client return frequency, loyalty, churn risk"},
    {"id": "memberships", "name": "Memberships", "description": "Active count, MRR, churn rate, new signups"},
    {"id": "routes", "name": "Routes", "description": "Daily stops, drive time, completion rate"},
    {"id": "equipment", "name": "Equipment", "description": "Tool/equipment status, maintenance schedule"},
    {"id": "invoices", "name": "Invoices", "description": "Outstanding/paid/overdue, follow-up list"},
    {"id": "staff_schedule", "name": "Staff Schedule", "description": "Shift planning, coverage gaps, who's working today"},
]

async def _run_card_selection(session_id: str):
    """Analyze onboarding answers and determine which cards to show,
    card configurations, and UI density. Saves result to Turso."""
    try:
        # Load the full onboarding conversation
        sess = await _load_session(session_id)
        if sess is None:
            return
        conv = sess["conversation"]
        conv_text = "\n".join(
            f"{'AI' if m['role']=='assistant' else 'Owner'}: {m['content']}"
            for m in conv
        )

        # Load behavioral profile
        profile = await _load_profile(session_id)

        # Build the card list for the LLM
        card_list = "\n".join(
            f"- {c['id']}: {c['name']} — {c['description']}"
            for c in AVAILABLE_CARDS
        )

        resp = await _spur_chat_completion(
            [
                {"role": "system", "content": (
                    "You are a dashboard configuration engine. You analyze a business owner's survey answers "
                    "and determine which dashboard cards they need, how each card should be configured, "
                    "and what UI density fits their tech comfort level.\n\n"
                    "Available cards:\n" + card_list + "\n\n"
                    "Respond as JSON with this structure:\n"
                    '{"cards": [{"id": "card_id", "config": {"key": "value"}}], '
                    '"ui_density": "simple|standard|detailed", '
                    '"business_name": "extracted business name", '
                    '"business_type": "restaurant|salon|plumber|etc"}\n\n'
                    "Rules:\n"
                    "- Only include cards relevant to what they actually mentioned\n"
                    "- Always include 'checklist' and 'goals' (universal)\n"
                    "- Include 'stress' if they mentioned wanting a dashboard or feeling overwhelmed\n"
                    "- config should contain specifics they mentioned (pos system name, platform name, etc)\n"
                    "- ui_density: 'simple' if they track things in their head or are tech-averse, "
                    "'detailed' if they use spreadsheets/analytics, 'standard' otherwise\n"
                    "- business_name: extract from conversation if mentioned, otherwise 'Your Business'\n"
                    "- Respond with ONLY the JSON, no other text\n"
                    "Note: 'staff' and 'staff_schedule' are different — select 'staff' for labor costs, "
                    "'staff_schedule' for shift planning. Don't select both unless the business explicitly mentions both costs and scheduling.\n"
                )},
                {"role": "user", "content": (
                    f"Behavioral profile:\n{profile[-800:] if profile else 'None yet'}\n\n"
                    f"Survey conversation:\n{conv_text}"
                )},
            ],
            ANALYSIS_MODEL,
            temperature=0.2,
            max_tokens=800,
            timeout=45.0,
        )
        if resp.status_code != 200:
            return

        data = resp.json()
        content = _extract_llm_content(data)

        # Parse JSON from response
        result = _extract_json_from_llm(content)
        if result is None:
            return

        cards = result.get("cards", [])
        ui_density = result.get("ui_density", "standard")
        business_name = result.get("business_name", "Your Business")
        business_type = result.get("business_type", "general")

        # Save to Turso as a business profile entry
        await _save_business_profile(session_id, {
            "cards": cards,
            "ui_density": ui_density,
            "business_name": business_name,
            "business_type": business_type,
        })

        # Also save initial card priorities (same order as selected)
        initial_priorities = [c["id"] for c in cards]
        await _save_card_priorities(session_id, initial_priorities, "onboarding")

    except Exception as e:
        logger.debug("_run_card_selection failed: %s", e)


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


# ── Check-in analysis (extract stress points, wins, priorities) ──
async def _run_checkin_analysis(session_id: str, conversation: list):
    """Analyze check-in conversation for stress points, wins, and card priorities."""
    try:
        conv_text = "\n".join(f"{'AI' if m['role']=='assistant' else 'Owner'}: {m['content']}" for m in conversation)
        resp = await _spur_chat_completion(
            [
                {"role": "system", "content": (
                    "You analyze a daily check-in conversation with a small business owner. "
                    "Extract: stress_points (things worrying them), wins (things going well), "
                    "priorities (card IDs to surface, in order of importance), and a one-sentence summary. "
                    "Available card IDs: " + ", ".join(c["id"] for c in AVAILABLE_CARDS) + ". "
                    "Respond as JSON: {\"stress_points\": [...], \"wins\": [...], \"priorities\": [...], \"summary\": \"one sentence here\"} "
                    "Only include priorities that are relevant to what they said. "
                    "If nothing stressed them, stress_points is empty. If no wins, wins is empty. "
                    "The summary should be a single natural sentence capturing the overall mood and focus of today's check-in. "
                    "Example: 'Staffing shortages are the main concern today, but the new Reuben special is selling well.'"
                )},
                {"role": "user", "content": f"Check-in conversation:\n{conv_text}"},
            ],
            ANALYSIS_MODEL,
            temperature=0.2,
            max_tokens=600,
        )
        if resp.status_code != 200:
            return
        data = resp.json()
        content = _extract_llm_content(data)

        # Parse JSON from response
        result = _extract_json_from_llm(content)
        if result is not None:
            stress_points = result.get("stress_points", [])
            wins = result.get("wins", [])
            priorities = result.get("priorities", [])
            summary = result.get("summary", "")

            await _save_checkin(session_id, conversation, stress_points, wins, priorities, summary)
            if priorities:
                await _save_card_priorities(session_id, priorities, "checkin")
    except Exception as e:
        logger.debug("_run_checkin_analysis failed: %s", e)


# ── Check-in session persistence (Turso-backed) ──────────────────
async def _load_checkin_session(session_id: str) -> dict:
    """Load check-in conversation from Turso. Returns {messages, step}."""
    try:
        rows = await _turso_query(
            "SELECT messages, step FROM checkin_sessions WHERE session_id=?",
            [{"type": "text", "value": session_id}]
        )
        if rows:
            return {
                "messages": json.loads(rows[0].get("messages", "[]")),
                "step": int(rows[0].get("step", 0)),
            }
    except Exception as e:
        logger.debug("_load_checkin_session failed: %s", e)
    return {"messages": [], "step": 0}


async def _save_checkin_session(session_id: str, messages: list, step: int):
    """Save check-in conversation to Turso (survives server restart)."""
    try:
        await _turso_execute(
            """INSERT OR REPLACE INTO checkin_sessions (session_id, messages, step, updated_at)
               VALUES (?, ?, ?, datetime('now'))""",
            [
                {"type": "text", "value": session_id},
                {"type": "text", "value": json.dumps(messages)},
                {"type": "text", "value": str(step)},
            ]
        )
    except Exception as e:
        logger.debug("_save_checkin_session failed: %s", e)


async def _clear_checkin_session(session_id: str):
    """Delete check-in conversation from Turso (after completion)."""
    try:
        await _turso_execute(
            "DELETE FROM checkin_sessions WHERE session_id=?",
            [{"type": "text", "value": session_id}]
        )
    except Exception as e:
        logger.debug("_clear_checkin_session failed: %s", e)


# ── Check-in chat endpoint ──────────────────────────────────────
# Check-in conversations are now persisted in Turso (checkin_sessions table).
# No in-memory dict, no TTL, no lock needed — the database handles it all.

@app.post("/api/survey/checkin")
async def checkin_chat(req: ChatRequest, request: Request):
    """Daily check-in mode for returning users who completed onboarding."""
    if not await _check_rate_limit(req.session_id):
        return JSONResponse(
            {"error": "Rate limit exceeded. Please slow down."},
            status_code=429,
        )

    # Link this session to the authenticated user (from auth middleware)
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        await _link_session_to_user(req.session_id, user_id)

    if not await _has_completed_onboarding(req.session_id):
        return JSONResponse({"error": "Onboarding not complete", "mode": "onboarding"})

    # Load check-in conversation from Turso (survives server restart)
    checkin = await _load_checkin_session(req.session_id)
    conv_messages = checkin["messages"]
    conv_step = checkin["step"]

    # Append the user's new answer
    conv_messages.append({"role": "user", "content": req.answer})
    conv_step += 1

    # Save updated state to Turso immediately
    await _save_checkin_session(req.session_id, conv_messages, conv_step)

    # Build prompt + stream LLM response
    system_prompt = await _build_checkin_prompt(req.session_id, conv_messages, conv_step)
    messages = [{"role": "system", "content": system_prompt}]

    # Send last 4 messages of the check-in conversation
    _append_recent_context(messages, conv_messages, window=4)

    # Tell the AI to react + ask the next check-in question
    messages.append({"role": "user", "content": (
        "React to what I said, then ask your next check-in question. Keep it short and natural."
    )})

    total_steps = 5
    session_id_ref = req.session_id  # capture for closure

    async def sse_stream():
        # Stream the LLM response via the shared helper
        gen = _stream_llm_response(messages, SURVEY_MODEL, max_tokens=800)
        full_response = ""
        async for chunk in gen:
            if chunk.startswith("data: {") and '"content"' in chunk:
                try:
                    full_response += json.loads(chunk[6:])["content"]
                except (json.JSONDecodeError, KeyError):
                    pass
            yield chunk

        # Add AI response to conversation and save to Turso
        conv_messages.append({"role": "assistant", "content": full_response})
        is_done = conv_step >= total_steps

        if is_done:
            # Run analysis and clear check-in session
            await _save_checkin_session(session_id_ref, conv_messages, conv_step)
            task = asyncio.create_task(_run_checkin_analysis(session_id_ref, list(conv_messages)))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
            await _clear_checkin_session(session_id_ref)
        else:
            await _save_checkin_session(session_id_ref, conv_messages, conv_step)

        yield f"data: {json.dumps({'done': is_done, 'mode': 'checkin', 'step': conv_step, 'total_steps': total_steps})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse_stream(), media_type="text/event-stream")


@app.get("/api/survey/checkin/latest")
async def get_latest_checkin(session_id: str):
    """Get the latest daily check-in for a session."""
    session_id = _validate_session_id_param(session_id)
    checkin = await _get_latest_checkin(session_id)
    if not checkin:
        return JSONResponse({"checkin": None, "message": "No check-ins yet."})
    return checkin


@app.get("/api/survey/checkin/status")
async def get_checkin_status(session_id: str):
    """Check if the user should see onboarding or check-in mode."""
    session_id = _validate_session_id_param(session_id)
    onboarded = await _has_completed_onboarding(session_id)
    latest = await _get_latest_checkin(session_id) if onboarded else None
    priorities = await _get_card_priorities(session_id) if onboarded else []
    return {
        "mode": "checkin" if onboarded else "onboarding",
        "onboarded": onboarded,
        "has_checkin_today": latest is not None,
        "latest_checkin": latest,
        "card_priorities": priorities,
    }


@app.get("/api/survey/priorities/{session_id}")
async def get_priorities(session_id: str):
    """Get card priorities for a session."""
    session_id = _validate_session_id_param(session_id)
    return {"priorities": await _get_card_priorities(session_id)}


@app.get("/api/survey/business-profile/{session_id}")
async def get_business_profile(session_id: str):
    """Get the business profile (card selection + config + UI density)."""
    session_id = _validate_session_id_param(session_id)
    profile = await _load_business_profile(session_id)
    if not profile:
        return JSONResponse({"profile": None, "message": "No business profile yet. Complete onboarding first."})
    return profile


@app.get("/api/survey/state")
async def get_state(session_id: str):
    session_id = _validate_session_id_param(session_id)
    sess = await _load_session(session_id)
    if sess is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    return _get_state(sess)


@app.get("/api/survey/transcript")
async def get_transcript(session_id: str):
    session_id = _validate_session_id_param(session_id)
    sess = await _load_session(session_id)
    if sess is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    active_questions = _get_questions_for_type(_detect_business_type(sess.get("conversation", [])))
    return {
        "conversation": sess["conversation"],
        "q_index": sess["q_index"],
        "total_questions": len(active_questions),
        "questions": active_questions,
    }


@app.post("/api/survey/reset")
async def reset(session_id: str):
    session_id = _validate_session_id_param(session_id)
    await _reset_session(session_id)
    return {"status": "ok", "message": "Survey reset."}


@app.get("/api/survey/questions")
async def get_questions():
    return {"questions": _get_questions_for_type("Other")}


@app.get("/api/survey/profile/{session_id}")
async def get_profile(session_id: str):
    session_id = _validate_session_id_param(session_id)
    profile = await _load_profile(session_id)
    if not profile:
        return JSONResponse({"profile": None, "message": "No profile yet."})
    return PlainTextResponse(profile, media_type="text/markdown")


@app.get("/api/survey/profiles")
async def list_profiles():
    rows = await _turso_query(
        "SELECT session_id, length(profile) as size, updated_at FROM survey_profiles WHERE profile != '' ORDER BY updated_at DESC"
    )
    profiles = [{"session_id": r["session_id"], "size_bytes": r["size"], "modified": r["updated_at"]} for r in rows]
    return {"profiles": profiles}


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "service": "survey",
        "has_api_key": bool(SPUR_DEMO_API_KEY),
        "has_db": bool(TURSO_DB_URL),
    }


# Serve frontend
_frontend_dir = str(pathlib.Path(__file__).resolve().parent / "frontend")
if not os.path.isdir(_frontend_dir):
    _frontend_dir = "/app/frontend"
if os.path.isdir(_frontend_dir):
    app.mount("/", app=StaticFiles(directory=_frontend_dir, html=True), name="frontend")
