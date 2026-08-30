# Daily 15 — Comprehensive Forensic Codebase Audit

**Date:** August 30, 2026  
**Branch:** `dev`  
**Auditor:** Hermes Agent (automated, model-driven)  
**Scope:** Full codebase — backend/main.py (1496 lines), frontend/index.html (816 lines), frontend/dashboard.html (1459 lines), Dockerfile (21 lines), .dockerignore (36 lines), requirements.txt (4 lines)  
**Methodology:** Full file read-through, line-by-line, across 4 audit threads  

---

## 1. Executive Summary

| Audit Thread | Score | Verdict |
|---|---|---|
| **Static / Style** | **C+** | Functional but inconsistent — inline imports, dead code blocks, mixed conventions, duplicated constants |
| **Structural / Routing** | **D+** | God-file architecture (44 functions, 13 endpoints in one file), state split across memory+DB+function-attributes, no separation of concerns |
| **Security / Performance** | **D** | No auth on any endpoint, unbounded input, rate-limiter race condition, 6 separate httpx client instances per request chain, no connection pooling |
| **Functional / Testing** | **F** | Zero tests. 4 of 20 card types can't render. Check-in state lost on restart. Business-type mismatch between frontend/backend. Dead StopAsyncIteration code. |

**Overall Grade: D+**

The application is a functional prototype that was hardened by previous automated scans (CORS, rate limiting, input validation, security headers, Docker hardening) but has never been architecturally reviewed. The core issues are structural: a 1496-line god-file with no module separation, no test coverage, no auth layer, and state management split across three incompatible mechanisms (Turso DB, in-memory dicts, function attributes). The frontend renders hardcoded mock data for all 20 card types — the dashboard is a UI shell, not a functional product.

---

## 2. Critical Violations

### CRITICAL-1: No Authentication on Any Endpoint
**File:** `backend/main.py`, lines 1430–1473  
**Severity:** CRITICAL  
```
@app.get("/api/survey/state")
async def get_state(session_id: str):          # ← any session_id = any data
    sess = await _load_session(req.session_id)
    return _get_state(sess)

@app.get("/api/survey/transcript")
async def get_transcript(session_id: str):     # ← full conversation exposed
    ...

@app.get("/api/survey/profile/{session_id}")  # ← behavioral profile exposed
    ...

@app.get("/api/survey/profiles")              # ← lists ALL sessions in DB
    rows = await _turso_query(
        "SELECT session_id, length(profile) as size, updated_at FROM survey_profiles ..."
    )
```
Every endpoint accepts a `session_id` query parameter with no authentication. Anyone who guesses or obtains a session ID can read another user's full conversation history, behavioral profile, and business data. The `/profiles` endpoint enumerates all sessions in the database.

### CRITICAL-2: Rate Limiter Race Condition — Not Thread-Safe
**File:** `backend/main.py`, lines 621–648  
**Severity:** HIGH  
```python
_rate_limit_store: dict[str, list[float]] = {}    # line 621 — module-level dict

def _check_rate_limit(session_id: str) -> bool:     # line 624 — NOT async, no lock
    now = time.time()
    window_start = now - _RATE_LIMIT_WINDOW
    timestamps = _rate_limit_store.get(session_id, [])  # read
    timestamps = [t for t in timestamps if t > window_start]
    if len(timestamps) >= _RATE_LIMIT_MAX_REQUESTS:
        _rate_limit_store[session_id] = timestamps       # write
        return False
    timestamps.append(now)                                # write
    _rate_limit_store[session_id] = timestamps           # write
    ...
```
`_check_rate_limit` is a synchronous function that reads and writes a shared dict. Under FastAPI's async event loop, concurrent requests to the same `session_id` can interleave between the read at line 630 and the write at line 640, allowing the rate limit to be bypassed. The check-in endpoint correctly uses `asyncio.Lock` (line 1287) but the rate limiter does not.

### CRITICAL-3: Unbounded Input — No Length Validation on `answer`
**File:** `backend/main.py`, lines 602–615  
**Severity:** HIGH  
```python
class ChatRequest(BaseModel):
    answer: str           # ← no max_length, no field_validator
    session_id: str       # ← validated (lines 606-615)

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, v: str) -> str:
        ...
```
`session_id` is validated (max 100 chars, alphanumeric only), but `answer` has **no validation whatsoever**. A user (or attacker) can submit a 10MB answer string. This flows directly into:
- Turso DB storage (line 980: `sess["conversation"].append({"role": "user", "content": req.answer})`)
- LLM API call (line 996: `messages.append({"role": "user", "content": msg["content"]})`)
- Behavioral analysis prompt (line 678: `f"ANSWER: {answer}"`)

