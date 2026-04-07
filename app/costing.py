from dataclasses import dataclass


@dataclass(frozen=True)
class ServingCostEstimate:
    serving_cogs_usd: float
    guardrail_usd: float


PROVIDER_COST_RATES = {
    "remote_fast": {"prompt_per_1k": 0.0028, "completion_per_1k": 0.0042, "base": 0.003},
    "remote_balanced": {"prompt_per_1k": 0.0038, "completion_per_1k": 0.0054, "base": 0.004},
    "premium_anthropic": {"prompt_per_1k": 0.011, "completion_per_1k": 0.024, "base": 0.008},
}


def estimate_serving_cost_usd(
    provider_key: str,
    prompt_tokens: int,
    completion_tokens: int,
    public_charge_usd: float,
    quality_check: bool,
    fallback_used: bool,
    retry_count: int,
) -> ServingCostEstimate:
    rates = PROVIDER_COST_RATES.get(provider_key, PROVIDER_COST_RATES["remote_balanced"])
    serving = (
        rates["base"]
        + (prompt_tokens / 1000.0) * rates["prompt_per_1k"]
        + (completion_tokens / 1000.0) * rates["completion_per_1k"]
    )
    if quality_check:
        serving += 0.006
    if fallback_used:
        serving += 0.009
    if retry_count:
        serving += retry_count * 0.004
    serving = round(serving, 4)
    guardrail = round(public_charge_usd - serving, 4)
    return ServingCostEstimate(serving_cogs_usd=serving, guardrail_usd=guardrail)
