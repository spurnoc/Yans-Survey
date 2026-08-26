"""
SPUR Survey — Benji / Yans Deli conversational survey.

Standalone FastAPI app. Chat-style adaptive survey with SSE streaming.
- Per-browser sessions via session_id, persisted to Turso (cloud SQLite)
- AI generates dynamic multiple-choice options based on conversation context
- Behavioral analysis runs after each answer, stored in DB (not filesystem)
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
PROBE_MODEL = os.getenv("PROBE_MODEL", "spur-glm-5-2")
TURSO_DB_URL = os.getenv("TURSO_DB_URL", "")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")

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


# ── Turso (libsql) persistence ────────────────────────────────────
import libsql_experimental as libsql

_db_conn = None

def _get_db():
    """Get a DB connection. For writes, use _get_write_db() instead."""
    global _db_conn
    if _db_conn is not None:
        try:
            _db_conn.execute("SELECT 1").fetchone()
            return _db_conn
        except Exception:
            _db_conn = None
    if not TURSO_DB_URL:
        raise Exception("TURSO_DB_URL not set")
    _db_conn = libsql.connect(TURSO_DB_URL, auth_token=TURSO_AUTH_TOKEN)
    return _db_conn


def _get_write_db():
    """Create a FRESH connection for writes. The cached connection
    doesn't reliably persist to Turso — a new connection per write
    ensures the data actually makes it to the remote database."""
    if not TURSO_DB_URL:
        raise Exception("TURSO_DB_URL not set")
    return libsql.connect(TURSO_DB_URL, auth_token=TURSO_AUTH_TOKEN)


def _row_to_dict(cursor, row) -> dict:
    """Convert a row tuple to a dict using cursor column names."""
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


def _fetchone(conn, query, params=()) -> dict | None:
    """Execute query and return one row as a dict, or None."""
    cur = conn.execute(query, params)
    row = cur.fetchone()
    if row is None:
        return None
    return _row_to_dict(cur, row)


def _fetchall(conn, query, params=()) -> list[dict]:
    """Execute query and return all rows as dicts."""
    cur = conn.execute(query, params)
    return [_row_to_dict(cur, row) for row in cur.fetchall()]


def init_db():
    conn = _get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS survey_sessions (
        session_id TEXT PRIMARY KEY,
        conversation TEXT DEFAULT '[]',
        q_index INTEGER DEFAULT 0,
        probe_count INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS survey_profiles (
        session_id TEXT PRIMARY KEY,
        profile TEXT DEFAULT '',
        updated_at TEXT DEFAULT (datetime('now'))
    );
    """)
    conn.commit()


def _load_session(session_id: str) -> dict:
    conn = _get_db()
    row = _fetchone(conn, "SELECT * FROM survey_sessions WHERE session_id=?", (session_id,))
    if row:
        return {
            "session_id": session_id,
            "conversation": json.loads(row["conversation"]),
            "q_index": row["q_index"],
            "probe_count": row["probe_count"],
        }
    else:
        conn.execute(
            "INSERT INTO survey_sessions (session_id) VALUES (?)",
            (session_id,),
        )
        conn.commit()
        return {
            "session_id": session_id,
            "conversation": [],
            "q_index": 0,
            "probe_count": 0,
        }


def _save_session(sess: dict):
    conn = _get_write_db()
    conn.execute("""
        INSERT OR REPLACE INTO survey_sessions (session_id, conversation, q_index, probe_count, updated_at)
        VALUES (?, ?, ?, ?, datetime('now'))
    """, (
        sess["session_id"],
        json.dumps(sess["conversation"]),
        sess["q_index"],
        sess["probe_count"],
    ))
    conn.commit()
    conn.close()


def _reset_session(session_id: str):
    conn = _get_write_db()
    conn.execute("""
        INSERT OR REPLACE INTO survey_sessions (session_id, conversation, q_index, probe_count, updated_at)
        VALUES (?, '[]', 0, 0, datetime('now'))
    """, (session_id,))
    conn.execute("""
        INSERT OR REPLACE INTO survey_profiles (session_id, profile, updated_at)
        VALUES (?, '', datetime('now'))
    """, (session_id,))
    conn.commit()
    conn.close()


