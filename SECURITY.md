# Security Architecture

- **Authentication & Access Control:**
  - Telegram: `TELEGRAM_ALLOWED_CHAT_IDS` allowlist blocks unauthorized users.
  - Web Dashboard / OS API: Optional `DASHBOARD_API_TOKEN` Bearer authentication on `/api/os/*`.
  - GitHub Webhook: HMAC-SHA256 verification via `X-Hub-Signature-256` (`GITHUB_WEBHOOK_SECRET`).
- **Prompt Injection Defense:** External web, RSS, and repository content is wrapped with `<untrusted_data>` boundaries (`wrap_untrusted_content`) and never grants tool permissions.
- **Parameter-Bound Approval Gates:** Sensitive actions (e.g., job application prep, PR creation) bind an approval record to `SHA-256(action_type, target, canonical_params)` so modifying parameters invalidates approval.
- **Code Execution Isolation:** Repo edits run inside disposable per-task directories (`data/workspaces/<id>`) with `_safe_resolve` blocking path traversal (`..`).
- **Secret Redaction & Audit Logging:** `redact_secrets` scrubs `ghp_`, `AIza`, Telegram bot tokens, and Bearer tokens before writing `audit_logs`.
