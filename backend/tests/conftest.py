"""
conftest.py — Shared pytest fixtures for the Daily 15 survey test suite.

All external calls (Turso DB, SPUR LLM API) are mocked so tests run
without any real database or API key.
"""
from __future__ import annotations

import json
import sys
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ── Path setup: make backend/ importable ────────────────────────────
# The backend source files (main.py, db.py, llm.py, cards.py, models.py)
# live in backend/ one level above tests/. Add backend/ to sys.path so
# that `import main`, `import db`, etc. work during tests.
BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# ── Ensure no real API keys / DB URLs leak into the test environment ──
# main.py reads env vars at import time, so set safe dummy values BEFORE
# importing the application module.
os.environ.setdefault("SPUR_DEMO_API_KEY", "test-key-not-real")
os.environ.setdefault("TURSO_DB_URL", "https://test.turso.io")
os.environ.setdefault("TURSO_AUTH_TOKEN", "test-token-not-real")

# ── Import application modules (after path + env are configured) ──────
import main  # noqa: E402
import db as db_module  # noqa: E402
import llm as llm_module  # noqa: E402
import cards as cards_module  # noqa: E402


# ════════════════════════════════════════════════════════════════════
#  Mock Turso DB fixtures
# ════════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_turso_query():
    """Patch _turso_query to return canned rows. Returns the mock so tests
    can configure return values."""
    with patch.object(main, "_turso_query", new_callable=AsyncMock) as m:
        m.return_value = []  # default: no rows
        yield m


@pytest.fixture
def mock_turso_execute():
    """Patch _turso_execute to be a no-op async mock."""
    with patch.object(main, "_turso_execute", new_callable=AsyncMock) as m:
        m.return_value = {"results": [{"response": {"result": {}}}]}
        yield m


@pytest.fixture
def mock_turso_request():
    """Patch the low-level _turso_request used by both query and execute."""
    with patch.object(main, "_turso_request", new_callable=AsyncMock) as m:
        m.return_value = {"results": [{"response": {"result": {"rows": [], "cols": []}}}]}
        yield m


@pytest.fixture
def mock_init_db():
    """Patch init_db so the lifespan handler doesn't hit the real DB."""
    with patch.object(main, "init_db", new_callable=AsyncMock) as m:
        yield m


@pytest.fixture
def mock_load_session():
    """Patch _load_session to return a fresh sample session dict."""
    sample = {
        "session_id": "test-session-001",
        "conversation": [],
        "q_index": 0,
        "probe_count": 0,
    }
    with patch.object(main, "_load_session", new_callable=AsyncMock) as m:
        m.return_value = sample
        yield m, sample


@pytest.fixture
def mock_load_or_create_session():
    """Patch _load_or_create_session to return a fresh session."""
    sample = {
        "session_id": "test-session-001",
        "conversation": [],
        "q_index": 0,
        "probe_count": 0,
    }
    with patch.object(main, "_load_or_create_session", new_callable=AsyncMock) as m:
        m.return_value = sample
        yield m, sample


@pytest.fixture
def mock_save_session():
    """Patch _save_session to be a no-op."""
    with patch.object(main, "_save_session", new_callable=AsyncMock) as m:
        yield m


@pytest.fixture
def mock_load_profile():
    """Patch _load_profile to return an empty string by default."""
    with patch.object(main, "_load_profile", new_callable=AsyncMock) as m:
        m.return_value = ""
        yield m


@pytest.fixture
def mock_save_profile():
    """Patch _save_profile to be a no-op."""
    with patch.object(main, "_save_profile", new_callable=AsyncMock) as m:
        yield m


@pytest.fixture
def mock_has_completed_onboarding():
    """Patch _has_completed_onboarding — default False (onboarding not done)."""
    with patch.object(main, "_has_completed_onboarding", new_callable=AsyncMock) as m:
        m.return_value = False
        yield m


@pytest.fixture
def mock_get_latest_checkin():
    """Patch _get_latest_checkin — default None (no prior check-in)."""
    with patch.object(main, "_get_latest_checkin", new_callable=AsyncMock) as m:
        m.return_value = None
        yield m


