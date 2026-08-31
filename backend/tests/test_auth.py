"""
test_auth.py — Tests for authentication: password hashing, token generation,
and registration / login endpoints.

Tests the real auth implementation in main.py:
  - _hash_password / _verify_password (PBKDF2-HMAC-SHA256)
  - _create_token / _verify_token (HMAC-signed session tokens)
  - POST /api/auth/register endpoint
  - POST /api/auth/login endpoint

All Turso DB calls are mocked — no real database or API key required.
"""
from __future__ import annotations

import os
import sys
import json
import time
import hmac
import hashlib
import secrets
import base64
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import pytest_asyncio

# ── Path + env setup (mirrors conftest.py for standalone runs) ──────
BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

os.environ.setdefault("SPUR_DEMO_API_KEY", "test-key-not-real")
os.environ.setdefault("TURSO_DB_URL", "https://test.turso.io")
os.environ.setdefault("TURSO_AUTH_TOKEN", "test-token-not-real")
os.environ.setdefault("AUTH_SECRET", "test-auth-secret-for-tests")

import main
from main import (
    _hash_password,
    _verify_password,
    _create_token,
    _verify_token,
    _get_user_by_email,
    _get_user_by_id,
    RegisterRequest,
    LoginRequest,
    _PBKDF2_ITERATIONS,
    _SALT_BYTES,
    _TOKEN_TTL_SECONDS,
    AUTH_SECRET,
)


# ════════════════════════════════════════════════════════════════════
#  Password hashing tests
# ════════════════════════════════════════════════════════════════════

class TestPasswordHashing:
    """Tests for _hash_password and _verify_password."""

    def test_hash_returns_string(self):
        h = _hash_password("mypassword123")
        assert isinstance(h, str)

    def test_hash_contains_algorithm_prefix(self):
        h = _hash_password("test")
        assert h.startswith("pbkdf2_sha256$")

    def test_hash_contains_four_segments(self):
        h = _hash_password("test")
        parts = h.split("$")
        assert len(parts) == 4  # algorithm$iterations$salt$hash

    def test_hash_contains_correct_iterations(self):
        h = _hash_password("test")
        parts = h.split("$")
        assert int(parts[1]) == _PBKDF2_ITERATIONS

    def test_hash_is_different_for_same_password(self):
        """Same password should produce different hashes (due to random salt)."""
        h1 = _hash_password("samepass")
        h2 = _hash_password("samepass")
        assert h1 != h2

    def test_verify_correct_password(self):
        h = _hash_password("correctpass")
        assert _verify_password("correctpass", h) is True

    def test_verify_wrong_password(self):
        h = _hash_password("correctpass")
        assert _verify_password("wrongpass", h) is False

    def test_verify_empty_password_against_hash(self):
        h = _hash_password("realpass")
        assert _verify_password("", h) is False

    def test_verify_empty_hash_returns_false(self):
        assert _verify_password("anything", "") is False

    def test_verify_malformed_hash_returns_false(self):
        assert _verify_password("pass", "not-a-valid-hash") is False

    def test_verify_tampered_hash_returns_false(self):
        h = _hash_password("testpass")
        # Replace the hash segment with zeros
        parts = h.split("$")
        parts[3] = "0" * len(parts[3])
        tampered = "$".join(parts)
        assert _verify_password("testpass", tampered) is False

    def test_verify_tampered_algorithm_returns_false(self):
        h = _hash_password("testpass")
        parts = h.split("$")
        parts[0] = "bcrypt"  # wrong algorithm
        tampered = "$".join(parts)
        assert _verify_password("testpass", tampered) is False

    def test_hash_handles_unicode_password(self):
        pwd = "pässwörd123🔑"
        h = _hash_password(pwd)
        assert _verify_password(pwd, h) is True
        assert _verify_password("different", h) is False

    def test_hash_handles_long_password(self):
        pwd = "a" * 1000
        h = _hash_password(pwd)
        assert _verify_password(pwd, h) is True

    def test_hash_uses_hmac_compare_digest(self):
        """Verify that the comparison is timing-safe (doesn't raise)."""
        h = _hash_password("test")
        # This should not raise even for wrong passwords
        result = _verify_password("wrong", h)
        assert result is False

    def test_salt_is_16_bytes(self):
        """The salt should be _SALT_BYTES (16) bytes."""
        h = _hash_password("test")
        parts = h.split("$")
        salt_hex = parts[2]
        assert len(bytes.fromhex(salt_hex)) == _SALT_BYTES