### CRITICAL-4: `_load_session` Creates DB Rows on GET Requests — Data Pollution
**File:** `backend/main.py`, lines 335–358  
**Severity:** HIGH  
```python
async def _load_session(session_id: str) -> dict:
    rows = await _turso_query(
        "SELECT * FROM survey_sessions WHERE session_id=?",
        [{"type": "text", "value": session_id}]
    )
    if rows:
        ...
    else:
        await _turso_execute(                              # ← INSERT on a GET!
            "INSERT INTO survey_sessions (session_id) VALUES (?)",
            [{"type": "text", "value": session_id}]
        )
```
`_load_session` is called by `GET /api/survey/state` (line 1432), `GET /api/survey/transcript` (line 1438), and `GET /api/survey/checkin/status` (via `_has_completed_onboarding`). Any GET request with a novel `session_id` creates a new row in `survey_sessions`. This means:
- Web crawlers create garbage rows
- The `/profiles` endpoint (line 1467) will list these garbage sessions
- An attacker can flood the DB with arbitrary session IDs

### CRITICAL-5: Check-in Conversations Lost on Server Restart
**File:** `backend/main.py`, lines 1302–1336  
**Severity:** HIGH  
```python
@app.post("/api/survey/checkin")
async def checkin_chat(req: ChatRequest):
    ...
    if not hasattr(checkin_chat, '_conversations'):       # line 1302
        checkin_chat._conversations = {}                  # function-attribute storage
    ...
    checkin_chat._conversations[checkin_key] = {"messages": [], "step": 0, "_created_at": now}
```
Active check-in conversations are stored as a function attribute (`checkin_chat._conversations`). If the server restarts (container redeploy, crash, OOM kill), all in-progress check-in conversations are lost. The user's check-in is incomplete and `_run_checkin_analysis` never fires, so no data is saved to Turso. This is a data-loss bug under normal operation.

### CRITICAL-6: 4 of 20 Card Types Have No Frontend Renderer
**File:** `backend/dashboard.html`, lines 695–1234; `backend/main.py`, lines 1063–1084  
**Severity:** MEDIUM-HIGH  
```python
# backend/main.py — AVAILABLE_CARDS defines 20 card IDs:
AVAILABLE_CARDS = [
    {"id": "sales", ...}, {"id": "reviews", ...}, {"id": "social", ...},
    {"id": "catering", ...}, {"id": "inventory", ...}, {"id": "staff", ...},
    {"id": "expenses", ...}, {"id": "checklist", ...}, {"id": "goals", ...},
    {"id": "stress", ...}, {"id": "contacts", ...}, {"id": "decisions", ...},
    {"id": "appointments", ...}, {"id": "pipeline", ...}, {"id": "retention", ...},
    {"id": "memberships", ...}, {"id": "routes", ...}, {"id": "equipment", ...},
    {"id": "invoices", ...}, {"id": "staff_schedule", ...},
]
```
```javascript
// frontend/dashboard.html — CARDS dict only has 16 renderers:
const CARDS = {
    sales, reviews, social, catering, inventory, checklist, goals, stress,
    appointments, pipeline, retention, memberships, routes, equipment,
    invoices, staff
    // MISSING: expenses, contacts, decisions, staff_schedule
};
```
If the LLM card selection engine returns `expenses`, `contacts`, `decisions`, or `staff_schedule`, the dashboard silently renders nothing (line 1329: `if (!cardFn) return null;`). The user sees gaps in their dashboard with no explanation.

