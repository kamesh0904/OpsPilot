# OpsPilot

> The ops person your startup can't afford to hire yet.

OpsPilot is a multi-agent AI system that monitors Linear, Notion, GitHub, and Slack simultaneously — finds what's falling through the cracks — and delivers structured, prioritized briefings to founders via Slack every morning, every evening, and on demand.

---

## Table of Contents

1. [What It Does](#what-it-does)
2. [Tech Stack](#tech-stack)
3. [Architecture](#architecture)
4. [Project Structure](#project-structure)
5. [Database Schema](#database-schema)
6. [The 4 Agents](#the-4-agents)
7. [Integration Clients](#integration-clients)
8. [Prompt Engineering](#prompt-engineering)
9. [On-Demand Query Optimization](#on-demand-query-optimization)
10. [Error Handling](#error-handling)
11. [Rate Limiting](#rate-limiting)
12. [API Reference](#api-reference)
13. [Scheduler](#scheduler)
14. [Deployment](#deployment)
15. [Environment Variables](#environment-variables)
16. [Build Order](#build-order)

---

## What It Does

### Daily Workflows

| Time | Trigger | Output |
|---|---|---|
| 9:00 AM daily | Scheduled | Morning briefing: urgent items, blockers, one recommendation |
| 6:00 PM daily | Scheduled | Evening pulse: what moved, what didn't, what's at risk tomorrow |
| Sunday 6:00 PM | Scheduled | Weekly digest: velocity trends, recurring patterns |
| Any time | `/opspilot <question>` in Slack | On-demand answer in < 15 seconds |

### Example Morning Briefing (Slack)

```
Good morning. Here's what needs your attention today:

🔴 URGENT
→ PR #47 (auth refactor) has been open 5 days
   Last reviewer: nobody assigned
   Blocking: mobile login feature

→ "Q4 Roadmap" Notion doc hasn't been updated in 47 days
   Still being referenced in 3 active tickets

🟡 WATCH
→ 3 Linear tickets moved to "In Progress" 8+ days ago
   No updates, no comments

✅ SHIPPED THIS WEEK
→ 4 PRs merged
→ 2 tickets closed
→ API response time improved (from commit messages)

💡 One thing I'd suggest: assign PR #47 before standup.
```

---

## Tech Stack

| Layer | Technology | Why |
|---|---|---|
| **Orchestration** | LangGraph | Stateful multi-agent graph with cross-run memory |
| **AI Model** | Gemini 1.5 Pro | 1M token context — reads all docs and tickets at once |
| **Agent Framework** | LangChain | Tool calling, structured outputs, model abstraction |
| **Backend** | FastAPI | Async API + scheduler lifespan management |
| **Database / State** | Supabase | Agent state persistence, flags, audit log |
| **Deployment** | GCP Cloud Run + Docker | Containerized, scalable, single-worker for Phase 1 |

---

## Architecture

### The 4-Agent LangGraph Pipeline

```
[Collector] → [Analyst] → [Decision] → [Action]
     ↓              ↓           ↓            ↓
  Raw data      Patterns    Priority     Slack msg
  from APIs     + flags     items        + tickets
                                         + logs
```

Each agent is a LangGraph **node**. The shared `OpsState` TypedDict flows through all of them. Supabase provides cross-run memory so the Decision agent remembers what it flagged yesterday.

### Two Distinct Execution Paths

Scheduled briefings and on-demand queries are **architecturally separate**:

```
Scheduled Run:                    On-Demand Query:
Collector (all 4 sources)         QueryRouter (classify intent)
    ↓                                  ↓
Analyst (full analysis)           Targeted Collector (relevant sources only)
    ↓                                  ↓
Decision (priority + dedup)       QuickAnalyst (focused prompt)
    ↓                                  ↓
Action (full Slack briefing)      DirectResponder (conversational reply)
```

Running the full pipeline for every `/opspilot` query would be too slow (30–60s) and wasteful. The on-demand path targets < 15 seconds end-to-end.

### LangGraph State Schema

```python
class OpsState(TypedDict):
    # Identity
    workspace_id: str
    run_type: str           # "morning" | "evening" | "weekly" | "ondemand"
    query: Optional[str]    # populated for on-demand queries only

    # Collected raw data
    linear_data: list[dict]
    notion_data: list[dict]
    github_data: list[dict]
    slack_data: list[dict]

    # Processed outputs
    analysis: dict              # Analyst output: patterns, anomalies, cross-tool findings
    priority_items: list[dict]  # Decision output: ranked, filtered, de-duped
    briefing: str               # Final formatted Slack message

    # Cross-run memory (loaded from Supabase at pipeline start)
    previous_flags: list[dict]
    config: dict                # Workspace config (thresholds, timing, repos, etc.)

    # Audit
    actions_taken: list[str]
    errors: list[str]           # Non-fatal errors accumulated during run
```

---

## Project Structure

```
opspilot/
├── app/
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── collector.py        # Collector Agent — fetches + normalizes all data
│   │   ├── analyst.py          # Analyst Agent — Gemini 1.5 Pro long-context analysis
│   │   ├── decision.py         # Decision Agent — stateful priority + deduplication
│   │   └── action.py           # Action Agent — Slack, Linear writes, logging
│   │
│   ├── graph/
│   │   ├── __init__.py
│   │   ├── state.py            # OpsState TypedDict + reducers
│   │   └── pipeline.py         # StateGraph construction + compilation
│   │
│   ├── integrations/
│   │   ├── __init__.py
│   │   ├── linear.py           # Linear GraphQL client (httpx)
│   │   ├── notion.py           # Notion REST client (notion-client SDK)
│   │   ├── github.py           # GitHub REST client (PyGithub)
│   │   └── slack.py            # Slack Bolt — read channels + write Block Kit
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   ├── errors.py           # OpsPilotError hierarchy
│   │   ├── rate_limiter.py     # Async token bucket per integration
│   │   └── retry.py            # Exponential backoff decorator
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── main.py             # FastAPI app + lifespan (APScheduler)
│   │   └── routes/
│   │       ├── briefing.py     # POST /briefing/trigger
│   │       ├── query.py        # POST /query
│   │       ├── config.py       # GET|POST /config/{workspace_id}
│   │       └── slack_events.py # POST /slack/events (slash commands)
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── client.py           # Supabase singleton + typed query helpers
│   │   └── models.py           # Pydantic models for all DB tables
│   │
│   ├── scheduler/
│   │   ├── __init__.py
│   │   └── jobs.py             # APScheduler: 9am, 6pm, Sunday jobs
│   │
│   └── config.py               # pydantic-settings — all env vars
│
├── tests/
│   ├── test_collector.py
│   ├── test_analyst.py
│   ├── test_decision.py
│   └── test_action.py
│
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```

---

## Database Schema

Five Supabase tables power OpsPilot's memory and audit trail.

### `workspaces`
Per-workspace configuration and credentials.
```sql
id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
name            text NOT NULL,
linear_api_key  text,
notion_api_key  text,
github_token    text,
slack_bot_token text,
slack_channel_id text,
config          jsonb,          -- thresholds, timing, repo list, etc.
created_at      timestamptz DEFAULT now()
```

### `agent_runs`
Audit log of every pipeline execution.
```sql
id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
workspace_id    uuid REFERENCES workspaces(id),
run_type        text,           -- "morning" | "evening" | "weekly" | "ondemand"
started_at      timestamptz,
completed_at    timestamptz,
status          text,           -- "success" | "partial" | "failed"
summary         text
```

### `flags`
Items the Decision agent has surfaced. Used for cross-run deduplication.
```sql
id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
workspace_id    uuid REFERENCES workspaces(id),
run_id          uuid REFERENCES agent_runs(id),
source          text,           -- "linear" | "notion" | "github" | "slack"
item_id         text,
item_title      text,
flag_type       text,           -- "stale_pr" | "stale_ticket" | "unassigned" | etc.
flagged_at      timestamptz DEFAULT now(),
resolved_at     timestamptz
```

### `actions_taken`
Every action the Action agent executed.
```sql
id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
workspace_id    uuid REFERENCES workspaces(id),
run_id          uuid REFERENCES agent_runs(id),
action_type     text,           -- "slack_message" | "linear_ticket" | "notion_draft"
target          text,
payload         jsonb,
executed_at     timestamptz DEFAULT now(),
success         bool
```

### `patterns`
Analyst's learned patterns over time (e.g. "backend tickets stall every Friday").
```sql
id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
workspace_id     uuid REFERENCES workspaces(id),
pattern_type     text,
description      text,
first_seen       timestamptz,
last_seen        timestamptz,
occurrence_count int DEFAULT 1,
metadata         jsonb
```

### `integration_cache`
GitHub ETag cache — prevents redundant API calls.
```sql
id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
workspace_id    uuid REFERENCES workspaces(id),
source          text,
resource_key    text,
etag            text,
cached_at       timestamptz DEFAULT now()
```

---

## The 4 Agents

### Collector Agent — `app/agents/collector.py`
**Role**: Fetch and normalize data. No AI reasoning.

- Calls all 4 integration clients in parallel via `asyncio.gather`
- Adds computed metadata: item age in days, staleness flag, assigned/unassigned status
- If one source fails, marks it as `unavailable` in state and continues with the rest
- Returns all raw data structured into `OpsState`

### Analyst Agent — `app/agents/analyst.py`
**Role**: Find what humans miss using Gemini 1.5 Pro's long context.

- Builds a single normalized prompt from all collected data (see [Prompt Engineering](#prompt-engineering))
- Uses `ChatGoogleGenerativeAI(model="gemini-1.5-pro")` via LangChain
- Returns structured JSON validated by Pydantic
- If JSON is malformed → re-prompts once with schema reminder
- If Gemini fails → falls back to rule-based summary

### Decision Agent — `app/agents/decision.py`
**Role**: Filter noise. Prioritize. Remember.

- Loads `previous_flags` from Supabase for this workspace
- Scores each analyst finding: urgency × impact × staleness
- Skips items flagged in the last 24h (unless they've escalated)
- Returns ranked `priority_items` list
- Persists new flags to Supabase `flags` table for next run's memory

### Action Agent — `app/agents/action.py`
**Role**: Make things happen.

- Formats Slack Block Kit briefing from `priority_items`
- Posts to configured Slack channel
- Creates Linear tickets for auto-actionable items (unreviewed PRs > 5 days, recurring Slack questions with no ticket)
- Logs all actions to Supabase `actions_taken`
- Adapts output format per `run_type`: morning / evening / weekly / ondemand
- Appends error digest footer if `state["errors"]` is non-empty

---

## Integration Clients

### Linear — `app/integrations/linear.py`
- GraphQL via `httpx` (no wrapper lib — full query control)
- Fetches tickets by team/project, filtered by status + last updated
- Paginates with cursors for large workspaces
- Returns normalized `LinearTicket` Pydantic objects

### Notion — `app/integrations/notion.py`
- Official `notion-client` Python SDK
- Lists pages in configured database(s)
- Captures: title, last edited time, last editor, content preview
- Rate-limited to 2.5 req/sec (under the 3/sec hard limit)

### GitHub — `app/integrations/github.py`
- `PyGithub` library for clean PR + commit interface
- Fetches open PRs: age, reviewer status, linked branch, commit recency
- ETag conditional requests to avoid redundant quota usage
- Read-only — no writes to GitHub in Phase 1

### Slack — `app/integrations/slack.py`
- `slack-bolt` async client
- **Read**: last N messages from configured channels
- **Write**: structured Block Kit messages with sections, dividers, action buttons
- Rate-limited to 0.9 req/sec (under the 1/sec Tier 3 limit)

---

## Prompt Engineering

The Analyst agent's prompt is the most critical engineering surface in OpsPilot. Every prompt is assembled dynamically from 5 sections.

### Section 1 — Role + Context
```
You are OpsPilot's Analyst — an expert ops intelligence engine for a
{team_size}-person startup. You are reading across Linear, Notion, GitHub,
and Slack simultaneously. Today is {date}. This is a {run_type} analysis.
```

### Section 2 — Normalized Data (NOT raw JSON)
Data is transformed before entering the prompt. This uses ~60% fewer tokens while preserving all signal:
```
=== LINEAR TICKETS ===
[TICKET-123] "Auth refactor" | status: In Progress | assigned: nobody
  → created: 8 days ago | last_updated: 5 days ago | comments: 0
  → labels: backend, priority-high

=== GITHUB PULL REQUESTS ===
[PR #47] "Auth refactor" | status: Open | reviewers: none assigned
  → opened: 5 days ago | commits: 3 | linked ticket: TICKET-123
  → last commit: 3 days ago

=== NOTION PAGES ===
["Q4 Roadmap"] | last edited: 47 days ago | editor: vivek@
  → referenced in: TICKET-89, TICKET-102, TICKET-118

=== SLACK SIGNALS ===
[#engineering] "anyone know where the payment retry logic lives?" (asked 3x this week, no answer)
```

### Section 3 — Configuration Context
```
Workspace thresholds (founder-configured):
- Stale ticket: no update in {stale_ticket_days} days
- Stale PR: no review in {stale_pr_days} days
- Stale Notion doc: no edit in {stale_doc_days} days
```

### Section 4 — Deduplication Context
```
Already flagged in the last 24 hours (do NOT re-surface these unless escalated):
- PR #47: open with no reviewers [flagged 18 hours ago]
- "Q4 Roadmap" Notion doc: stale [flagged 2 days ago, still unresolved]
```

### Section 5 — Output Schema (strict JSON)
```json
{
  "patterns": [
    {
      "type": "string",
      "description": "string",
      "severity": "low|medium|high",
      "sources": ["string"],
      "affected_items": ["string"]
    }
  ],
  "priority_flags": [
    {
      "title": "string",
      "reason": "string",
      "urgency": "watch|urgent|critical",
      "source": "string",
      "item_id": "string",
      "suggested_action": "string"
    }
  ],
  "shipped_this_period": ["string"],
  "velocity_note": "string",
  "one_recommendation": "string"
}
```

Output is validated with Pydantic. If validation fails → re-prompt once. If it fails again → fall back to rule-based summary.

### Prompt Variants by Run Type

| Run Type | Focus | Output Shape |
|---|---|---|
| `morning` | What needs action TODAY | URGENT + WATCH + one recommendation |
| `evening` | What moved, what's at risk | SHIPPED + AT RISK |
| `weekly` | Trends, recurring blockers | Pattern analysis + velocity note |
| `ondemand` | Specific question only | Direct answer + bullet evidence |

### Token Budget
- Normalize all data before sending (structured text, not JSON blobs)
- Cap Slack history at 50 messages per channel
- Cap Notion page content at 2,000 chars per page
- If total input exceeds 200k tokens → prioritize newest and highest-activity items first

---

## On-Demand Query Optimization

Running the full pipeline for every `/opspilot` query is wrong. The on-demand path is a separate, leaner architecture targeting **< 15 seconds** end-to-end.

### Step 1: QueryRouter (Intent Classification)
Before fetching a single byte of data, a lightweight Gemini Flash call classifies the question:

```python
class QueryIntent(BaseModel):
    sources_needed: list[str]  # e.g. ["linear", "github"]
    entities: list[str]        # e.g. ["payment feature", "PR #47"]
    query_type: str            # "blocker" | "status" | "ownership" | "timeline"
    time_range: str            # "today" | "this_week" | "all_time"
```

**Example:**
- Query: `"what's blocking the payment feature?"`
- Intent: `sources=["linear", "github"], entities=["payment"], type="blocker"`
- Result: Only Linear + GitHub are queried, filtered to "payment" items — Notion and Slack are skipped entirely

### Step 2: Targeted Collection
Instead of a full sweep, filtered API calls are made:
- Linear: issues where title/label contains entity keywords
- GitHub: PRs where title/branch contains entity keywords
- Collection time: **~3 seconds** vs ~15 seconds for full sweep

### Step 3: QuickAnalyst Prompt
```
Answer this specific question: "{question}"

Using only the following relevant data:
{targeted_data}

Be direct. Identify the specific blocker/status/owner.
Format: 1–2 sentence answer, then bullet supporting evidence.
```

### Slack UX — Handling the 3-Second Timeout
Slack slash commands time out if no response is sent within 3 seconds. OpsPilot handles this with a two-step reply:

```
User:       /opspilot what's blocking the payment feature?

OpsPilot:   ⏳ Looking into it...          ← sent immediately (< 1s)

[~10 seconds later — message is updated]

OpsPilot:   🔍 Payment feature blockers:
            → PR #52 "Stripe webhook handler" — open 6 days, no reviewer assigned
            → TICKET-89 "Payment retry logic" — In Progress, assigned to @sarah,
               no update in 4 days
            Suggestion: Assign PR #52 to a reviewer now.
```

---

## Error Handling

A single agent failure must never crash the pipeline silently. All failures are caught, categorized, logged to `state["errors"]`, and gracefully degraded.

### Error Taxonomy

| Layer | Error Type | Strategy |
|---|---|---|
| Integration client | API timeout / 5xx | Retry with exponential backoff (max 3×), then skip source |
| Integration client | 401 Unauthorized | Immediately fail + Slack alert to founder (bad token) |
| Integration client | 404 Not Found | Skip item, log warning, continue |
| Collector Agent | One source fails | Mark source `unavailable` in state, continue with remaining |
| Analyst Agent | Gemini API error | Retry once, then fall back to rule-based summary |
| Analyst Agent | Malformed JSON output | Re-prompt once with schema, then use raw text |
| Decision Agent | Supabase query fails | Continue without previous flags, log error |
| Action Agent | Slack post fails | Retry once, save briefing to Supabase for manual retrieval |
| Action Agent | Linear ticket creation fails | Log failure, add to retry queue for next run |
| Full Pipeline | Unhandled exception | Set `run_status = "failed"` in Supabase, send fallback Slack alert |

### Exception Hierarchy — `app/core/errors.py`

```python
class OpsPilotError(Exception):
    def __init__(self, message: str, source: str, recoverable: bool = True):
        self.source = source          # "linear" | "gemini" | "slack" | etc.
        self.recoverable = recoverable
        super().__init__(message)

class IntegrationError(OpsPilotError): pass   # API client failure
class AnalysisError(OpsPilotError): pass       # Gemini failure
class ActionError(OpsPilotError): pass         # Output delivery failure
```

Each agent node catches `OpsPilotError`. Recoverable errors are appended to `state["errors"]` and execution continues. Non-recoverable errors abort the run. The Action agent always checks `state["errors"]` and appends a digest to the briefing footer.

### Golden Rule
> A partial briefing delivered is always better than a full briefing lost.
> If 2 of 4 sources fail, OpsPilot sends a briefing from the 2 that succeeded with a note: *"GitHub and Notion data unavailable this run."*

---

## Rate Limiting

### Limits by Integration

| Integration | Limit | Strategy |
|---|---|---|
| **Linear** | 1,500 req/hour (GraphQL) | Batch queries, cursor pagination, cache between runs |
| **Notion** | 3 req/sec | `asyncio.Semaphore(3)` throttle + retry on 429 |
| **GitHub** | 5,000 req/hour (authenticated) | ETag conditional requests — skip unchanged data |
| **Slack** | 1 req/sec (Tier 3 Web API) | Sequential writes with 1.1s gap |
| **Gemini 1.5 Pro** | Tier-dependent | Single call per run — never chunk across multiple calls |

### Rate Limiter — `app/core/rate_limiter.py`

```python
class RateLimiter:
    """Async token bucket limiter. One singleton instance per integration."""
    def __init__(self, calls_per_second: float):
        self.period = 1.0 / calls_per_second
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            wait = self._last_call + self.period - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

notion_limiter = RateLimiter(calls_per_second=2.5)
slack_limiter  = RateLimiter(calls_per_second=0.9)
```

### Exponential Backoff — `app/core/retry.py`

```python
async def with_retry(fn, max_attempts=3, base_delay=1.0):
    for attempt in range(max_attempts):
        try:
            return await fn()
        except RateLimitError:
            if attempt == max_attempts - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))  # 1s → 2s → 4s
```

### GitHub ETag Caching
ETag headers from GitHub responses are stored in `integration_cache`. On the next run, requests include `If-None-Match: <etag>` — GitHub returns `304 Not Modified` at zero quota cost if data hasn't changed. Reduces GitHub API usage by ~70% for stable repos.

---

## API Reference

### `POST /briefing/trigger`
Manually triggers the full LangGraph pipeline.
```json
Request:  { "workspace_id": "uuid", "run_type": "morning|evening|weekly" }
Response: { "run_id": "uuid", "status": "started", "briefing_preview": "..." }
```

### `POST /query`
Triggers the on-demand query path.
```json
Request:  { "workspace_id": "uuid", "question": "what's blocking payments?" }
Response: { "answer": "...", "sources_used": ["linear", "github"], "run_id": "uuid" }
```

### `GET /config/{workspace_id}`
Returns current workspace configuration.

### `POST /config/{workspace_id}`
Updates workspace configuration (thresholds, timing, channels, repos).

### `POST /slack/events`
Receives Slack slash command events (`/opspilot <question>`).
- Verifies Slack signing secret
- Immediately acknowledges (prevents 3s timeout)
- Triggers on-demand query pipeline in background
- Updates Slack message with answer when ready

---

## Scheduler

Implemented with `APScheduler AsyncIOScheduler` inside FastAPI's lifespan context:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(morning_briefing, "cron", hour=9,  minute=0)
    scheduler.add_job(evening_pulse,    "cron", hour=18, minute=0)
    scheduler.add_job(weekly_digest,    "cron", day_of_week="sun", hour=18)
    scheduler.start()
    yield
    scheduler.shutdown()
```

Each job loads all active workspaces from Supabase and runs the pipeline for each one.

> **Cloud Run Note**: APScheduler in-process is acceptable for Phase 1 with a single worker and `min-instances=1` set to prevent cold starts from missing scheduled jobs.

---

## Deployment

### Dockerfile
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

### docker-compose.yml (local dev)
```yaml
version: "3.9"
services:
  opspilot:
    build: .
    ports:
      - "8080:8080"
    env_file:
      - .env
```

### GCP Cloud Run
```bash
gcloud builds submit --tag gcr.io/PROJECT_ID/opspilot
gcloud run deploy opspilot \
  --image gcr.io/PROJECT_ID/opspilot \
  --platform managed \
  --region us-central1 \
  --min-instances 1 \
  --memory 512Mi \
  --set-env-vars-from-file .env
```

---

## Environment Variables

Copy `.env.example` to `.env` and fill in all values:

```bash
# AI
GOOGLE_API_KEY=

# Supabase
SUPABASE_URL=
SUPABASE_SERVICE_KEY=

# Integrations
LINEAR_API_KEY=
NOTION_API_KEY=
GITHUB_TOKEN=
SLACK_BOT_TOKEN=
SLACK_SIGNING_SECRET=

# Config
BRIEFING_CHANNEL_ID=        # Slack channel ID for briefings
MORNING_HOUR=9              # 24h format
EVENING_HOUR=18
STALE_TICKET_DAYS=5
STALE_PR_DAYS=3
STALE_DOC_DAYS=30
```

---

## Build Order

Files are built in strict dependency order — each step builds on the last.

| Step | Files | Why First |
|---|---|---|
| 1 | `requirements.txt`, `config.py`, `.env.example` | Everything depends on config |
| 2 | `db/client.py`, `db/models.py` + SQL schema | Agents need DB access |
| 3 | `core/errors.py`, `core/rate_limiter.py`, `core/retry.py` | Integrations depend on these |
| 4 | `integrations/linear.py`, `notion.py`, `github.py`, `slack.py` | Collector depends on these |
| 5 | `graph/state.py`, `graph/pipeline.py` | Agents need state + graph defined |
| 6 | `agents/collector.py` | First node in the graph |
| 7 | `agents/analyst.py` | Depends on collector output |
| 8 | `agents/decision.py` | Depends on analyst output + DB flags |
| 9 | `agents/action.py` | Last node — depends on all prior agents |
| 10 | `api/main.py` + all routes | Wraps the graph in HTTP |
| 11 | `scheduler/jobs.py` | Plugs into API lifespan |
| 12 | `Dockerfile`, `docker-compose.yml` | Packages the whole system |
| 13 | `tests/` | Tests against built system |
