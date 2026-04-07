from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    latency_ms: int
    prompt_tokens_est: int
    completion_tokens_est: int
    retry_count: int
    fallback_used: bool


class ProviderClient:
    name: str

    def generate(self, prompt: str, system: str | None = None) -> ProviderResponse:
        raise NotImplementedError

