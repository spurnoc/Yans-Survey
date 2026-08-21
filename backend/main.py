"""
SPUR Survey — Benji / Yans Deli conversational survey.

Standalone FastAPI app. Chat-style adaptive survey with SSE streaming.
- Per-browser sessions via session_id, persisted to SQLite (survives restarts)
- AI generates dynamic multiple-choice options based on conversation context
- Behavioral analysis runs after each answer, writes findings to /data/profiles/{session_id}.md
- Findings are fed back into the system prompt so the AI adapts in real-time
"""
from __future__ import annotations

import os, json, time, sqlite3, re
from typing import Optional
from pathlib import Path
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
SURVEY_MODEL = os.getenv("SURVEY_MODEL", "spur-chat-mini")
ANALYSIS_MODEL = os.getenv("ANALYSIS_MODEL", "spur-chat-mini")
DB_PATH = os.getenv("DB_PATH", "/data/survey.db")
PROFILES_DIR = os.getenv("PROFILES_DIR", "/data/profiles")

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


# ── SQLite persistence ────────────────────────────────────────────
def _get_db() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


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
    """)
    conn.commit()
    conn.close()


def _load_session(session_id: str) -> dict:
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT * FROM survey_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
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
    finally:
        conn.close()


def _save_session(sess: dict):
    conn = _get_db()
    try:
        conn.execute("""
            INSERT INTO survey_sessions (session_id, conversation, q_index, probe_count, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(session_id) DO UPDATE SET
                conversation=excluded.conversation,
                q_index=excluded.q_index,
                probe_count=excluded.probe_count,
                updated_at=datetime('now')
        """, (
            sess["session_id"],
            json.dumps(sess["conversation"]),
            sess["q_index"],
            sess["probe_count"],
        ))
        conn.commit()
    finally:
        conn.close()


def _reset_session(session_id: str):
    conn = _get_db()
    try:
        conn.execute("""
            INSERT INTO survey_sessions (session_id, conversation, q_index, probe_count, updated_at)
            VALUES (?, '[]', 0, 0, datetime('now'))
            ON CONFLICT(session_id) DO UPDATE SET
                conversation='[]', q_index=0, probe_count=0, updated_at=datetime('now')
        """, (session_id,))
        conn.commit()
    finally:
        conn.close()
        # Also remove the profile file
    profile_path = Path(PROFILES_DIR) / f"{session_id}.md"
    if profile_path.exists():
        profile_path.unlink()


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
def _profile_path(session_id: str) -> Path:
    d = Path(PROFILES_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{session_id}.md"


def _load_profile(session_id: str) -> str:
    p = _profile_path(session_id)
    if p.exists():
        return p.read_text(encoding="utf-8")
    return ""


def _save_profile(session_id: str, content: str):
    p = _profile_path(session_id)
    p.write_text(content, encoding="utf-8")


def _build_analysis_prompt(question_text: str, answer: str, existing_profile: str) -> list[dict]:
    """Build messages for the behavioral analysis LLM call."""
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
    """Run behavioral analysis and save to .md file. Non-blocking on errors."""
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


# ── System prompt builder (now includes behavioral profile) ───────
def _build_system_prompt(sess: dict) -> str:
    q = QUESTIONS[sess["q_index"]] if sess["q_index"] < len(QUESTIONS) else None
    next_q = QUESTIONS[sess["q_index"] + 1] if sess["q_index"] + 1 < len(QUESTIONS) else None

    current_q_text = q["text"] if q else "Survey complete."
    next_q_text = next_q["text"] if next_q else ""
    next_q_type = next_q["type"] if next_q else ""
    next_q_tag = next_q.get("tag", "") if next_q else ""
    already_probed = sess["probe_count"] > 0

    if not q:
        return (
            "You are conducting a conversational survey with Benji, the owner of Yans Deli. "
            "The survey is now complete. Thank him naturally and say something genuine about what he shared."
        )

    if next_q_type == "choice":
        choice_instruction = (
            f"\nThe next question (#{next_q['id']}) is a multiple-choice question. "
            f'The topic is: "{next_q_text}"\n'
            f"The question tag is: {next_q_tag}\n"
            "Do NOT use fixed options. Generate 3-5 answer choices that are natural to how Benji has been talking.\n"
            "Make the choices specific and concrete, grounded in what he's said so far. "
            'Mix in a "something else" or "not sure" option.\n'
            'Present the choices naturally in the conversation — "Would you say you\'re more [A], [B], or maybe [C]?"\n'
            "Then ALSO append a special marker at the very end of your response, on its own line, formatted as:\n"
            "CHOICES: [option 1] | [option 2] | [option 3]\n"
            "The frontend will parse this to render tappable buttons."
        )
    else:
        choice_instruction = ""

    # Load behavioral profile if it exists
    profile = _load_profile(sess["session_id"])
    profile_section = ""
    if profile:
        profile_section = (
            "\nBEHAVIORAL PROFILE (what you've learned about Benji so far — adapt your questioning style accordingly):\n"
            f"{profile}\n"
        )

    return (
        f"""You are conducting a conversational survey with Benji, the owner of Yans Deli. You're having a real conversation — one question at a time, react to his answers like a normal person would, then move on.

CRITICAL RULES:
1. You are NOT a robot. React to what he says. "Got it." "That makes sense." "Honestly, that's smart." Be real but brief — one short sentence max.
2. If his answer is thin or vague (like "I don't know" or one word for an open question), ask ONE follow-up probe to draw him out. Natural phrasing: "Can you say more about that?" "What do you mean by that?" Only probe ONCE per question, then move on.
3. If his answer is substantive, DON'T probe. React and move to the next question.
4. When asking the next question, don't just read it verbatim. Rephrase it naturally to fit the conversation. Keep the meaning, change the words.
5. Keep everything SHORT. Your reaction + the next question should be 2-3 sentences total. This is a conversation, not an essay.

CURRENT STATE:
- Current question #{q['id']}: {current_q_text}
- Already probed this question: {already_probed}
- Next question #{next_q['id'] if next_q else 'done'}: {next_q_text}
- Next question type: {next_q_type}
- Next question tag: {next_q_tag}

Respond as plain text. Structure: [short reaction to his answer] [transition] [next question naturally phrased]. If this is a probe, don't ask the next question — just ask the probe.
{choice_instruction}

Example good response: "Honestly, tracking all that in your head is impressive but probably stressful. When a customer says something nice, a bad review comes in, or a big catering order lands — what happens next?"
Example probe: "Got it. Can you say more about what that looks like day to day?"
Example with choices: "Makes sense. If I asked you right now how many catering orders you did last month — could you pull that up, or is it more of a rough guess?\\nCHOICES: I could find it exactly | Rough guess | No idea at all"
{profile_section}"""
    )


def _parse_choices(text: str) -> tuple[str, list[str]]:
    """Extract CHOICES marker from AI response. Returns (clean_text, choices_list)."""
    match = re.search(r'\nCHOICES:\s*(.+)$', text, re.IGNORECASE)
    if not match:
        return text.strip(), []

    choices_str = match.group(1).strip()
    choices = [c.strip() for c in choices_str.split('|') if c.strip()]
    clean_text = text[:match.start()].strip()
    return clean_text, choices


@app.post("/api/survey/chat")
async def chat(req: ChatRequest):
    sess = _load_session(req.session_id)

    if sess["q_index"] >= len(QUESTIONS):
        return JSONResponse({"done": True, "message": "Survey already complete."})

    # Get the current question text for analysis
    current_q = QUESTIONS[sess["q_index"]]
    current_q_text = current_q["text"]

    sess["conversation"].append({"role": "user", "content": req.answer})

    # Fire behavioral analysis in the background — don't block the survey.
    # The profile will be ready by the time the NEXT answer comes in.
    import asyncio
    asyncio.create_task(_run_analysis(current_q_text, req.answer, req.session_id))

    system_prompt = _build_system_prompt(sess)
    messages = [{"role": "system", "content": system_prompt}]
    for msg in sess["conversation"][-6:]:
        if msg["role"] == "user":
            messages.append({"role": "user", "content": msg["content"]})
        else:
            messages.append({"role": "assistant", "content": msg["content"]})

    if len(sess["conversation"]) == 1:
        first_q = QUESTIONS[0]
        messages.insert(1, {"role": "assistant", "content": first_q["text"]})

    async def sse_stream():
        # Real streaming — tokens appear as the model generates them.
        # For reasoning models, delta.content may be null while delta.reasoning
        # is being emitted. We skip reasoning tokens and only stream content,
        # but if the model finishes reasoning and there's no content (edge case),
        # we fall back to a non-streaming retry.

        if not SPUR_DEMO_API_KEY:
            yield f"data: {json.dumps({'error': 'No API key configured'})}\n\n"
            return

        full_response = ""

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

                    # Edge case: reasoning model returned only reasoning, no content
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

        # Parse choices from the response
        clean_text, choices = _parse_choices(full_response)
        if not choices:
            choices = []

        # Determine if AI advanced or probed
        next_q = QUESTIONS[sess["q_index"] + 1] if sess["q_index"] + 1 < len(QUESTIONS) else None

        if next_q:
            next_words = set(next_q["text"].lower().split())
            response_words = set(clean_text.lower().split())
            overlap = len(next_words & response_words) / max(len(next_words), 1)

            if overlap > 0.3 or sess["probe_count"] >= 1:
                sess["q_index"] += 1
                sess["probe_count"] = 0
            else:
                sess["probe_count"] += 1
        else:
            sess["q_index"] += 1

        # Store the clean text (without CHOICES marker) in conversation
        sess["conversation"].append({"role": "assistant", "content": clean_text})

        # Save to SQLite
        _save_session(sess)

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


# ── Profile endpoint — returns the .md file ──────────────────────
@app.get("/api/survey/profile/{session_id}")
async def get_profile(session_id: str):
    """Return the behavioral profile as markdown text."""
    profile = _load_profile(session_id)
    if not profile:
        return JSONResponse({"profile": None, "message": "No profile yet — the respondent hasn't answered any questions."})
    return PlainTextResponse(profile, media_type="text/markdown")


@app.get("/api/survey/profiles")
async def list_profiles():
    """List all profile files available."""
    d = Path(PROFILES_DIR)
    if not d.exists():
        return {"profiles": []}
    profiles = []
    for f in sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = f.stat()
        profiles.append({
            "session_id": f.stem,
            "size_bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })
    return {"profiles": profiles}


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "service": "survey",
        "has_api_key": bool(SPUR_DEMO_API_KEY),
    }


@app.on_event("startup")
async def _startup():
    init_db()
    Path(PROFILES_DIR).mkdir(parents=True, exist_ok=True)


# Serve frontend
_frontend_dir = str(pathlib.Path(__file__).resolve().parent / "frontend")
if not os.path.isdir(_frontend_dir):
    _frontend_dir = "/app/frontend"
if os.path.isdir(_frontend_dir):
    app.mount("/", app=StaticFiles(directory=_frontend_dir, html=True), name="frontend")
