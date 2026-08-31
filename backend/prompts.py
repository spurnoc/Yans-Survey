"""
SPUR Survey — Prompt-building functions and question constants.

Extracted from main.py. All business-type-aware question definitions,
business-type detection, and system-prompt construction live here.

Dependencies (DB + LLM helpers) are imported from db.py and llm.py.
Late imports are used inside functions that are called from db.py
to avoid circular imports (db._has_completed_onboarding and
db._detect_business_type_from_session import from prompts).
"""
from __future__ import annotations

import re
import json
import logging
import os

logger = logging.getLogger(__name__)

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
    """Build the system prompt for a daily check-in conversation."""
    # Late imports to avoid circular dependency: db.py imports from prompts
    # at module level via late imports inside functions.
    from db import _load_profile, _turso_query, _get_latest_checkin

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
    conv = []
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


async def _build_system_prompt(sess: dict, answered_q_id: int, answered_q_text: str, target_q_index: int) -> str:
    """Build the system prompt for the next survey question."""
    # Late import to avoid circular dependency
    from db import _load_profile

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
