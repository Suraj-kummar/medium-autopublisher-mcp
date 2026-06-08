# Medium Auto-Publisher MCP Server

An autonomous, production-grade MCP server that researches, writes, illustrates, and saves Medium articles as local drafts — completely unattended. Schedule a topic, walk away, and find a polished article with header image waiting in your `drafts/` folder.

---

## What It Does

When triggered (by schedule or immediately), the server runs a **5-stage autonomous pipeline**:

| Stage | Module | Description |
|-------|--------|-------------|
| 1 · Research | `pipeline/researcher.py` | Claude decomposes topic → 3 Tavily searches in parallel → deduplicated sources |
| 2 · Write | `pipeline/writer.py` | Claude `claude-sonnet-4-6` writes 1200–1800 word Medium article + title/subtitle/tags/image prompt |
| 2.5 · Validate | `pipeline/validator.py` | 6-check quality gate — blocks publish if score < 0.70 |
| 3 · Illustrate | `pipeline/illustrator.py` | DALL-E 3 generates 1792×1024 header image (b64_json) |
| 4+5 · Save | `pipeline/publisher.py` | Article markdown + header PNG saved to `drafts/{slug}/` |

---

## MCP Tools

| Tool | Description |
|------|-------------|
| `schedule_article` | Schedule a future publish (idempotent — duplicate topic+time returns existing job) |
| `publish_now` | Fire pipeline immediately; returns job_id to poll |
| `list_scheduled` | List all pending/running jobs |
| `cancel_job` | Cancel a pending job |
| `get_status` | Poll job status, current stage, URL, error, and duration |
| `preview_article` | Research + write only — zero image/publish API credits |
| `list_drafts` | Browse all saved draft articles in `drafts/` |
| `read_draft` | Read a specific draft's full markdown content |
| `generate_calendar` | AI-powered content calendar — generates topic ideas with tags, dates, and rationale |
| `batch_schedule` | Schedule multiple articles at once from a calendar or manual list |
 
---

## Project Structure

```
medium-autopublisher-mcp/
├── server.py                # MCP entry point, all 10 tools + dual transport
├── config.py                # All constants + Settings (pydantic-settings)
├── Dockerfile               # Cloud deployment container
├── docker-compose.yml       # One-command cloud deployment
├── pipeline/
│   ├── orchestrator.py      # Stage runner with per-stage error handling
│   ├── researcher.py        # Tavily multi-query research (asyncio.gather)
│   ├── writer.py            # Claude article generation
│   ├── illustrator.py       # DALL-E 3 image generation
│   ├── publisher.py         # Local draft saver (markdown + PNG)
│   ├── validator.py         # 6-check quality gate
│   └── calendar.py          # AI content calendar generator
├── scheduler/
│   ├── job_manager.py       # APScheduler + SQLite CRUD
│   └── fingerprint.py       # SHA-256 job dedup
├── models/
│   ├── schemas.py           # Pydantic v2 input/output models
│   └── errors.py            # Typed exception hierarchy
├── tests/                   # Full pytest suite (respx mocks — zero API spend)
├── .env.example             # Secret template
├── requirements.txt
└── pyproject.toml           # pytest config
```

---

## Setup

### 1. Clone and enter the project

```bash
git clone <your-repo>
cd medium-autopublisher-mcp
```

### 2. Create a virtual environment

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure secrets

```bash
cp .env.example .env
```

Edit `.env` and fill in all values:

