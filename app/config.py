from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent
PLACEHOLDER_SECRETS = {"replace-me", "sk_test_replace", "whsec_replace", "pk_test_replace", "admin-dev-key"}


class Settings(BaseSettings):
    app_name: str = "AI Bridge"
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    base_url: str = "http://127.0.0.1:8000"
    database_url: str = Field(default=f"sqlite:///{BASE_DIR / 'data.db'}")
    terminal_cli_command: str = "aibridge"
    terminal_cli_alias: str = "ab"
    terminal_api_path: str = "/terminal/messages"

    secret_key: str = "replace-me"
    admin_api_key: str = "admin-dev-key"

    stripe_secret_key: str = "sk_test_replace"
    stripe_webhook_secret: str = "whsec_replace"
    stripe_publishable_key: str = "pk_test_replace"
    stripe_success_url: str = "http://127.0.0.1:8000/payments/success?session_id={CHECKOUT_SESSION_ID}"
    stripe_cancel_url: str = "http://127.0.0.1:8000/payments/cancel"

    provider_mock_enabled: bool = False
    provider_local_enabled: bool = False
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
    provider_premium_model: str = ""
    provider_premium_api_key: str | None = None

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

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def secure_cookies(self) -> bool:
        return self.app_env == "production"

    @property
    def payment_ready(self) -> bool:
        if self.app_env == "testing":
            return bool(self.stripe_secret_key and self.stripe_webhook_secret)
        values = (self.stripe_secret_key, self.stripe_webhook_secret, self.stripe_publishable_key)
        return all(value and value not in PLACEHOLDER_SECRETS for value in values)

    @property
    def session_ready(self) -> bool:
        return bool(self.secret_key and self.secret_key not in PLACEHOLDER_SECRETS)

    def provider_ready(self, provider_key: str) -> bool:
        mapping = {
            "remote_fast": (self.provider_fast_base_url, self.provider_fast_model, self.provider_fast_api_key),
            "remote_balanced": (self.provider_remote_base_url, self.provider_remote_model, self.provider_remote_api_key),
            "premium_anthropic": (self.provider_premium_base_url, self.provider_premium_model, self.provider_premium_api_key),
            "local_future": (
                self.provider_local_base_url,
                self.provider_local_model,
                self.provider_local_api_key,
            ),
        }
        if provider_key not in mapping:
            return False
        base_url, model, api_key = mapping[provider_key]
        if provider_key == "local_future" and not self.provider_local_enabled:
            return False
        return bool(base_url and model and api_key)

    def require_payment_ready(self) -> None:
        if not self.payment_ready:
            raise RuntimeError("Stripe checkout is not configured.")


@lru_cache
def get_settings() -> Settings:
    return Settings()
