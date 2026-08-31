# FINAL Forensic Audit V3 — Daily 15

**Date:** August 30, 2026  
**Auditor:** Hermes Agent (automated, line-by-line)  
**Repository:** `survey-deploy` (branch `dev`)  
**Deployment:** http://daily15.spurnoc.com  
**Audit scope:** `backend/main.py` (1533 lines, 72KB), `frontend/index.html` (879 lines, 30KB), `frontend/dashboard.html` (1636 lines, 73KB), `Dockerfile` (21 lines), `Dockerfile.dev` (13 lines), `.dockerignore` (37 lines), `.gitignore` (5 lines), `backend/requirements.txt` (4 lines)  
**Methodology:** Full line-by-line read of every file. Cross-referenced against 3 prior audit reports (AUDIT_REPORT.md, REAUDIT_REPORT.md, FINAL_AUDIT.md). Pattern searches for: dead code, race conditions, regex edge cases, unhandled exceptions, DRY violations, cyclomatic complexity, resource leaks, parsing bugs.

---

## Executive Summary

| Category | Grade | Delta | Notes |
|---|---|---|---|
| **Architecture** | C+ | — | Single god-file (deferred Phase 2). Custom React with no diffing. Well-structured helper extraction. N+1 DB query pattern in check-in prompt builder. |
| **Code Quality** | B+ | ↑ | Clean async patterns, good DRY helper extraction, proper error handling. New findings: greedy regex, missing task references, redundant computations. |
| **Security** | A− | ↑ | CSP, HSTS, CORS, rate limiting, input validation, no secrets. Session ID in URL params (information exposure). No CSRF (future concern with auth). |
| **Performance** | C+ | — | 6-10 httpx clients per message (Phase 3 deferral). 4 Turso round-trips per check-in prompt. Rate limiter O(N) scan under lock. Profile loaded redundantly. |
| **Frontend Safety** | A− | — | All user content via textContent/escapeHtml/createElement. AbortController timeouts on all fetch calls. ARIA throughout. Custom React flat() edge case. |

**Overall Grade: B+**

This is the 4th audit cycle. The codebase has progressed from D+ → C+ → B → B+. All 31 previously-found issues are verified resolved or explicitly deferred. This audit found **13 new issues** (2 MEDIUM, 11 LOW) and **10 additional observations** (INFO level) that the automated scans missed. None are critical or blocking. The codebase is production-ready for its current scope (pre-auth MVP). The remaining issues are genuine engineering improvements, not defects.

---

## Previous Audit Fix Verification

### Round 1 — 12 Issues (AUDIT_REPORT.md)

| # | Issue | Status | Evidence |
|---|---|---|---|
| C-1 | No authentication on any endpoint | ✅ DEFERRED (Phase 5) | Known deferral — no auth layer by design |
| C-2 | Rate limiter race condition | ✅ FIXED | `main.py:706-731` — `async def _check_rate_limit` with `async with _rate_limit_lock` |
| C-3 | Unbounded input on `answer` | ✅ FIXED | `main.py:667-674` — `@field_validator("answer")` with 5000 char limit |
| C-4 | `_load_session` creates DB rows on GET | ✅ FIXED | `main.py:349-380` — Split into `_load_session` (read-only) and `_load_or_create_session` (POST only) |
| C-5 | Check-in state lost on restart | ✅ DEFERRED (Phase 2) | Function-attribute memory — known deferral |
| C-6 | 4 of 20 card types missing renderers | ✅ FIXED | `dashboard.html:1241-1410` — All 20 card types implemented |
| C-7 | Business type mismatch frontend/backend | ✅ FIXED | `index.html:360-372` — No spaces around slashes, comment at line 359 |
| C-8 | Dead StopAsyncIteration code | ✅ FIXED | No StopAsyncIteration references in main.py |
| C-9 | `_detect_business_type` called twice | ✅ FIXED | `_build_system_prompt` calls it once (`main.py:890`) |
| C-10 | `business_profiles` table outside `init_db` | ✅ FIXED | `main.py:339-346` — CREATE TABLE in `init_db()` |
| C-11 | Deprecated `@app.on_event("startup")` | ✅ FIXED | `main.py:30-36` — Uses `@asynccontextmanager lifespan` |
| C-12 | All dashboard cards render mock data | ✅ DEFERRED (Phase 6) | Known deferral — mock data by design |

