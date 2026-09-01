# WEB APP Forensic Audit Report — survey-deploy (experimental branch)

**Audit Date:** August 31, 2026  
**Auditor:** Hermes Agent (automated line-by-line review)  
**Files Audited:** 19 source files, 10,586 lines of code  
**Previous Audits:** 7 rounds (D+ → C+ → B → B+ → 0 issues)  

---

## Executive Summary (A–F Scores)

| Category | Grade | Assessment |
|----------|-------|------------|
| **Architecture** | **B−** | Monolithic `main.py` (3,377 lines) is a god-module containing auth, survey, check-in, admin, export, branding, i18n, reminders, voice, and business logic. Extracted modules (`db.py`, `llm.py`, `prompts.py`, `cards.py`, `models.py`) exist but are **fully duplicated** inside `main.py` — the app runs entirely from `main.py` and the extracted modules are dead code. Dockerfile only copies `main.py`. |
| **Code Quality** | **C+** | Functional but heavily duplicated. Massive copy-paste between `main.py` and extracted modules. Inconsistent error handling (some endpoints swallow exceptions, others raise). Magic numbers throughout. No type annotations on many functions. SSE streaming has a resource leak in the non-pooled-client path. |
| **Security** | **B** | PBKDF2-HMAC-SHA256 with 100K iterations, timing-safe comparison, CSP/HSTS/CORS headers, session_id validation. However: export endpoints are public (no auth), admin token stored in `sessionStorage` (XSS-vulnerable), branding endpoint accepts arbitrary `logo_url` without validation, and the `_is_public_path` function has logic gaps. |
| **Performance** | **B−** | Shared `httpx.AsyncClient` for connection pooling is good. But: admin endpoints do N+1 queries (one per user for business type detection), rate limiter uses a global lock with O(N) cleanup, streaming fallback creates a new client per request, and the dashboard frontend fetches 4 API endpoints in parallel with no caching. |
| **Overall** | **B−** | The app is functional and has been hardened through 7 audit rounds. Security fundamentals are solid. The primary concern is **architectural debt**: `main.py` is 3,377 lines with massive code duplication from an incomplete module extraction. The extracted modules are never used at runtime (Dockerfile doesn't copy them), making them dead code that creates a maintenance hazard. |

---

## Critical Violations

### 1. DEAD CODE: Extracted Modules Never Used at Runtime

**Severity:** CRITICAL (architectural)  
**File:** `Dockerfile:8`  
**Line:** `COPY backend/main.py .`

The Dockerfile only copies `main.py` — not `db.py`, `llm.py`, `prompts.py`, `cards.py`, or `models.py`. At runtime, `main.py`'s `try/except ImportError` block (lines 36-41) falls back to `_use_pooled_clients = False`, meaning:

- Connection pooling is **disabled** in production
- Every Turso DB call creates a new `httpx.AsyncClient` (line 291)
- Every LLM call creates a new `httpx.AsyncClient` (line 1277)
- The shared client in `db.py` and `llm.py` is never used

This is a **performance regression** — the entire connection pooling optimization is dead code in production.

### 2. MASSIVE CODE DUPLICATION: main.py Contains Full Copies of Extracted Modules

**Severity:** CRITICAL (maintainability)  
**Files:** `backend/main.py` vs `backend/db.py`, `backend/llm.py`, `backend/prompts.py`, `backend/cards.py`, `backend/models.py`

Every function in the extracted modules is **duplicated verbatim** in `main.py`:

| Function | main.py lines | Extracted module | Status |
|----------|---------------|-----------------|--------|
| `_turso_request` | 270-302 | `db.py:47-68` | Duplicated |
| `_turso_query` | 313-342 | `db.py:79-108` | Duplicated |
| `_load_session` | 521-535 | `db.py:164-178` | Duplicated |
| `_save_session` | 555-565 | `db.py:198-208` | Duplicated |
| `_spur_chat_completion` | 1257-1292 | `llm.py:65-112` | Duplicated (divergent!) |
| `_stream_llm_response` | 1458-1542 | `llm.py:115-178` | Duplicated (divergent!) |
| `_extract_llm_content` | 1238-1242 | `llm.py:44-49` | Duplicated |
| `BUSINESS_TYPES` | 123-128 | `prompts.py:24-29` | Duplicated |
| `UNIVERSAL_QUESTIONS` | 131-140 | `prompts.py:32-41` | Duplicated |
| `QUESTIONS_BY_TYPE` | 146-202 | `prompts.py:47-103` | Duplicated |
| `_detect_business_type` | 212-246 | `prompts.py:115-149` | Duplicated |
| `_build_system_prompt` | 1372-1441 | `prompts.py:311-384` | Duplicated |
| `_build_checkin_prompt` | 1054-1114 | `prompts.py:242-308` | Duplicated |
| `CHECKIN_QUESTIONS_BY_TYPE` | 919-997 | `prompts.py:153-231` | Duplicated |
| `_run_card_selection` | 1778-1864 | `cards.py:42-128` | Duplicated (divergent!) |
| `AVAILABLE_CARDS` | 1755-1776 | `cards.py:18-39` | Duplicated |
| `ChatRequest` | 1117-1139 | `models.py:13-35` | Duplicated |

**Divergence already occurring:** `_spur_chat_completion` in `main.py` (line 1257) passes `stream` as a parameter, while `llm.py` (line 65) creates a dedicated streaming client. `_run_card_selection` in `main.py` (line 1826) slices `profile[-800:]` while `cards.py` (line 90) slices `profile[:800]`. These are **behavioral differences** that will cause bugs when someone edits one copy but not the other.

### 3. RESOURCE LEAK: Streaming Client Not Closed in Non-Pooled Path

**Severity:** HIGH  
**File:** `backend/main.py:1493-1494`  
**Lines:** `client = httpx.AsyncClient(timeout=httpx.Timeout(90.0))` / `await client.__aenter__()`

In the `_stream_llm_response` function (non-pooled path), a new `httpx.AsyncClient` is created and manually entered via `__aenter__()`, but there is **no `__aexit__()` call** to close it. The client is never closed, leaking TCP connections on every streaming survey response.

Compare with `llm.py:131` which correctly uses `async with httpx.AsyncClient(...) as client:`.

### 4. EXPORT ENDPOINTS HAVE NO AUTHENTICATION

**Severity:** HIGH (security)  
**File:** `backend/main.py:712-729`

All export endpoints are in `_PUBLIC_PATHS`:
```python
_PUBLIC_PATHS = {
    ...
    "/api/export/survey",
    "/api/export/checkins",
    "/api/export/profile",
    "/api/export/all",
}
```

Anyone with a `session_id` can download a user's full survey transcript, behavioral profile, all check-ins, and card priorities — **without any authentication token**. The `session_id` is a client-generated string (`s-` + timestamp + random), making it guessable/enumerable.

### 5. ADMIN N+1 QUERY PROBLEM

**Severity:** MEDIUM (performance)  
**File:** `backend/main.py:2740-2756, 2786-2795`

Both `admin_stats` and `admin_users` execute a separate `_turso_query` for **each user** to detect their business type:

```python
for u in recent_users:  # line 2744
    sess_rows = await _turso_query(
        "SELECT conversation FROM survey_sessions WHERE user_id=? ...", ...
    )
```

With 1,000 users, this generates 1,000+ HTTP round-trips to Turso. Should be a single JOIN query.

### 6. ADMIN BUSINESSES N+1 + INCORRECT MEMBER COUNT

**Severity:** MEDIUM  
**File:** `backend/main.py:2811-2856`

`admin_businesses` iterates all survey_sessions and for each:
- Calls `_load_business_profile` (1 Turso query)
- Calls `_turso_query` for member count (1 Turso query)
- Calls `_turso_query` for last check-in (1 Turso query)

That's 3 queries per session. Additionally, the "member_count" (line 2836) counts `survey_sessions` with the same `user_id`, which is **not** the business member count — it's the session count per user. The `business_members` table is never queried.

### 7. `_is_public_path` BRANDING ROUTE LOGIC IS INCORRECT

**Severity:** MEDIUM (security)  
**File:** `backend/main.py:742`

```python
if path.startswith("/api/branding/") and not path.endswith("/branding"):
    return True
```

This makes ALL `GET /api/branding/{business_id}` endpoints public (which is intended — frontend fetches before auth). But the `POST /api/branding` endpoint (line 3301) uses `Depends(_auth_dependency)`, which is correct. However, the condition `not path.endswith("/branding")` is fragile — any path ending in `/branding` would be caught by this, and the logic doesn't distinguish GET from POST.

### 8. CHECK-IN COMPLETION RACE CONDITION

**Severity:** MEDIUM  
**File:** `backend/main.py:2047-2058`

When the check-in is done (`is_done == True`), the code:
1. Saves the check-in session to Turso (line 2052)
2. Fires `_run_checkin_analysis` as a background task (line 2053)
3. **Immediately** clears the check-in session from Turso (line 2056)

If the background task hasn't finished reading the conversation from `checkin_sessions` before `_clear_checkin_session` runs, the analysis will fail. The analysis function (`_run_checkin_analysis`, line 1894) receives `list(conv_messages)` as an argument, so it doesn't re-read from DB — this is actually safe. But if the process restarts between step 2 and the task executing, the analysis is lost with no error.

### 9. `_build_checkin_prompt` UNDEFINED VARIABLE IF NO ONBOARDING

**Severity:** MEDIUM  
**File:** `backend/main.py:1089`

```python
business_type = _detect_business_type(conv) if conv else "Other"
```

The variable `conv` is only defined inside the `if sess_rows and sess_rows[0].get("conversation"):` block (line 1069-1070). If `sess_rows` is empty or has no conversation, `conv` is never assigned, but line 1089 references it. Python will raise `UnboundLocalError`.

**Note:** This bug exists in `main.py` but NOT in `prompts.py:283` which correctly initializes `conv = []` before the if-block (line 262). This is a divergence caused by the code duplication.

### 10. BUSINESS CREATION RACE CONDITION

**Severity:** MEDIUM  
**File:** `backend/main.py:2949-2963`

```python
await _turso_execute("INSERT INTO businesses (name, type) VALUES (?, ?)", ...)
# Fetch the newly created business by name + created_at
rows = await _turso_query(
    "SELECT business_id, name, type, created_at FROM businesses WHERE name=? ORDER BY created_at DESC LIMIT 1", ...
)
```

If two businesses with the same name are created concurrently, the SELECT will return the wrong one. Should use `last_insert_rowid()` or a RETURNING clause. Also, `business_id_val` (line 2944) is computed but never used.

### 11. SMTP WITHOUT TLS VERIFICATION

**Severity:** LOW (security)  
**File:** `backend/main.py:1365-1368`

```python
with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
```

`starttls()` is called without a context, meaning certificate validation is not enforced. Should use `server.starttls(context=ssl.create_default_context())`.

### 12. REMINDER LOOP TIMEZONE BUG

**Severity:** LOW  
**File:** `backend/main.py:2412`

```python
now = datetime.now(timezone.utc).astimezone()
current_hhmm = now.strftime("%H:%M")
```

The reminder loop converts UTC to the **server's local timezone**, but `reminder_time` is stored as the user's preferred time. If the server is in UTC and the user set "08:00" expecting EST, the reminder fires at 8:00 UTC (3-4 AM EST). There's no per-user timezone field.

### 13. WEEKLY SUMMARY LAST_SENT_DATE COLLISION

**Severity:** LOW  
**File:** `backend/main.py:2419-2425`

The weekly summary uses `last_sent_date` field with a `weekly_sent_key` (`{today}_weekly`), but the daily reminder also writes to `last_sent_date` with today's date. If a daily reminder was already sent today, the weekly query `WHERE enabled=1 AND last_sent_date != ?` (line 2424) will exclude users who already got their daily reminder — which is backwards. The weekly summary should use a separate column.

### 14. FRONTEND: `showThinking` USES `innerHTML` WITH TRANSLATED TEXT

**Severity:** LOW (XSS)  
**File:** `frontend/index.html:1424`

```javascript
area.innerHTML = `<div class="text-wrap"><div class="text-input" style="opacity:0.4;cursor:default">${t('thinking')}</div>...`;
```

The `t('thinking')` output is inserted via `innerHTML`. If translations are compromised or a malicious i18n response is returned, this is an XSS vector. The `showInput` function (line 1346) correctly uses `createElement` + `textContent`, but `showThinking` and `switchToTextInput` (line 1408) use `innerHTML` with translated strings.

### 15. DASHBOARD `fetchData` DOES NOT SEND AUTH HEADERS

**Severity:** MEDIUM  
**File:** `frontend/dashboard.html:556-559`

```javascript
const [transcriptResp, profileResp, ...] = await Promise.all([
    fetch(`${API}/transcript?session_id=${sessionId}`, { signal }).then(r => r.json()),
    ...
]);
```

The dashboard makes API calls without sending the `Authorization` header. Since the export/transcript/profile endpoints are in `_PUBLIC_PATHS`, this works — but it means the dashboard has **no auth verification**. Anyone who knows a `session_id` can view the full dashboard.

### 16. `hexToRgba` IN INDEX.HTML DOESN'T HANDLE 3-CHAR HEX

**Severity:** LOW  
**File:** `frontend/index.html:863-870`

The `hexToRgba` function in `index.html` only handles 6-character hex (`#ffffff`), but the dashboard version (line 700-706) handles 3-character hex (`#fff`). Inconsistency could cause branding to silently fail for 3-char hex colors.

### 17. CARD SELECTION PROFILE SLICING DIVERGENCE

**Severity:** LOW  
**File:** `backend/main.py:1826` vs `backend/cards.py:90`

- `main.py:1826`: `profile[-800:]` (last 800 chars)
- `cards.py:90`: `profile[:800]` (first 800 chars)

These produce different LLM inputs, leading to inconsistent card selection behavior depending on which code path runs.

---

## Redundancy Report

### Fully Duplicated Code (main.py ↔ extracted modules)

| Code Block | main.py LOC | Module LOC | Delta |
|-----------|-------------|-----------|-------|
| Turso HTTP functions (`_turso_request`, `_turso_query`, `_turso_execute`) | ~80 lines | `db.py` ~80 lines | Identical |
| Session/profile CRUD (`_load_session`, `_save_session`, `_reset_session`, etc.) | ~120 lines | `db.py` ~120 lines | Identical |
| Check-in helpers (`_get_latest_checkin`, `_save_checkin`, `_get_card_priorities`, etc.) | ~100 lines | `db.py` ~80 lines | Identical |
| LLM functions (`_spur_chat_completion`, `_stream_llm_response`, `_extract_llm_content`) | ~120 lines | `llm.py` ~120 lines | **Divergent** |
| Question constants (`BUSINESS_TYPES`, `UNIVERSAL_QUESTIONS`, `QUESTIONS_BY_TYPE`, `Q1`, `Q5`) | ~100 lines | `prompts.py` ~100 lines | Identical |
| `_detect_business_type` | ~35 lines | `prompts.py` ~35 lines | Identical |
| `_build_system_prompt` | ~70 lines | `prompts.py` ~70 lines | Identical |
| `_build_checkin_prompt` | ~60 lines | `prompts.py` ~60 lines | **Divergent** (conv variable bug) |
| `CHECKIN_QUESTIONS_BY_TYPE` | ~80 lines | `prompts.py` ~80 lines | Identical |
| `_run_card_selection` | ~90 lines | `cards.py` ~90 lines | **Divergent** (profile slicing) |
| `AVAILABLE_CARDS` | ~25 lines | `cards.py` ~25 lines | Identical |
| `ChatRequest` model | ~25 lines | `models.py` ~25 lines | Identical |

**Total duplicated lines:** ~900+ lines (27% of main.py is duplicated in modules)

### Redundant `_get_all_checkins` vs `_get_latest_checkin`

`_get_latest_checkin` (line 838) queries `ORDER BY created_at DESC LIMIT 1`.  
`_get_all_checkins` (line 858) queries `ORDER BY created_at ASC`.  
Both parse the same row structure. Could share a `_parse_checkin_row` helper.

### Redundant SMTP Send Functions

- `_send_email_sync` (line 1363) — for transcript emails
- `_send_email_to_sync` (line 2272) — for reminder emails

Both are identical except `_send_email_to_sync` takes a `to_email` parameter. Should be one function.

### Redundant `init_db` in main.py and db.py

`main.py:345-489` has a full `init_db()` that creates all 12 tables.  
`db.py:111-161` has a partial `init_db()` that only creates 7 tables (missing users, reminder_settings, businesses, business_members, business_invites, branding). The db.py version is incomplete and would fail if used.

### Redundant Frontend i18n Systems

- `index.html` has `FALLBACK_TRANSLATIONS` (~90 lines) + fetches from `/api/i18n/{lang}`
- `dashboard.html` has `I18N_DEFAULTS` (~80 lines) + fetches from `/api/i18n/{lang}`
- Backend has `_I18N_STRINGS_EN` / `_I18N_STRINGS_FR` (~40 lines)

Three separate translation systems with partially overlapping keys.

---

## Refactoring Roadmap

### Phase 1: Fix Critical Issues (1-2 days)

1. **Fix the Dockerfile** to copy all backend modules:
   ```dockerfile
   COPY backend/ . 
   ```
   This enables connection pooling and uses the extracted modules.

2. **OR: Remove the extracted modules** if the intent is to keep `main.py` as the single source. Delete `db.py`, `llm.py`, `prompts.py`, `cards.py`, `models.py` and remove the `try/except ImportError` block (lines 36-41).

3. **Fix the `_build_checkin_prompt` `UnboundLocalError`** (main.py:1089) — initialize `conv = []` before the if-block, matching the fix in `prompts.py:262`.

4. **Fix the streaming client resource leak** (main.py:1493) — use `async with` or add explicit `__aexit__` cleanup.

5. **Add auth to export endpoints** — remove `/api/export/*` from `_PUBLIC_PATHS` or add per-session authorization checks.

### Phase 2: Security Hardening (1-2 days)

6. **Validate `logo_url`** in branding — restrict to HTTPS URLs, validate domain, or use a allowlist.
7. **Fix admin token storage** — use httpOnly cookies or a more secure mechanism than `sessionStorage`.
8. **Fix SMTP TLS** — pass `ssl.create_default_context()` to `starttls()`.
9. **Add per-user timezone** to reminder settings and use it in the reminder loop.
10. **Fix weekly summary `last_sent_date` collision** — add a `last_weekly_sent_date` column.

### Phase 3: Performance (1-2 days)

11. **Fix admin N+1 queries** — replace per-user loops with JOIN queries:
    ```sql
    SELECT u.user_id, u.email, u.created_at, ss.conversation
    FROM users u
    LEFT JOIN survey_sessions ss ON ss.user_id = u.user_id
    ORDER BY u.created_at DESC
    ```

12. **Fix `admin_businesses` N+1** — batch-load business profiles and check-ins.

13. **Add caching** to dashboard API calls — `transcript`, `profile`, `checkin/status`, `business-profile` could be cached with short TTLs.

14. **Fix rate limiter** — replace global `asyncio.Lock` with per-session locking or use a token bucket algorithm.

### Phase 4: Architecture (3-5 days)

15. **Split `main.py`** into proper modules:
    - `app.py` — FastAPI app, middleware, lifespan
    - `routes/survey.py` — survey chat, state, transcript
    - `routes/checkin.py` — check-in chat, status
    - `routes/auth.py` — register, login, me
    - `routes/admin.py` — admin stats, users, businesses
    - `routes/export.py` — CSV/JSON export
    - `routes/business.py` — create, invite, accept, members
    - `routes/branding.py` — get/save branding
    - `routes/i18n.py` — translations
    - `routes/voice.py` — transcription
    - `routes/reminder.py` — reminder settings + background loop

16. **Consolidate i18n** into a single JSON file per language, loaded by both frontend and backend.

17. **Add OpenAPI schema validation** for all request/response models.

18. **Add integration tests** that test the full SSE streaming flow end-to-end.

### Phase 5: Code Quality (2-3 days)

19. **Remove magic numbers** — extract `_RATE_LIMIT_WINDOW`, `_RATE_LIMIT_MAX_REQUESTS`, `total_steps = 5`, profile truncation lengths, etc. into named constants.

20. **Add type annotations** to all function signatures (many are missing return types).

21. **Consolidate duplicate SMTP functions** into one `_send_email(msg, to_email)` helper.

22. **Add structured logging** with request IDs for tracing.

23. **Fix the `business_id_val` dead variable** (main.py:2944).

24. **Make `_d15GetCardId` in dashboard.html** more robust — it relies on parsing kicker text which is fragile.

---

## API Contract Mismatches (Frontend ↔ Backend)

| Endpoint | Frontend Call | Backend Response | Issue |
|----------|--------------|-------------------|-------|
| `GET /api/survey/checkin/status` | Expects `latest_checkin.created_at` as ISO date | Backend returns Turso's `datetime('now')` format (`YYYY-MM-DD HH:MM:SS`) | `new Date(created_at)` may fail in some browsers |
| `POST /api/survey/chat` | Sends `{ answer, session_id }` in body | Expects `ChatRequest` model | OK — matches |
| `POST /api/survey/checkin` | Sends `{ answer, session_id }` in body | Expects `ChatRequest` model | OK — matches |
| `GET /api/survey/state` | Expects `q_index`, `total_questions`, `conversation` | Returns `_get_state(sess)` | OK — matches |
| `GET /api/branding/{business_id}` | Dashboard fetches by `sessionId` (a string like `s-xxx`) | Backend expects `business_id` as `int` (path param) | **MISMATCH**: frontend passes string session_id, backend tries to parse as int — FastAPI will return 422 |
| `POST /api/reminder/settings` | Frontend sends `{ email, reminder_time, enabled }` (no `phone`) | Backend `ReminderSettingsRequest` requires `email`, `phone` has default | OK — `phone` defaults to `""` |
| `GET /api/survey/priorities/{session_id}` | Dashboard doesn't call this | Backend returns `{ "priorities": [...] }` | Dashboard uses `checkin/status` instead |
| SSE stream | Frontend expects `data: {"content": "..."}` and `data: {"done": true, "state": {...}, "choices": [...]}` | Backend yields all of these | OK — matches |

**The branding endpoint mismatch is a real bug:** The frontend calls `fetch(\`/api/branding/${encodeURIComponent(sessionId)}\`)` where `sessionId` is a string like `s-lx2k3m-abc123`. The backend route `GET /api/branding/{business_id}` expects an `int` path parameter. FastAPI will return a 422 validation error, and branding will silently fail (the frontend catches the error and sets `branding = null`).

---

## Test Coverage Assessment

| Area | Test File | Coverage | Gaps |
|------|-----------|----------|------|
| ChatRequest validation | test_survey.py | Good | — |
| `_detect_business_type` | test_survey.py | Excellent (11 types, fuzzy) | — |
| `_get_questions_for_type` | test_survey.py | Good | — |
| `_parse_response` | test_survey.py | Good | — |
| Rate limiter | test_survey.py | Good | — |
| `_build_system_prompt` | test_survey.py | Good | — |
| `_build_checkin_prompt` | test_survey.py | Good | — |
| Password hashing | test_auth.py | Excellent | — |
| Token generation/verification | test_auth.py | Excellent | — |
| Register endpoint | test_auth.py | Good | No test for concurrent registration race |
| Login endpoint | test_auth.py | Good | — |
| Card data | test_cards.py | Good | — |
| SSE streaming flow | — | **NONE** | No integration test for the streaming endpoint |
| Admin endpoints | — | **NONE** | No tests for admin stats/users/businesses/checkins |
| Export endpoints | — | **NONE** | No tests for CSV/JSON export |
| Business endpoints | — | **NONE** | No tests for create/invite/accept-invite/members |
| Branding endpoints | — | **NONE** | No tests for get/save branding |
| i18n endpoint | — | **NONE** | No tests for translation retrieval |
| Voice transcription | — | **NONE** | No tests for `/api/transcribe` |
| Reminder loop | — | **NONE** | No tests for background reminder loop |
| Weekly summary | — | **NONE** | No tests for weekly summary generation |

**Test coverage estimate:** ~40% of backend logic is tested. The survey/auth/cards core is well-covered, but all newer features (admin, export, business, branding, i18n, voice, reminders, weekly summary) have zero test coverage.

---

## Dockerfile Issues

1. **Only copies `main.py`** — missing `db.py`, `llm.py`, `prompts.py`, `cards.py`, `models.py`
2. **No `.dockerignore` for `backend/tests/`** — test files could leak into image (though `.dockerignore` excludes `*.md`, tests are `.py`)
3. **Health check uses `urllib`** — works but doesn't verify response body, only status 200
4. **No multi-stage build** — final image includes pip cache and build tools

---

## Summary

The survey-deploy web app has been hardened through 7 audit rounds and the core survey + auth + check-in flows are solid. However, this forensic audit found **17 issues** (3 critical, 5 high, 6 medium, 3 low) that automated scans missed:

- The module extraction is **incomplete and dangerous** — `main.py` contains full duplicated copies of all extracted modules, with divergences already appearing (profile slicing direction, `conv` variable scoping bug, streaming client handling). The Dockerfile doesn't copy the extracted modules, so they're dead code in production.

- **Export endpoints have no authentication**, allowing anyone with a session_id to download full user data.

- The **admin endpoints have severe N+1 query patterns** that will cause performance degradation at scale.

- **Newer features** (admin, export, business, branding, i18n, voice, reminders, weekly summary) have **zero test coverage**.

- A **real API contract mismatch** exists on the branding endpoint (frontend passes string session_id, backend expects int business_id).

The recommended path forward is Phase 1 of the refactoring roadmap: decide whether to use the extracted modules (fix Dockerfile) or delete them (remove dead code), then fix the critical bugs.
