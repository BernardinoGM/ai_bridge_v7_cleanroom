from app.config import Settings


def benchmark_cost_usd(prompt_tokens: int, completion_tokens: int, settings: Settings) -> float:
    prompt_cost = (prompt_tokens / 1_000_000) * settings.benchmark_input_cost_per_1m
    completion_cost = (completion_tokens / 1_000_000) * settings.benchmark_output_cost_per_1m
    return round(prompt_cost + completion_cost, 6)


def cost_zone(serving_cogs_usd: float, benchmark_usd: float, settings: Settings) -> str:
    if benchmark_usd <= 0:
        return "unknown"
    ratio = serving_cogs_usd / benchmark_usd
    if ratio <= settings.serving_target_ratio:
        return "target"
    if ratio <= settings.serving_healthy_ratio:
        return "healthy"
    if ratio <= settings.serving_redline_ratio:
        return "redline"
    return "breach"

