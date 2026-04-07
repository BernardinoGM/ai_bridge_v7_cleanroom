from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from app.config import Settings
from app.providers.base import ProviderClient, ProviderResponse


class ProviderExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderSpec:
    provider_key: str
    base_url: str
    model: str
    api_key: str | None = None


class OpenAICompatibleProviderClient(ProviderClient):
    def __init__(self, name: str, spec: ProviderSpec) -> None:
        self.name = name
        self.spec = spec

    def generate(self, prompt: str, system: str | None = None) -> ProviderResponse:
        started = time.perf_counter()
        headers = {"Content-Type": "application/json"}
        if self.spec.api_key:
            headers["Authorization"] = f"Bearer {self.spec.api_key}"
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": self.spec.model, "messages": messages, "stream": False}
        try:
            with httpx.Client(timeout=45.0) as client:
                response = client.post(f"{self.spec.base_url.rstrip('/')}/chat/completions", headers=headers, json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderExecutionError(f"{self.name} execution failed") from exc
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ProviderResponse(
            text=content,
            latency_ms=latency_ms,
            prompt_tokens_est=int(usage.get("prompt_tokens", max(24, len(prompt) // 4))),
            completion_tokens_est=int(usage.get("completion_tokens", max(48, min(220, len(content) // 3)))),
            retry_count=0,
            fallback_used=False,
        )


class AnthropicProviderClient(ProviderClient):
    def __init__(self, name: str, spec: ProviderSpec) -> None:
        self.name = name
        self.spec = spec

    def generate(self, prompt: str, system: str | None = None) -> ProviderResponse:
        started = time.perf_counter()
        headers = {
            "x-api-key": self.spec.api_key or "",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": self.spec.model,
            "max_tokens": 800,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system
        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(f"{self.spec.base_url.rstrip('/')}/v1/messages", headers=headers, json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderExecutionError(f"{self.name} execution failed") from exc
        data = response.json()
        text_chunks = [item.get("text", "") for item in data.get("content", []) if item.get("type") == "text"]
        content = "\n".join(chunk for chunk in text_chunks if chunk).strip()
        usage = data.get("usage", {})
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ProviderResponse(
            text=content or "Handled in the assured lane through AI Bridge.",
            latency_ms=latency_ms,
            prompt_tokens_est=int(usage.get("input_tokens", max(24, len(prompt) // 4))),
            completion_tokens_est=int(usage.get("output_tokens", max(48, min(220, len(content) // 3 if content else 120)))),
            retry_count=0,
            fallback_used=False,
        )


def build_provider_clients(settings: Settings) -> dict[str, ProviderClient]:
    clients: dict[str, ProviderClient] = {
        "remote_fast": OpenAICompatibleProviderClient(
            name="remote_fast",
            spec=ProviderSpec(
                provider_key="remote_fast",
                base_url=settings.provider_fast_base_url,
                model=settings.provider_fast_model,
                api_key=settings.provider_fast_api_key,
            ),
        ),
        "remote_balanced": OpenAICompatibleProviderClient(
            name="remote_balanced",
            spec=ProviderSpec(
                provider_key="remote_balanced",
                base_url=settings.provider_remote_base_url,
                model=settings.provider_remote_model,
                api_key=settings.provider_remote_api_key,
            ),
        ),
        "premium_anthropic": AnthropicProviderClient(
            name="premium_anthropic",
            spec=ProviderSpec(
                provider_key="premium_anthropic",
                base_url=settings.provider_premium_base_url,
                model=settings.provider_premium_model,
                api_key=settings.provider_premium_api_key,
            ),
        ),
    }
    if settings.provider_local_enabled and settings.provider_local_base_url and settings.provider_local_model:
        clients["local_future"] = OpenAICompatibleProviderClient(
            name="local_future",
            spec=ProviderSpec(
                provider_key="local_future",
                base_url=settings.provider_local_base_url,
                model=settings.provider_local_model,
                api_key=settings.provider_local_api_key,
            ),
        )
    return clients