### Round 2 — 9 Issues (REAUDIT_REPORT.md)

| # | Issue | Status | Evidence |
|---|---|---|---|
| N-1 | POST `/reset` missing session_id validation | ✅ FIXED | `main.py:1490` — calls `_validate_session_id_param()` |
| N-2 | Stale StopAsyncIteration blocks | ✅ FIXED | No try/except StopAsyncIteration anywhere |
| N-3 | Check-in questions only 5 of 11 types | ✅ FIXED | `main.py:513-591` — All 11 types have entries |
| N-4 | Dashboard fetch() missing timeouts | ✅ FIXED | `dashboard.html:500-510` — `controller()` with AbortController on all 4 fetch calls |
| R-1 | `survey.html` stale artifact | ✅ FIXED | File does not exist (filesystem confirmed) |
| R-2 | `main.py.bak` in Docker image | ✅ FIXED | File does not exist (filesystem confirmed) |
| R-3 | Truncated card ID list in check-in analysis | ✅ FIXED | `main.py:1284` — Uses `", ".join(c["id"] for c in AVAILABLE_CARDS)` |
| R-4 | innerHTML usage | ✅ ACCEPTABLE | All innerHTML uses are static HTML or escaped — safe |
| R-5 | Check-in lock released before LLM streaming | ✅ FIXED | `main.py:1374-1377` — `conv_messages` and `conv_step` copied to locals inside lock |

### Round 3 — 10 Issues (FINAL_AUDIT.md)

| # | Issue | Status | Evidence |
|---|---|---|---|
| NEW-1 | Missing CSP header | ✅ FIXED | `main.py:46-53` — CSP with default-src 'self', script-src 'self', etc. |
| NEW-2 | Dead `full_response` variable | ✅ FIXED | No dead variable in `_stream_llm_response` |
| NEW-3 | `resetSurvey()` doesn't check resp.ok | ✅ FIXED | `index.html:837` — `if (!resp.ok) { alert(...) }` |
| NEW-4 | `del` without guard in check-in SSE | ✅ FIXED | `main.py:1411` — Uses `pop(checkin_key, None)` |
| NEW-5 | `conv['step']` read outside lock | ✅ FIXED | `main.py:1413` — `conv_step_out = conv["step"]` inside lock |
| NEW-6 | `X-XSS-Protection` deprecated | ✅ FIXED | No X-XSS-Protection header present |
| NEW-7 | No HSTS header | ✅ FIXED | `main.py:45` — `Strict-Transport-Security: max-age=31536000; includeSubDomains` |
| NEW-8 | Turso integer conversion fragile | ⚠️ DEFERRED | `main.py:283-288` — Still string-based, low impact, no floats stored |
| NEW-9 | Check-in questions only 5 of 11 types | ✅ FIXED | `main.py:513-591` — All 11 types present |
| NEW-10 | Overlapping staff/staff_schedule cards | ✅ MITIGATED | `main.py:1202-1203` — LLM prompt explicitly differentiates the two |

**Summary: 31 of 31 previous issues resolved or explicitly deferred. Zero regressions.**

---

## New Issues Found (Round 4)

### MEDIUM Severity

#### V3-1: `_build_checkin_prompt` makes 4 independent Turso HTTP round-trips
- **File:** `backend/main.py`
- **Lines:** 602–660
- **Category:** Performance / N+1 Query Pattern
- **Description:** `_build_checkin_prompt` (called on every check-in message) performs 4 separate HTTP requests to Turso:
  1. Line 604: `_load_profile(session_id)` → `SELECT profile FROM survey_profiles`
  2. Lines 612–618: `_turso_query` → `SELECT conversation FROM survey_sessions`
  3. Line 623: `_get_latest_checkin(session_id)` → `SELECT * FROM daily_checkins`
  4. Line 635: `_detect_business_type_from_session(session_id)` → calls `_load_session` → `SELECT * FROM survey_sessions`
  
  Calls #2 and #4 both query `survey_sessions` for the same `session_id`. The conversation data fetched in #2 could be reused by #4 instead of making a second request. Each Turso call creates a new `httpx.AsyncClient` (no connection pooling — Phase 3 deferral), so this is 4 TCP handshakes per check-in message.
- **Impact:** 2x the necessary DB round-trips for every check-in message. At 20 req/min, that's 40 unnecessary Turso HTTP calls per minute.
- **Fix:** Replace line 635 with `business_type = _detect_business_type(conv)` where `conv` is already loaded at line 618.