### CRITICAL-7: Business Type Mismatch Between Frontend and Backend
**File:** `frontend/index.html`, lines 347–359; `backend/main.py`, lines 68–73  
**Severity:** MEDIUM  
```javascript
// frontend/index.html — spaces around the slash:
const BUSINESS_TYPES = [
  'Restaurant / Cafe',
  'Salon / Spa / Barber',
  'Plumber / Electrician / HVAC',
  ...
];
```
```python
# backend/main.py — no spaces:
BUSINESS_TYPES = [
    "Restaurant/Cafe", "Salon/Spa/Barber", "Plumber/Electrician/HVAC",
    ...
]
```
The frontend sends `'Restaurant / Cafe'` (with spaces) as the answer. The backend `_detect_business_type` (line 177–179) does `btype.lower() in first_answer`, checking for `"restaurant/cafe"` in the answer string. Since the answer contains `"restaurant / cafe"` (with spaces), the exact match fails. The function then falls through to the fuzzy match (lines 181–201), which catches `"restaurant"` — but this is fragile and means the exact-match loop (lines 177–179) is dead code for frontend-submitted answers.

### CRITICAL-8: Dead Code — StopAsyncIteration Never Raised
**File:** `backend/main.py`, lines 1022–1023, 1370–1371  
**Severity:** MEDIUM  
```python
    try:
        async for chunk in gen:
            if chunk.startswith("data: {") and '"content"' in chunk:
                full_response += json.loads(chunk[6:])["content"]
            yield chunk
    except StopAsyncIteration as stop:        # ← DEAD CODE
        full_response = stop.value or ""     # ← NEVER EXECUTES
```
In Python 3.7+, `StopAsyncIteration` raised inside an `async for` loop is a `RuntimeError` (PEP 479). The `_stream_llm_response` function is an async generator — it cannot return a value via `StopAsyncIteration`. The `except StopAsyncIteration` block is dead code. This means if the streaming helper were to fail silently, `full_response` would be empty and the user would see a blank message.

### CRITICAL-9: `_detect_business_type` Called Twice in `_build_system_prompt`
**File:** `backend/main.py`, lines 778, 819  
**Severity:** LOW (performance waste)  
```python
async def _build_system_prompt(sess: dict, answered_q_id: int, answered_q_text: str, target_q_index: int) -> str:
    business_type = _detect_business_type(sess.get("conversation", []))  # line 778
    active_questions = _get_questions_for_type(business_type)           # line 779
    ...
    business_type = _detect_business_type(sess.get("conversation", []))  # line 819 — DUPLICATE
    active_questions = _get_questions_for_type(business_type)             # line 820 — DUPLICATE
```
The same detection and question lookup runs twice, producing identical results. Wasted CPU on every chat request.

### CRITICAL-10: `business_profiles` Table Created Outside `init_db()`
**File:** `backend/main.py`, lines 1192–1198  
**Severity:** MEDIUM  
```python
async def _save_business_profile(session_id: str, profile_data: dict):
    await _turso_execute(
        """CREATE TABLE IF NOT EXISTS business_profiles (    # ← DDL in a save function
            session_id TEXT PRIMARY KEY, ...
        )"""
    )
    await _turso_execute(
        """INSERT OR REPLACE INTO business_profiles ..."""
    )
```
The `business_profiles` table is created inside `_save_business_profile` rather than in `init_db()` (lines 290–332). This means:
1. The table creation runs on every `_save_business_profile` call (unnecessary overhead)
2. If `init_db()` is used to verify schema integrity, this table is missing
3. The `ALTER TABLE daily_checkins ADD COLUMN summary` migration (line 322) is in `init_db` but `business_profiles` creation is not — inconsistent schema management

### CRITICAL-11: Deprecated `@app.on_event("startup")`
**File:** `backend/main.py`, lines 1486–1488  
**Severity:** LOW  
```python
@app.on_event("startup")
async def _startup():
    await init_db()
```
`@app.on_event("startup")` is deprecated since FastAPI 0.93 (current version is 0.115.6). Should use the `lifespan` context manager. Will break in future FastAPI versions.

### CRITICAL-12: All Dashboard Cards Render Hardcoded Mock Data
**File:** `frontend/dashboard.html`, lines 696–1233  
**Severity:** HIGH (product-level)  
Every single card type renders hardcoded data defined inline in the card function:
```javascript
sales: (config, priority) => {
    const days = [42, 38, 55, 47, 62, 71, 58];  // ← hardcoded
    const today = 71;                             // ← hardcoded
    const yesterday = 62;                         // ← hardcoded
    ...
reviews: (config, priority) => {
    const reviews = [
      { text: 'Best sandwich in town!', sentiment: 'positive', time: '2h ago' },  // ← hardcoded
      ...
```
No card fetches real data from any API. The `onClick` handlers are all empty: `onClick: () => {}`. The dashboard is a non-functional UI mockup that presents the same fake data to every business. This is the single biggest gap between what the product appears to be and what it actually is.

