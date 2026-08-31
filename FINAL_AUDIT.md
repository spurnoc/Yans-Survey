# FINAL Forensic Audit Report — Daily 15

**Date:** August 30, 2026  
**Auditor:** Hermes Agent (automated)  
**Repository:** `survey-deploy` (branch `dev`, commit `baf01b3`)  
**Deployment:** http://daily15.spurnoc.com  
**Audit scope:** `backend/main.py` (1484 lines), `frontend/index.html` (878 lines), `frontend/dashboard.html` (1636 lines), `Dockerfile`, `.dockerignore`, `backend/requirements.txt`

---

## Executive Summary

| Category | Grade | Notes |
|---|---|---|
| **Security** | B+ | No XSS, CORS locked down, input validation on all endpoints, rate limiting. Missing CSP header (medium). X-XSS-Protection deprecated. |
| **Code Quality** | B− | Clean async patterns, proper error handling, no bare excepts. Dead variable in `_stream_llm_response`, stale comments, minor `del` race edge case. |
| **Error Handling** | B | Best-effort try/except on all background tasks, SSE error yields, graceful degradation. `resetSurvey` doesn't check response status. |
| **Frontend Safety** | A− | All user content via `textContent`/`escapeHtml`/React `createElement`. AbortController timeouts on all fetch calls. ARIA throughout. |
| **Docker/Deploy** | A | Non-root user, healthcheck, `.dockerignore` with `*.bak`, pinned deps. No `.bak` files or stale artifacts in image. |
| **Architecture** | C+ | Single god-file (deferred Phase 2), function-attribute memory for check-ins (deferred Phase 2), no module separation. Functional but hard to maintain. |

**Overall Grade: B**

All 9 issues from the re-audit (fix round 2) are **verified resolved**. No regression bugs were introduced. 10 new issues were found — 1 MEDIUM severity, 9 LOW severity. None are critical or blocking. The codebase has improved significantly from the initial D+ grade through two rounds of fixes.

---

## Re-Audit Fix Verification (Round 2 — All 9 Confirmed)

| # | Issue | Status | Evidence |
|---|---|---|---|
| 1 | POST `/reset` missing session_id validation | ✅ Fixed | `main.py:1440-1441` — calls `_validate_session_id_param()` which checks empty, length >100, regex `^[a-zA-Z0-9_-]+$` |
| 2 | Dead `StopAsyncIteration` blocks | ✅ Fixed | No `StopAsyncIteration` references anywhere in `main.py` |
| 3 | Misleading `StopAsyncIteration` docstring | ✅ Fixed | No `StopAsyncIteration` references anywhere in `main.py` |
| 4 | `_run_checkin_analysis` hardcoded 8 card IDs | ✅ Fixed | `main.py:1237` — uses `", ".join(c["id"] for c in AVAILABLE_CARDS)` (all 20 cards) |
| 5 | Check-in lock race window | ✅ Fixed | `main.py:1328-1329` — `conv_messages` and `conv_step` copied to locals before lock release |
| 6 | Dashboard fetch() missing timeouts | ✅ Fixed | `dashboard.html:499-510` — `controller()` with `setTimeout` abort on all 4 fetch calls; `index.html:347-356` — `fetchWithTimeout()` helper |
| 7 | Stale `frontend/survey.html` with XSS | ✅ Fixed | File does not exist (confirmed via filesystem search) |
| 8 | `backend/main.py.bak` in Docker image | ✅ Fixed | File does not exist (confirmed via filesystem search) |
| 9 | `*.bak` missing from `.dockerignore` | ✅ Fixed | `.dockerignore:37` — `*.bak` present |

---

## Previous Audit Fix Verification (Round 1 — All 12 Confirmed Intact)

No regressions were found in any of the 12 original fixes:

