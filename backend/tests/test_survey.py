"""
test_survey.py — Tests for the Daily 15 survey flow.

Covers:
  - ChatRequest validation (session_id format, answer length)
  - _validate_session_id_param function
  - _detect_business_type for all 11 types (exact + fuzzy matching)
  - _get_questions_for_type returns 13 questions for each type
  - _parse_response extracts CHOICES correctly
  - Rate limiter blocks after 20 requests
  - _build_system_prompt generates correct prompt
  - _build_checkin_prompt generates correct prompt
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

import main
from main import (
    ChatRequest,
    _validate_session_id_param,
    _detect_business_type,
    _get_questions_for_type,
    _parse_response,
    _check_rate_limit,
    _build_system_prompt,
    _build_checkin_prompt,
    BUSINESS_TYPES,
    _RATE_LIMIT_MAX_REQUESTS,
    _RATE_LIMIT_WINDOW,
)
from pydantic import ValidationError


# ════════════════════════════════════════════════════════════════════
#  ChatRequest validation
# ════════════════════════════════════════════════════════════════════

class TestChatRequestValidation:
    """Tests for Pydantic ChatRequest model validation."""

    def test_valid_chat_request(self):
        req = ChatRequest(answer="Restaurant/Cafe", session_id="abc-123_test")
        assert req.answer == "Restaurant/Cafe"
        assert req.session_id == "abc-123_test"

    def test_valid_session_id_with_hyphens(self):
        req = ChatRequest(answer="test", session_id="session-id-001")
        assert req.session_id == "session-id-001"

    def test_valid_session_id_with_underscores(self):
        req = ChatRequest(answer="test", session_id="session_id_001")
        assert req.session_id == "session_id_001"

    def test_valid_session_id_alphanumeric_only(self):
        req = ChatRequest(answer="test", session_id="abc123")
        assert req.session_id == "abc123"

    # ── session_id format validation ───────────────────────────────

    def test_empty_session_id_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            ChatRequest(answer="test", session_id="")
        assert "empty" in str(exc_info.value).lower()

    def test_session_id_with_spaces_rejected(self):
        with pytest.raises(ValidationError):
            ChatRequest(answer="test", session_id="has space")

    def test_session_id_with_special_chars_rejected(self):
        with pytest.raises(ValidationError):
            ChatRequest(answer="test", session_id="bad@chars!")

    def test_session_id_with_slash_rejected(self):
        with pytest.raises(ValidationError):
            ChatRequest(answer="test", session_id="a/b")

    def test_session_id_with_dot_rejected(self):
        with pytest.raises(ValidationError):
            ChatRequest(answer="test", session_id="a.b")

    def test_session_id_too_long_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            ChatRequest(answer="test", session_id="a" * 101)
        assert "100" in str(exc_info.value)

    def test_session_id_max_length_accepted(self):
        """100 characters is the boundary — should be accepted."""
        req = ChatRequest(answer="test", session_id="a" * 100)
        assert len(req.session_id) == 100

    # ── answer validation ───────────────────────────────────────────

    def test_empty_answer_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            ChatRequest(answer="", session_id="valid-id")
        assert "empty" in str(exc_info.value).lower()

    def test_whitespace_only_answer_rejected(self):
        with pytest.raises(ValidationError):
            ChatRequest(answer="   ", session_id="valid-id")

    def test_answer_over_5000_chars_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            ChatRequest(answer="x" * 5001, session_id="valid-id")
        assert "5000" in str(exc_info.value)

    def test_answer_exactly_5000_chars_accepted(self):
        """5000 characters is the boundary — should be accepted."""
        req = ChatRequest(answer="x" * 5000, session_id="valid-id")
        assert len(req.answer) == 5000


# ════════════════════════════════════════════════════════════════════
#  _validate_session_id_param
# ════════════════════════════════════════════════════════════════════

class TestValidateSessionIdParam:
    """Tests for _validate_session_id_param (used by GET endpoints)."""

    def test_valid_session_id_returns_id(self):
        assert _validate_session_id_param("valid-id_123") == "valid-id_123"

    def test_empty_session_id_raises_422(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _validate_session_id_param("")
        assert exc_info.value.status_code == 422

    def test_session_id_too_long_raises_422(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _validate_session_id_param("a" * 101)
        assert exc_info.value.status_code == 422

    def test_session_id_invalid_chars_raises_422(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            _validate_session_id_param("bad@chars!")

    def test_session_id_with_space_raises_422(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            _validate_session_id_param("has space")

    def test_error_message_mentions_alphanumeric(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _validate_session_id_param("bad@char")
        detail = exc_info.value.detail
        assert "alphanumeric" in detail.lower() or "hyphen" in detail.lower()


# ════════════════════════════════════════════════════════════════════
#  _detect_business_type — all 11 types
# ════════════════════════════════════════════════════════════════════

class TestDetectBusinessType:
    """Tests for _detect_business_type covering all 11 types."""

    def test_empty_conversation_returns_other(self):
        assert _detect_business_type([]) == "Other"

    def test_no_user_messages_returns_other(self):
        conv = [{"role": "assistant", "content": "Hello!"}]
        assert _detect_business_type(conv) == "Other"

    def test_all_11_types_exact_match(self, sample_conversations):
        """Exact business-type strings should match directly."""
        for btype, conv in sample_conversations.items():
            detected = _detect_business_type(conv)
            assert detected == btype, (
                f"Expected {btype!r}, got {detected!r} for conversation: {conv}"
            )

    def test_all_11_types_fuzzy_match(self, sample_fuzzy_conversations):
        """Natural-language answers should be detected via fuzzy/regex matching."""
        for btype, conv in sample_fuzzy_conversations.items():
            detected = _detect_business_type(conv)
            assert detected == btype, (
                f"Fuzzy: expected {btype!r}, got {detected!r} for: {conv[0]['content']!r}"
            )

    def test_business_types_list_has_11_entries(self):
        assert len(BUSINESS_TYPES) == 11

    def test_business_types_list_contains_expected_types(self):
        expected = {
            "Restaurant/Cafe", "Salon/Spa/Barber", "Plumber/Electrician/HVAC",
            "Retail/Boutique", "Gym/Fitness Studio", "Landscaping/Lawn Care",
            "Auto Repair/Detailing", "Cleaning Service", "Photography/Video",
            "Real Estate", "Other",
        }
        assert set(BUSINESS_TYPES) == expected

    # ── Fuzzy match edge cases ─────────────────────────────────────

    def test_fuzzy_restaurant_from_cafe(self):
        conv = [{"role": "user", "content": "I own a small cafe"}]
        assert _detect_business_type(conv) == "Restaurant/Cafe"

    def test_fuzzy_salon_from_barber(self):
        conv = [{"role": "user", "content": "I'm a barber"}]
        assert _detect_business_type(conv) == "Salon/Spa/Barber"

    def test_fuzzy_plumber_from_electrician(self):
        conv = [{"role": "user", "content": "I'm an electrician"}]
        assert _detect_business_type(conv) == "Plumber/Electrician/HVAC"

    def test_fuzzy_plumber_from_hvac(self):
        conv = [{"role": "user", "content": "We do HVAC"}]
        assert _detect_business_type(conv) == "Plumber/Electrician/HVAC"

    def test_fuzzy_retail_from_boutique(self):
        conv = [{"role": "user", "content": "I run a boutique"}]
        assert _detect_business_type(conv) == "Retail/Boutique"

    def test_fuzzy_retail_from_store(self):
        conv = [{"role": "user", "content": "I have a store downtown"}]
        assert _detect_business_type(conv) == "Retail/Boutique"

    def test_fuzzy_gym_from_fitness(self):
        conv = [{"role": "user", "content": "I run a fitness studio"}]
        assert _detect_business_type(conv) == "Gym/Fitness Studio"

    def test_fuzzy_landscaping_from_lawn(self):
        conv = [{"role": "user", "content": "I do lawn care"}]
        assert _detect_business_type(conv) == "Landscaping/Lawn Care"

    def test_fuzzy_auto_from_repair(self):
        conv = [{"role": "user", "content": "I do repair work"}]
        assert _detect_business_type(conv) == "Auto Repair/Detailing"

    def test_fuzzy_cleaning_from_clean(self):
        conv = [{"role": "user", "content": "I clean houses"}]
        assert _detect_business_type(conv) == "Cleaning Service"

    def test_fuzzy_photography_from_photo(self):
        conv = [{"role": "user", "content": "I'm a photographer"}]
        assert _detect_business_type(conv) == "Photography/Video"

    def test_fuzzy_real_estate(self):
        conv = [{"role": "user", "content": "I work in realty"}]
        assert _detect_business_type(conv) == "Real Estate"

    def test_fuzzy_unknown_returns_other(self):
        conv = [{"role": "user", "content": "I do consulting"}]
        assert _detect_business_type(conv) == "Other"

    def test_case_insensitive_matching(self):
        conv = [{"role": "user", "content": "RESTAURANT/CAFE"}]
        assert _detect_business_type(conv) == "Restaurant/Cafe"

    def test_detect_uses_first_user_message_only(self):
        conv = [
            {"role": "user", "content": "Restaurant/Cafe"},
            {"role": "assistant", "content": "Great!"},
            {"role": "user", "content": "Plumber/Electrician/HVAC"},  # later msg
        ]
        assert _detect_business_type(conv) == "Restaurant/Cafe"


# ════════════════════════════════════════════════════════════════════
#  _get_questions_for_type — returns 13 questions for each type
# ════════════════════════════════════════════════════════════════════

class TestGetQuestionsForType:
    """Tests for _get_questions_for_type."""

    def test_returns_13_questions_for_each_type(self, sample_business_types):
        for btype in sample_business_types:
            questions = _get_questions_for_type(btype)
            assert len(questions) == 13, (
                f"Expected 13 questions for {btype!r}, got {len(questions)}"
            )

    def test_first_question_is_always_q1_business_type(self):
        for btype in BUSINESS_TYPES:
            questions = _get_questions_for_type(btype)
            q1 = questions[0]
            assert q1["id"] == 1
            assert q1["tag"] == "business_type"
            assert "business" in q1["text"].lower()

    def test_questions_are_sequential_ids(self):
        """Question IDs should be 1 through 13."""
        for btype in BUSINESS_TYPES:
            questions = _get_questions_for_type(btype)
            ids = [q["id"] for q in questions]
            assert ids == list(range(1, 14)), (
                f"Question IDs for {btype!r} are not sequential 1-13: {ids}"
            )

    def test_questions_have_required_fields(self):
        for btype in BUSINESS_TYPES:
            questions = _get_questions_for_type(btype)
            for q in questions:
                assert "id" in q
                assert "text" in q
                assert "type" in q
                assert "tag" in q
                assert q["type"] in ("text", "choice")

    def test_unknown_type_falls_back_to_other(self):
        """Passing a type not in QUESTIONS_BY_TYPE should fall back to 'Other'."""
        questions = _get_questions_for_type("Nonexistent Type")
        assert len(questions) == 13

    def test_q5_is_proactive_question(self):
        """Q5 is shared across all types and has tag 'proactive'."""
        for btype in BUSINESS_TYPES:
            questions = _get_questions_for_type(btype)
            q5 = questions[4]  # 0-indexed, so index 4 is Q5
            assert q5["id"] == 5
            assert q5["tag"] == "proactive"

    def test_universal_questions_present_in_all_types(self):
        """Q6-Q13 (universal questions) should be present for every type."""
        universal_tags = {"trust", "ai_trust", "second_opinion", "pain_point",
                         "tech_comfort", "habits", "density", "ux_reaction"}
        for btype in BUSINESS_TYPES:
            questions = _get_questions_for_type(btype)
            tags = {q["tag"] for q in questions}
            assert universal_tags.issubset(tags), (
                f"Missing universal tags for {btype!r}: {universal_tags - tags}"
            )


# ════════════════════════════════════════════════════════════════════
#  _parse_response — CHOICES extraction
# ════════════════════════════════════════════════════════════════════

class TestParseResponse:
    """Tests for _parse_response — extracts CHOICES from AI response text."""

    def test_extracts_choices_from_marker(self, sample_llm_response_with_choices):
        clean_text, choices = _parse_response(sample_llm_response_with_choices)
        assert len(choices) == 4
        assert "We write it down" in choices[0]
        assert "Something else" in choices[-1]

    def test_choices_are_stripped_of_whitespace(self):
        text = "Nice!\nCHOICES:  Option A  |  Option B  |  Option C"
        _, choices = _parse_response(text)
        assert all(c == c.strip() for c in choices)
        assert choices == ["Option A", "Option B", "Option C"]

    def test_clean_text_excludes_choices_line(self, sample_llm_response_with_choices):
        clean_text, _ = _parse_response(sample_llm_response_with_choices)
        assert "CHOICES:" not in clean_text

    def test_no_choices_returns_empty_list(self, sample_llm_response_no_choices):
        _, choices = _parse_response(sample_llm_response_no_choices)
        assert choices == []

    def test_empty_choices_marker(self):
        text = "Hello!\nCHOICES: "
        clean_text, choices = _parse_response(text)
        assert choices == []

    def test_single_choice(self):
        text = "Got it.\nCHOICES: Only one option"
        _, choices = _parse_response(text)
        assert len(choices) == 1
        assert choices[0] == "Only one option"

    def test_choices_case_insensitive_marker(self):
        text = "Sure.\nchoices: A | B | C"
        _, choices = _parse_response(text)
        assert len(choices) == 3

    def test_choices_marker_anywhere_in_text(self):
        text = "Some intro text\nCHOICES: X | Y\nSome trailing text"
        clean_text, choices = _parse_response(text)
        assert len(choices) == 2
        assert "CHOICES" not in clean_text

    def test_pipe_only_choices_ignored(self):
        text = "Response.\nCHOICES: | | |"
        _, choices = _parse_response(text)
        # Empty segments between pipes are filtered out
        assert choices == []

    def test_clean_text_collapses_excessive_newlines(self):
        text = "Line 1\n\n\n\n\nLine 2\nCHOICES: A | B"
        clean_text, _ = _parse_response(text)
        # Should not have 3+ consecutive newlines
        assert "\n\n\n" not in clean_text

    def test_clean_text_stripped(self):
        text = "  Hello  \nCHOICES: A"
        clean_text, _ = _parse_response(text)
        assert clean_text == "Hello"


# ════════════════════════════════════════════════════════════════════
#  Rate limiter
# ════════════════════════════════════════════════════════════════════

class TestRateLimiter:
    """Tests for the in-memory per-session rate limiter."""

    @pytest.mark.asyncio
    async def test_allows_first_request(self, clean_rate_limit_store):
        result = await _check_rate_limit("session-1")
        assert result is True

    @pytest.mark.asyncio
    async def test_allows_up_to_20_requests(self, clean_rate_limit_store):
        for i in range(20):
            result = await _check_rate_limit("session-rate-1")
            assert result is True, f"Request {i+1} should be allowed"

    @pytest.mark.asyncio
    async def test_blocks_21st_request(self, clean_rate_limit_store):
        for _ in range(20):
            await _check_rate_limit("session-rate-2")
        result = await _check_rate_limit("session-rate-2")
        assert result is False

    @pytest.mark.asyncio
    async def test_rate_limit_is_per_session(self, clean_rate_limit_store):
        """Different sessions have independent limits."""
        for _ in range(20):
            await _check_rate_limit("session-a")
        # session-b should still be allowed
        result = await _check_rate_limit("session-b")
        assert result is True

    @pytest.mark.asyncio
    async def test_rate_limit_stores_timestamps(self, clean_rate_limit_store):
        await _check_rate_limit("session-store-1")
        assert "session-store-1" in main._rate_limit_store
        assert len(main._rate_limit_store["session-store-1"]) == 1

    @pytest.mark.asyncio
    async def test_rate_limit_max_is_20(self):
        """Verify the configured limit is 20."""
        assert _RATE_LIMIT_MAX_REQUESTS == 20

    @pytest.mark.asyncio
    async def test_rate_limit_window_is_60(self):
        """Verify the configured window is 60 seconds."""
        assert _RATE_LIMIT_WINDOW == 60


# ════════════════════════════════════════════════════════════════════
#  _build_system_prompt
# ════════════════════════════════════════════════════════════════════

class TestBuildSystemPrompt:
    """Tests for _build_system_prompt."""

    @pytest.mark.asyncio
    async def test_prompt_contains_business_type(self, mock_load_profile, sample_session):
        """The detected business type should appear in the prompt."""
        prompt = await _build_system_prompt(sample_session, 1, "When a customer says something nice...", 2)
        assert "Restaurant/Cafe" in prompt

    @pytest.mark.asyncio
    async def test_prompt_contains_question_number(self, mock_load_profile, sample_session):
        prompt = await _build_system_prompt(sample_session, 1, "old question", 2)
        assert "question #" in prompt.lower()

    @pytest.mark.asyncio
    async def test_prompt_contains_target_question_text(self, mock_load_profile, sample_session):
        """The target question text should be in the prompt."""
        target_index = 2
        prompt = await _build_system_prompt(sample_session, 1, "answered", target_index)
        active_questions = _get_questions_for_type("Restaurant/Cafe")
        target_q_text = active_questions[target_index]["text"]
        assert target_q_text in prompt

    @pytest.mark.asyncio
    async def test_prompt_includes_choice_instruction_for_choice_type(self, mock_load_profile, sample_session):
        """When the target question is type 'choice', the CHOICES instruction should be present."""
        # Q1 is type "choice"
        prompt = await _build_system_prompt(sample_session, 0, "", 0)
        assert "CHOICES" in prompt

    @pytest.mark.asyncio
    async def test_prompt_excludes_choice_instruction_for_text_type(self, mock_load_profile, sample_session):
        """When the target question is type 'text', no CHOICES instruction."""
        # Q2 for Restaurant/Cafe is type "text"
        prompt = await _build_system_prompt(sample_session, 0, "", 1)
        assert "CHOICES" not in prompt

    @pytest.mark.asyncio
    async def test_prompt_contains_asked_questions(self, mock_load_profile, sample_session):
        """Previously asked questions should be listed."""
        prompt = await _build_system_prompt(sample_session, 1, "answered text", 2)
        assert "QUESTIONS ALREADY ASKED" in prompt

    @pytest.mark.asyncio
    async def test_prompt_includes_profile_when_present(self, sample_session):
        """When a behavioral profile exists, it should be included in the prompt."""
        with patch.object(main, "_load_profile", new_callable=AsyncMock) as mock_lp:
            mock_lp.return_value = "## Communication Style\n- Very casual speaker"
            prompt = await _build_system_prompt(sample_session, 1, "answered", 2)
            assert "BEHAVIORAL PROFILE" in prompt
            assert "Very casual speaker" in prompt

    @pytest.mark.asyncio
    async def test_prompt_excludes_profile_when_empty(self, sample_session):
        with patch.object(main, "_load_profile", new_callable=AsyncMock) as mock_lp:
            mock_lp.return_value = ""
            prompt = await _build_system_prompt(sample_session, 1, "answered", 2)
            assert "BEHAVIORAL PROFILE" not in prompt

    @pytest.mark.asyncio
    async def test_prompt_completion_message_when_no_more_questions(self, mock_load_profile, sample_completed_session):
        """When all questions are done, prompt should say survey is complete."""
        prompt = await _build_system_prompt(sample_completed_session, 12, "last question", 13)
        assert "complete" in prompt.lower()

    @pytest.mark.asyncio
    async def test_prompt_has_rules_section(self, mock_load_profile, sample_session):
        prompt = await _build_system_prompt(sample_session, 1, "answered", 2)
        assert "CRITICAL RULES" in prompt


# ════════════════════════════════════════════════════════════════════
#  _build_checkin_prompt
# ════════════════════════════════════════════════════════════════════

class TestBuildCheckinPrompt:
    """Tests for _build_checkin_prompt."""

    @pytest.mark.asyncio
    async def test_prompt_contains_checkin_language(self, mock_all_db):
        """Prompt should use check-in / daily conversation language."""
        mocks = mock_all_db
        # Simulate an existing onboarding conversation in Turso
        mocks["turso_query"].return_value = [
            {"conversation": json.dumps([{"role": "user", "content": "Restaurant/Cafe"}])}
        ]
        mocks["load_profile"].return_value = ""

        prompt = await _build_checkin_prompt("test-sess-1", [{"role": "user", "content": "Good day"}], 0)
        assert "check-in" in prompt.lower() or "daily" in prompt.lower()

    @pytest.mark.asyncio
    async def test_prompt_contains_step_number(self, mock_all_db):
        mocks = mock_all_db
        mocks["turso_query"].return_value = [
            {"conversation": json.dumps([{"role": "user", "content": "Restaurant/Cafe"}])}
        ]
        prompt = await _build_checkin_prompt("test-sess-1", [{"role": "user", "content": "Hi"}], 2)
        assert "STEP 2" in prompt

    @pytest.mark.asyncio
    async def test_prompt_includes_profile_when_present(self, mock_all_db):
        mocks = mock_all_db
        mocks["turso_query"].return_value = [
            {"conversation": json.dumps([{"role": "user", "content": "Restaurant/Cafe"}])}
        ]
        mocks["load_profile"].return_value = "## Communication Style\n- Casual speaker"

        prompt = await _build_checkin_prompt("test-sess-1", [{"role": "user", "content": "Hi"}], 0)
        assert "BEHAVIORAL PROFILE" in prompt
        assert "Casual speaker" in prompt

    @pytest.mark.asyncio
    async def test_prompt_includes_onboarding_summary(self, mock_all_db):
        mocks = mock_all_db
        mocks["turso_query"].return_value = [
            {"conversation": json.dumps([
                {"role": "user", "content": "Restaurant/Cafe"},
                {"role": "user", "content": "We write things down"},
            ])}
        ]
        mocks["load_profile"].return_value = ""

        prompt = await _build_checkin_prompt("test-sess-1", [{"role": "user", "content": "Hi"}], 0)
        assert "onboarding" in prompt.lower() or "told us" in prompt.lower()

    @pytest.mark.asyncio
    async def test_prompt_has_rules_section(self, mock_all_db):
        mocks = mock_all_db
        mocks["turso_query"].return_value = [
            {"conversation": json.dumps([{"role": "user", "content": "Restaurant/Cafe"}])}
        ]
        prompt = await _build_checkin_prompt("test-sess-1", [{"role": "user", "content": "Hi"}], 0)
        assert "RULES" in prompt

    @pytest.mark.asyncio
    async def test_prompt_uses_business_type_specific_questions(self, mock_all_db):
        """Different business types should produce different check-in questions."""
        mocks = mock_all_db

        # Restaurant
        mocks["turso_query"].return_value = [
            {"conversation": json.dumps([{"role": "user", "content": "Restaurant/Cafe"}])}
        ]
        restaurant_prompt = await _build_checkin_prompt("test-sess-1", [{"role": "user", "content": "Hi"}], 1)

        # Salon
        mocks["turso_query"].return_value = [
            {"conversation": json.dumps([{"role": "user", "content": "Salon/Spa/Barber"}])}
        ]
        salon_prompt = await _build_checkin_prompt("test-sess-1", [{"role": "user", "content": "Hi"}], 1)

        # The step-2 check-in questions differ between restaurant and salon
        assert restaurant_prompt != salon_prompt

    @pytest.mark.asyncio
    async def test_prompt_clamps_step_to_max(self, mock_all_db):
        """Steps beyond the question list should clamp to the last question."""
        mocks = mock_all_db
        mocks["turso_query"].return_value = [
            {"conversation": json.dumps([{"role": "user", "content": "Restaurant/Cafe"}])}
        ]
        prompt = await _build_checkin_prompt("test-sess-1", [{"role": "user", "content": "Hi"}], 99)
        # Should contain "Wrap up" which is the last question for all types
        assert "wrap up" in prompt.lower()
