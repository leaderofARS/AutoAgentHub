# AutoAgentHub — Step-by-Step Documentation

This guide documents everything created in `C:\Users\Asus\Documents\AutoAgentHub`, every credential you need to provide, how to connect the MCP servers, and how to deploy the app so it runs **24/7 even when your computer is shut down**.

---

## Step 1: What Was Built (File-by-File)

| File | Purpose |
| :--- | :--- |
| `.env.example` | Template for all API keys, tokens, repo settings, and daily schedules. |
| `pyproject.toml` | Python 3.11+ dependencies (`fastapi`, `python-telegram-bot`, `apscheduler`, `mcp`, `google-genai`, `httpx`, `pytest`). |
| `src/autoagenthub/config.py` | Typed `Settings` loader reading `.env` and creating `data/`. |
| `src/autoagenthub/agent_runner.py` | Unified runner supporting `gemini` (Gemini API), `agy` (Antigravity CLI `-p`), and `custom` CLI agents. |
| `src/autoagenthub/github_tools.py` | Inspects GitHub repos for bug issues, open PRs, missing `README.md`/`docs/`, and can open a documentation PR. |
| `src/autoagenthub/news_tools.py` | Searches latest news using Tavily, Gemini Search Grounding, or Google News RSS fallback. |
| `src/autoagenthub/jobs_tools.py` | Searches Remotive & Arbeitnow daily, deduplicates in `data/seen_jobs.json`, drafts tailored notes, and queues for approval in `data/pending_jobs.json`. |
| `src/autoagenthub/messaging_tools.py` | Outbound senders for Telegram Bot API and Meta WhatsApp Cloud API. |
| `src/autoagenthub/mcp_server.py` | FastMCP server (`autoagenthub-mcp`) exposing all GitHub, News, Jobs, Messaging, and Agent tools over MCP. |
| `src/autoagenthub/resilience.py` | Circuit Breaker, Token-Bucket Rate Limiter, and Exponential Backoff with jitter. |
| `src/autoagenthub/task_store.py` | Durable SQLite WAL task queue, atomic lease claiming, idempotency (`dedupe_key`), and Dead-Letter Queue. |
| `src/autoagenthub/orchestrator.py` | Async concurrent worker pool, backpressure, fan-out/fan-in `/digest` pipeline, and live metrics. |
| `src/autoagenthub/server.py` | FastAPI (`202 Accepted` async webhooks/API, `/metrics`, `/api/dlq`) + Telegram Bot + APScheduler. |
| `mcp_config.example.json` | Example MCP config to connect both `autoagenthub` and `@modelcontextprotocol/server-github`. |
| `Dockerfile` | Container definition for 24/7 cloud hosting. |
| `tests/test_core.py` | Automated unit tests for config, routing, and job approval queue. |

---

## Step 2: Gather Your Access Tokens

Copy `.env.example` to `.env` and fill in the services you want to use:

1. **Gemini API (`GEMINI_API_KEY`)**
   - Create a key in Google AI Studio.
   - Enables the default cloud AI agent, repo audits, tailored job pitches, and Google Search-grounded news.
2. **GitHub (`GITHUB_TOKEN` & `GITHUB_DEFAULT_REPO`)**
   - Create a GitHub Personal Access Token (fine-grained or classic with `repo` scope).
   - Set `GITHUB_DEFAULT_REPO=your-username/your-repo`.
