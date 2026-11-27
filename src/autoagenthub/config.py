"""Application configuration loaded from environment variables or .env."""

from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed configuration for AutoAgentHub."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # AI / Agent backends
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    agent_backend: str = "auto"  # auto | agy | gemini | custom
    custom_agent_cmd: str = ""
    agy_skip_permissions: bool = True
    agy_timeout_seconds: int = 300

    # GitHub
    github_token: str = ""
    github_default_repo: str = ""
    github_webhook_secret: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: str = ""
    telegram_default_chat_id: str = ""

    # WhatsApp Cloud API
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = "autoagenthub_verify_123"
    whatsapp_default_recipient: str = ""

    # Web Search
    tavily_api_key: str = ""

    # Jobs & Schedules
    job_query: str = "software engineer python ai"
    job_location: str = "remote"
    job_user_name: str = "Applicant"
    job_user_bio: str = "Software engineer experienced in Python, TypeScript, and AI systems."
    daily_jobs_time: str = "09:00"
    daily_repo_check_time: str = "08:30"

    # Server & Storage
    port: int = 8000
    data_dir: Path = Path("data")

    # Concurrency, Queue & Resilience
    worker_concurrency: int = 4
    max_queue_size: int = 500
    task_max_retries: int = 3
    task_lease_seconds: int = 120
    circuit_breaker_threshold: int = 5
    circuit_breaker_cooldown_sec: float = 60.0
    rate_limit_per_min: int = 30

    @property
    def allowed_telegram_ids(self) -> set[int]:
        """Parse comma-separated Telegram chat IDs into a set of integers."""
        if not self.telegram_allowed_chat_ids.strip():
            return set()
        return {
            int(item.strip())
            for item in self.telegram_allowed_chat_ids.split(",")
            if item.strip().lstrip("-").isdigit()
        }


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