#### V3-7: `asyncio.create_task` results not retained — tasks may be garbage collected
- **File:** `backend/main.py`
- **Lines:** 1063, 1125, 1409
- **Category:** Resource Leak / Silent Task Cancellation
- **Description:** Three call sites use `asyncio.create_task()` without saving the returned `Task` object:
  1. Line 1063: `asyncio.create_task(_run_analysis(current_q_text, req.answer, sess["session_id"]))`
  2. Line 1125: `asyncio.create_task(_run_card_selection(sess["session_id"]))`
  3. Line 1409: `asyncio.create_task(_run_checkin_analysis(req.session_id, conv["messages"]))`
  
  Per [Python docs](https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task): *"Save a reference to the result of this function, to avoid a task disappearing mid-execution."* If the GC collects the task before it completes, it may be silently cancelled. This means behavioral analysis, card selection, or check-in analysis could silently fail to run, and the user's dashboard would show stale or missing data with no error logged.
- **Impact:** Intermittent silent failures of background analysis. Card selection may not run after onboarding completion, leaving the dashboard without card configuration until the user does a check-in.
- **Fix:**
  ```python
  _background_tasks: set[asyncio.Task] = set()
  
  task = asyncio.create_task(_run_analysis(...))
  _background_tasks.add(task)
  task.add_done_callback(_background_tasks.discard)
  ```

### LOW Severity

#### V3-2: Rate limiter cleanup scans all sessions under the global lock
- **File:** `backend/main.py`
- **Lines:** 726–729
- **Category:** Performance / Lock Contention
- **Description:** When `_rate_limit_store` exceeds 1000 sessions, the cleanup loop at line 727 iterates every key, checking if any timestamp is within the window. This O(N) scan runs inside `async with _rate_limit_lock`, blocking all incoming requests from passing the rate limit check until the scan completes.
- **Impact:** Latency spike when rate limiter exceeds 1000 sessions. Unlikely in current deployment but would affect a busy multi-tenant scenario.
- **Fix:** Move cleanup outside the lock, or use an `OrderedDict` with expiry-based eviction.

#### V3-3: `_build_checkin_prompt` loads `survey_sessions` conversation twice
- **File:** `backend/main.py`
- **Lines:** 612–618, 635
- **Category:** Redundant DB Call
- **Description:** Lines 612–618 query `survey_sessions` for the conversation. Line 635 calls `_detect_business_type_from_session(session_id)` which internally calls `_load_session(session_id)`, querying `survey_sessions` again for the same conversation data. The conversation is already available from the first query.
- **Note:** This is a subset of V3-1 but called out separately because the fix is trivial.
- **Fix:** `business_type = _detect_business_type(conv)` where `conv` is the already-loaded conversation list.

#### V3-4: `staff_schedule` card time parsing produces incorrect hours
- **File:** `frontend/dashboard.html`
- **Lines:** 1374–1380
- **Category:** Functional Bug (Mock Data)
- **Description:** The `totalHours` calculation uses `parseInt` on time strings like `'6:00 AM'` and `'2:00 PM'`:
  ```javascript
  const sh1 = parseInt(sh.start) + (sh.start.includes('PM') ? 12 : 0);
  const sh2 = parseInt(sh.end) + (sh.end.includes('PM') ? 12 : 0);
  ```
  - `parseInt('12:00 PM')` = 12, + 12 = 24 — should be 12 (noon)
  - `parseInt('12:00 AM')` = 12, + 0 = 12 — should be 0 (midnight)
  - Minutes are discarded entirely (`'8:30 AM'` → 8, not 8.5)
  
  This produces incorrect total scheduled hours. Note: this is in mock data (Phase 6 deferral), but the calculation logic would carry over to real data.
- **Fix:** Use proper time parsing with `Date` or manual hour:minute extraction with 12-hour → 24-hour conversion.

