from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    app_name: str = "AI Bridge v7 Cleanroom"
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    base_url: str = "http://127.0.0.1:8000"
    database_url: str = Field(default=f"sqlite:///{BASE_DIR / 'data.db'}")
    secret_key: str = "replace-me"
    stripe_secret_key: str = "sk_test_replace"
    stripe_webhook_secret: str = "whsec_replace"
    stripe_publishable_key: str = "pk_test_replace"
    admin_api_key: str = "admin-dev-key"
    provider_mock_enabled: bool = False
    provider_local_enabled: bool = False
    stripe_success_url: str = "http://127.0.0.1:8000/payments/success?session_id={CHECKOUT_SESSION_ID}"
    stripe_cancel_url: str = "http://127.0.0.1:8000/payments/cancel"
    bill_guard_enabled: bool = True
    auto_reload_enabled: bool = False
    priority_queue_enabled: bool = True
    analytics_pro_enabled: bool = True
    custom_routing_enabled: bool = True
    team_vault_enabled: bool = True
    default_currency: str = "usd"
    benchmark_input_cost_per_1m: float = 15.0
    benchmark_output_cost_per_1m: float = 75.0
    serving_target_ratio: float = 0.10
    serving_healthy_ratio: float = 0.15
    serving_redline_ratio: float = 0.25
    provider_local_base_url: str = ""
    provider_local_model: str = ""
    provider_local_api_key: str | None = None
    provider_fast_base_url: str = "https://api.deepseek.com"
    provider_fast_model: str = "deepseek-chat"
    provider_fast_api_key: str | None = None
    provider_remote_base_url: str = "https://api.deepseek.com"
    provider_remote_model: str = "deepseek-chat"
    provider_remote_api_key: str | None = None
    provider_premium_base_url: str = "https://api.anthropic.com"
    provider_premium_model: str = "claude-sonnet"
    provider_premium_api_key: str | None = None
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        if self.app_env == "production":
            placeholders = {"replace-me", "sk_test_replace", "whsec_replace", "pk_test_replace", "admin-dev-key"}
            secret_fields = {
                "SECRET_KEY": self.secret_key,
                "STRIPE_SECRET_KEY": self.stripe_secret_key,
                "STRIPE_WEBHOOK_SECRET": self.stripe_webhook_secret,
                "STRIPE_PUBLISHABLE_KEY": self.stripe_publishable_key,
                "ADMIN_API_KEY": self.admin_api_key,
            }
            offending_fields = [name for name, value in secret_fields.items() if value in placeholders]
            if offending_fields:
                raise ValueError(
                    "Production configuration contains placeholder secrets in: "
                    + ", ".join(offending_fields)
                )
            required_non_placeholder = {
                "provider_fast_api_key": self.provider_fast_api_key,
                "provider_remote_api_key": self.provider_remote_api_key,
                "provider_premium_api_key": self.provider_premium_api_key,
                "provider_fast_base_url": self.provider_fast_base_url,
                "provider_remote_base_url": self.provider_remote_base_url,
                "provider_premium_base_url": self.provider_premium_base_url,
            }
            missing = [name for name, value in required_non_placeholder.items() if not value]
            if missing:
                raise ValueError(f"Production configuration missing required values: {', '.join(missing)}")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
