"""
SPUR Survey — Card data and card selection engine.

Extracted from main.py. AVAILABLE_CARDS is also referenced by prompts.py
to avoid duplication — import from here.
"""
from __future__ import annotations

import logging

from llm import _spur_chat_completion, _extract_llm_content, _extract_json_from_llm, ANALYSIS_MODEL
from db import _load_session, _load_profile, _save_business_profile, _save_card_priorities

logger = logging.getLogger(__name__)


# ── Available dashboard cards ────────────────────────────────────
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
                    f"Behavioral profile:\n{profile[:800] if profile else 'None yet'}\n\n"
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