@pytest.fixture
def mock_get_card_priorities():
    """Patch _get_card_priorities — default empty list."""
    with patch.object(main, "_get_card_priorities", new_callable=AsyncMock) as m:
        m.return_value = []
        yield m


@pytest.fixture
def mock_all_db():
    """Convenience: patch ALL Turso DB functions at once so no real DB call
    can escape during tests."""
    with patch.object(main, "_turso_query", new_callable=AsyncMock, return_value=[]) as q, \
         patch.object(main, "_turso_execute", new_callable=AsyncMock) as e, \
         patch.object(main, "_turso_request", new_callable=AsyncMock) as r, \
         patch.object(main, "init_db", new_callable=AsyncMock) as i, \
         patch.object(main, "_load_session", new_callable=AsyncMock, return_value=None) as ls, \
         patch.object(main, "_load_or_create_session", new_callable=AsyncMock) as locs, \
         patch.object(main, "_save_session", new_callable=AsyncMock) as ss, \
         patch.object(main, "_load_profile", new_callable=AsyncMock, return_value="") as lp, \
         patch.object(main, "_save_profile", new_callable=AsyncMock) as sp, \
         patch.object(main, "_has_completed_onboarding", new_callable=AsyncMock, return_value=False) as hco, \
         patch.object(main, "_get_latest_checkin", new_callable=AsyncMock, return_value=None) as glc, \
         patch.object(main, "_get_card_priorities", new_callable=AsyncMock, return_value=[]) as gcp, \
         patch.object(main, "_load_business_profile", new_callable=AsyncMock, return_value=None) as lbp, \
         patch.object(main, "_save_business_profile", new_callable=AsyncMock) as sbp, \
         patch.object(main, "_load_checkin_session", new_callable=AsyncMock) as lcs, \
         patch.object(main, "_save_checkin_session", new_callable=AsyncMock) as scs, \
         patch.object(main, "_clear_checkin_session", new_callable=AsyncMock) as ccs, \
         patch.object(main, "_save_checkin", new_callable=AsyncMock) as sck, \
         patch.object(main, "_save_card_priorities", new_callable=AsyncMock) as scp, \
         patch.object(main, "_reset_session", new_callable=AsyncMock) as rs:

        locs.return_value = {
            "session_id": "test-session-001",
            "conversation": [],
            "q_index": 0,
            "probe_count": 0,
        }
        lcs.return_value = {"messages": [], "step": 0}

        yield {
            "turso_query": q,
            "turso_execute": e,
            "turso_request": r,
            "init_db": i,
            "load_session": ls,
            "load_or_create_session": locs,
            "save_session": ss,
            "load_profile": lp,
            "save_profile": sp,
            "has_completed_onboarding": hco,
            "get_latest_checkin": glc,
            "get_card_priorities": gcp,
            "load_business_profile": lbp,
            "save_business_profile": sbp,
            "load_checkin_session": lcs,
            "save_checkin_session": scs,
            "clear_checkin_session": ccs,
            "save_checkin": sck,
            "save_card_priorities": scp,
            "reset_session": rs,
        }


# ════════════════════════════════════════════════════════════════════
#  Mock LLM fixtures
# ════════════════════════════════════════════════════════════════════

def _make_llm_response(content: str = "Sure, that makes sense.", status_code: int = 200):
    """Build a fake httpx.Response object for _spur_chat_completion mocks."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {
        "choices": [
            {"message": {"content": content, "reasoning": ""}}
        ]
    }
    resp.text = json.dumps({"choices": [{"message": {"content": content}}]})
    return resp


@pytest.fixture
def mock_llm_response():
    """Return the factory so tests can build custom responses."""
    return _make_llm_response


@pytest.fixture
def mock_spur_chat_completion():
    """Patch _spur_chat_completion to return a fake response.
    Tests can override return_value with mock_llm_response(text)."""
    fake = _make_llm_response("That makes sense. How do you handle bookings right now?")
    with patch.object(main, "_spur_chat_completion", new_callable=AsyncMock) as m:
        m.return_value = fake
        yield m, fake


@pytest.fixture
def mock_stream_llm_response():
    """Patch _stream_llm_response to yield canned SSE chunks."""
    async def _fake_stream(messages, model, max_tokens):
        yield 'data: {"content": "That makes sense."}\n\n'
        yield 'data: {"content": " How do you handle bookings?"}\n\n'
        yield "data: [DONE]\n\n"

    with patch.object(main, "_stream_llm_response", side_effect=_fake_stream) as m:
        yield m


@pytest.fixture
def mock_run_analysis():
    """Patch _run_analysis so it doesn't fire real LLM calls in background."""
    with patch.object(main, "_run_analysis", new_callable=AsyncMock) as m:
        yield m