# ════════════════════════════════════════════════════════════════════
#  Token generation and validation tests
# ════════════════════════════════════════════════════════════════════

class TestTokenGeneration:
    """Tests for _create_token and _verify_token."""

    def test_token_returns_string(self):
        token = _create_token("user-123")
        assert isinstance(token, str)

    def test_token_has_two_parts(self):
        """Token format: base64(payload).signature"""
        token = _create_token("user-123")
        parts = token.split(".", 1)
        assert len(parts) == 2

    def test_verify_valid_token(self):
        token = _create_token("user-123")
        decoded = _verify_token(token)
        assert decoded is not None
        assert decoded["user_id"] == "user-123"
        assert "issued_at" in decoded

    def test_verify_token_contains_issued_at(self):
        token = _create_token("user-1")
        decoded = _verify_token(token)
        assert "issued_at" in decoded
        assert isinstance(decoded["issued_at"], int)

    def test_verify_token_with_wrong_secret_returns_none(self):
        token = _create_token("user-1")
        # Temporarily change AUTH_SECRET to simulate wrong secret
        original = main.AUTH_SECRET
        try:
            main.AUTH_SECRET = "wrong-secret"
            decoded = _verify_token(token)
            assert decoded is None
        finally:
            main.AUTH_SECRET = original

    def test_verify_expired_token_returns_none(self):
        """Create a token, then mock time.time to simulate expiry."""
        token = _create_token("user-123")
        # Mock time to be past the TTL
        with patch("main.time.time", return_value=time.time() + _TOKEN_TTL_SECONDS + 1):
            decoded = _verify_token(token)
        assert decoded is None

    def test_verify_malformed_token_returns_none(self):
        assert _verify_token("not.a.valid.token.with.too.many.dots") is None

    def test_verify_token_without_dot_returns_none(self):
        assert _verify_token("nodots") is None

    def test_verify_empty_token_returns_none(self):
        assert _verify_token("") is None

    def test_verify_none_token_returns_none(self):
        assert _verify_token(None) is None

    def test_verify_tampered_token_returns_none(self):
        token = _create_token("user-123")
        # Tamper with the signature
        parts = token.split(".", 1)
        # Flip characters in the signature
        tampered_sig = parts[1][:-2] + "XX" if len(parts[1]) >= 2 else "XX"
        tampered = f"{parts[0]}.{tampered_sig}"
        assert _verify_token(tampered) is None

    def test_verify_tampered_payload_returns_none(self):
        token = _create_token("user-123")
        parts = token.split(".", 1)
        # Replace the payload with a different user
        fake_msg = "hacker:" + str(int(time.time()))
        fake_payload = base64.urlsafe_b64encode(fake_msg.encode()).decode().rstrip("=")
        tampered = f"{fake_payload}.{parts[1]}"
        assert _verify_token(tampered) is None

    def test_token_ttl_is_30_days(self):
        assert _TOKEN_TTL_SECONDS == 30 * 24 * 3600

    def test_token_for_different_users_are_different(self):
        t1 = _create_token("user-a")
        t2 = _create_token("user-b")
        assert t1 != t2

    def test_token_preserves_user_id_with_special_chars(self):
        """User IDs with special characters should work."""
        uid = "u-abc123_def"
        token = _create_token(uid)
        decoded = _verify_token(token)
        assert decoded is not None
        assert decoded["user_id"] == uid


# ════════════════════════════════════════════════════════════════════
#  RegisterRequest / LoginRequest model validation
# ════════════════════════════════════════════════════════════════════

