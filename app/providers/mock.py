from app.providers.base import ProviderClient, ProviderResponse


class MockProviderClient(ProviderClient):
    def __init__(self, name: str, speed_bias_ms: int) -> None:
        self.name = name
        self.speed_bias_ms = speed_bias_ms

    def generate(self, prompt: str, system: str | None = None) -> ProviderResponse:
        prompt_tokens = max(24, len(prompt) // 4)
        completion_tokens = max(48, min(220, len(prompt) // 3))
        system_prefix = f"{system.strip()} | " if system else ""
        lane_label = self.name.replace("_", " ").replace("lane", "lane").strip()
        text = (
            f"{system_prefix}Handled in the {lane_label} through AI Bridge. "
            "Quality checks are applied when needed, and premium reasoning is only invoked when the task requires it."
        )
        return ProviderResponse(
            text=text,
            latency_ms=self.speed_bias_ms + min(700, len(prompt) * 2),
            prompt_tokens_est=prompt_tokens,
            completion_tokens_est=completion_tokens,
            retry_count=0,
            fallback_used=False,
        )


def build_mock_clients() -> dict[str, ProviderClient]:
    return {
        "remote_fast": MockProviderClient(name="fast lane", speed_bias_ms=240),
        "remote_balanced": MockProviderClient(name="smart lane", speed_bias_ms=420),
        "premium_anthropic": MockProviderClient(name="assured lane", speed_bias_ms=860),
    }
