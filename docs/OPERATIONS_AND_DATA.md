# Operations, Data Model, Cost Management & Troubleshooting

- **Data Model (`db.py` & `task_store.py`):** Normalized SQLite WAL tables (`profiles`, `projects`, `experiments`, `memories`, `jobs`, `job_evaluations`, `applications`, `tech_items`, `watchlists`, `market_observations`, `geopolitical_events`, `approvals`, `events`, `audit_logs`, `model_usage`, `tasks`, `dead_letter_queue`).
- **Observability:** `GET /health`, `GET /metrics`, `GET /api/tasks`, `GET /api/dlq`, `GET /api/os/audit`, `GET /api/os/events`.
- **Cost Management (`model_gateway.py`):** Deterministic filtering & deduplication run *before* LLM calls; `model_usage` tracks estimated input/output tokens per call.
- **Backup & Recovery:** Back up `data/platform_os.db` and `data/autoagenthub.db` using `sqlite3 <db> ".backup backup.db"`.
- **Troubleshooting:** If Telegram stops responding after 15 minutes on Render Free, set `TELEGRAM_WEBHOOK_URL=https://<your-app>.onrender.com` in Render Environment Variables so Telegram wakes the container automatically.