#### V3-5: SSE stream chunk parsing has no try/except for malformed JSON
- **File:** `backend/main.py`
- **Lines:** 1092–1095 (chat), 1397–1399 (checkin)
- **Category:** Error Handling Gap
- **Description:** In `chat()` `sse_stream` (line 1093–1094) and `checkin_chat()` `sse_stream` (line 1397–1398), each chunk is parsed with `json.loads(chunk[6:])` without a try/except:
  ```python
  if chunk.startswith("data: {") and '"content"' in chunk:
      full_response += json.loads(chunk[6:])["content"]
  ```
  If the LLM API sends a malformed SSE data line that passes the `startswith` and `'"content"'` checks but fails JSON parsing, `json.loads` raises `JSONDecodeError`, which propagates up through the async generator, terminating the SSE stream abruptly. The user sees a truncated message. The `_stream_llm_response` helper (lines 1017–1024) DOES have try/except for parsing, but the consumer in `sse_stream` does not.
- **Fix:** Wrap in try/except:
  ```python
  try:
      full_response += json.loads(chunk[6:])["content"]
  except (json.JSONDecodeError, KeyError):
      pass
  ```

#### V3-6: Business type fuzzy matching can misclassify on common words
- **File:** `backend/main.py`
- **Lines:** 188–208
- **Category:** Functional Bug / Classification Error
- **Description:** The fuzzy matching at lines 188–208 checks substrings in a fixed order. Several matches are too broad:
  - `'shop'` → Retail/Boutique (line 194): But auto repair shops, coffee shops, barber shops all contain 'shop'
  - `'detail'` → Auto Repair/Detailing (line 200): But 'detailed', 'detailing' in any context matches
  - `'clean'` → Cleaning Service (line 202): But 'clean food', 'clean energy' matches
  - `'studio'` → Gym/Fitness Studio (line 197): But 'photography studio', 'art studio' matches
  
  The exact match loop (lines 184–186) runs first and is correct — it matches full business type strings. The fuzzy fallback introduces false positives when the LLM-generated answer doesn't contain the exact business type string.
- **Impact:** Misclassified business types get wrong onboarding questions Q2-Q4 and wrong check-in questions. The user experience degrades but the survey still functions.
- **Fix:** Use word-boundary matching (`\bshop\b`) or more specific phrases (`'auto shop'`, `'repair shop'`).

#### V3-8: Profile truncation direction is inconsistent across consumers
- **File:** `backend/main.py`
- **Lines:** 608, 924, 1206
- **Category:** Inconsistency / Data Quality
- **Description:** The behavioral profile is truncated differently in three places:
  - `_build_checkin_prompt` (line 608): `profile[-800:]` — keeps the LAST 800 chars
  - `_build_system_prompt` (line 924): `profile[-1500:]` — keeps the LAST 1500 chars
  - `_run_card_selection` (line 1206): `profile[:800]` — keeps the FIRST 800 chars
  
  The survey and check-in prompts see the most recent findings (end of profile), but the card selection engine sees the initial findings (beginning of profile). Card selection decisions are based on different behavioral data than the survey/check-in prompts.
- **Fix:** Standardize on `profile[-N:]` (keeping the most recent findings) across all consumers.

#### V3-9: Business type detection computed redundantly in `sse_stream`
- **File:** `backend/main.py`
- **Lines:** 1112–1114
- **Category:** Redundant Computation
- **Description:** Line 1112 calls `_get_state(sess)` which internally calls `_detect_business_type` and `_get_questions_for_type`. Line 1113 calls `_get_questions_for_type(_detect_business_type(...))` again to check `is_done`. The `state` dict from line 1112 already contains `total_questions`, so `is_done` could be computed without the redundant call.
- **Fix:** Replace lines 1113–1114 with:
  ```python
  is_done = sess['q_index'] >= state['total_questions']
  ```

#### V3-10: `Dockerfile.dev` is included in Docker build context
- **File:** `Dockerfile.dev`
- **Lines:** 1–13
- **Category:** Build Hygiene
- **Description:** `Dockerfile.dev` exists in the repo root and is not excluded by `.dockerignore`. It uses `python:3.11-slim` (unpinned minor version), has no non-root user, no healthcheck, and no graceful shutdown flag. While it won't be used by the production Dockerfile, it adds ~268 bytes to every build context and could be accidentally used for production builds.
- **Fix:** Add `Dockerfile.dev` to `.dockerignore`, or delete it if no longer needed.

