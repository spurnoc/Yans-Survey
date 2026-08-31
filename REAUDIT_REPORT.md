# Daily 15 — Forensic Re-Audit Report

**Date:** August 30, 2026  
**Branch:** `dev` (commit `16afa91`)  
**Auditor:** Hermes Agent (automated, model-driven)  
**Scope:** Full codebase — `backend/main.py` (1492 lines), `frontend/index.html` (878 lines), `frontend/dashboard.html` (1630 lines), `Dockerfile` (21 lines), `.dockerignore` (36 lines), `requirements.txt` (4 lines)  
**Methodology:** Line-by-line read of every file. Python syntax check via `py_compile`. Pattern searches for dead code, stale references, innerHTML usage, missing awaits. Cross-reference against previous `AUDIT_REPORT.md`.

---

## 1. Executive Summary

| Audit Thread | Score | Verdict |
|---|---|---|
| **Static / Style** | **B-** | Clean — all imports at module top, dead code removed, validators in place. Minor: two stale `StopAsyncIteration` except blocks remain, a `.bak` file and stale `survey.html` are in the repo. |
| **Structural / Routing** | **C** | Same god-file architecture (44 functions, 13 endpoints in one file). State still split across memory + DB + function attributes. No module separation. But session load is now correctly split into read-only vs create. |
| **Security / Performance** | **C+** | Rate limiter now uses `asyncio.Lock`. Input validated. CORS restricted. Security headers present. But: still no auth, new httpx client created per DB call, `_run_checkin_analysis` available card list is truncated (8 of 20). |
| **Functional / Testing** | **D+** | All 20 card renderers present. No tests. Check-in state still in function-attribute memory. Dashboard still all mock data. Frontend `fetch` calls in dashboard lack timeouts. |

**Overall Grade: C+**

The fixes landed. The codebase is noticeably better than the previous audit's D+ grade. The 12 critical violations are all resolved or have known-accepted deferrals. However, this re-audit found **4 new issues introduced by the fixes** and **5 residual issues** that remain despite being listed as fixed.

---

## 2. Previous Audit Violations — Verification Status

### CRITICAL-1: No Authentication on Any Endpoint
**Status:** ✅ KNOWN-ACCEPTED (Phase 5 deferral)  
The endpoints still accept `session_id` as a query parameter with no auth. This is explicitly marked as a known deferral for Phase 5 production readiness. However, the `POST /api/survey/reset` endpoint (line 1449) accepts `session_id` as a query parameter with **no validation** — see NEW-1 below.

### CRITICAL-2: Rate Limiter Race Condition
**Status:** ✅ FIXED  
`backend/main.py` lines 651-680: `_check_rate_limit` is now `async` and uses `async with _rate_limit_lock` (line 660) for all dict read/write operations. Opportunistic cleanup at line 675 prevents unbounded growth. Correct implementation.

### CRITICAL-3: Unbounded Input — No Length Validation on `answer`
**Status:** ✅ FIXED  
`backend/main.py` lines 616-623: `ChatRequest.answer` now has a `@field_validator` with `len(v) > 5000` check (line 621). `session_id` also validated (lines 625-634) with max 100 chars and `^[a-zA-Z0-9_-]+$` regex.

### CRITICAL-4: `_load_session` Creates DB Rows on GET Requests
**Status:** ✅ FIXED  
`backend/main.py` lines 340-371: Split into `_load_session` (read-only, returns `None` if not found, line 340) and `_load_or_create_session` (INSERT if not found, line 357). GET endpoints use `_load_session`; POST endpoints use `_load_or_create_session`. Correct.

### CRITICAL-5: Check-in Conversations Lost on Server Restart
**Status:** ⚠️ PARTIALLY FIXED  
TTL (30 min) and max conversations (100) are implemented (lines 1280-1281). `asyncio.Lock` protects access (line 1304). However, state is still stored as `checkin_chat._conversations` function attribute (line 1297) — **in-memory only**. Server restart still loses active check-in conversations. This is a known architectural limitation (Phase 2 deferral).

### CRITICAL-6: 4 of 20 Card Types Have No Frontend Renderer
**Status:** ✅ FIXED  
`frontend/dashboard.html` lines 1235-1404: All 4 missing renderers (`expenses`, `contacts`, `decisions`, `staff_schedule`) are now implemented. Each renders metrics, list items, and action buttons consistent with the existing card patterns.