| Fix | Status | Location |
|---|---|---|
| Async I/O (no blocking calls in async paths) | ✅ | SMTP via `asyncio.to_thread` (`main.py:823`) |
| No bare `except:` | ✅ | All except clauses catch specific exceptions |
| Dead code removed | ✅ | No orphaned functions or unreachable code |
| XSS prevention | ✅ | `textContent`, `escapeHtml()`, `createElement` throughout |
| CORS locked down | ✅ | Specific origins only (`main.py:48-59`) |
| Rate limiting | ✅ | Per-session, 20 req/60s, asyncio.Lock (`main.py:648-680`) |
| Input validation | ✅ | `ChatRequest` validators + `_validate_session_id_param()` on all endpoints |
| Security headers | ✅ | nosniff, DENY, XSS-Protection, Referrer-Policy (`main.py:39-46`) |
| Memory leak TTL | ✅ | 30-min TTL + max 100 conversations + opportunistic cleanup (`main.py:1271-1318`) |
| Thread safety lock | ✅ | `asyncio.Lock` on all `_conversations` access (`main.py:1273, 1295, 1355`) |
| Duplicate SSE helper | ✅ | Single `_stream_llm_response()` used by both `chat()` and `checkin_chat()` |
| ARIA attributes | ✅ | `role`, `aria-label`, `aria-live`, `aria-valuenow` throughout both HTML files |

---

## New Issues Found

### MEDIUM Severity

#### NEW-1: Missing Content-Security-Policy Header
- **File:** `backend/main.py`
- **Lines:** 39–46
- **Category:** Security Hardening Gap
- **Description:** The security headers middleware sets `X-Content-Type-Options`, `X-Frame-Options`, `X-XSS-Protection`, and `Referrer-Policy`, but does **not** set `Content-Security-Policy` (CSP). CSP is the primary modern defense against XSS — it restricts which scripts/styles/resources the browser will execute. The app loads external resources (Google Fonts) but has no CSP to whitelist them.
- **Impact:** If any XSS injection path is discovered (e.g., via LLM output), there is no browser-level defense-in-depth to prevent script execution.
- **Recommended Fix:**
  ```python
  response.headers["Content-Security-Policy"] = (
      "default-src 'self'; "
      "script-src 'self'; "
      "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
      "font-src 'self' https://fonts.gstatic.com; "
      "connect-src 'self'; "
      "img-src 'self' data:;"
  )
  ```

### LOW Severity

#### NEW-2: Dead Variable in `_stream_llm_response`
- **File:** `backend/main.py`
- **Lines:** 934, 974, 983, 991–992
- **Category:** Dead Code
- **Description:** The function builds `full_response` by accumulating content chunks (line 974) and the reasoning-model fallback (line 983), but this variable is never yielded or returned. Lines 991–992 contain a comment referencing a "`_full_response` sentinel yielded at the end" that does not exist. Callers (`chat()` at line 1047–1049, `checkin_chat()` at line 1349–1351) reconstruct `full_response` themselves by parsing each yielded chunk, so the function works correctly. The variable and comment are dead code.
- **Recommended Fix:** Remove the `full_response` variable and the misleading comment on lines 991–992.

#### NEW-3: `resetSurvey()` Doesn't Check Response Status
- **File:** `frontend/index.html`
- **Lines:** 834–838
- **Category:** Error Handling
- **Description:** `resetSurvey()` calls `fetchWithTimeout()` but never checks `resp.ok`. If the reset API returns an error (422, 500, etc.), the page still reloads, showing the user stale data with no error indication.
- **Recommended Fix:**
  ```javascript
  const resp = await fetchWithTimeout(`${API}/reset?session_id=${sessionId}`, { method: 'POST' });
  if (!resp.ok) { alert('Reset failed. Please try again.'); return; }
  location.reload();
  ```

