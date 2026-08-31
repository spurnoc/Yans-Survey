"""
SPUR Survey — LLM API functions for the SPUR chat completion API.

All functions extracted from main.py. Uses a shared module-level
httpx.AsyncClient for connection pooling — all LLM calls reuse the
same TCP connection.
"""
from __future__ import annotations

import json
import re
import logging
from typing import AsyncGenerator

import httpx

logger = logging.getLogger(__name__)

# ── Config (read from environment at import time, matching main.py) ──
import os

SPUR_API_BASE = os.getenv("SPUR_API_BASE", "https://ai.spuric.com/v1")
SPUR_DEMO_API_KEY = os.getenv("SPUR_DEMO_API_KEY", "")
SURVEY_MODEL = os.getenv("SURVEY_MODEL", "spur-glm-5-2")
ANALYSIS_MODEL = os.getenv("ANALYSIS_MODEL", "spur-glm-air")


# ── Shared httpx.AsyncClient for connection pooling ──────────────
_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Return a shared module-level httpx.AsyncClient (lazy-init).

    All LLM calls reuse the same client (and TCP connection) for free
    connection pooling. Re-created automatically if closed.
    """
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30.0)
    return _http_client


def _extract_llm_content(data: dict) -> str:
    """Extract text content from an LLM chat completion response.
    Falls back to 'reasoning' if 'content' is empty.
    """
    msg = data["choices"][0]["message"]
    return msg.get("content") or msg.get("reasoning") or ""


def _extract_json_from_llm(content: str) -> dict | None:
    """Extract and parse a JSON object from LLM response text.
    Returns None if no JSON found or parse fails.
    """
    json_match = re.search(r'\{.*\}', content, re.DOTALL)
    if not json_match:
        return None
    try:
        return json.loads(json_match.group())
    except json.JSONDecodeError:
        return None


async def _spur_chat_completion(messages, model, temperature=0.6, max_tokens=1000, stream=False, timeout=30.0) -> httpx.Response:
    """Send a chat completion request to the SPUR API. Returns the httpx.Response.

    Uses the shared module-level httpx.AsyncClient for connection pooling.
    Note: when stream=True, a dedicated streaming client is used because
    the shared client's timeout may be too short for streaming responses.
    """
    if stream:
        # Streaming needs a longer-lived client with extended timeout
        client = httpx.AsyncClient(timeout=httpx.Timeout(timeout))
        try:
            resp = await client.post(
                f"{SPUR_API_BASE}/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": True,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                headers={
                    "Authorization": f"Bearer {SPUR_DEMO_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            return resp
        except Exception:
            await client.aclose()
            raise
    else:
        client = _get_client()
        resp = await client.post(
            f"{SPUR_API_BASE}/chat/completions",
            json={
                "model": model,
                "messages": messages,
                "stream": False,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            headers={
                "Authorization": f"Bearer {SPUR_DEMO_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        return resp


async def _stream_llm_response(messages: list[dict], model: str, max_tokens: int) -> AsyncGenerator[str, None]:
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
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0)) as client:
            async with client.stream(
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
            ) as resp:
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


def _append_recent_context(messages: list[dict], conversation: list[dict], window: int = 4) -> list[dict]:
    """Append the last N messages from conversation to messages list."""
    recent = conversation[-window:]
    for msg in recent:
        if msg["role"] == "user":
            messages.append({"role": "user", "content": msg["content"]})
        else:
            messages.append({"role": "assistant", "content": msg["content"]})
    return messages