| Variable | Where to get it |
|----------|----------------|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) → API Keys |
| `OPENAI_API_KEY` | [platform.openai.com](https://platform.openai.com) → API Keys |
| `TAVILY_API_KEY` | [tavily.com](https://tavily.com) — free tier: 1000 req/month |

### 5. Run the server

```bash
# Local (Claude Desktop — stdio transport)
python server.py

# Cloud (HTTP — SSE transport)
python server.py --transport sse --port 8080
```

---

## Claude Desktop Integration

Copy the contents of `claude_desktop_config.json` into your Claude Desktop config file:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

Update the absolute paths to match your installation, then restart Claude Desktop.

---

## Usage Examples

### Generate a content calendar

```
generate_calendar(
  niche="AI in software engineering",
  num_articles=4,
  cadence="weekly",
  style_hint="technical"
)
```

Returns 4 topic ideas with tags, dates, style hints, and rationale.

### Batch schedule all articles

```
batch_schedule(
  articles=[
    {
      "topic": "Why RAG Is Replacing Fine-Tuning",
      "publish_at": "2026-05-05T09:00:00+05:30",
      "tags": ["AI", "RAG", "LLM", "Engineering", "Tech"],
      "style_hint": "technical"
    },
    {
      "topic": "The Hidden Cost of AI Hallucinations",
      "publish_at": "2026-05-12T09:00:00+05:30",
      "tags": ["AI", "Production", "Reliability", "MLOps", "Tech"],
      "style_hint": "case-study"
    }
  ]
)
```

### Schedule a single article

```
schedule_article(
  topic="How AI agents are replacing SaaS workflows in 2026",
  publish_at="2026-07-01T09:00:00+05:30",
  tags=["AI", "SaaS", "Automation", "Productivity", "Future"],
  style_hint="technical"
)
```

### Publish immediately

```
publish_now(
  topic="The real cost of technical debt in startups",
  tags=["Engineering", "Startups", "TechDebt", "Leadership", "Software"],
  style_hint="opinion"
)
```

Returns immediately. Poll with `get_status(job_id)`.

### Preview before spending credits

```
preview_article(
  topic="Why PostgreSQL is eating the database market",
  style_hint="technical"
)
```

Returns full article markdown + quality score. **No DALL-E credits spent.**

### Browse saved drafts

```
list_drafts()     → shows all drafts with metadata
read_draft(slug="why-rag-is-replacing-fine-tuning")  → full article
```

---

## Cloud Deployment

### Docker (recommended)

```bash
# Build and run
docker compose up --build -d

# Check health
curl http://localhost:8080/health

# View logs
docker compose logs -f

# Stop
docker compose down
```

### Manual (any VPS)

```bash
# On your cloud VM
git clone <your-repo>
cd medium-autopublisher-mcp
pip install -r requirements.txt
cp .env.example .env   # fill in API keys

# Run in SSE mode
python server.py --transport sse --port 8080
```

### Supported platforms

Works on any VPS or container platform:
- **DigitalOcean** Droplet / App Platform
- **AWS** EC2 / ECS / Fargate
- **Google Cloud** Cloud Run / Compute Engine
- **Railway**, **Fly.io**, **Render**

---

## Quality Gate

Every article passes through 6 automated checks before the image is generated:

| Check | Scoring |
|-------|---------|
| `word_count` | 1200–1800 words = 1.0 · outside = 0.5 |
| `has_h2_sections` | ≥4 sections = 1.0 · 2–3 = 0.6 · fewer = 0.2 |
| `has_sources` | ≥2 markdown links = 1.0 · 1 = 0.5 · 0 = 0.0 |
| `no_placeholders` | No `[INSERT`/`TODO`/`...`/`PLACEHOLDER` = 1.0 · any found = 0.0 |
| `title_quality` | 40–90 chars, not ALL-CAPS, ends in word char = 1.0 |
| `has_closing_cta` | Last 200 chars has `?` or CTA keyword = 1.0 · missing = 0.6 |

**Score < 0.70** → job fails with full report. **Score 0.70–0.80** → logged as WARNING, publish proceeds.

---

## Running Tests

```bash
pytest -v
```

All tests run with **zero real API calls** — respx intercepts every HTTP request at the network layer.

```
tests/test_calendar.py     — calendar generation, cadence dates, entry capping
tests/test_researcher.py   — deduplication, rate-limit handling, typed results
tests/test_writer.py       — JSON parsing, code fence stripping, error handling
tests/test_publisher.py    — local draft saving, file URI output
tests/test_scheduler.py    — fingerprint dedup, CRUD, retry counter, cancel logic
tests/test_validator.py    — all 6 checks, threshold gate, placeholder detection
```

---

## Idempotency

Two independent layers prevent duplicate publishes:

1. **Fingerprint check** (Layer 1): `SHA-256(topic + publish_at)[:16]` checked before every `schedule_article`. Same topic + same time = same fingerprint = existing job returned, no new job created.

2. **APScheduler `coalesce=True, max_instances=1`** (Layer 2): If the server crashes and restarts mid-window, only one execution fires even if two triggers accumulated.

---

## Rate Limits

| API | Calls per pipeline run | Notes |
|-----|----------------------|-------|
| Anthropic | 2 | Decompose + write — safe at tier 1 |
| OpenAI DALL-E | 1 | 5 images/min limit — safe |
| Tavily | 3 | 1000 req/month free tier — monitor monthly |

All external calls use **exponential-backoff retry** (3 attempts, 1s → 2s → 4s) and honour `Retry-After` headers on 429 responses.

---

## Environment Variables Reference

```bash
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
TAVILY_API_KEY=tvly-...
LOG_LEVEL=INFO                   # DEBUG | INFO | WARNING | ERROR
MAX_CONCURRENT_JOBS=3
SCHEDULER_DB_PATH=./scheduler.db  # APScheduler job store
JOBS_DB_PATH=./jobs.db            # Application metadata store
```

---

## License

MIT