---

## 3. Redundancy Report

### REDUNDANCY-1: Duplicate Turso HTTP Client Code
**Files:** `backend/main.py`, lines 223–244 (`_turso_execute`) and 247–287 (`_turso_query`)  
**Duplicated code:**
```python
# Appears in BOTH functions (identical):
stmt = {"sql": sql}
if args:
    stmt["args"] = [{"type": a.get("type", "text"), "value": str(a["value"])} for a in args]
body = {"requests": [{"type": "execute", "stmt": stmt}]}
async with httpx.AsyncClient(timeout=30.0) as client:
    resp = await client.post(
        _turso_url(),
        json=body,
        headers={
            "Authorization": f"Bearer {TURSO_AUTH_TOKEN}",
            "Content-Type": "application/json",
        },
    )
if resp.status_code != 200:
    raise Exception(f"Turso API error: {resp.status_code} {resp.text[:300]}")
```
~15 lines duplicated.  
**Proposed unified function:**
```python
async def _turso_request(sql: str, args: list = None) -> dict:
    """Execute SQL via Turso HTTP API and return raw response JSON."""
    stmt = {"sql": sql}
    if args:
        stmt["args"] = [{"type": a.get("type", "text"), "value": str(a["value"])} for a in args]
    body = {"requests": [{"type": "execute", "stmt": stmt}]}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(_turso_url(), json=body, headers=_TURSO_HEADERS)
    if resp.status_code != 200:
        raise Exception(f"Turso API error: {resp.status_code} {resp.text[:300]}")
    return resp.json()

async def _turso_execute(sql: str, args: list = None):
    await _turso_request(sql, args)

async def _turso_query(sql: str, args: list = None) -> list[dict]:
    data = await _turso_request(sql, args)
    # ... parse rows (existing code from lines 264-287)
```

### REDUNDANCY-2: LLM Response Content Extraction (4 occurrences)
**File:** `backend/main.py`  
**Lines:** 716–719, 945–948, 1151–1154, 1259–1262  
**Duplicated code:**
```python
msg = data["choices"][0]["message"]
content = msg.get("content") or ""
if not content and msg.get("reasoning"):
    content = msg["reasoning"]
```
4 identical occurrences.  
**Proposed unified function:**
```python
def _extract_llm_content(data: dict) -> str:
    """Extract text content from an LLM completion response, handling reasoning models."""
    msg = data["choices"][0]["message"]
    return msg.get("content") or msg.get("reasoning") or ""
```

### REDUNDANCY-3: JSON Regex Extraction from LLM (2 occurrences)
**File:** `backend/main.py`  
**Lines:** 1157–1164 and 1265–1269  
**Duplicated code:**
```python
import re as _re                              # ← re-imported inside function
json_match = _re.search(r'\{.*\}', content, _re.DOTALL)
if not json_match:
    return
try:
    result = json.loads(json_match.group())
except json.JSONDecodeError:
    return/...
```
**Proposed unified function:**
```python
def _extract_json_from_llm(content: str) -> dict | None:
    """Extract and parse a JSON object from an LLM response string."""
    match = re.search(r'\{.*\}', content, re.DOTALL)  # re already imported at module top
    if not match:
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        return None
```

### REDUNDANCY-4: SSE Stream Consumption Pattern (2 occurrences)
**File:** `backend/main.py`  
**Lines:** 1018–1023 (chat) and 1366–1371 (checkin)  
**Duplicated code:**
```python
gen = _stream_llm_response(messages, SURVEY_MODEL, max_tokens=X)
full_response = ""
try:
    async for chunk in gen:
        if chunk.startswith("data: {") and '"content"' in chunk:
            full_response += json.loads(chunk[6:])["content"]
        yield chunk
except StopAsyncIteration as stop:
    full_response = stop.value or ""
```
2 identical occurrences (plus dead StopAsyncIteration code).  
**Proposed unified function:**
```python
async def _consume_sse_stream(gen, yield_chunks=True):
    """Consume an SSE generator, accumulating full_response. Yields chunks if requested."""
    full_response = ""
    async for chunk in gen:
        if chunk.startswith("data: {") and '"content"' in chunk:
            try:
                full_response += json.loads(chunk[6:])["content"]
            except (json.JSONDecodeError, KeyError):
                pass
        if yield_chunks:
            yield chunk
    return full_response
```