### CRITICAL-7: Business Type Mismatch Between Frontend and Backend
**Status:** ✅ FIXED  
`frontend/index.html` lines 360-372: `BUSINESS_TYPES` array now uses no spaces around slashes (`'Restaurant/Cafe'`, `'Salon/Spa/Barber'`, etc.). Matches backend `BUSINESS_TYPES` (lines 78-83). Comment at line 359 notes this requirement.

### CRITICAL-8: Dead Code — StopAsyncIteration Never Raised
**Status:** ⚠️ PARTIALLY FIXED  
The `except StopAsyncIteration` blocks are still present at lines 1058 and 1360. They now contain `pass` instead of trying to read `stop.value`, which makes them harmless. However, they are **still dead code** — in Python 3.7+, `StopAsyncIteration` inside an `async for` is a `RuntimeError` (PEP 479). The blocks will never execute. The docstring at lines 934-938 still references `StopAsyncIteration` as if it were functional. See NEW-2.

### CRITICAL-9: `_detect_business_type` Called Twice
**Status:** ✅ FIXED  
`backend/main.py` `_build_system_prompt` (lines 837-906): `_detect_business_type` and `_get_questions_for_type` are called once at lines 839-840. No duplicate call remains.

### CRITICAL-10: `business_profiles` Table Created Outside `init_db()`
**Status:** ✅ FIXED  
`backend/main.py` lines 330-337: `business_profiles` CREATE TABLE is now in `init_db()`. `_save_business_profile` (lines 1209-1218) only does INSERT OR REPLACE, no DDL.

### CRITICAL-11: Deprecated `@app.on_event("startup")`
**Status:** ✅ FIXED  
`backend/main.py` lines 30-34: Uses `@asynccontextmanager` lifespan. Line 36: `app = FastAPI(title="SPUR Survey", lifespan=lifespan)`. No `on_event` decorator remains.

### CRITICAL-12: All Dashboard Cards Render Hardcoded Mock Data
**Status:** ✅ KNOWN-ACCEPTED (Phase 6 deferral)  
All 20 card renderers still use hardcoded data defined inline in the card functions. All `onClick` handlers are `() => {}`. Explicitly marked as a known deferral.

---

## 3. New Issues Introduced by Fixes (Regression Audit)