#### NEW-4: `del` Without Guard in Check-in SSE Stream
- **File:** `backend/main.py`
- **Lines:** 1364
- **Category:** Race Condition (Edge Case)
- **Description:** Inside `sse_stream()`, line 1364 does `del checkin_chat._conversations[checkin_key]` without guarding against `KeyError`. If two concurrent requests for the same session both reach `is_done=True` at step 5, the second `del` will raise `KeyError` (the key was already deleted by the first request). This is inside the lock (serialized), but the `del` itself is unguarded. The `sse_stream` function has no try/except, so the error would terminate the SSE stream abruptly.
- **Impact:** Very low — requires two simultaneous requests on the same session at exactly step 5. Rate limiter (20 req/60s) makes this unlikely but not impossible.
- **Recommended Fix:** Use `checkin_chat._conversations.pop(checkin_key, None)` instead of `del`.

#### NEW-5: `conv['step']` Read Outside Lock
- **File:** `backend/main.py`
- **Lines:** 1366
- **Category:** Race Condition (Cosmetic)
- **Description:** Line 1366 reads `conv['step']` in the yield statement, **outside** the `async with _checkin_lock` block (which ends at line 1364). The `conv` dict reference is still valid (Python keeps the object alive), but if `is_done` was `False` and another concurrent request modified `step` before this line executes, the SSE payload would contain a stale step value. This doesn't affect functionality (the step is informational), but it's a minor consistency issue.
- **Recommended Fix:** Read `conv_step_out = conv["step"]` inside the lock block and use the local in the yield.

#### NEW-6: `X-XSS-Protection` Header Is Deprecated
- **File:** `backend/main.py`
- **Lines:** 44
- **Category:** Deprecated Header
- **Description:** The `X-XSS-Protection: 1; mode=block` header has been deprecated and removed from modern browsers (Chrome 78+, Edge, Firefox). It provides no actual protection in current browsers. It should be supplemented with (or replaced by) `Content-Security-Policy`.
- **Impact:** No active harm — the header is simply ignored by modern browsers.

#### NEW-7: No `Strict-Transport-Security` (HSTS) Header
- **File:** `backend/main.py`
- **Lines:** 39–46
- **Category:** Security Hardening Gap
- **Description:** No `Strict-Transport-Security` header is set. While HTTPS termination may occur at the reverse proxy layer, setting HSTS from the app ensures browsers refuse to connect via HTTP. This is defense-in-depth.
- **Recommended Fix:** Add `response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"` (conditionally, when behind HTTPS).

#### NEW-8: `_turso_query` Integer Conversion Is Fragile
- **File:** `backend/main.py`
- **Lines:** 277–280
- **Category:** Type Safety
- **Description:** `_turso_query` converts numeric strings back to integers using `val.lstrip('-').isdigit()`. This works for positive/negative integers but silently fails for float strings (e.g., `'3.2'` would remain a string). Since Turso stores all values as strings by design, and current usage only stores integers, this isn't actively breaking. However, if float values are ever stored, they would come back as strings unexpectedly.
- **Impact:** No active issue — all current database values are integers or text.

#### NEW-9: Check-in Questions Only Cover 5 of 11 Business Types
- **File:** `backend/main.py`
- **Lines:** 504–540
- **Category:** Incomplete Data
- **Description:** `CHECKIN_QUESTIONS_BY_TYPE` defines tailored check-in questions for only 5 business types (Restaurant, Salon, Plumber, Retail, Gym). The remaining 6 types (Landscaping, Auto Repair, Cleaning, Photography, Real Estate, Other) fall through to `DEFAULT_CHECKIN`. This is functionally correct — the fallback exists — but those users get generic check-in questions instead of type-specific ones.
- **Impact:** Minor UX inconsistency for 6 business types.

#### NEW-10: Overlapping `staff` and `staff_schedule` Cards
- **File:** `backend/main.py`
- **Lines:** 1096, 1110
- **Category:** Redundancy
- **Description:** `AVAILABLE_CARDS` includes both `staff` ("Staff & Labor") and `staff_schedule` ("Staff Schedule"). These overlap significantly in function — both display staff members, hours, and coverage gaps. The LLM card selection engine could select both, resulting in two similar cards on the dashboard. The dashboard also has separate render functions for both (`dashboard.html:1201` and `1365`).
- **Recommended Fix:** Merge into a single card, or add explicit differentiation (e.g., `staff` for cost/labor metrics, `staff_schedule` for shift planning) and instruct the LLM to pick only one.