### REDUNDANCY-5: SPUR API POST Boilerplate (4 occurrences)
**File:** `backend/main.py`  
**Lines:** 698–712, 890–905, 1107–1146, 1228–1255  
**Duplicated code:**
```python
async with httpx.AsyncClient(timeout=X) as client:
    resp = await client.post(
        f"{SPUR_API_BASE}/chat/completions",
        json={
            "model": MODEL,
            "messages": messages,
            "stream": False,
            "temperature": T,
            "max_tokens": N,
        },
        headers={
            "Authorization": f"Bearer {SPUR_DEMO_API_KEY}",
            "Content-Type": "application/json",
        },
    )
```
4 occurrences of ~10 lines each.  
**Proposed unified function:**
```python
async def _spur_chat_completion(messages: list[dict], model: str, 
                                  temperature: float = 0.6, max_tokens: int = 1000,
                                  stream: bool = False, timeout: float = 30.0):
    """Make a SPUR API chat completion request."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(
            f"{SPUR_API_BASE}/chat/completions",
            json={"model": model, "messages": messages, "stream": stream,
                  "temperature": temperature, "max_tokens": max_tokens},
            headers={"Authorization": f"Bearer {SPUR_DEMO_API_KEY}",
                     "Content-Type": "application/json"},
        )
```

### REDUNDANCY-6: Context Window Message Building (2 occurrences)
**File:** `backend/main.py`  
**Lines:** 993–998 (chat) and 1347–1352 (checkin)  
**Duplicated code:**
```python
recent = conv["messages"][-4:]
for msg in recent:
    if msg["role"] == "user":
        messages.append({"role": "user", "content": msg["content"]})
    else:
        messages.append({"role": "assistant", "content": msg["content"]})
```
2 identical occurrences.  
**Proposed unified function:**
```python
def _append_recent_context(messages: list, conversation: list, window: int = 4):
    """Append the last N messages from a conversation to the messages list."""
    for msg in conversation[-window:]:
        messages.append({"role": msg["role"], "content": msg["content"]})
```

### REDUNDANCY-7: Q5 "Proactive" Question Identical Across All 11 Business Types
**File:** `backend/main.py`, lines 93, 99, 105, 111, 117, 123, 129, 135, 141, 147, 153  
**Duplicated code:**
```python
{"id": 5, "text": "Would you rather this just show you what happened, or tell you what to do next?", "type": "choice", "tag": "proactive"}
```
Appears identically in all 11 entries of `QUESTIONS_BY_TYPE`.  
**Proposed fix:** Move Q5 to `UNIVERSAL_QUESTIONS` or a shared `COMMON_TAIL_QUESTIONS` list and append it after the type-specific Q2–Q4.

### REDUNDANCY-8: Card Rendering `.map()` → `list-item` Pattern (15 occurrences)
**File:** `frontend/dashboard.html`  
**Lines:** 736–741, 768–775, 802–809, 830–837, 861–868, 889–899, 945–955, 991–998, 1027–1034, 1064–1069, 1100–1107, 1135–1145, 1179–1186, 1216–1226  
**Duplicated code:**
```javascript
items.map((item, i) =>
    React.createElement('div', { key: i, className: 'list-item' },
        React.createElement('span', { className: 'list-item-label' }, item.name),
        React.createElement('span', null,
            Pill({ tone: ..., }, item.status)
        )
    )
)
```
~15 occurrences.  
**Proposed unified component:**
```javascript
function ListItems({ items, labelKey, valueKey, toneFn }) {
    return items.map((item, i) =>
        React.createElement('div', { key: i, className: 'list-item' },
            React.createElement('span', { className: 'list-item-label' }, 
                typeof labelKey === 'function' ? labelKey(item) : item[labelKey]),
            React.createElement('span', null, 
                Pill({ tone: toneFn(item) }, typeof valueKey === 'function' ? valueKey(item) : item[valueKey]))
        )
    );
}
```

