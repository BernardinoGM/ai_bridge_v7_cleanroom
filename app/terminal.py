from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.agents import ExecutionStrategy, RuntimeExecutionPlan, runtime_plan_for_strategy, strategy_summary
from app.benchmark import benchmark_cost_usd
from app.billing import debit_usage, wallet_balance
from app.config import Settings
from app.costing import estimate_serving_cost_usd
from app.models import UsageEvent
from app.pricing import estimate_public_charge
from app.providers.base import ProviderClient, ProviderResponse
from app.providers.real import ProviderExecutionError


TERMINAL_TEMPORARY_MESSAGE = "This workflow is temporarily unavailable. Please retry in a moment."


@dataclass(frozen=True)
class TerminalExecutionResult:
    content: str
    mode: str
    request_id: str
    provider_family: str
    fallback_used: bool
    usage: dict[str, int]
    telemetry: dict[str, bool]
    strategy: dict


def build_terminal_setup_commands(raw_key: str | None, settings: Settings) -> list[str]:
    key_line = (
        f'export AB_API_KEY="{raw_key}"'
        if raw_key
        else '# Generate a fresh AB key to get a copy-ready terminal setup block.'
    )
    return [
        'python3 -m venv ~/.aibridge',
        '~/.aibridge/bin/python -m pip install --upgrade pip setuptools wheel',
        '~/.aibridge/bin/pip install "git+https://github.com/BernardinoGM/ai_bridge_v7_cleanroom.git@main"',
        'export PATH="$HOME/.aibridge/bin:$PATH"',
        key_line,
        settings.terminal_cli_command,
    ]


def sanitize_terminal_reply(prompt: str, reply: str, strategy: ExecutionStrategy) -> str:
    normalized_prompt = prompt.strip().lower()
    lowered = reply.lower()
    blocked_markers = (
        "selected model",
        "does not exist",
        "may not have access",
        "handled in the",
        "fast lane",
        "smart lane",
        "assured lane",
        "premium reasoning",
        "quality checks are applied",
        "provider",
        "deepseek",
        "anthropic",
        "claude",
    )
    contains_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in reply)
    if normalized_prompt in {"hello", "hi", "hey", "hello!", "hi!", "hey!"}:
        return "Hello! How can I help you today?"
    if contains_cjk or any(marker in lowered for marker in blocked_markers):
        if strategy.task_type in {"coding", "mixed"}:
            return "I can help with that. Share the repo task, error, or next step."
        return "Hello! How can I help you today?"
    return reply


def _execute_with_fallback(
    execution_profile: str,
    fallback_execution_profile: str | None,
    registry: dict[str, ProviderClient],
    prompt: str,
    system: str | None,
) -> tuple[ProviderResponse, str, bool]:
    if execution_profile not in registry:
        raise HTTPException(status_code=503, detail=TERMINAL_TEMPORARY_MESSAGE)
    try:
        return registry[execution_profile].generate(prompt=prompt, system=system), execution_profile, False
    except ProviderExecutionError:
        if not fallback_execution_profile or fallback_execution_profile not in registry:
            raise HTTPException(status_code=503, detail=TERMINAL_TEMPORARY_MESSAGE)
        try:
            fallback_response = registry[fallback_execution_profile].generate(prompt=prompt, system=system)
            return fallback_response, fallback_execution_profile, True
        except ProviderExecutionError as exc:
            raise HTTPException(status_code=503, detail=TERMINAL_TEMPORARY_MESSAGE) from exc


def execute_terminal_strategy(
    *,
    user_id: int,
    strategy: ExecutionStrategy,
    system: str | None,
    prompt: str,
    db: Session,
    settings: Settings,
    endpoint: str,
    task_id: str | None,
    request_id: str,
    registry: dict[str, ProviderClient],
) -> TerminalExecutionResult:
    runtime_plan: RuntimeExecutionPlan = runtime_plan_for_strategy(strategy, "terminal")
    provider_response, execution_profile_used, fallback_used = _execute_with_fallback(
        runtime_plan.primary_execution_profile,
        runtime_plan.fallback_execution_profile,
        registry,
        prompt,
        system,
    )
    reply_text = sanitize_terminal_reply(prompt, provider_response.text, strategy)
    public_charge = estimate_public_charge(
        mode=runtime_plan.visible_mode,
        prompt_tokens=provider_response.prompt_tokens_est,
        completion_tokens=provider_response.completion_tokens_est,
        quality_check=runtime_plan.quality_check,
    )
    if wallet_balance(db, user_id, "main") < public_charge:
        raise HTTPException(status_code=402, detail="Insufficient main balance. Top up to continue.")
    cost_estimate = estimate_serving_cost_usd(
        provider_key=execution_profile_used,
        prompt_tokens=provider_response.prompt_tokens_est,
        completion_tokens=provider_response.completion_tokens_est,
        public_charge_usd=public_charge,
        quality_check=runtime_plan.quality_check,
        fallback_used=fallback_used,
        retry_count=provider_response.retry_count,
    )
    benchmark = benchmark_cost_usd(provider_response.prompt_tokens_est, provider_response.completion_tokens_est, settings)
    debit_usage(
        db,
        user_id=user_id,
        amount_usd=public_charge,
        request_id=request_id,
        mode=runtime_plan.visible_mode,
    )
    db.add(
        UsageEvent(
            user_id=user_id,
            task_id=task_id,
            request_id=request_id,
            endpoint=endpoint,
            mode=runtime_plan.visible_mode,
            public_charge_usd=public_charge,
            serving_cogs_usd=cost_estimate.serving_cogs_usd,
            benchmark_cost_usd=benchmark,
            gross_margin_guardrail_usd=cost_estimate.guardrail_usd,
            route_chosen=execution_profile_used,
            premium_escalated=runtime_plan.premium_escalated,
            local_model_hit=False,
            fallback_used=fallback_used,
            retry_count=provider_response.retry_count,
            quality_check_triggered=runtime_plan.quality_check,
            latency_ms=provider_response.latency_ms,
            prompt_tokens_est=provider_response.prompt_tokens_est,
            completion_tokens_est=provider_response.completion_tokens_est,
            request_payload={"prompt": prompt[:500], "mode": runtime_plan.visible_mode, "strategy": strategy_summary(strategy)},
            response_excerpt=reply_text[:200],
        )
    )
    db.commit()
    return TerminalExecutionResult(
        content=reply_text,
        mode=runtime_plan.visible_mode,
        request_id=request_id,
        provider_family=execution_profile_used,
        fallback_used=fallback_used,
        usage={
            "prompt_tokens": provider_response.prompt_tokens_est,
            "completion_tokens": provider_response.completion_tokens_est,
        },
        telemetry={
            "premium_escalated": runtime_plan.premium_escalated,
            "quality_check_triggered": runtime_plan.quality_check,
            "local_model_hit": False,
        },
        strategy=strategy_summary(strategy),
    )