---

## Redundancy Report

| # | Item | Location | Description |
|---|---|---|---|
| 1 | `staff` / `staff_schedule` cards | `main.py:1096,1110`; `dashboard.html:1201,1365` | Two cards with overlapping purpose (staff hours + coverage). See NEW-10. |
| 2 | `_turso_execute` wraps `_turso_request` | `main.py:251-256` | `_turso_execute` is a one-line wrapper that calls `_turso_request` and returns its result. It adds no value over calling `_turso_request` directly. Could be removed and callers updated to use `_turso_request`. |
| 3 | `QUESTIONS` constant vs `_get_questions_for_type()` | `main.py:163, 165-168` | `QUESTIONS = [Q1] + UNIVERSAL_QUESTIONS` is only used by `/api/survey/questions` endpoint. All other code uses `_get_questions_for_type()`. The `QUESTIONS` constant is a simplified/incorrect view (missing Q5 and type-specific Q2-Q4) that could mislead. |
| 4 | `full_response` in `_stream_llm_response` | `main.py:934, 974, 983` | Dead variable — built but never used. See NEW-2. |
| 5 | `X-XSS-Protection` header | `main.py:44` | Deprecated header providing no protection in modern browsers. See NEW-6. |

---

## Refactoring Roadmap

### Phase 1 — Quick Wins (Low effort, immediate value)
1. **Add CSP header** — Single line in the security headers middleware. Replaces the deprecated `X-XSS-Protection` as the primary XSS defense.
2. **Fix `del` → `pop`** in check-in SSE stream — One-word change, eliminates KeyError race.
3. **Remove dead `full_response` variable** and misleading comment in `_stream_llm_response`.
4. **Add `resp.ok` check** in `resetSurvey()` — prevents silent failure UX.
5. **Read `conv['step']` inside lock** — copy to local for consistency.

### Phase 2 — Architecture (Deferred — not blocking)
- Split `main.py` into modules: `routes/`, `db.py`, `llm.py`, `prompts.py`, `cards.py`
- Migrate check-in state from function-attribute memory to Turso persistence
- Consolidate `staff` / `staff_schedule` cards or enforce mutual exclusivity

### Phase 3 — Performance (Deferred — not blocking)
- Add `httpx.AsyncClient` connection pooling (shared client instead of per-request)
- Consider Turso connection caching / batching

### Phase 4 — Testing & CI/CD (Deferred — not blocking)
- Add pytest test suite for API endpoints, Turso helpers, prompt builders
- Add CI/CD pipeline with linting (ruff), type checking (mypy), and Docker build validation

### Phase 5 — Production Readiness (Deferred — not blocking)
- Add authentication layer (JWT or session-based)
- Add user account management
- Add data isolation per user

### Phase 6 — Real Data Integration (Deferred — not blocking)
- Replace mock/hardcoded data in dashboard cards with real API integrations
- Fix `staff_schedule` time parsing when implementing real shift data

---

## Files Audited

| File | Lines | Status |
|---|---|---|
| `backend/main.py` | 1484 | Fully read and analyzed |
| `frontend/index.html` | 878 | Fully read and analyzed |
| `frontend/dashboard.html` | 1636 | Fully read and analyzed |
| `Dockerfile` | 21 | Fully read and analyzed |
| `.dockerignore` | 37 | Fully read and analyzed |
| `backend/requirements.txt` | 4 | Fully read and analyzed |

## Files Confirmed Deleted

| File | Previous Issue | Status |
|---|---|---|
| `frontend/survey.html` | XSS-vulnerable Benji/Yans Deli artifact | ✅ Deleted |
| `backend/main.py.bak` | 71KB stale backup in Docker image | ✅ Deleted |

---

*Audit conducted via line-by-line code review of all source files. No automated linters or test suites were run (none exist — deferred Phase 4). All line numbers reference the current state of the `dev` branch at commit `baf01b3`.*