### REDUNDANCY-9: CSS `.mode-pill` Duplicated Between Files
**Files:** `frontend/index.html` lines 89–104; `frontend/dashboard.html` lines 82–96  
~15 CSS lines duplicated nearly verbatim.  
**Proposed fix:** Extract shared CSS to a `common.css` file.

---

## 4. Refactoring Roadmap

### Phase 1: Critical Security & Correctness (BLOCKING — do first)

| # | Action | Files | Effort | Depends On |
|---|---|---|---|---|
| 1.1 | Add `max_length` validator to `ChatRequest.answer` | `main.py:602-604` | 5 min | — |
| 1.2 | Add `asyncio.Lock` to rate limiter or convert to per-session lock dict | `main.py:618-648` | 30 min | — |
| 1.3 | Fix `_load_session` to not INSERT on read-only GET endpoints; split into `_load_session_or_create` (POST) and `_load_session` (GET, returns None if missing) | `main.py:335-358` | 45 min | — |
| 1.4 | Move `business_profiles` CREATE TABLE to `init_db()` | `main.py:1192-1198` → `main.py:290-332` | 5 min | — |
| 1.5 | Fix business type string mismatch: align frontend `BUSINESS_TYPES` with backend (remove spaces) | `index.html:347-359` or `main.py:68-73` | 5 min | — |
| 1.6 | Remove dead `StopAsyncIteration` except blocks | `main.py:1022-1023, 1370-1371` | 5 min | — |
| 1.7 | Add missing card renderers for `expenses`, `contacts`, `decisions`, `staff_schedule` OR remove them from `AVAILABLE_CARDS` | `dashboard.html:695-1234` | 2 hrs | — |

### Phase 2: Architectural Refactoring (after Phase 1)

| # | Action | Files | Effort | Depends On |
|---|---|---|---|---|
| 2.1 | Split `main.py` into modules: `routes/survey.py`, `routes/checkin.py`, `routes/dashboard.py`, `db/turso.py`, `llm/spur.py`, `prompts.py`, `models.py` | `main.py` (1496 lines → ~8 files) | 1 day | 1.1-1.6 |
| 2.2 | Create shared `db/turso.py` with `_turso_request()` unified function | `main.py:223-287` | 1 hr | 2.1 |
| 2.3 | Create shared `llm/spur.py` with `_spur_chat_completion()` and `_extract_llm_content()` | `main.py:698-712, 890-905, 1107-1146, 1228-1255` | 2 hrs | 2.1 |
| 2.4 | Extract `CHECKIN_QUESTIONS_BY_TYPE` to module-level constant (currently rebuilt per call) | `main.py:530-576` → module top | 10 min | 2.1 |
| 2.5 | Move Q5 to `UNIVERSAL_QUESTIONS` or a shared constant; deduplicate `QUESTIONS_BY_TYPE` | `main.py:88-155` | 30 min | — |
| 2.6 | Replace `@app.on_event("startup")` with `lifespan` context manager | `main.py:1486-1488` | 15 min | 2.1 |
| 2.7 | Add 5 missing business-type check-in question sets (Landscaping, Auto Repair, Cleaning, Photography, Real Estate, Other) | `main.py:530-566` | 1 hr | 2.4 |
| 2.8 | Migrate check-in conversation state from function-attribute memory to Turso (new `checkin_sessions` table) | `main.py:1302-1383` | 3 hrs | 2.2 |

### Phase 3: Frontend Hardening (after Phase 2)

| # | Action | Files | Effort | Depends On |
|---|---|---|---|---|
| 3.1 | Add `AbortController` with timeout to all `fetch()` calls | `index.html:644, 377, 407, 774, 780`; `dashboard.html:500-504` | 1 hr | — |
| 3.2 | Add SSE reconnection logic with exponential backoff | `index.html:663-698` | 2 hrs | 3.1 |
| 3.3 | Replace empty `catch {}` with logged error handler | `index.html:696` | 5 min | — |
| 3.4 | Replace `innerHTML` in `showComplete()`/`showCheckinComplete()` with `createElement` calls | `index.html:581-588, 601-606` | 30 min | — |
| 3.5 | Extract shared CSS to `common.css` | `index.html:11-309`, `dashboard.html:10-449` | 1 hr | — |
| 3.6 | Create `ListItems` shared component to deduplicate 15 card `.map()` patterns | `dashboard.html:695-1234` | 2 hrs | — |