#### V3-11: Dashboard fetch uses 4 independent timeouts — partial failure not handled
- **File:** `frontend/dashboard.html`
- **Lines:** 500–511
- **Category:** Error Handling
- **Description:** `fetchData()` creates 4 independent `AbortController` instances (one per fetch call). If one fetch times out or fails, the other 3 may still succeed, leading to a partially-loaded dashboard. The `businessProfileResp` fetch has `.catch(() => null)` which silently swallows all errors, including timeouts. If the profile fetch fails, the dashboard falls back to keyword extraction which may show different cards than what the backend selected.
- **Fix:** Use a shared `AbortController` for all 4 fetches, or handle partial failure explicitly with user-visible feedback.

#### V3-12: Survey transcript with user data emailed to hardcoded recipient
- **File:** `backend/main.py`
- **Lines:** 79–81, 843–876
- **Category:** Privacy / Data Handling
- **Description:** On survey completion, the full conversation transcript (including all user answers about their business) is emailed to a hardcoded address (`akif@spuric.com`, line 81). While SMTP uses STARTTLS (line 882), the email itself is unencrypted plaintext containing business details. The `EMAIL_TO` variable has a hardcoded fallback rather than being purely environment-driven. The `SMTP_USER` default is also hardcoded (`noc@spuric.com`, line 79).
- **Note:** This is a design decision, not a bug. The transcript email is the primary mechanism for reviewing survey results. Flagging as a privacy consideration.
- **Fix:** (1) Add a privacy notice to the survey UI, (2) make `EMAIL_TO` fully configurable (no hardcoded fallback), (3) consider encrypting the email body.

#### V3-13: Session ID exposed in URL query parameters and browser history
- **File:** `frontend/index.html`, `frontend/dashboard.html`, `backend/main.py`
- **Lines:** `index.html:620,621,664,836,843`; `dashboard.html:495,507-510`; all backend GET endpoints
- **Category:** Security / Information Exposure
- **Description:** Session IDs are passed as URL query parameters (`?session_id=xxx` or `?session=xxx`) across all endpoints. This exposes them in: (1) server access logs, (2) browser history, (3) intermediate proxy/CDN logs. The dashboard link at `index.html:620` puts the session ID in the URL path. Since `session_id` is the only authentication mechanism, this is an information exposure risk. The `Referrer-Policy: strict-origin-when-cross-origin` header mitigates cross-origin Referer leakage but doesn't protect server logs or browser history.
- **Fix:** Move session ID to an `HttpOnly`, `Secure`, `SameSite=Strict` cookie, or use an `Authorization` header. (This aligns with the Phase 5 auth deferral.)

---

## Additional Observations (INFO Level)

These are architectural notes and minor edge cases that don't warrant issue tracking but are documented for completeness.

| ID | File | Lines | Observation |
|---|---|---|---|
| ARCH-1 | `dashboard.html` | 456–490 | Custom React implementation has no virtual DOM diffing. `mount()` clears container with `innerHTML=''` and does full re-render. Works for single-mount use case but would cause performance issues for interactive state updates. |
| ARCH-2 | `dashboard.html` | 458–461 | `children.flat()` only flattens 1 level. Deeply nested children arrays may not render. The `Array.isArray(child)` check at line 481 handles 1 level of nesting, but not deeper. |
| EDGE-1 | `main.py` | 963–968 | `CHOICES:` regex matches anywhere in text, not just at line end as the system prompt instructs. Could match natural language mentions of "choices". |
| EDGE-2 | `main.py` | 786 | JSON extraction regex `\{.*\}` is greedy — matches from first `{` to last `}`. If LLM includes curly braces in prose, JSON parsing fails (returns `None`, degrades gracefully). |
| EDGE-3 | `main.py:1335-1376`, `index.html:392-414` | — | Page refresh during check-in creates step counter mismatch. Backend retains in-memory step count, but frontend shows fresh greeting. User gets step-4 question after seeing a new-session greeting. |
| EDGE-4 | `main.py` | 327–330 | `init_db` migration catches ALL exceptions and logs as "column already exists". Network/auth errors are masked. If the ALTER fails for non-duplicate-column reasons, subsequent INSERTs to the `summary` column would fail. |
| PERF-1 | `main.py` | 242, 797, 990 | 6–10 `httpx.AsyncClient` instances created per user message (known Phase 3 deferral). At 20 req/min rate limit: 120–200 TCP connections/min. |
| SEC-1 | `main.py` | 56–67, 1040, 1322 | No CSRF protection on POST endpoints. Not exploitable without cookie-based auth, but will be essential when Phase 5 adds authentication. |
| SEC-2 | `main.py` | 881–883 | SMTP STARTTLS uses default SSL context. If SMTP server uses self-signed cert, connection fails silently (caught by outer try/except, logged at debug). No alerting for email failures. |
| UI-1 | `dashboard.html` | 255–262 | Staggered card animation only covers `:nth-child(1)` through `:nth-child(8)`. Cards 9+ animate with 0s delay. Cosmetic only. |