@pytest.fixture
def mock_run_card_selection():
    """Patch _run_card_selection so it doesn't fire real LLM calls."""
    with patch.object(main, "_run_card_selection", new_callable=AsyncMock) as m:
        yield m


@pytest.fixture
def mock_run_checkin_analysis():
    """Patch _run_checkin_analysis."""
    with patch.object(main, "_run_checkin_analysis", new_callable=AsyncMock) as m:
        yield m


@pytest.fixture
def mock_send_transcript_email():
    """Patch _send_transcript_email so no email is sent."""
    with patch.object(main, "_send_transcript_email", new_callable=AsyncMock) as m:
        yield m


# ════════════════════════════════════════════════════════════════════
#  Test client fixture (httpx AsyncClient via ASGI transport)
# ════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def async_client(mock_all_db, mock_stream_llm_response, mock_run_analysis,
                       mock_run_card_selection, mock_run_checkin_analysis,
                       mock_send_transcript_email):
    """Async test client using httpx.ASGITransport against the FastAPI app.

    DB, LLM, and email are all mocked so no external calls are made.
    """
    import httpx
    from main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def sync_client(mock_all_db, mock_stream_llm_response, mock_run_analysis,
                mock_run_card_selection, mock_run_checkin_analysis,
                mock_send_transcript_email):
    """Sync test client using Starlette's TestClient."""
    from starlette.testclient import TestClient
    from main import app

    with TestClient(app) as client:
        yield client


# ════════════════════════════════════════════════════════════════════
#  Sample data fixtures
# ════════════════════════════════════════════════════════════════════

@pytest.fixture
def sample_business_types():
    """All 11 business types defined in main.py."""
    return [
        "Restaurant/Cafe",
        "Salon/Spa/Barber",
        "Plumber/Electrician/HVAC",
        "Retail/Boutique",
        "Gym/Fitness Studio",
        "Landscaping/Lawn Care",
        "Auto Repair/Detailing",
        "Cleaning Service",
        "Photography/Video",
        "Real Estate",
        "Other",
    ]


@pytest.fixture
def sample_conversations():
    """Sample first-answer conversation for each business type, to exercise
    _detect_business_type."""
    return {
        "Restaurant/Cafe": [
            {"role": "user", "content": "Restaurant/Cafe"},
        ],
        "Salon/Spa/Barber": [
            {"role": "user", "content": "Salon/Spa/Barber"},
        ],
        "Plumber/Electrician/HVAC": [
            {"role": "user", "content": "Plumber/Electrician/HVAC"},
        ],
        "Retail/Boutique": [
            {"role": "user", "content": "Retail/Boutique"},
        ],
        "Gym/Fitness Studio": [
            {"role": "user", "content": "Gym/Fitness Studio"},
        ],
        "Landscaping/Lawn Care": [
            {"role": "user", "content": "Landscaping/Lawn Care"},
        ],
        "Auto Repair/Detailing": [
            {"role": "user", "content": "Auto Repair/Detailing"},
        ],
        "Cleaning Service": [
            {"role": "user", "content": "Cleaning Service"},
        ],
        "Photography/Video": [
            {"role": "user", "content": "Photography/Video"},
        ],
        "Real Estate": [
            {"role": "user", "content": "Real Estate"},
        ],
        "Other": [
            {"role": "user", "content": "I run a consulting firm"},
        ],
    }