### Phase 4: Testing & Infrastructure (after Phase 3)

| # | Action | Files | Effort | Depends On |
|---|---|---|---|---|
| 4.1 | Add `pytest` + `httpx.AsyncClient` test harness for API endpoints | new `tests/` dir | 3 hrs | 2.1 |
| 4.2 | Write tests for: rate limiter concurrency, session CRUD, business type detection, SSE streaming, check-in flow | `tests/` | 1 day | 4.1 |
| 4.3 | Add `pytest-cov` and target 60% coverage on backend | `requirements.txt` | 2 hrs | 4.2 |
| 4.4 | Add `requirements.lock` or `uv.lock` for reproducible builds | new file | 15 min | — |
| 4.5 | Add Docker `HEALTHCHECK` using `python -c` with early-exit on missing env vars (fail-fast) | `Dockerfile` | 30 min | — |
| 4.6 | Add GitHub Actions CI: lint (ruff), type check (mypy), test, Docker build | new `.github/workflows/ci.yml` | 2 hrs | 4.1-4.3 |

### Phase 5: Production Readiness (after Phase 4)

| # | Action | Files | Effort | Depends On |
|---|---|---|---|---|
| 5.1 | Implement auth layer (session token validation, rate limit per IP+session) | `main.py` | 1 day | 1.1-1.3 |
| 5.2 | Replace hardcoded mock card data with real API integrations | `dashboard.html:695-1234` | 1 week+ | — |
| 5.3 | Add connection pooling for Turso HTTP client (reuse `httpx.AsyncClient`) | `main.py:233, 253` | 2 hrs | 2.2 |
| 5.4 | Add structured logging (JSON) with request IDs | `main.py:18` | 3 hrs | 2.1 |
| 5.5 | Add monitoring/metrics endpoint (Prometheus format) | `main.py` | 3 hrs | 2.1 |

---

## 5. Additional Findings (Non-Critical)

### 5.1 No Connection Pooling
**File:** `backend/main.py`, lines 233, 253, 698, 890, 1107, 1228  
Every DB and LLM call creates a new `httpx.AsyncClient` instance. For a survey flow that makes 3–5 DB calls + 1–2 LLM calls per user message, this means 4–7 TCP connections established and torn down per request. Should use a shared `httpx.AsyncClient` with connection pooling.

### 5.2 Inline Imports
**File:** `backend/main.py`, lines 727–728, 1157, 1265  
```python
import smtplib                          # line 727 — mid-file
from email.mime.text import MIMEText    # line 728
from email.mime.multipart import MIMEMultipart  # line 729
...
import re as _re                        # line 1157 — re-imported inside function
...
import re as _re                        # line 1265 — re-imported inside function
```
`re` is already imported at module top (line 12). The `import re as _re` inside functions is unnecessary. SMTP imports should be at module top.

### 5.3 `get_latest_checkin` Endpoint Has No `session_id` Validation
**File:** `backend/main.py`, lines 1391–1397  
```python
@app.get("/api/survey/checkin/latest")
async def get_latest_checkin(session_id: str):  # ← no validation, unlike ChatRequest
```
This GET endpoint accepts `session_id` as a query parameter with no validation (unlike `ChatRequest` which has `field_validator`). Same issue on lines 1401, 1416, 1422, 1431, 1437, 1449, 1460.

### 5.4 `_has_completed_onboarding` Makes Redundant DB Calls
**File:** `backend/main.py`, lines 420–431  
```python
async def _has_completed_onboarding(session_id: str) -> bool:
    rows = await _turso_query(                           # call 1: SELECT q_index
        "SELECT q_index FROM survey_sessions WHERE session_id=?",
        [{"type": "text", "value": session_id}]
    )
    if not rows:
        return False
    sess = await _load_session(session_id)              # call 2: SELECT * (includes q_index again)
    active_questions = _get_questions_for_type(...)
    return int(rows[0].get("q_index", 0)) >= len(active_questions)
```
Two DB round-trips for data that can be fetched in one. `_load_session` already returns `q_index`; the first `_turso_query` is redundant.

