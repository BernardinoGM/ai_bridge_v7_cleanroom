from __future__ import annotations

from dataclasses import dataclass
import re

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
TERMINAL_GREETING_INTAKE_MESSAGE = "Paste the bug, task, diff, stack trace, or repo question."
TERMINAL_IDENTITY_INTAKE_MESSAGE = "I'm your coding terminal. Paste the bug, task, diff, stack trace, or repo question."
TERMINAL_CAPABILITY_INTAKE_MESSAGE = "Yes. Tell me what you need built, fixed, reviewed, or explained."
TERMINAL_CODING_INTAKE_MESSAGE = (
    "Tell me what you need built, fixed, reviewed, or explained. Include the file, diff, language, or current error."
)
PUBLIC_INSTALLER_URL = "https://getaibridge.com/install.sh"


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


LOW_INFORMATION_TERMINAL_INPUTS = {
    "hello",
    "hi",
    "hey",
    "hello!",
    "hi!",
    "hey!",
    "what can you do",
    "what do you do",
    "help",
}

IDENTITY_META_INPUTS = {
    "who are you",
    "what is your name",
    "who am i talking to",
    "aibridge",
}

CAPABILITY_INPUTS = {
    "what can you do",
    "what do you do",
    "help",
    "can you code",
    "could you code",
}

VAGUE_CODING_INPUTS = {
    "code",
    "code for me",
    "build",
    "build something",
    "fix",
    "review diff",
    "write tests",
}

UNDERSPECIFIED_CODING_PHRASES = (
    "i want to code",
    "i wanna code",
    "want to code",
    "wanna code",
    "deliver code",
    "code for me",
    "review this",
    "review diff",
    "write code",
    "write a test",
    "write tests",
    "build a tiny game",
    "build something",
    "tiny game",
    "build it",
    "implement it",
    "fix it",
    "ship it",
)


def build_terminal_setup_commands(raw_key: str | None, settings: Settings) -> list[str]:
    key_line = (
        f'export AB_API_KEY="{raw_key}"'
        if raw_key
        else '# Generate a fresh AB key to get a copy-ready terminal setup block.'
    )
    return [
        f"curl -fsSL {PUBLIC_INSTALLER_URL} | bash",
        key_line,
        settings.terminal_cli_command,
    ]


def _normalize_prompt(prompt: str) -> str:
    return re.sub(r"\s+", " ", prompt.strip().lower())


def _is_option_reference(prompt: str) -> bool:
    normalized = _normalize_prompt(prompt)
    return bool(re.fullmatch(r"(option\s*)?\d+", normalized)) or normalized in {
        "i mean i will choose 1",
        "i'll choose 1",
        "i choose 1",
        "choose 1",
        "option 1",
    }


def _is_context_reference(prompt: str) -> bool:
    normalized = _normalize_prompt(prompt)
    return normalized in {
        "continue",
        "same file",
        "same bug",
        "same task",
        "same repo",
        "same error",
        "continue that",
        "continue this",
    }


def _is_underspecified_coding_intent(prompt: str, strategy: ExecutionStrategy) -> bool:
    normalized = _normalize_prompt(prompt)
    if normalized in LOW_INFORMATION_TERMINAL_INPUTS or normalized in IDENTITY_META_INPUTS:
        return False
    if normalized in CAPABILITY_INPUTS or normalized in VAGUE_CODING_INPUTS:
        return True
    if any(phrase in normalized for phrase in UNDERSPECIFIED_CODING_PHRASES):
        return True
    if any(term in normalized for term in ("build ", "implement ", "fix bug", "review diff", "write a test", "write tests", "patch ", "refactor ", "fix ", "code ")):
        return True
    if strategy.task_type not in {"coding", "mixed"}:
        return False
    if normalized.startswith(("need help with", "working on", "building ", "i need help with")) and len(normalized.split()) <= 8:
        return True
    if len(normalized.split()) <= 4 and not any(
        marker in normalized
        for marker in ("error", "traceback", ".py", ".ts", ".js", "repo", "diff", "stack", "file", "test", "bug")
    ):
        return True
    return False


def build_terminal_intake_reply(
    prompt: str,
    strategy: ExecutionStrategy,
    task_context: dict[str, str | None] | None = None,
) -> str | None:
    normalized = _normalize_prompt(prompt)
    summary = (task_context or {}).get("summary") or (task_context or {}).get("last_user_message") or ""
    if normalized in IDENTITY_META_INPUTS:
        return TERMINAL_IDENTITY_INTAKE_MESSAGE
    if normalized in CAPABILITY_INPUTS:
        return TERMINAL_CAPABILITY_INTAKE_MESSAGE
    if normalized in LOW_INFORMATION_TERMINAL_INPUTS:
        return TERMINAL_GREETING_INTAKE_MESSAGE
    if _is_option_reference(prompt):
        if summary:
            return f"Option noted for: {summary[:80]}. Paste the file, diff, stack trace, or exact task."
        return "If you're choosing an option, paste the bug, file, diff, or current error."
    if _is_context_reference(prompt):
        if summary:
            return f"Continuing: {summary[:80]}. Paste the file, diff, stack trace, or next coding step."
        return "Continue with the file, diff, stack trace, or exact coding step."
    if _is_underspecified_coding_intent(prompt, strategy):
        return TERMINAL_CODING_INTAKE_MESSAGE
    return None


def sanitize_terminal_reply(prompt: str, reply: str, strategy: ExecutionStrategy) -> str:
    normalized_prompt = _normalize_prompt(prompt)
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
    if normalized_prompt in IDENTITY_META_INPUTS:
        return TERMINAL_IDENTITY_INTAKE_MESSAGE
    if normalized_prompt in {"hello", "hi", "hey", "hello!", "hi!", "hey!"}:
        return TERMINAL_GREETING_INTAKE_MESSAGE
    if normalized_prompt in CAPABILITY_INPUTS:
        return TERMINAL_CAPABILITY_INTAKE_MESSAGE
    if normalized_prompt in VAGUE_CODING_INPUTS:
        return TERMINAL_CODING_INTAKE_MESSAGE
    if contains_cjk or any(marker in lowered for marker in blocked_markers):
        if strategy.task_type in {"coding", "mixed"}:
            return "Share the repo task, error, file, diff, or next step."
        return "Paste the task or repo question you want worked on."
    if len(reply.split()) > 80 and normalized_prompt in LOW_INFORMATION_TERMINAL_INPUTS:
        return TERMINAL_GREETING_INTAKE_MESSAGE
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
    task_context: dict[str, str | None] | None = None,
) -> TerminalExecutionResult:
    intake_reply = build_terminal_intake_reply(prompt, strategy, task_context)
    if intake_reply:
        return TerminalExecutionResult(
            content=intake_reply,
            mode="smart",
            request_id=request_id,
            provider_family="ab_orchestrator",
            fallback_used=False,
            usage={"prompt_tokens": 0, "completion_tokens": 0},
            telemetry={
                "premium_escalated": False,
                "quality_check_triggered": False,
                "local_model_hit": False,
            },
            strategy=strategy_summary(strategy),
        )
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