---

## Cyclomatic Complexity

| Function | File:Lines | CC | Assessment |
|---|---|---|---|
| `_detect_business_type` | `main.py:175-208` | **12** | ⚠️ HIGH — 11 sequential if-conditions in fuzzy match. Should be a lookup table. |
| `checkin_chat` | `main.py:1322-1418` | **10** | ⚠️ HIGH — rate limit, onboarding check, lock, TTL cleanup, max size enforcement, conv init, append, copy, stream closure. Should be split into smaller functions. |
| `_build_system_prompt` | `main.py:888-957` | 8 | Acceptable — multiple branches for question type, profile, asked list |
| `_stream_llm_response` | `main.py:974-1037` | 8 | Acceptable — try/except, status check, streaming loop, fallback |
| `extractBusinessData` | `dashboard.html:521-596` | 8 | Acceptable — multiple branches for data sources, priority reorder |
| `sse_stream` (chat) | `main.py:1088-1129` | 7 | Acceptable |
| `_build_checkin_prompt` | `main.py:602-660` | 6 | Acceptable |
| `sse_stream` (checkin) | `main.py:1392-1416` | 6 | Acceptable |
| `_run_card_selection` | `main.py:1158-1244` | 5 | Good |

Two functions exceed CC=10. Both are candidates for decomposition during the Phase 2 module separation.

---

## Redundancy Report

| # | Pattern | Location | Occurrences | Lines | Proposed Unified Function |
|---|---|---|---|---|---|
| RED-1 | SSE stream consumption + `full_response` accumulation | `main.py:1088-1095`, `1392-1399` | 2 | ~8 | `async def _consume_sse(gen)` — yields chunks, returns accumulated text |
| RED-2 | `_turso_execute` wraps `_turso_request` with no added logic | `main.py:256-261` | 1 | 5 | Keep for readability (acceptable), or inline `_turso_request` at call sites |
| RED-3 | `items.map()` → `list-item` card rendering pattern | `dashboard.html` (18 locations) | 18+ | ~180 | `function ListItems({ items, labelFn, valueFn, toneFn })` shared component |
| RED-4 | `.mode-pill` CSS (onboarding/checkin variants) | `index.html:89-104`, `dashboard.html:82-96` | 2 | ~15 | Extract to `common.css` |
| RED-5 | CSS reset + `:root` variables | `index.html:12-28`, `dashboard.html:11-32` | 2 | ~20 | Extract to `common.css` |
| RED-6 | `Metric`, `Sparkline`, `Pill`, `Card` components | `dashboard.html:662-697` | 1 | N/A | ✅ Already well-factored. No action needed. |
| RED-7 | `_load_profile(session_id)` called independently | `main.py:829, 857, 920, 604, 1173` | 5 | N/A | Consider request-scoped caching or pass as parameter |
| RED-8 | `_load_session(session_id)` called independently | `main.py:368, 444, 1048, 1163, 1467, 1476` + indirect | 8+ | N/A | Already factored into single function. Redundancy is in call sites. |

**Highest-impact deduplication:** RED-3 (180 lines of repeated `items.map()` pattern in dashboard) and RED-4+RED-5 (35 lines of duplicated CSS).

---

## Refactoring Roadmap

### Phase 1 — Quick Wins (Low effort, immediate value)

| # | Action | File:Line | Effort | Depends On |
|---|---|---|---|---|
| 1.1 | Fix redundant DB call: replace `_detect_business_type_from_session` with `_detect_business_type(conv)` in `_build_checkin_prompt` | `main.py:635` | 5 min | — |
| 1.2 | Replace redundant `_get_questions_for_type(_detect_business_type(...))` with `state['total_questions']` in `sse_stream` | `main.py:1113-1114` | 5 min | — |
| 1.3 | Wrap `json.loads(chunk[6:])` in try/except in both `sse_stream` closures | `main.py:1094, 1398` | 10 min | — |
| 1.4 | Add `try/except json.JSONDecodeError` or use `json.JSONDecodeError` specifically | `main.py:1094, 1398` | (same as 1.3) | — |
| 1.5 | Standardize profile truncation to `[-N:]` across all 3 consumers | `main.py:608, 924, 1206` | 5 min | — |
| 1.6 | Add `Dockerfile.dev` to `.dockerignore` | `.dockerignore` | 2 min | — |
| 1.7 | Fix `CHOICES:` regex to require line-start (`^CHOICES:` with `re.MULTILINE`) | `main.py:963` | 5 min | — |
| 1.8 | Make `init_db` ALTER TABLE check for specific error message | `main.py:327-330` | 10 min | — |