@pytest.fixture
def sample_fuzzy_conversations():
    """Conversations that use fuzzy / natural-language answers to exercise
    the regex-based fallback detection in _detect_business_type."""
    return {
        "Restaurant/Cafe": [
            {"role": "user", "content": "I run a restaurant downtown"},
        ],
        "Salon/Spa/Barber": [
            {"role": "user", "content": "I own a hair salon"},
        ],
        "Plumber/Electrician/HVAC": [
            {"role": "user", "content": "I'm a plumber by trade"},
        ],
        "Retail/Boutique": [
            {"role": "user", "content": "I have a retail shop on Main St"},
        ],
        "Gym/Fitness Studio": [
            {"role": "user", "content": "I own a gym and fitness studio"},
        ],
        "Landscaping/Lawn Care": [
            {"role": "user", "content": "I do landscaping and lawn care"},
        ],
        "Auto Repair/Detailing": [
            {"role": "user", "content": "I run an auto repair business"},
        ],
        "Cleaning Service": [
            {"role": "user", "content": "I have a cleaning business"},
        ],
        "Photography/Video": [
            {"role": "user", "content": "I do photography and video work"},
        ],
        "Real Estate": [
            {"role": "user", "content": "I work in real estate"},
        ],
        "Other": [
            {"role": "user", "content": "I do something totally different"},
        ],
    }


@pytest.fixture
def sample_session():
    """A mid-survey session with some conversation history."""
    return {
        "session_id": "test-session-001",
        "conversation": [
            {"role": "user", "content": "Restaurant/Cafe"},
            {"role": "assistant", "content": "Got it! When a customer says something nice, what happens next?"},
        ],
        "q_index": 1,
        "probe_count": 0,
    }


@pytest.fixture
def sample_completed_session():
    """A session that has completed all 13 questions."""
    return {
        "session_id": "test-session-complete",
        "conversation": [
            {"role": "user", "content": "Restaurant/Cafe"},
            {"role": "assistant", "content": "Got it."},
            {"role": "user", "content": "We celebrate briefly then move on"},
            {"role": "assistant", "content": "Makes sense."},
            {"role": "user", "content": "I can usually find it"},
            {"role": "assistant", "content": "Nice."},
            {"role": "user", "content": "I usually know why"},
            {"role": "assistant", "content": "Good."},
            {"role": "user", "content": "Show me what happened"},
            {"role": "assistant", "content": "Sure."},
            {"role": "user", "content": "Yes that would turn me off"},
            {"role": "assistant", "content": "Understood."},
            {"role": "user", "content": "Maybe, depends on what it is"},
            {"role": "assistant", "content": "Fair enough."},
            {"role": "user", "content": "Pricing decisions"},
            {"role": "assistant", "content": "Got it."},
            {"role": "user", "content": "Staff scheduling is a pain"},
            {"role": "assistant", "content": "I hear you."},
            {"role": "user", "content": "I look things up on my phone, it's easy"},
            {"role": "assistant", "content": "Makes sense."},
            {"role": "user", "content": "Phone, quick checks mostly"},
            {"role": "assistant", "content": "Good to know."},
            {"role": "user", "content": "That would help a lot"},
            {"role": "assistant", "content": "Great."},
            {"role": "user", "content": "I ask someone for help"},
            ],
        "q_index": 13,
        "probe_count": 0,
    }


@pytest.fixture
def sample_llm_response_with_choices():
    """Sample LLM response text that contains a CHOICES marker."""
    return (
        "That makes sense! It's great that you celebrate briefly.\n\n"
        "When you get a big catering order, how do you usually keep track of it?\n"
        "CHOICES: We write it down | We use a POS system | It's in my head | Something else"
    )


@pytest.fixture
def sample_llm_response_no_choices():
    """Sample LLM response text with no CHOICES marker."""
    return "That's interesting! Tell me more about how you handle that."


@pytest.fixture
def clean_rate_limit_store():
    """Reset the in-memory rate limiter store before and after each test
    that uses it."""
    main._rate_limit_store.clear()
    yield
    main._rate_limit_store.clear()