3. **Telegram (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS`, `TELEGRAM_DEFAULT_CHAT_ID`)**
   - Open Telegram and message `@BotFather` -> `/newbot` -> copy the bot token.
   - Message `@userinfobot` on Telegram to get your numeric chat ID and put it in both `TELEGRAM_ALLOWED_CHAT_IDS` and `TELEGRAM_DEFAULT_CHAT_ID`.
4. **WhatsApp Cloud API (`WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_DEFAULT_RECIPIENT`)** *(Optional)*
   - Create a Meta Developer app -> add **WhatsApp** product -> copy the temporary/permanent access token and Phone Number ID.
   - Once deployed to a public HTTPS URL, set the Meta webhook callback URL to `https://<your-domain>/webhook/whatsapp` and verify token to match `WHATSAPP_VERIFY_TOKEN`.
5. **Tavily (`TAVILY_API_KEY`)** *(Optional)*
   - If omitted, news search automatically uses Gemini Google Search Grounding or Google News RSS.

---

## Step 3: Connect GitHub & Messaging MCP Servers

Because Antigravity's built-in Marketplace didn't have pre-packaged GitHub, Telegram, or WhatsApp plugins, `AutoAgentHub` includes its own **FastMCP server** (`autoagenthub-mcp`) plus an example config for the official GitHub MCP server:

1. Open `mcp_config.example.json`.
2. Replace the placeholder tokens (`GITHUB_TOKEN`, `TELEGRAM_BOT_TOKEN`, etc.).
3. Merge the `mcpServers` entries into your Antigravity MCP configuration (`~/.gemini/antigravity/mcp_config.json` or `~/.gemini/config/mcp_config.json`).
4. Restart/reload MCP servers in Antigravity. You will then have MCP tools for:
   - `inspect_github_repo`, `audit_github_repo`, `upload_github_doc_pr`
   - `search_latest_news`
   - `find_jobs`, `list_queued_jobs`, `approve_job`
   - `send_telegram`, `send_whatsapp`
   - `run_external_agent`

---

## Step 4: Test Locally on Windows

```powershell
cd C:\Users\Asus\Documents\AutoAgentHub
copy .env.example .env
# Edit .env with your keys
uv sync --extra dev
uv run pytest
uv run autoagenthub-server
```

Then message your Telegram bot:
- `/help` — see all commands
- `/repo` — audit your GitHub repo for bugs, PRs, and doc gaps
- `/code [owner/repo] <instruction>` — clone your repo, run **Antigravity (`agy -p`)** to edit code, push a branch, and open a Pull Request
- `/agy <prompt>` — run Antigravity CLI directly on any task
- `/news AI agents` — get latest web news
- `/jobs python developer` — fetch matching jobs with **Approve** buttons
- `/digest` — run GitHub + News + Jobs concurrently in parallel
- `/status` — inspect live worker pool, queue depth, and circuit breakers
- Or type any coding question in plain text.

### Trigger Antigravity Directly From GitHub
1. In your GitHub repo **Settings → Webhooks → Add webhook**:
   - **Payload URL:** `https://<your-domain>/webhook/github`
   - **Content type:** `application/json`
   - **Secret:** match `GITHUB_WEBHOOK_SECRET` in `.env`
   - **Events:** select **Issues** and **Issue comments**
2. Comment `@antigravity fix the login bug and add tests` on any Issue or PR — AutoAgentHub will clone the repo, run Antigravity (`agy`), push a new branch, open a Pull Request, and notify your Telegram/WhatsApp.

---

## Step 5: Deploy 24/7 So It Runs Even When Your PC Is Shut Down

To run continuously when your Windows computer is turned off:

### Option A: 1-Click Cloud Container (Render / Railway / Fly.io — Zero Server Maintenance)
1. Push this folder to a private GitHub repository (make sure `.env` is **not** committed — it is already in `.gitignore`).
2. On **Render**, use the included `render.yaml` Blueprint (or on **Railway / Fly.io**, deploy from the `Dockerfile`).
3. Add your `.env` variables (`GEMINI_API_KEY`, `GITHUB_TOKEN`, `TELEGRAM_BOT_TOKEN`, etc.) in the dashboard. Keep `AGENT_BACKEND=auto` so it automatically uses the built-in Gemini repo-editing agent inside the Docker container.
4. Set health check path to `/health`.
5. Telegram polling + `APScheduler` daily jobs + `/webhook/whatsapp` + `/webhook/github` will now run **24/7 even when your laptop is shut down**.

### Option B: Always-On Linux Cloud VM (Runs Both `agy` Antigravity CLI + Gemini 24/7)
1. Create a small Ubuntu VM (e.g. Google Cloud Compute Engine, AWS EC2, or DigitalOcean).
2. Clone your repo onto the VM, install `agy` if you want Antigravity CLI directly, and sign in to `agy` once interactively.
3. Run the included bootstrap script to install everything as a 24/7 `systemd` service:
   ```bash
   chmod +x deploy/setup-linux-vm.sh
   ./deploy/setup-linux-vm.sh
   ```
   Or run with Docker Compose: `docker compose up -d`.

---

## Note on Auto-Applying for Jobs
Many external job portals (LinkedIn, Workday, Greenhouse, etc.) use CAPTCHAs, login walls, or custom multi-page forms. AutoAgentHub **finds jobs daily, drafts tailored pitches, and queues them for your 1-click `/apply` approval** so inaccurate applications aren't submitted blindly and bot protections aren't violated.