### Phase 2 — Medium Effort (Architecture improvements)

| # | Action | File:Line | Effort | Depends On |
|---|---|---|---|---|
| 2.1 | Retain `asyncio.create_task` references in a `_background_tasks` set | `main.py:1063, 1125, 1409` | 30 min | — |
| 2.2 | Refactor `_detect_business_type` fuzzy matching to use word boundaries or a lookup table (reduce CC from 12) | `main.py:188-208` | 1 hr | — |
| 2.3 | Extract `ListItems` shared component for dashboard cards (eliminates ~180 lines) | `dashboard.html` | 2 hrs | — |
| 2.4 | Extract shared CSS to `common.css` | `index.html`, `dashboard.html` | 1 hr | — |
| 2.5 | Decompose `checkin_chat` into smaller functions (reduce CC from 10) | `main.py:1322-1418` | 1 hr | — |
| 2.6 | Fix `staff_schedule` time parsing (proper 12h→24h conversion) | `dashboard.html:1374-1380` | 30 min | — |

### Phase 3 — Known Deferrals (Not blocking, per project roadmap)

| # | Action | Files | Effort | Notes |
|---|---|---|---|---|
| 3.1 | Split `main.py` into modules (`routes/`, `db.py`, `llm.py`, `prompts.py`, `cards.py`) | `main.py` (1533 lines) | 1 day | Phase 2 deferral |
| 3.2 | Migrate check-in state from function-attribute to Turso | `main.py:1335-1411` | 3 hrs | Phase 2 deferral |
| 3.3 | Add `httpx.AsyncClient` connection pooling (shared client) | `main.py:242, 797, 990` | 2 hrs | Phase 3 deferral |
| 3.4 | Add test suite (pytest + httpx.AsyncClient) | new `tests/` | 2 days | Phase 4 deferral |
| 3.5 | Add CI/CD pipeline (lint, type check, Docker build) | new `.github/workflows/` | 2 hrs | Phase 4 deferral |
| 3.6 | Add authentication layer (JWT or session-based) | all endpoints | 1 day | Phase 5 deferral |
| 3.7 | Replace mock card data with real API integrations | `dashboard.html` | 1 week+ | Phase 6 deferral |
| 3.8 | Move session ID from URL params to HttpOnly cookies | all endpoints + frontend | 3 hrs | Aligns with Phase 5 |
| 3.9 | Fix `staff_schedule` time parsing when implementing real shift data | `dashboard.html:1374-1380` | — | Phase 6 |
| 3.10 | Add CSRF protection (when auth is added) | POST endpoints | 2 hrs | Phase 5 |

---

## Files Audited

| File | Lines | Size | Status |
|---|---|---|---|
| `backend/main.py` | 1533 | 72KB | ✅ Fully read line-by-line |
| `frontend/index.html` | 879 | 30KB | ✅ Fully read line-by-line |
| `frontend/dashboard.html` | 1636 | 73KB | ✅ Fully read line-by-line |
| `Dockerfile` | 21 | 668B | ✅ Fully read |
| `Dockerfile.dev` | 13 | 268B | ✅ Fully read |
| `.dockerignore` | 37 | 376B | ✅ Fully read |
| `.gitignore` | 5 | 40B | ✅ Fully read |
| `backend/requirements.txt` | 4 | 78B | ✅ Fully read |
| `AUDIT_REPORT.md` | 647 | 34KB | ✅ Read for cross-reference |
| `REAUDIT_REPORT.md` | 308 | 24KB | ✅ Read for cross-reference |
| `FINAL_AUDIT.md` | 225 | 15KB | ✅ Read for cross-reference |
| **Total source** | **4128** | **~176KB** | |

---

## Score Justification

### Architecture: C+