### NEW-1: `POST /api/survey/reset` Missing `session_id` Validation — MEDIUM
**File:** `backend/main.py`, line 1449  
```python
@app.post("/api/survey/reset")
async def reset(session_id: str):
    await _reset_session(session_id)
```
The `reset` endpoint accepts `session_id` as a bare query parameter with **no validation**. Every other GET endpoint now calls `_validate_session_id_param()` (lines 1384, 1394, 1410, 1417, 1426, 1435, 1461), but this POST endpoint does not. This means an attacker can pass arbitrary strings (including SQL injection payloads, though Turso's parameterized queries mitigate this) as `session_id`. More critically, any `session_id` value can trigger a `DELETE`-equivalent operation on any session's data. The `_reset_session` function (lines 387-397) executes `INSERT OR REPLACE` with the raw `session_id`, meaning an attacker can overwrite any session's conversation with empty data.

**Fix:** Add `session_id = _validate_session_id_param(session_id)` as the first line of the function.

### NEW-2: Stale `StopAsyncIteration` Exception Blocks Remain — LOW
**File:** `backend/main.py`, lines 1058-1059 and 1360-1361  
```python
except StopAsyncIteration:
    pass
```
The previous audit flagged the `except StopAsyncIteration as stop: full_response = stop.value` pattern as dead code. The fix changed the body to `pass` (harmless), but **left the dead `except` block in place**. The docstring of `_stream_llm_response` (lines 934-938) still references `StopAsyncIteration` as a return mechanism. In Python 3.7+ (PEP 479), `StopAsyncIteration` inside an `async for` is wrapped as `RuntimeError` and would never be caught by this `except` block. The blocks and the docstring paragraphs are misleading dead code.

**Fix:** Remove both `try`/`except` wrappers — just use `async for chunk in gen: ...`. Remove the `StopAsyncIteration` references from the docstring (lines 934-938, 997-998).

### NEW-3: `CHECKIN_QUESTIONS_BY_TYPE` Only Covers 5 of 11 Business Types — MEDIUM
**File:** `backend/main.py`, lines 504-540  
The `CHECKIN_QUESTIONS_BY_TYPE` dict has entries for only: `Restaurant/Cafe`, `Salon/Spa/Barber`, `Plumber/Electrician/HVAC`, `Retail/Boutique`, `Gym/Fitness Studio`. Missing: `Landscaping/Lawn Care`, `Auto Repair/Detailing`, `Cleaning Service`, `Photography/Video`, `Real Estate`, `Other`. 

The `_build_checkin_prompt` function (line 586) uses `CHECKIN_QUESTIONS_BY_TYPE.get(business_type, DEFAULT_CHECKIN)`, so missing types fall back to `DEFAULT_CHECKIN` — this is **functionally correct** but means 6 business types get generic check-in questions rather than tailored ones. The previous audit's Phase 2 roadmap item 2.7 explicitly called for adding these 5 sets, and it was not done. Not a regression per se, but the fix that moved `CHECKIN_QUESTIONS_BY_TYPE` to module level (from inline in `_build_checkin_prompt`) did not address the incomplete coverage.

### NEW-4: Dashboard `fetch()` Calls Lack Timeouts — MEDIUM
**File:** `frontend/dashboard.html`, lines 501-504  
```javascript
const [transcriptResp, profileResp, checkinStatusResp, businessProfileResp] = await Promise.all([
    fetch(`${API}/transcript?session_id=${sessionId}`).then(r => r.json()),
    fetch(`${API}/profile/${sessionId}`).then(r => r.ok ? r.text() : ''),
    fetch(`${API}/checkin/status?session_id=${sessionId}`).then(r => r.json()),
    fetch(`${API}/business-profile/${sessionId}`).then(r => r.ok ? r.json() : null).catch(() => null),
]);
```
The previous audit added `fetchWithTimeout` to `frontend/index.html` (line 347) with `AbortController`-based timeout. However, `frontend/dashboard.html` was **not updated** — it uses bare `fetch()` without any timeout. If the backend is slow or unresponsive, these 4 parallel requests will hang indefinitely, leaving the dashboard stuck on the skeleton loading state. The `.catch(() => null)` on line 504 only catches network errors, not timeouts.

**Fix:** Replace all 4 `fetch()` calls with `fetchWithTimeout()` or add `AbortSignal.timeout(30000)` to each.

---

## 4. Residual Issues (Listed as Fixed but Not Fully Fixed)

### RESIDUAL-1: `survey.html` Still Contains Stale "Benji/Yans Deli" Content
**File:** `frontend/survey.html`, lines 7, 281, 292, 407, 557  
The previous audit stated "Stale references → no Benji/Yans Deli." This is true for the **active** frontend files (`index.html`, `dashboard.html`). However, `frontend/survey.html` still exists in the repo and contains:
- `<title>Yans Deli — Daily 15 Survey</title>` (line 7)
- `<div class="header-title">Yans Deli</div>` (line 281)
- `Hey Benji — quick survey...` (line 292)
- `Thanks Benji — this gives us a real picture...` (line 407)

This file is served by FastAPI's `StaticFiles` mount (line 1492) and is accessible at `/survey.html`. It also contains the old XSS-vulnerable `innerHTML` pattern with unescaped user content in `showInput()` (line 381: `onclick=\"submitAnswer('${c.replace(/'/g, \"\\\\'\")}')\"`), the empty `catch {}` (line 490), hardcoded `totalQuestions = 13`, and no `session_id` in fetch calls.

**Fix:** Delete `frontend/survey.html` — it is a pre-refactor artifact superseded by `index.html`.

### RESIDUAL-2: `backend/main.py.bak` Committed to Repo
**File:** `backend/main.py.bak` (1496 lines, 71KB)  
A backup of the pre-fix `main.py` is committed to the repo. While `.dockerignore` excludes `*.md` and `*.pyc`, it does **not** exclude `*.bak` files, so this 71KB file is included in the Docker image. This wastes image space and is a version control anti-pattern (git history serves this purpose).

**Fix:** Delete `backend/main.py.bak` and add `*.bak` to `.dockerignore`.

### RESIDUAL-3: `_run_checkin_analysis` Has Truncated Card ID List
**File:** `backend/main.py`, line 1246  
```python
"Available card IDs: sales, reviews, social, catering, inventory, checklist, goals, stress. "
```
The `AVAILABLE_CARDS` list (lines 1099-1120) defines 20 card IDs, but the check-in analysis prompt only lists 8 of them. This means `_run_checkin_analysis` can only ever recommend cards from those 8, even if the onboarding-selected cards include `staff`, `expenses`, `contacts`, `decisions`, `appointments`, `pipeline`, `retention`, `memberships`, `routes`, `equipment`, `invoices`, or `staff_schedule`. The check-in priority reordering will never surface these cards as priorities, creating a mismatch with the dashboard card selection engine (which considers all 20).

**Fix:** Replace the hardcoded string with a dynamic list: `"Available card IDs: " + ", ".join(c["id"] for c in AVAILABLE_CARDS) + ". "`

### RESIDUAL-4: InnerHTML Used for Typing Indicator and Typewriter Effect
**File:** `frontend/index.html`, lines 699, 769, 775, 789  
The XSS fix replaced user-content `innerHTML` with `createElement`/`textContent` for choice buttons (line 526) and conversation rendering (lines 472-487). However, `innerHTML` is still used in:
- Line 699: `aiBubble.innerHTML = '<div class="typing"><span></span>...'` — **safe** (no user data, static HTML)
- Line 769: `aiBubble.innerHTML = '<span class="stream-cursor"></span>'` — **safe** (static HTML)
- Line 775: `aiBubble.innerHTML = escapeHtml(finalText.slice(0, i)) + '<span class="stream-cursor"></span>'` — **safe** (`escapeHtml()` is applied)
- Line 789: `aiBubble.innerHTML = escapeHtml(finalText)` — **safe** (`escapeHtml()` is applied)
- Line 859: `panel.innerHTML = html` in `loadTranscript()` — **safe** (`escapeHtml()` applied to all user content at lines 851, 853)

All remaining `innerHTML` uses either contain no user data or apply `escapeHtml()`. This is **acceptable** but could be further hardened by using DOM APIs instead.

### RESIDUAL-5: Check-in Lock Released Before LLM Streaming — Race Window
**File:** `backend/main.py`, lines 1337-1338  
```python
    # ── Lock released: build prompt + stream LLM without holding it ──
    system_prompt = await _build_checkin_prompt(req.session_id, conv["messages"], conv["step"])
```
After releasing the lock at line 1337, the code accesses `conv["messages"]` and `conv["step"]` (line 1338) without holding the lock. `conv` is a reference to `checkin_chat._conversations[checkin_key]`, and if another request for the same `session_id` arrives concurrently, it could modify `conv["messages"]` or `conv["step"]` while `_build_checkin_prompt` is reading them. The `sse_stream()` closure (lines 1351-1376) re-acquires the lock before appending to `conv["messages"]` (line 1365), but the prompt-building phase between lock release and stream start is unprotected.

This is a **low-probability race** (requires two check-in messages from the same session within the LLM call window), but it's a real concurrency issue.

**Fix:** Copy `messages` and `step` into local variables inside the lock before releasing it.

---

## 5. Redundancy Report

### RED-1: SSE Stream Consumption Pattern Still Duplicated (2 occurrences)
**File:** `backend/main.py`, lines 1053-1059 and 1355-1361  
```python
gen = _stream_llm_response(messages, SURVEY_MODEL, max_tokens=X)
full_response = ""
try:
    async for chunk in gen:
        if chunk.startswith("data: {") and '"content"' in chunk:
            full_response += json.loads(chunk[6:])["content"]
        yield chunk
except StopAsyncIteration:
    pass
```
Identical 7-line pattern in both `chat()` and `checkin_chat()` SSE generators. The shared `_stream_llm_response` helper (line 923) was created (REDUNDANCY-4 from previous audit is resolved), but the **consumption** pattern is still duplicated. Could be extracted into a helper that yields chunks and returns the accumulated text.

### RED-2: `_turso_execute` Is a Trivial Wrapper
**File:** `backend/main.py`, lines 251-256  
```python
async def _turso_execute(sql: str, args: list = None):
    """Execute a SQL statement via the Turso HTTP API (async)."""
    return await _turso_request(sql, args)
```
`_turso_execute` is a one-line pass-through to `_turso_request`. It adds no logic and exists only for naming clarity. This is acceptable as a readability aid but is technically redundant.

### RED-3: `hasattr` Check Duplicated in Check-in Handler
**File:** `backend/main.py`, lines 1297-1298 and 1306-1307  
```python
if not hasattr(checkin_chat, '_conversations'):  # line 1297 — before lock
    checkin_chat._conversations = {}
...
async with _checkin_lock:
    if not hasattr(checkin_chat, '_conversations'):  # line 1306 — inside lock (redundant)
        checkin_chat._conversations = {}
```
The `hasattr` check at line 1297 runs **before** acquiring the lock and is not protected. The check at line 1306 runs **inside** the lock and is the safe one. The pre-lock check at 1297 is redundant — if the dict doesn't exist, the in-lock check will create it. The pre-lock check only serves as a fast-path optimization that avoids entering the lock if the dict already exists, but it introduces a TOCTOU window.

### RED-4: Card `.map()` → `list-item` Pattern (20+ occurrences)
**File:** `frontend/dashboard.html`, throughout lines 695-1404  
Every card renderer contains a near-identical `items.map((item, i) => React.createElement('div', { key: i, className: 'list-item' }, ...))` pattern. With 20 card renderers, this is the single most duplicated pattern in the codebase. A shared `ListItems` component would eliminate ~200 lines of repetition.

### RED-5: CSS Duplicated Between Frontend Files
**Files:** `frontend/index.html` (lines 11-309) and `frontend/dashboard.html` (lines 10-450)  
Both files define the same CSS reset, `--accent` color variables, `mode-pill` styles, scrollbar styles, and animation keyframes. No shared CSS file exists.

---

## 6. Refactoring Roadmap

### Phase 1: Fix New Issues (BLOCKING — do immediately)

| # | Action | File:Line | Effort |
|---|---|---|---|
| 1.1 | Add `_validate_session_id_param()` to `reset` endpoint | `main.py:1449` | 5 min |
| 1.2 | Remove stale `except StopAsyncIteration: pass` blocks + fix docstring | `main.py:1058, 1360, 934-938` | 10 min |
| 1.3 | Add `fetchWithTimeout` to dashboard `fetchData()` calls | `dashboard.html:501-504` | 15 min |
| 1.4 | Delete `frontend/survey.html` (stale Benji/Yans Deli artifact) | `survey.html` | 1 min |
| 1.5 | Delete `backend/main.py.bak` and add `*.bak` to `.dockerignore` | `main.py.bak`, `.dockerignore` | 2 min |
| 1.6 | Fix `_run_checkin_analysis` card ID list to be dynamic | `main.py:1246` | 5 min |
| 1.7 | Copy `conv` fields to locals inside lock before release | `main.py:1337-1338` | 10 min |

### Phase 2: Architectural Refactoring (Known deferrals)

| # | Action | Files | Effort |
|---|---|---|---|
| 2.1 | Split `main.py` into modules: `routes/`, `db/turso.py`, `llm/spur.py`, `prompts.py`, `models.py` | `main.py` (1492 lines → ~8 files) | 1 day |
| 2.2 | Migrate check-in state from function-attribute to Turso (`checkin_sessions` table) | `main.py:1297-1378` | 3 hrs |
| 2.3 | Add missing 6 business-type check-in question sets | `main.py:504-540` | 1 hr |
| 2.4 | Extract shared `ListItems` component for dashboard cards | `dashboard.html` | 2 hrs |
| 2.5 | Extract shared CSS to `common.css` | `index.html`, `dashboard.html` | 1 hr |

### Phase 3: Production Readiness (Known deferrals)

| # | Action | Files | Effort |
|---|---|---|---|
| 3.1 | Add authentication layer (session tokens, API keys) | All endpoints | 1 day |
| 3.2 | Add test suite (unit tests for validators, integration tests for endpoints, SSE stream tests) | New `tests/` dir | 2 days |
| 3.3 | Integrate real data sources for dashboard cards (POS, review APIs, social APIs) | `dashboard.html` | Ongoing |
| 3.4 | Add connection pooling for Turso HTTP client (currently creates new `httpx.AsyncClient` per query) | `main.py:227-248` | 2 hrs |

---

## 7. File-by-File Assessment

### `backend/main.py` (1492 lines, 69KB)
**Python syntax:** ✅ Passes `py_compile`  
**Imports:** ✅ All at module top (lines 10-28). No inline imports.  
**Async/await:** ✅ All Turso calls are async. All endpoint handlers are async. `asyncio.create_task` used correctly for fire-and-forget background work (lines 1024, 1089, 1371).  
**Error handling:** ✅ All `except Exception` blocks log via `logger.debug()` (lines 213, 320, 787, 824, 1206, 1230, 1275). No bare `except:`.  
**DRY helpers:** ✅ `_turso_request`, `_extract_llm_content`, `_extract_json_from_llm`, `_spur_chat_completion`, `_append_recent_context`, `_stream_llm_response` all exist and are used.  
**Remaining issues:** Dead `StopAsyncIteration` blocks, `reset` endpoint missing validation, truncated card ID list in check-in analysis, check-in race window.

### `frontend/index.html` (878 lines, 30KB)
**XSS:** ✅ Safe. Choice buttons use `createElement`/`textContent` (lines 527-548). Conversation rendering uses `textContent` (lines 476, 483). Typewriter uses `escapeHtml()` (lines 775, 789). Transcript panel uses `escapeHtml()` (lines 851, 853).  
**Accessibility:** ✅ ARIA attributes on progress bar (`role="progressbar"`, `aria-valuenow`, `aria-valuemax`), chat area (`role="log"`, `aria-live="polite"`), reset button (`role="button"`, `aria-label`, `tabindex`). Choice buttons have `aria-label`.  
**Fetch timeouts:** ✅ `fetchWithTimeout` with `AbortController` (line 347). Used on all API calls (lines 390, 420, 706, 836, 842).  
**Business types:** ✅ No spaces around slashes.  
**Remaining issues:** `innerHTML` used for static HTML (safe but not ideal).

### `frontend/dashboard.html` (1630 lines, 73KB)
**Card renderers:** ✅ All 20 card types implemented (lines 695-1404).  
**Error handling:** ✅ Each card wrapped in try/catch with `renderCardError` fallback (lines 1501-1507). Global error state with retry button (lines 1548-1559).  
**Accessibility:** ✅ ARIA on cards (`role="article"`, `aria-label`), card grid (`role="region"`, `aria-label`), loading state (`role="status"`, `aria-live`), error state (`role="alert"`).  
**Remaining issues:** No fetch timeouts (NEW-4). All data is hardcoded mock (known Phase 6 deferral).

### `Dockerfile` (21 lines)
**Hardening:** ✅ Non-root user `appuser` (lines 12-14). Healthcheck with 30s interval, 10s timeout, 15s start period (lines 18-19). Pinned `python:3.11.16-slim` base.  
**Build:** ✅ Requirements copied and installed before app code (layer caching).  
**Remaining issues:** `main.py.bak` is included in the image (not in `.dockerignore`).

### `.dockerignore` (36 lines)
**Coverage:** ✅ Excludes `.git`, `__pycache__`, `.env`, `*.db`, `node_modules`, IDE configs, `*.md`, Docker artifacts.  
**Missing:** Does not exclude `*.bak` files.

### `requirements.txt` (4 lines)
**Pinning:** ✅ All 4 dependencies pinned to exact versions (`fastapi==0.115.6`, `uvicorn==0.34.0`, `httpx==0.28.1`, `pydantic==2.10.4`).  
**Note:** `smptlib` and `email` are stdlib (no pip install needed). Correct.

---

## 8. Summary Scorecard

| Category | Previous | Current | Delta |
|---|---|---|---|
| Static / Style | C+ | **B-** | ↑ |
| Structural / Routing | D+ | **C** | ↑ |
| Security / Performance | D | **C+** | ↑ |
| Functional / Testing | F | **D+** | ↑ |
| **Overall** | **D+** | **C+** | ↑ |

**Bottom line:** The fixes worked. The codebase moved from D+ to C+. The 12 original critical violations are resolved or explicitly deferred. 4 new issues were introduced (1 MEDIUM, 2 LOW, 1 MEDIUM that was pre-existing but unnoticed). The most actionable items are: (1) add validation to the `reset` endpoint, (2) add fetch timeouts to the dashboard, (3) delete the stale `survey.html` and `main.py.bak` files, and (4) fix the truncated card ID list in check-in analysis.