### 5.5 Check-in Lock Ordering Issue
**File:** `backend/main.py`, lines 1309–1342, 1374–1375  
The lock is released before LLM streaming (line 1342), then re-acquired after streaming to append the AI response (line 1374). If two check-in messages arrive for the same session, the second request reads `conv["step"]` before the first's AI response is appended. This can cause the check-in to advance past step 5 without saving.

### 5.6 Empty `catch {}` in Frontend
**File:** `frontend/index.html`, line 696  
```javascript
try {
    const data = JSON.parse(dataStr);
    ...
} catch {}   // ← silently swallows all JSON parse errors
```
If the SSE stream sends malformed JSON, the error is silently ignored. Should at minimum log to console.

### 5.7 `totalQuestions` Hardcoded to 13 in Frontend
**File:** `frontend/index.html`, lines 319, 340  
```html
<div class="header-sub" id="header-sub">Question 1 of 13</div>  <!-- line 319 -->
```
```javascript
let totalQuestions = 13;  // line 340 — hardcoded, should come from API
```
The total is fetched from the API (line 409: `totalQuestions = state.total_questions || 13`), but the initial HTML display says "13" and the fallback is also 13. Since business-type-aware question counts vary (13 for most, but the structure allows for different counts), this is fragile.

### 5.8 No `.env.example` File
No `.env.example` or `.env.template` file exists. Required environment variables must be discovered by reading `main.py` lines 51–63. New developers will struggle to set up the project.

### 5.9 Dockerfile `.dockerignore` Excludes `*.md`
**File:** `.dockerignore`, line 31  
```
*.md
```
This excludes all markdown files from the Docker build context, including any README or documentation that might be needed. Not a bug, but worth noting — if documentation is ever needed at runtime (e.g., API docs), this will silently exclude it.

---

## 6. Score Justification

### Static / Style: C+
**Strengths:** Consistent naming (underscore convention for internal functions), type hints on most functions, proper use of `from __future__ import annotations`, Pydantic models for request validation.  
**Weaknesses:** Inline imports (lines 727–729, 1157, 1265), dead code blocks (StopAsyncIteration), duplicate function calls (lines 778/819), 44 functions in a single file with no module separation, constants mixed with logic.

### Structural / Routing: D+
**Strengths:** Clean API design (RESTful-ish), good endpoint naming, SSE streaming is well-architected, card selection engine is a good separation of the LLM concern.  
**Weaknesses:** God-file (1496 lines, 44 functions, 13 endpoints), state management split across 3 mechanisms (Turso DB, in-memory dict with function attributes, module-level dict for rate limiting), no service layer, no repository pattern, DDL inside save functions, check-in questions rebuilt per call, `init_db` doesn't create all tables.

### Security / Performance: D
**Strengths:** Security headers middleware, CORS configured with explicit origins, session_id input validation (alphanumeric + length), non-root Docker user, graceful shutdown.  
**Weaknesses:** No authentication on any endpoint, unbounded `answer` input, rate limiter race condition, data pollution via GET endpoints, no connection pooling (6+ httpx clients per request chain), session_id in URL query params (dashboard), no fail-fast on missing env vars, `_rate_limit_store` unbounded growth mitigation is weak (only triggers at 1000 sessions).

### Functional / Testing: F
**Strengths:** Good error handling in dashboard (try/catch per card, skeleton loading, error states with retry), ARIA labels on most interactive elements, responsive CSS breakpoints.  
**Weaknesses:** Zero tests, 4/20 card types can't render, business type mismatch frontend/backend, check-in state lost on restart, StopAsyncIteration dead code means silent failures, all dashboard data is hardcoded mock, no SSE reconnection, no fetch timeout, empty catch block, check-in only supports 5/11 business types.

---

## Appendix: File Inventory

| File | Lines | Size | Role |
|---|---|---|---|
| `backend/main.py` | 1496 | 71 KB | FastAPI app — all backend logic |
| `frontend/index.html` | 816 | 28 KB | Survey + check-in chat UI |
| `frontend/dashboard.html` | 1459 | 63 KB | Custom React dashboard (16 card types) |
| `Dockerfile` | 21 | 668 B | Python 3.11.16-slim container |
| `.dockerignore` | 36 | 370 B | Build context exclusions |
| `backend/requirements.txt` | 4 | 78 B | Pinned dependencies (4 packages) |
| **Total** | **3832** | **~163 KB** | |

**No test files. No lock file. No .env.example. No CI configuration. No README in build context.**
