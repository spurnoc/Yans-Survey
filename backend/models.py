"""
SPUR Survey — Pydantic request models.

Extracted from main.py. Importable by main.py and other modules.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, field_validator


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
