"""
SPUR Survey — Benji / Yans Deli conversational survey.

Standalone FastAPI app. Chat-style adaptive survey with SSE streaming.
- Per-browser sessions via session_id, persisted to Turso via HTTP API
- AI generates dynamic multiple-choice options based on conversation context
- Behavioral analysis runs after each answer, stored in DB
- Findings are fed back into the system prompt so the AI adapts in real-time
"""
from __future__ import annotations

import os, json, time, re
from typing import Optional
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Header, Body
from fastapi.responses import StreamingResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import pathlib

app = FastAPI(title="SPUR Survey")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

# Email settings
SMTP_HOST = os.getenv("SMTP_HOST", "mail.spuric.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "noc@spuric.com")
SMTP_PASS = os.getenv("SMTP_PASS", "")
EMAIL_TO = os.getenv("EMAIL_TO", "akif@spuric.com")

# ── The 13 questions (fixed order, AI adapts delivery and choices) ──
QUESTIONS = [
    {"id": 1, "text": "Right now, how do you keep track of all this? Reviews, money, marketing.", "type": "text", "tag": "tracking"},
    {"id": 2, "text": "When a customer says something nice, a bad review comes in, or a big catering order lands, what happens next?", "type": "text", "tag": "reactions"},
    {"id": 3, "text": "If I asked you right now how many catering orders you did last month, could you actually find that, or is it more of a guess?", "type": "choice", "tag": "catering_data"},
    {"id": 4, "text": "When something goes wrong in the shop, do you usually already know why, or are you guessing?", "type": "choice", "tag": "problem_solving"},
    {"id": 5, "text": "Would you rather this just show you what happened, or tell you what to do next?", "type": "choice", "tag": "proactive"},
    {"id": 6, "text": "If it gave you a bad suggestion once, would that turn you off the whole thing?", "type": "choice", "tag": "trust"},
    {"id": 7, "text": "Would you trust something like \"post the reuben on Thursdays\" coming from a screen?", "type": "choice", "tag": "ai_trust"},
    {"id": 8, "text": "Is there a decision you make regularly where you'd want a second opinion?", "type": "text", "tag": "second_opinion"},
    {"id": 9, "text": "What's the one thing about running the shop that's been bugging you lately?", "type": "text", "tag": "pain_point"},
    {"id": 10, "text": "Walk me through the last time you looked something up on your phone or a website. Was it easy?", "type": "text", "tag": "tech_comfort"},
    {"id": 11, "text": "When you check something like Uber Eats or Google, is it on your phone or a computer? Quick check, or do you sit down for it?", "type": "text", "tag": "habits"},
    {"id": 12, "text": "If everything — sales, reviews, money, staffing — was on one screen at once, does that help or feel like a lot?", "type": "choice", "tag": "density"},
    {"id": 13, "text": "When something's confusing on a screen, what do you usually do?", "type": "choice", "tag": "ux_reaction"},
]


# ── Turso HTTP API persistence ───────────────────────────────────
def _turso_url():
    """Convert the Turso libsql URL to the HTTP API URL."""
    url = TURSO_DB_URL
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    return url.rstrip("/") + "/v2/pipeline"


def _turso_execute(sql: str, args: list = None):
    """Execute a SQL statement via the Turso HTTP API.
    Args should be a list of dicts like {"type": "text", "value": "hello"}.
    The value is ALWAYS a string, even for integers.
    """
    stmt = {"sql": sql}
    if args:
        # Ensure all values are strings
        stmt["args"] = [{"type": a.get("type", "text"), "value": str(a["value"])} for a in args]
    body = {"requests": [{"type": "execute", "stmt": stmt}]}
    resp = httpx.post(
        _turso_url(),
        json=body,
        headers={
            "Authorization": f"Bearer {TURSO_AUTH_TOKEN}",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise Exception(f"Turso API error: {resp.status_code} {resp.text[:300]}")
    return resp.json()


def _turso_query(sql: str, args: list = None) -> list[dict]:
    """Execute a SELECT and return rows as dicts."""
    stmt = {"sql": sql}
    if args:
        stmt["args"] = [{"type": a.get("type", "text"), "value": str(a["value"])} for a in args]
    body = {"requests": [{"type": "execute", "stmt": stmt}]}
    resp = httpx.post(
        _turso_url(),
        json=body,
        headers={
            "Authorization": f"Bearer {TURSO_AUTH_TOKEN}",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise Exception(f"Turso API error: {resp.status_code} {resp.text[:300]}")
    data = resp.json()
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
                # Try to convert numeric strings back to int
                if val and val.lstrip('-').isdigit():
                    val = int(val)
                values.append(val)
            else:
                values.append(v)
        rows.append(dict(zip(cols, values)))
    return rows


def init_db():
    _turso_execute("""
    CREATE TABLE IF NOT EXISTS survey_sessions (
        session_id TEXT PRIMARY KEY,
        conversation TEXT DEFAULT '[]',
        q_index INTEGER DEFAULT 0,
        probe_count INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    )
    """)
    _turso_execute("""
    CREATE TABLE IF NOT EXISTS survey_profiles (
        session_id TEXT PRIMARY KEY,
        profile TEXT DEFAULT '',
        updated_at TEXT DEFAULT (datetime('now'))
    )
    """)
    _turso_execute("""
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
        _turso_execute("ALTER TABLE daily_checkins ADD COLUMN summary TEXT DEFAULT ''")
    except Exception:
        pass  # Column already exists
    _turso_execute("""
    CREATE TABLE IF NOT EXISTS card_priorities (
        session_id TEXT PRIMARY KEY,
        ordered_cards TEXT DEFAULT '[]',
        last_updated_by TEXT DEFAULT 'onboarding',
        updated_at TEXT DEFAULT (datetime('now'))
    )
    """)


def _load_session(session_id: str) -> dict:
    rows = _turso_query(
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
    else:
        _turso_execute(
            "INSERT INTO survey_sessions (session_id) VALUES (?)",
            [{"type": "text", "value": session_id}]
        )
        return {
            "session_id": session_id,
            "conversation": [],
            "q_index": 0,
            "probe_count": 0,
        }


def _save_session(sess: dict):
    _turso_execute(
        """INSERT OR REPLACE INTO survey_sessions (session_id, conversation, q_index, probe_count, updated_at)
           VALUES (?, ?, ?, ?, datetime('now'))""",
        [
            {"type": "text", "value": sess["session_id"]},
            {"type": "text", "value": json.dumps(sess["conversation"])},
            {"type": "integer", "value": sess["q_index"]},
            {"type": "integer", "value": sess["probe_count"]},
        ]
    )


def _reset_session(session_id: str):
    _turso_execute(
        """INSERT OR REPLACE INTO survey_sessions (session_id, conversation, q_index, probe_count, updated_at)
           VALUES (?, '[]', 0, 0, datetime('now'))""",
        [{"type": "text", "value": session_id}]
    )
    _turso_execute(
        """INSERT OR REPLACE INTO survey_profiles (session_id, profile, updated_at)
           VALUES (?, '', datetime('now'))""",
        [{"type": "text", "value": session_id}]
    )


def _load_profile(session_id: str) -> str:
    rows = _turso_query(
        "SELECT profile FROM survey_profiles WHERE session_id=?",
        [{"type": "text", "value": session_id}]
    )
    if rows and rows[0].get("profile"):
        return rows[0]["profile"]
    return ""


def _save_profile(session_id: str, content: str):
    _turso_execute(
        """INSERT OR REPLACE INTO survey_profiles (session_id, profile, updated_at)
           VALUES (?, ?, datetime('now'))""",
        [
            {"type": "text", "value": session_id},
            {"type": "text", "value": content},
        ]
    )


def _get_state(sess: dict) -> dict:
    return {
        "q_index": sess["q_index"],
        "total_questions": len(QUESTIONS),
        "probe_count": sess["probe_count"],
        "conversation": sess["conversation"],
        "current_question": QUESTIONS[sess["q_index"]] if sess["q_index"] < len(QUESTIONS) else None,
    }


# ── Daily check-in helpers ───────────────────────────────────────
def _has_completed_onboarding(session_id: str) -> bool:
    """Check if this session has completed the onboarding survey."""
    rows = _turso_query(
        "SELECT q_index FROM survey_sessions WHERE session_id=?",
        [{"type": "text", "value": session_id}]
    )
    return rows and int(rows[0].get("q_index", 0)) >= len(QUESTIONS)


def _get_latest_checkin(session_id: str) -> dict | None:
    """Get the most recent daily check-in for a session."""
    rows = _turso_query(
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


def _save_checkin(session_id: str, conversation: list, stress_points: list, wins: list, priorities: list, summary: str = ""):
    """Save a daily check-in to Turso."""
    _turso_execute(
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


def _get_card_priorities(session_id: str) -> list:
    """Get the current card priority ordering for a session."""
    rows = _turso_query(
        "SELECT ordered_cards FROM card_priorities WHERE session_id=?",
        [{"type": "text", "value": session_id}]
    )
    if rows and rows[0].get("ordered_cards"):
        return json.loads(rows[0]["ordered_cards"])
    return []


def _save_card_priorities(session_id: str, ordered_cards: list, updated_by: str = "checkin"):
    """Save or update card priorities for a session."""
    _turso_execute(
        """INSERT OR REPLACE INTO card_priorities (session_id, ordered_cards, last_updated_by, updated_at)
           VALUES (?, ?, ?, datetime('now'))""",
        [
            {"type": "text", "value": session_id},
            {"type": "text", "value": json.dumps(ordered_cards)},
            {"type": "text", "value": updated_by},
        ]
    )


# ── Check-in system prompt ───────────────────────────────────────
def _build_checkin_prompt(session_id: str, conversation: list, checkin_step: int) -> str:
    # Load business profile (behavioral) for context
    profile = _load_profile(session_id)
    profile_section = ""
    if profile:
        if len(profile) > 800:
            profile = profile[-800:]
        profile_section = f"\nBEHAVIORAL PROFILE (how this person communicates and thinks):\n{profile}\n"

    # Load onboarding answers for context
    sess_rows = _turso_query(
        "SELECT conversation FROM survey_sessions WHERE session_id=?",
        [{"type": "text", "value": session_id}]
    )
    onboarding_summary = ""
    if sess_rows and sess_rows[0].get("conversation"):
        conv = json.loads(sess_rows[0]["conversation"])
        answers = [m["content"] for m in conv if m["role"] == "user"]
        onboarding_summary = "What they told us during onboarding:\n" + "\n".join(f"- {a[:100]}" for a in answers[:5]) + "\n"

    # Load last check-in for continuity
    last_checkin = _get_latest_checkin(session_id)
    last_checkin_section = ""
    if last_checkin:
        last_stress = last_checkin["stress_points"]
        last_wins = last_checkin["wins"]
        if last_stress:
            last_checkin_section += f"\nLast check-in stress points: {', '.join(last_stress)}\n"
        if last_wins:
            last_checkin_section += f"Last check-in wins: {', '.join(last_wins)}\n"
        last_checkin_section += f"(Last check-in was on {last_checkin['created_at']})\n"

    # Check-in questions flow (3-5 short questions)
    CHECKIN_QUESTIONS = [
        "Ask how their day is going. Keep it casual.",
        "Ask what's on their plate today — what's the main thing they're dealing with?",
        "Ask if anything is stressing them out right now. If they mentioned stress last time, ask how that went.",
        "Ask if they had any wins since last time — anything go well?",
        "Wrap up naturally. Tell them you've noted their priorities and the dashboard is ready.",
    ]

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


async def _run_analysis(question_text: str, answer: str, session_id: str):
    """Run behavioral analysis and save to DB. Best-effort, non-blocking."""
    try:
        existing = _load_profile(session_id)
        messages = _build_analysis_prompt(question_text, answer, existing)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{SPUR_API_BASE}/chat/completions",
                json={
                    "model": ANALYSIS_MODEL,
                    "messages": messages,
                    "stream": False,
                    "temperature": 0.3,
                    "max_tokens": 600,
                },
                headers={
                    "Authorization": f"Bearer {SPUR_DEMO_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code != 200:
                return
            data = resp.json()
            msg = data["choices"][0]["message"]
            content = msg.get("content") or ""
            if not content and msg.get("reasoning"):
                content = msg["reasoning"]
            if content:
                _save_profile(session_id, content.strip())
    except Exception:
        pass  # analysis is best-effort, don't block the survey


# ── Email transcript on survey completion ───────────────────────
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def _send_transcript_email(sess: dict):
    """Send the survey transcript to akif@spuric.com. Best-effort."""
    try:
        conv = sess["conversation"]
        # Build readable transcript
        lines = ["YANS DELI SURVEY — TRANSCRIPT", "=" * 40, ""]
        for msg in conv:
            if msg["role"] == "assistant":
                lines.append(f"AI: {msg['content']}")
            else:
                lines.append(f"Benji: {msg['content']}")
            lines.append("")

        # Attach behavioral profile if exists
        profile = _load_profile(sess["session_id"])
        if profile:
            lines.append("=" * 40)
            lines.append("BEHAVIORAL PROFILE")
            lines.append("=" * 40)
            lines.append(profile)

        transcript_text = "\n".join(lines)

        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = EMAIL_TO
        msg["Subject"] = f"Yans Deli Survey — Session Complete ({sess['session_id'][:12]})"

        msg.attach(MIMEText(transcript_text, "plain"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())
    except Exception:
        pass  # best-effort — don't break the survey


# ── System prompt builder ────────────────────────────────────────
def _build_system_prompt(sess: dict, answered_q_id: int, answered_q_text: str, target_q_index: int) -> str:
    target_q = QUESTIONS[target_q_index] if target_q_index < len(QUESTIONS) else None

    if not target_q:
        return (
            "You are conducting a conversational survey with Benji, the owner of Yans Deli. "
            "The survey is now complete. Thank him naturally and say something genuine about what he shared."
        )

    target_q_text = target_q["text"]
    target_q_type = target_q["type"]
    target_q_id = target_q["id"]

    if target_q_type == "choice":
        choice_instruction = (
            f"\nQuestion #{target_q_id} is a multiple-choice question. "
            f'The topic is: "{target_q_text}"\n'
            "Generate 3-5 answer choices natural to how Benji has been talking. "
            "Make them specific and concrete. Mix in a 'something else' or 'not sure' option.\n"
            "IMPORTANT: Do NOT say the choices out loud. Just ask the question naturally. "
            "After your response, on a SEPARATE line at the very end, put ONLY:\n"
            "CHOICES: [option 1] | [option 2] | [option 3]\n"
            "This line is invisible to the user."
        )
    else:
        choice_instruction = ""

    # Load behavioral profile if it exists
    profile = _load_profile(sess["session_id"])
    profile_section = ""
    if profile:
        if len(profile) > 1500:
            profile = profile[-1500:]
        profile_section = (
            "\nBEHAVIORAL PROFILE (what you've learned about Benji so far — adapt your questioning style accordingly):\n"
            f"{profile}\n"
        )

    # Build list of questions already asked
    asked_questions = []
    for i in range(target_q_index):
        if i < len(QUESTIONS):
            asked_questions.append(f"Q{QUESTIONS[i]['id']}: {QUESTIONS[i]['text']}")
    asked_list = "\n".join(asked_questions) if asked_questions else "None yet"

    return (
        f"""You are conducting a conversational survey with Benji, the owner of Yans Deli. You're having a real conversation — one question at a time, react to his answers like a normal person would, then move on.

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
    choices_match = re.search(r'CHOICES:\s*(.+)', text, re.IGNORECASE)
    if choices_match:
        choices_str = choices_match.group(1).strip()
        choices = [c.strip() for c in choices_str.split('|') if c.strip()]

    clean_text = re.sub(r'CHOICES:\s*.+', '', text, flags=re.IGNORECASE)
    clean_text = re.sub(r'\n{3,}', '\n\n', clean_text).strip()
    return clean_text, choices


@app.post("/api/survey/chat")
async def chat(req: ChatRequest):
    sess = _load_session(req.session_id)

    if sess["q_index"] >= len(QUESTIONS):
        return JSONResponse({"done": True, "message": "Survey already complete."})

    current_q = QUESTIONS[sess["q_index"]]
    current_q_text = current_q["text"]

    sess["conversation"].append({"role": "user", "content": req.answer})

    # Fire behavioral analysis in the background
    import asyncio
    asyncio.create_task(_run_analysis(current_q_text, req.answer, sess["session_id"]))

    answered_q_id = current_q["id"]
    answered_q_text = current_q_text
    target_q_index = sess["q_index"] + 1

    system_prompt = _build_system_prompt(sess, answered_q_id, answered_q_text, target_q_index)
    messages = [{"role": "system", "content": system_prompt}]

    # Send last 4 messages for context
    recent = sess["conversation"][-4:]
    for msg in recent:
        if msg["role"] == "user":
            messages.append({"role": "user", "content": msg["content"]})
        else:
            messages.append({"role": "assistant", "content": msg["content"]})

    if len(sess["conversation"]) == 1:
        first_q = QUESTIONS[0]
        messages.insert(1, {"role": "assistant", "content": first_q["text"]})

    # Tell the AI to react + ask the next question (rephrased)
    if target_q_index < len(QUESTIONS):
        target_q = QUESTIONS[target_q_index]
        messages.append({"role": "user", "content": (
            f"React to my answer in one short sentence, then ask me this survey question "
            f"(rephrase it naturally to fit our conversation — keep the meaning, change the words):\n"
            f"\"{target_q['text']}\""
        )})

    async def sse_stream():
        full_response = ""

        if not SPUR_DEMO_API_KEY:
            yield f"data: {json.dumps({'error': 'No API key configured'})}\n\n"
            return

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(90.0)) as client:
                async with client.stream(
                    "POST",
                    f"{SPUR_API_BASE}/chat/completions",
                    json={
                        "model": SURVEY_MODEL,
                        "messages": messages,
                        "stream": True,
                        "temperature": 0.6,
                        "max_tokens": 1200,
                    },
                    headers={
                        "Authorization": f"Bearer {SPUR_DEMO_API_KEY}",
                        "Content-Type": "application/json",
                    },
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        yield f"data: {json.dumps({'error': body.decode(errors='replace')[:200]})}\n\n"
                        return

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
                                full_response += content
                                yield f"data: {json.dumps({'content': content})}\n\n"
                        except (json.JSONDecodeError, IndexError):
                            continue

                    # Edge case: reasoning model returned only reasoning, no content
                    if not got_content:
                        resp2 = await client.post(
                            f"{SPUR_API_BASE}/chat/completions",
                            json={
                                "model": SURVEY_MODEL,
                                "messages": messages,
                                "stream": False,
                                "temperature": 0.6,
                                "max_tokens": 1200,
                            },
                            headers={
                                "Authorization": f"Bearer {SPUR_DEMO_API_KEY}",
                                "Content-Type": "application/json",
                            },
                        )
                        if resp2.status_code == 200:
                            msg = resp2.json()["choices"][0]["message"]
                            full_response = msg.get("content") or ""
                            if not full_response and msg.get("reasoning"):
                                full_response = msg["reasoning"]
                            if full_response:
                                for i in range(0, len(full_response), 3):
                                    yield f"data: {json.dumps({'content': full_response[i:i+3]})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)[:200]})}\n\n"
            return

        # Parse choices from the response
        clean_text, choices = _parse_response(full_response)

        # ALWAYS advance
        sess["q_index"] = target_q_index
        sess["probe_count"] = 0

        sess["conversation"].append({"role": "assistant", "content": clean_text})

        # Save to Turso via HTTP API
        try:
            _save_session(sess)
        except Exception as save_err:
            yield f"data: {json.dumps({'error': f'Save failed: {str(save_err)[:200]}'})}\n\n"

        state = _get_state(sess)
        is_done = sess['q_index'] >= len(QUESTIONS)
        yield f"data: {json.dumps({'state': state, 'choices': choices, 'done': is_done})}\n\n"

        # Send transcript email when survey completes
        if is_done:
            try:
                _send_transcript_email(sess)
            except Exception:
                pass  # best-effort
            # Run card selection engine in background
            try:
                asyncio.create_task(_run_card_selection(sess["session_id"]))
            except Exception:
                pass

        yield "data: [DONE]\n\n"

    return StreamingResponse(sse_stream(), media_type="text/event-stream")


# ── Card selection engine ────────────────────────────────────────
AVAILABLE_CARDS = [
    {"id": "sales", "name": "Sales Tracker", "description": "Daily revenue, POS summary, trend vs yesterday"},
    {"id": "reviews", "name": "Review Monitor", "description": "Google/TripAdvisor reviews, sentiment, pending replies"},
    {"id": "social", "name": "Social Pulse", "description": "Instagram/Facebook engagement, posting cadence"},
    {"id": "catering", "name": "Catering Pipeline", "description": "Open quotes, follow-ups, pipeline value"},
    {"id": "inventory", "name": "Inventory Tracker", "description": "Supply schedule, low stock alerts"},
    {"id": "staff", "name": "Staff & Labor", "description": "Hours, costs, coverage gaps"},
    {"id": "expenses", "name": "Expense Tracker", "description": "Food costs, overhead, margins"},
    {"id": "checklist", "name": "Daily Checklist", "description": "Morning routine, prep list, priorities"},
    {"id": "goals", "name": "Goal Tracker", "description": "Monthly targets, progress bars"},
    {"id": "stress", "name": "Wellbeing", "description": "Daily check-in prompt, stress trends"},
    {"id": "contacts", "name": "Customer Contacts", "description": "Email/phone list builder"},
    {"id": "decisions", "name": "Decision Helper", "description": "Second opinion for recurring decisions"},
]

async def _run_card_selection(session_id: str):
    """Analyze onboarding answers and determine which cards to show,
    card configurations, and UI density. Saves result to Turso."""
    try:
        # Load the full onboarding conversation
        sess = _load_session(session_id)
        conv = sess["conversation"]
        conv_text = "\n".join(
            f"{'AI' if m['role']=='assistant' else 'Owner'}: {m['content']}"
            for m in conv
        )

        # Load behavioral profile
        profile = _load_profile(session_id)

        # Build the card list for the LLM
        card_list = "\n".join(
            f"- {c['id']}: {c['name']} — {c['description']}"
            for c in AVAILABLE_CARDS
        )

        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                f"{SPUR_API_BASE}/chat/completions",
                json={
                    "model": ANALYSIS_MODEL,
                    "messages": [
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
                            "- Respond with ONLY the JSON, no other text"
                        )},
                        {"role": "user", "content": (
                            f"Behavioral profile:\n{profile[:800] if profile else 'None yet'}\n\n"
                            f"Survey conversation:\n{conv_text}"
                        )},
                    ],
                    "stream": False,
                    "temperature": 0.2,
                    "max_tokens": 800,
                },
                headers={
                    "Authorization": f"Bearer {SPUR_DEMO_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code != 200:
                return

            data = resp.json()
            msg = data["choices"][0]["message"]
            content = msg.get("content") or ""
            if not content and msg.get("reasoning"):
                content = msg["reasoning"]

            # Parse JSON from response
            import re as _re
            # Find the JSON object in the response
            json_match = _re.search(r'\{.*\}', content, _re.DOTALL)
            if not json_match:
                return

            try:
                result = json.loads(json_match.group())
            except json.JSONDecodeError:
                return

            cards = result.get("cards", [])
            ui_density = result.get("ui_density", "standard")
            business_name = result.get("business_name", "Your Business")
            business_type = result.get("business_type", "general")

            # Save to Turso as a business profile entry
            _save_business_profile(session_id, {
                "cards": cards,
                "ui_density": ui_density,
                "business_name": business_name,
                "business_type": business_type,
            })

            # Also save initial card priorities (same order as selected)
            initial_priorities = [c["id"] for c in cards]
            _save_card_priorities(session_id, initial_priorities, "onboarding")

    except Exception:
        pass


def _save_business_profile(session_id: str, profile_data: dict):
    """Save the business profile (card selection + config) to Turso."""
    _turso_execute(
        """CREATE TABLE IF NOT EXISTS business_profiles (
            session_id TEXT PRIMARY KEY,
            profile_data TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )"""
    )
    _turso_execute(
        """INSERT OR REPLACE INTO business_profiles (session_id, profile_data, updated_at)
           VALUES (?, ?, datetime('now'))""",
        [
            {"type": "text", "value": session_id},
            {"type": "text", "value": json.dumps(profile_data)},
        ]
    )


def _load_business_profile(session_id: str) -> dict | None:
    """Load the business profile from Turso."""
    try:
        rows = _turso_query(
            "SELECT profile_data FROM business_profiles WHERE session_id=?",
            [{"type": "text", "value": session_id}]
        )
        if rows and rows[0].get("profile_data"):
            return json.loads(rows[0]["profile_data"])
    except Exception:
        pass
    return None


# ── Check-in analysis (extract stress points, wins, priorities) ──
async def _run_checkin_analysis(session_id: str, conversation: list):
    """Analyze check-in conversation for stress points, wins, and card priorities."""
    try:
        conv_text = "\n".join(f"{'AI' if m['role']=='assistant' else 'Owner'}: {m['content']}" for m in conversation)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{SPUR_API_BASE}/chat/completions",
                json={
                    "model": ANALYSIS_MODEL,
                    "messages": [
                        {"role": "system", "content": (
                            "You analyze a daily check-in conversation with a small business owner. "
                            "Extract: stress_points (things worrying them), wins (things going well), "
                            "priorities (card IDs to surface, in order of importance), and a one-sentence summary. "
                            "Available card IDs: sales, reviews, social, catering, inventory, checklist, goals, stress. "
                            "Respond as JSON: {\"stress_points\": [...], \"wins\": [...], \"priorities\": [...], \"summary\": \"one sentence here\"} "
                            "Only include priorities that are relevant to what they said. "
                            "If nothing stressed them, stress_points is empty. If no wins, wins is empty. "
                            "The summary should be a single natural sentence capturing the overall mood and focus of today's check-in. "
                            "Example: 'Staffing shortages are the main concern today, but the new Reuben special is selling well.'"
                        )},
                        {"role": "user", "content": f"Check-in conversation:\n{conv_text}"},
                    ],
                    "stream": False,
                    "temperature": 0.2,
                    "max_tokens": 600,
                },
                headers={
                    "Authorization": f"Bearer {SPUR_DEMO_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code != 200:
                return
            data = resp.json()
            msg = data["choices"][0]["message"]
            content = msg.get("content") or ""
            if not content and msg.get("reasoning"):
                content = msg["reasoning"]

            # Parse JSON from response
            import re as _re
            json_match = _re.search(r'\{.*\}', content, _re.DOTALL)
            if json_match:
                try:
                    result = json.loads(json_match.group())
                    stress_points = result.get("stress_points", [])
                    wins = result.get("wins", [])
                    priorities = result.get("priorities", [])
                    summary = result.get("summary", "")

                    _save_checkin(session_id, conversation, stress_points, wins, priorities, summary)
                    if priorities:
                        _save_card_priorities(session_id, priorities, "checkin")
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass


# ── Check-in chat endpoint ──────────────────────────────────────
@app.post("/api/survey/checkin")
async def checkin_chat(req: ChatRequest):
    """Daily check-in mode for returning users who completed onboarding."""
    if not _has_completed_onboarding(req.session_id):
        return JSONResponse({"error": "Onboarding not complete", "mode": "onboarding"})

    # Load existing check-in conversation from memory (stored in a separate dict)
    # or start a new one
    checkin_key = f"checkin_{req.session_id}"
    if not hasattr(checkin_chat, '_conversations'):
        checkin_chat._conversations = {}
    if checkin_key not in checkin_chat._conversations:
        checkin_chat._conversations[checkin_key] = {"messages": [], "step": 0}

    conv = checkin_chat._conversations[checkin_key]
    conv["messages"].append({"role": "user", "content": req.answer})
    conv["step"] += 1

    system_prompt = _build_checkin_prompt(req.session_id, conv["messages"], conv["step"])
    messages = [{"role": "system", "content": system_prompt}]

    # Send last 4 messages of the check-in conversation
    recent = conv["messages"][-4:]
    for msg in recent:
        if msg["role"] == "user":
            messages.append({"role": "user", "content": msg["content"]})
        else:
            messages.append({"role": "assistant", "content": msg["content"]})

    # Tell the AI to react + ask the next check-in question
    messages.append({"role": "user", "content": (
        "React to what I said, then ask your next check-in question. Keep it short and natural."
    )})

    total_steps = 5

    async def sse_stream():
        full_response = ""

        if not SPUR_DEMO_API_KEY:
            yield f"data: {json.dumps({'error': 'No API key configured'})}\n\n"
            return

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(90.0)) as client:
                async with client.stream(
                    "POST",
                    f"{SPUR_API_BASE}/chat/completions",
                    json={
                        "model": SURVEY_MODEL,
                        "messages": messages,
                        "stream": True,
                        "temperature": 0.6,
                        "max_tokens": 800,
                    },
                    headers={
                        "Authorization": f"Bearer {SPUR_DEMO_API_KEY}",
                        "Content-Type": "application/json",
                    },
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        yield f"data: {json.dumps({'error': body.decode(errors='replace')[:200]})}\n\n"
                        return

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
                                full_response += content
                                yield f"data: {json.dumps({'content': content})}\n\n"
                        except (json.JSONDecodeError, IndexError):
                            continue

                    # Fallback for reasoning models
                    if not got_content:
                        resp2 = await client.post(
                            f"{SPUR_API_BASE}/chat/completions",
                            json={
                                "model": SURVEY_MODEL,
                                "messages": messages,
                                "stream": False,
                                "temperature": 0.6,
                                "max_tokens": 800,
                            },
                            headers={
                                "Authorization": f"Bearer {SPUR_DEMO_API_KEY}",
                                "Content-Type": "application/json",
                            },
                        )
                        if resp2.status_code == 200:
                            msg = resp2.json()["choices"][0]["message"]
                            full_response = msg.get("content") or ""
                            if not full_response and msg.get("reasoning"):
                                full_response = msg["reasoning"]
                            if full_response:
                                for i in range(0, len(full_response), 3):
                                    yield f"data: {json.dumps({'content': full_response[i:i+3]})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)[:200]})}\n\n"
            return

        # Add AI response to conversation
        conv["messages"].append({"role": "assistant", "content": full_response})

        # Check if check-in is complete
        is_done = conv["step"] >= total_steps

        # Run analysis and save when done
        if is_done:
            import asyncio
            asyncio.create_task(_run_checkin_analysis(req.session_id, conv["messages"]))
            # Clear the in-memory conversation
            del checkin_chat._conversations[checkin_key]

        yield f"data: {json.dumps({'done': is_done, 'mode': 'checkin', 'step': conv['step'], 'total_steps': total_steps})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse_stream(), media_type="text/event-stream")


@app.get("/api/survey/checkin/latest")
async def get_latest_checkin(session_id: str):
    """Get the latest daily check-in for a session."""
    checkin = _get_latest_checkin(session_id)
    if not checkin:
        return JSONResponse({"checkin": None, "message": "No check-ins yet."})
    return checkin


@app.get("/api/survey/checkin/status")
async def get_checkin_status(session_id: str):
    """Check if the user should see onboarding or check-in mode."""
    onboarded = _has_completed_onboarding(session_id)
    latest = _get_latest_checkin(session_id) if onboarded else None
    priorities = _get_card_priorities(session_id) if onboarded else []
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
    return {"priorities": _get_card_priorities(session_id)}


@app.get("/api/survey/business-profile/{session_id}")
async def get_business_profile(session_id: str):
    """Get the business profile (card selection + config + UI density)."""
    profile = _load_business_profile(session_id)
    if not profile:
        return JSONResponse({"profile": None, "message": "No business profile yet. Complete onboarding first."})
    return profile


@app.get("/api/survey/state")
async def get_state(session_id: str):
    sess = _load_session(session_id)
    return _get_state(sess)


@app.get("/api/survey/transcript")
async def get_transcript(session_id: str):
    sess = _load_session(session_id)
    return {
        "conversation": sess["conversation"],
        "q_index": sess["q_index"],
        "total_questions": len(QUESTIONS),
        "questions": QUESTIONS,
    }


@app.post("/api/survey/reset")
async def reset(session_id: str):
    _reset_session(session_id)
    return {"status": "ok", "message": "Survey reset."}


@app.get("/api/survey/questions")
async def get_questions():
    return {"questions": QUESTIONS}


@app.get("/api/survey/profile/{session_id}")
async def get_profile(session_id: str):
    profile = _load_profile(session_id)
    if not profile:
        return JSONResponse({"profile": None, "message": "No profile yet."})
    return PlainTextResponse(profile, media_type="text/markdown")


@app.get("/api/survey/profiles")
async def list_profiles():
    rows = _turso_query(
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


@app.on_event("startup")
async def _startup():
    init_db()


# Serve frontend
_frontend_dir = str(pathlib.Path(__file__).resolve().parent / "frontend")
if not os.path.isdir(_frontend_dir):
    _frontend_dir = "/app/frontend"
if os.path.isdir(_frontend_dir):
    app.mount("/", app=StaticFiles(directory=_frontend_dir, html=True), name="frontend")