def _load_profile(session_id: str) -> str:
    conn = _get_db()
    row = _fetchone(conn, "SELECT profile FROM survey_profiles WHERE session_id=?", (session_id,))
    if row and row["profile"]:
        return row["profile"]
    return ""


def _save_profile(session_id: str, content: str):
    conn = _get_write_db()
    conn.execute("""
        INSERT OR REPLACE INTO survey_profiles (session_id, profile, updated_at)
        VALUES (?, ?, datetime('now'))
    """, (session_id, content))
    conn.commit()
    conn.close()


def _get_state(sess: dict) -> dict:
    return {
        "q_index": sess["q_index"],
        "total_questions": len(QUESTIONS),
        "probe_count": sess["probe_count"],
        "conversation": sess["conversation"],
        "current_question": QUESTIONS[sess["q_index"]] if sess["q_index"] < len(QUESTIONS) else None,
    }


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


async def _should_probe_llm(question_text: str, answer: str, already_probed: bool) -> bool:
    """Ask a smarter model whether this answer needs a follow-up probe."""
    if already_probed:
        return False  # never probe twice
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{SPUR_API_BASE}/chat/completions",
                json={
                    "model": PROBE_MODEL,
                    "messages": [
                        {"role": "system", "content": (
                            "You judge whether a survey answer is substantive enough to move on, "
                            "or too thin/vague and needs a follow-up. "
                            "Reply with ONLY 'PROBE' or 'ADVANCE' — nothing else. "
                            "PROBE if the answer is vague, evasive, or missing key detail. "
                            "ADVANCE if the answer actually addresses the question, even if brief."
                        )},
                        {"role": "user", "content": (
                            f"QUESTION: {question_text}\n"
                            f"ANSWER: {answer}\n"
                            f"Already probed: {already_probed}\n"
                            f"Decide: PROBE or ADVANCE?"
                        )},
                    ],
                    "stream": False,
                    "temperature": 0.1,
                    "max_tokens": 800,
                },
                headers={
                    "Authorization": f"Bearer {SPUR_DEMO_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code != 200:
                return False
            data = resp.json()
            msg = data["choices"][0]["message"]
            content = msg.get("content") or ""
            if not content and msg.get("reasoning"):
                content = msg["reasoning"]
            return "PROBE" in content.upper()
    except Exception:
        return False  # on error, just advance


async def _verify_advance_llm(target_question: str, ai_response: str) -> bool:
    """Ask a smarter model whether the AI response actually asks the target question."""
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{SPUR_API_BASE}/chat/completions",
                json={
                    "model": PROBE_MODEL,
                    "messages": [
                        {"role": "system", "content": (
                            "You are a QA checker. You verify whether a survey AI actually asked "
                            "the question it was supposed to ask. "
                            "Reply with ONLY 'YES' or 'NO'. "
                            "YES if the response asks about the same topic as the target question "
                            "(even if rephrased). NO if the response is asking about something else "
                            "(a different topic or a follow-up probe on a previous question)."
                        )},
                        {"role": "user", "content": (
                            f"TARGET QUESTION: {target_question}\n\n"
                            f"AI RESPONSE: {ai_response}\n\n"
                            f"Does the AI response ask about the same topic as the target question?"
                        )},
                    ],
                    "stream": False,
                    "temperature": 0.1,
                    "max_tokens": 800,
                },
                headers={
                    "Authorization": f"Bearer {SPUR_DEMO_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code != 200:
                return True  # on error, assume advance (don't block the survey)
            data = resp.json()
            msg = data["choices"][0]["message"]
            content = msg.get("content") or ""
            if not content and msg.get("reasoning"):
                content = msg["reasoning"]
            return "YES" in content.upper()
    except Exception:
        return True  # on error, assume advance


# ── System prompt builder (includes behavioral profile) ─────────
def _build_system_prompt(sess: dict, should_probe: bool, answered_q_id: int, answered_q_text: str, target_q_index: int) -> str:
    # The target question is the one the AI should ask now.
    # target_q_index is passed in so we don't modify sess["q_index"] yet.
    target_q = QUESTIONS[target_q_index] if target_q_index < len(QUESTIONS) else None

    if not target_q:
        return (
            "You are conducting a conversational survey with Benji, the owner of Yans Deli. "
            "The survey is now complete. Thank him naturally and say something genuine about what he shared."
        )

    target_q_text = target_q["text"]
    target_q_type = target_q["type"]
    target_q_tag = target_q.get("tag", "")
    target_q_id = target_q["id"]

    if target_q_type == "choice" and not should_probe:
        choice_instruction = (
            f"\nQuestion #{target_q_id} is a multiple-choice question. "
            f'The topic is: "{target_q_text}"\n'
            f"Tag: {target_q_tag}\n"
            "Generate 3-5 answer choices natural to how Benji has been talking. "
            "Make them specific and concrete. Mix in a 'something else' or 'not sure' option.\n"
            "IMPORTANT: Do NOT say the choices out loud. Just ask the question naturally. "
            "After your response, on a SEPARATE line at the very end, put ONLY:\n"
            "CHOICES: [option 1] | [option 2] | [option 3]\n"
            "This line is invisible to the user."
        )
    else:
        choice_instruction = ""

    # Load behavioral profile if it exists (cap to last 1500 chars to keep prompt lean)
    profile = _load_profile(sess["session_id"])
    profile_section = ""
    if profile:
        if len(profile) > 1500:
            profile = profile[-1500:]
        profile_section = (
            "\nBEHAVIORAL PROFILE (what you've learned about Benji so far — adapt your questioning style accordingly):\n"
            f"{profile}\n"
        )

    # Build list of questions already asked (so AI doesn't repeat them)
    asked_questions = []
    for i in range(target_q_index):
        if i < len(QUESTIONS):
            asked_questions.append(f"Q{QUESTIONS[i]['id']}: {QUESTIONS[i]['text']}")
    asked_list = "\n".join(asked_questions) if asked_questions else "None yet"

    # The backend has ALREADY decided whether to probe or advance.
    # Tell the AI exactly what to do.
    if should_probe:
        action_instruction = (
            f"\nINSTRUCTION: The respondent's answer to question #{answered_q_id} was short or vague. "
            f"Ask ONE follow-up probe to draw him out "
            f"(e.g. 'Can you say more about that?' or 'What do you mean by that?'). "
            f"Do NOT ask the next survey question yet."
        )
    elif target_q_id != answered_q_id:
        # Advancing to a new question
        action_instruction = (
            f"\nINSTRUCTION: React briefly to his answer, then ask question #{target_q_id}: "
            f'"{target_q_text}". Rephrase it naturally — do not read it verbatim. '
            f"This is a NEW question about a different topic. Do NOT repeat anything from the asked list."
        )
    else:
        action_instruction = (
            f"\nINSTRUCTION: React briefly to his answer, then ask question #{target_q_id}: "
            f'"{target_q_text}". Rephrase it naturally.'
        )

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
{action_instruction}
{choice_instruction}
{profile_section}"""
    )


def _parse_response(text: str) -> tuple[str, list[str], str]:
    """Extract CHOICES markers and determine action from AI response.
    Returns (clean_text, choices_list, action) where action is 'ADVANCE' or 'PROBE'.
    """
    choices = []

    # Extract CHOICES
    choices_match = re.search(r'CHOICES:\s*(.+)', text, re.IGNORECASE)
    if choices_match:
        choices_str = choices_match.group(1).strip()
        choices = [c.strip() for c in choices_str.split('|') if c.strip()]

    # Strip all markers from visible text
    clean_text = re.sub(r'ACTION:\s*\w+', '', text, flags=re.IGNORECASE)
    clean_text = re.sub(r'CHOICES:\s*.+', '', clean_text, flags=re.IGNORECASE)
    clean_text = re.sub(r'\n{3,}', '\n\n', clean_text).strip()

    # Check if AI explicitly said ADVANCE or PROBE
    action_match = re.search(r'ACTION:\s*(ADVANCE|PROBE)', text, re.IGNORECASE)
    if action_match:
        action = action_match.group(1).upper()
    else:
        # AI didn't include the marker — guess based on content.
        # If response is short and ends with a question mark but doesn't
        # reference the next scripted question, it's likely a probe.
        action = "ADVANCE"  # default to advance

    return clean_text, choices, action


@app.post("/api/survey/chat")
async def chat(req: ChatRequest):
    sess = _load_session(req.session_id)

    if sess["q_index"] >= len(QUESTIONS):
        return JSONResponse({"done": True, "message": "Survey already complete."})

    # Get the current question (the one being answered right now)
    current_q = QUESTIONS[sess["q_index"]]
    current_q_text = current_q["text"]

    sess["conversation"].append({"role": "user", "content": req.answer})

    # Fire behavioral analysis in the background — don't block the survey.
    import asyncio
    asyncio.create_task(_run_analysis(current_q_text, req.answer, sess["session_id"]))

    answered_q_id = current_q["id"]
    answered_q_text = current_q_text

    # Determine the target question (what the AI should ask next)
    target_q_index = sess["q_index"] + 1  # always aim for the next question
    should_probe = False

    # Build the prompt with the target question
    system_prompt = _build_system_prompt(sess, should_probe, answered_q_id, answered_q_text, target_q_index)
    messages = [{"role": "system", "content": system_prompt}]

    # Send ONLY the last 4 messages for context — too much history makes
    # the AI follow the conversation pattern instead of instructions
    recent = sess["conversation"][-4:]
    for msg in recent:
        if msg["role"] == "user":
            messages.append({"role": "user", "content": msg["content"]})
        else:
            messages.append({"role": "assistant", "content": msg["content"]})

    # For the first answer, inject the opening question so the AI knows what was asked
    if len(sess["conversation"]) == 1:
        first_q = QUESTIONS[0]
        messages.insert(1, {"role": "assistant", "content": first_q["text"]})

    # AI generates a reaction AND rephrases the next question.
    # Backend streams whatever the AI produces — no appending needed.
    # The AI sees the exact question text and is told to rephrase it naturally.
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
        clean_text, choices, _ = _parse_response(full_response)

        # ALWAYS advance — the AI generated the reaction + rephrased question.
        # No probe detection, no appending. Just advance.
        sess["q_index"] = target_q_index
        sess["probe_count"] = 0

        # Store the clean text (without CHOICES marker) in conversation
        sess["conversation"].append({"role": "assistant", "content": clean_text})

        # Save to Turso — wrap in try/except so we can see if it fails
        try:
            _save_session(sess)
        except Exception as save_err:
            # Send the error through the stream so we can debug
            yield f"data: {json.dumps({'error': f'Save failed: {str(save_err)[:200]}'})}\n\n"

        state = _get_state(sess)
        yield f"data: {json.dumps({'state': state, 'choices': choices, 'done': sess['q_index'] >= len(QUESTIONS)})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse_stream(), media_type="text/event-stream")


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


# ── Profile endpoint — returns the .md content from DB ───────────
@app.get("/api/survey/profile/{session_id}")
async def get_profile(session_id: str):
    """Return the behavioral profile as markdown text."""
    profile = _load_profile(session_id)
    if not profile:
        return JSONResponse({"profile": None, "message": "No profile yet."})
    return PlainTextResponse(profile, media_type="text/markdown")


@app.get("/api/survey/profiles")
async def list_profiles():
    """List all profiles in the DB."""
    conn = _get_db()
    rows = _fetchall(conn,
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
