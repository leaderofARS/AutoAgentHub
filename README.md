# AutoAgentHub

A 24/7 cloud-ready multi-agent automation hub that connects **Telegram** and **WhatsApp** to **GitHub**, **Web News Search**, **Daily Job Discovery**, and **AI Agents** (Gemini API, Antigravity CLI `agy`, or any custom CLI agent) — plus a built-in **MCP Server**.

## Features

- **Telegram & WhatsApp Control:** Type any coding problem or command from your phone and get results back.
- **GitHub Repository Auditor:** Checks open issues/bugs, feature PRs, and documentation gaps, and can open documentation Pull Requests.
- **Web Search & Latest News:** Uses Tavily, Gemini Google Search Grounding, or Google News RSS to summarize live news on any topic.
- **Daily Job Finder & Approval Queue:** Searches remote job boards daily, drafts tailored application notes, and sends them to your chat for 1-click approval (`/apply <job_id>`).
- **MCP Server (`autoagenthub-mcp`):** Exposes GitHub, Telegram, WhatsApp, News, Jobs, and external agent tools to Antigravity or any MCP client.

## Quickstart

```bash
cd C:\Users\Asus\Documents\AutoAgentHub
copy .env.example .env
# Fill in your keys in .env
uv sync --extra dev
uv run pytest
uv run autoagenthub-server
```

See [STEP_BY_STEP_GUIDE.md](STEP_BY_STEP_GUIDE.md) for full setup, API key instructions, MCP connection, and 24/7 cloud deployment (so it runs even when your PC is shut down).