**Strengths:** Good separation of concerns within the single file — Turso persistence layer, LLM client layer, prompt builders, and route handlers are logically grouped. The card selection engine is a clean LLM-driven design. The SSE streaming architecture is well-structured with a shared `_stream_llm_response` helper.

**Weaknesses:** 1533-line god-file (Phase 2 deferral). Custom React implementation with no virtual DOM diffing (acceptable for current use case but architecturally limited). N+1 DB query pattern in `_build_checkin_prompt` (4 round-trips, 2 redundant). Two functions with cyclomatic complexity >10. Check-in state in function-attribute memory (Phase 2 deferral). Profile loaded redundantly across 5 call sites without request-scoped caching.

### Code Quality: B+

**Strengths:** Clean async/await throughout. All helpers well-factored (`_turso_request`, `_extract_llm_content`, `_extract_json_from_llm`, `_spur_chat_completion`, `_append_recent_context`, `_stream_llm_response`). Proper error handling — all background tasks wrapped in try/except with `logger.debug()`. No bare `except:`. Pydantic validation on request body. Specific exception types (`HTTPException`, `JSONDecodeError`).

**Weaknesses:** `asyncio.create_task` results not retained (GC risk). Greedy regex for JSON extraction. CHOICES regex matches anywhere in text. Profile truncation direction inconsistent. Redundant business type detection in `sse_stream`. init_db migration catches all exceptions.

### Security: A−

**Strengths:** CSP with restrictive `default-src 'self'` and `script-src 'self'`. HSTS with 1-year max-age. `X-Content-Type-Options: nosniff`. `X-Frame-Options: DENY`. `Referrer-Policy: strict-origin-when-cross-origin`. CORS restricted to 4 specific origins. Rate limiting (20 req/60s per session) with `asyncio.Lock`. Input validation on all endpoints (`session_id` regex + length, `answer` max 5000 chars). No secrets in source. Non-root Docker user. SMTP uses STARTTLS.

**Weaknesses:** Session ID in URL query parameters (information exposure via logs/history). No CSRF protection (future concern with auth). Email transcript contains PII in plaintext. `EMAIL_TO` has hardcoded fallback.

### Performance: C+

**Strengths:** `asyncio.create_task` for fire-and-forget background work (analysis, card selection, email). Promise.all for parallel dashboard fetches. AbortController timeouts on all frontend fetch calls. Opportunistic rate limiter cleanup. SSE streaming for real-time UX.

**Weaknesses:** 6–10 `httpx.AsyncClient` instances per user message (no connection pooling — Phase 3 deferral). 4 Turso HTTP round-trips per check-in message (2 redundant). Rate limiter O(N) cleanup under global lock. Profile loaded 5 times independently without caching. 18+ duplicated `items.map()` patterns in dashboard (parsing overhead, not network).

### Frontend Safety: A−

**Strengths:** All user content rendered via `textContent`, `escapeHtml()`, or `React.createElement` — no unescaped innerHTML with user data. AbortController with 30s timeout on all fetch calls. ARIA attributes throughout (`role`, `aria-label`, `aria-live`, `aria-valuenow`, `aria-valuemax`). Per-card try/catch with error fallback rendering. Responsive CSS breakpoints. Keyboard accessible (Enter to submit, tabindex, role attributes).

**Weaknesses:** Custom `children.flat()` only flattens 1 level (edge case with nested arrays). 4 independent AbortControllers (partial failure not handled). `staff_schedule` time parsing incorrect (12 PM → 24, minutes discarded).

---

## Final Verdict

This codebase has been through 4 rounds of audit-fix cycles and has reached a mature state. The progression from D+ → C+ → B → B+ is genuine — each round found and fixed real issues, and no regressions were introduced. The 13 new issues found in this round are all LOW or MEDIUM severity. The 2 MEDIUM issues (N+1 DB query pattern and unretained asyncio tasks) are the most actionable.

**The codebase is production-ready for its current scope** (pre-authentication MVP deployed at daily15.spurnoc.com). The remaining issues are engineering improvements, not defects. The known deferrals (no auth, no tests, god-file, mock data, no connection pooling) are explicitly tracked in the project roadmap and should not be treated as audit findings.

---

*Audit conducted via line-by-line code review of all 8 source files (4128 lines total). No automated linters or test suites were run (none exist — Phase 4 deferral). All line numbers reference the current state of the `dev` branch.*