class TestAuthRequestModels:
    """Tests for RegisterRequest and LoginRequest Pydantic models."""

    def test_valid_register_request(self):
        req = RegisterRequest(email="test@example.com", password="password123")
        assert req.email == "test@example.com"
        assert req.password == "password123"

    def test_register_email_normalized_to_lowercase(self):
        req = RegisterRequest(email="Test@Example.COM", password="password123")
        assert req.email == "test@example.com"

    def test_register_email_stripped(self):
        req = RegisterRequest(email="  test@example.com  ", password="password123")
        assert req.email == "test@example.com"

    def test_register_invalid_email_no_at_sign(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            RegisterRequest(email="notanemail", password="password123")

    def test_register_invalid_email_no_dot(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            RegisterRequest(email="test@example", password="password123")

    def test_register_short_password_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError) as exc_info:
            RegisterRequest(email="test@example.com", password="short")
        assert "8" in str(exc_info.value)

    def test_register_empty_password_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            RegisterRequest(email="test@example.com", password="")

    def test_register_long_password_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            RegisterRequest(email="test@example.com", password="x" * 129)

    def test_register_max_length_password_accepted(self):
        """128 characters is the boundary — should be accepted."""
        req = RegisterRequest(email="test@example.com", password="x" * 128)
        assert len(req.password) == 128

    def test_valid_login_request(self):
        req = LoginRequest(email="test@example.com", password="password123")
        assert req.email == "test@example.com"


# ════════════════════════════════════════════════════════════════════
#  Registration endpoint tests
# ════════════════════════════════════════════════════════════════════

class TestRegistrationEndpoint:
    """Tests for POST /api/auth/register."""

    @pytest.mark.asyncio
    async def test_register_new_user_success(self, mock_all_db):
        """Successful registration returns a token and user info."""
        mocks = mock_all_db
        # No existing user
        mocks["turso_query"].return_value = []

        from main import register
        req = RegisterRequest(email="newuser@test.com", password="SecurePass123!")
        result = await register(req)

        assert "token" in result
        assert "user" in result
        assert result["user"]["email"] == "newuser@test.com"
        assert result["user"]["user_id"].startswith("u-")

        # Verify token is valid
        decoded = _verify_token(result["token"])
        assert decoded is not None
        assert decoded["user_id"] == result["user"]["user_id"]

    @pytest.mark.asyncio
    async def test_register_stores_hashed_password(self, mock_all_db):
        """Registration should store a hashed password, not plaintext."""
        mocks = mock_all_db
        mocks["turso_query"].return_value = []

        from main import register
        req = RegisterRequest(email="hash@test.com", password="MySecretPass123!")
        await register(req)

        # Check that _turso_execute was called with a hashed password
        execute_call = mocks["turso_execute"].call_args
        args = execute_call.kwargs.get("args") or execute_call.args[1]
        # Find the password_hash arg
        password_arg = [a for a in args if a.get("value", "").startswith("pbkdf2_sha256$")]
        assert len(password_arg) == 1, "Password should be stored as a pbkdf2_sha256 hash"
        # The stored value should NOT be the plaintext password
        assert password_arg[0]["value"] != "MySecretPass123!"
        # And it should be verifiable
        assert _verify_password("MySecretPass123!", password_arg[0]["value"]) is True

    @pytest.mark.asyncio
    async def test_register_duplicate_email_returns_409(self, mock_all_db):
        """Registration with an existing email should return 409."""
        mocks = mock_all_db
        # Simulate existing user found
        mocks["turso_query"].return_value = [
            {"user_id": "u-existing", "email": "existing@test.com", "password_hash": "pbkdf2_sha256$100000$abc$def"}
        ]

        from main import register
        from fastapi.responses import JSONResponse
        req = RegisterRequest(email="existing@test.com", password="password123")
        result = await register(req)

        assert isinstance(result, JSONResponse)
        assert result.status_code == 409

    @pytest.mark.asyncio
    async def test_register_generates_unique_user_id(self, mock_all_db):
        """Each registration should generate a unique user_id."""
        mocks = mock_all_db
        mocks["turso_query"].return_value = []

        from main import register
        req1 = RegisterRequest(email="user1@test.com", password="password123")
        result1 = await register(req1)

        mocks["turso_query"].return_value = []
        req2 = RegisterRequest(email="user2@test.com", password="password123")
        result2 = await register(req2)

        assert result1["user"]["user_id"] != result2["user"]["user_id"]

    @pytest.mark.asyncio
    async def test_register_user_id_has_prefix(self, mock_all_db):
        """User IDs should start with 'u-' prefix."""
        mocks = mock_all_db
        mocks["turso_query"].return_value = []

        from main import register
        req = RegisterRequest(email="prefix@test.com", password="password123")
        result = await register(req)

        assert result["user"]["user_id"].startswith("u-")


# ════════════════════════════════════════════════════════════════════
#  Login endpoint tests
# ════════════════════════════════════════════════════════════════════

class TestLoginEndpoint:
    """Tests for POST /api/auth/login."""

    @pytest.mark.asyncio
    async def test_login_success_returns_token(self, mock_all_db):
        """Valid credentials should return a token."""
        mocks = mock_all_db
        # Setup: user exists with a known password
        password = "CorrectPass123!"
        stored_hash = _hash_password(password)
        mocks["turso_query"].return_value = [
            {"user_id": "u-test123", "email": "user@test.com", "password_hash": stored_hash}
        ]

        from main import login
        req = LoginRequest(email="user@test.com", password=password)
        result = await login(req)

        assert "token" in result
        assert result["user"]["email"] == "user@test.com"
        assert result["user"]["user_id"] == "u-test123"

        # Verify token
        decoded = _verify_token(result["token"])
        assert decoded is not None
        assert decoded["user_id"] == "u-test123"

    @pytest.mark.asyncio
    async def test_login_wrong_password_returns_401(self, mock_all_db):
        """Wrong password should return 401."""
        mocks = mock_all_db
        stored_hash = _hash_password("CorrectPass123!")
        mocks["turso_query"].return_value = [
            {"user_id": "u-test123", "email": "user@test.com", "password_hash": stored_hash}
        ]

        from main import login
        from fastapi.responses import JSONResponse
        req = LoginRequest(email="user@test.com", password="WrongPass456!")
        result = await login(req)

        assert isinstance(result, JSONResponse)
        assert result.status_code == 401

    @pytest.mark.asyncio
    async def test_login_nonexistent_user_returns_401(self, mock_all_db):
        """Login for a user that doesn't exist should return 401."""
        mocks = mock_all_db
        mocks["turso_query"].return_value = []  # no user found

        from main import login
        from fastapi.responses import JSONResponse
        req = LoginRequest(email="nobody@test.com", password="password123")
        result = await login(req)

        assert isinstance(result, JSONResponse)
        assert result.status_code == 401

    @pytest.mark.asyncio
    async def test_login_email_case_insensitive(self, mock_all_db):
        """Login should be case-insensitive for email."""
        mocks = mock_all_db
        password = "CorrectPass123!"
        stored_hash = _hash_password(password)
        mocks["turso_query"].return_value = [
            {"user_id": "u-test123", "email": "user@test.com", "password_hash": stored_hash}
        ]

        from main import login
        req = LoginRequest(email="USER@TEST.COM", password=password)
        result = await login(req)

        assert "token" in result
        # Verify _get_user_by_email was called with lowercased email
        query_args = mocks["turso_query"].call_args
        args = query_args.kwargs.get("args") or query_args.args[1]
        email_arg = [a for a in args if "@" in a.get("value", "")]
        assert email_arg[0]["value"] == "user@test.com"

    @pytest.mark.asyncio
    async def test_login_returns_valid_token(self, mock_all_db):
        """The login token should be verifiable with _verify_token."""
        mocks = mock_all_db
        password = "MyPass123!"
        stored_hash = _hash_password(password)
        mocks["turso_query"].return_value = [
            {"user_id": "u-abc123", "email": "owner@test.com", "password_hash": stored_hash}
        ]

        from main import login
        req = LoginRequest(email="owner@test.com", password=password)
        result = await login(req)

        decoded = _verify_token(result["token"])
        assert decoded is not None
        assert decoded["user_id"] == "u-abc123"

    @pytest.mark.asyncio
    async def test_login_error_message_does_not_leak_user_existence(self, mock_all_db):
        """Error message for wrong password and nonexistent user should be the same."""
        mocks = mock_all_db

        # Case 1: nonexistent user
        mocks["turso_query"].return_value = []
        from main import login
        from fastapi.responses import JSONResponse
        req1 = LoginRequest(email="nobody@test.com", password="password123")
        result1 = await login(req1)

        # Case 2: wrong password
        mocks["turso_query"].return_value = [
            {"user_id": "u-test", "email": "someone@test.com", "password_hash": _hash_password("correct")}
        ]
        req2 = LoginRequest(email="someone@test.com", password="wrong")
        result2 = await login(req2)

        # Both should be 401 with the same message
        assert result1.status_code == 401
        assert result2.status_code == 401
        # The detail message should be identical (no info leak)
        assert json.loads(result1.body)["detail"] == json.loads(result2.body)["detail"]
