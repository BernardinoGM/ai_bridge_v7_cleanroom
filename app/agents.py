from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentProfile, User


ROLLING_WINDOW_DAYS = 7


def _now_ts() -> int:
    return int(datetime.utcnow().timestamp())


def _trim_window(events: list[int]) -> list[int]:
    cutoff = int((datetime.utcnow() - timedelta(days=ROLLING_WINDOW_DAYS)).timestamp())
    return [value for value in events if value >= cutoff]


def _append_event(hints: dict, key: str) -> None:
    values = list(hints.get(key, []))
    values.append(_now_ts())
    hints[key] = _trim_window(values)


def _derive_stack_hint(prompt: str) -> str:
    lowered = prompt.lower()
    if any(term in lowered for term in ["python", "pytest", "pip", "fastapi", "django"]):
        return "python"
    if any(term in lowered for term in ["typescript", "javascript", "node", "react", "next.js"]):
        return "typescript"
    if any(term in lowered for term in ["sql", "postgres", "mysql", "sqlite"]):
        return "sql"
    if any(term in lowered for term in ["go ", "golang"]):
        return "go"
    if any(term in lowered for term in ["rust", "cargo"]):
        return "rust"
    return "general"


def hydrate_profile_for_request(profile: AgentProfile, prompt: str, source_surface: str | None) -> dict:
    hints = dict(profile.learned_hints_json or {})
    bootstrapped = bool(hints.get("profile_bootstrapped_at"))
    lowered = prompt.lower()
    stack_hint = _derive_stack_hint(prompt)
    plan_first = any(term in lowered for term in ["plan", "outline", "strategy", "approach first"])
    explanation_dense = any(term in lowered for term in ["explain", "walk me through", "teach"])
    debug_heavy = any(term in lowered for term in ["debug", "trace", "error", "failing test", "stack trace"])
    coding_work = any(term in lowered for term in ["repo", "patch", "refactor", "test", "debug", "commit", "diff", "function", "class ", "cli"])

    if not bootstrapped:
        hints["profile_bootstrapped_at"] = _now_ts()
        hints["returning_session_count"] = 0
    else:
        hints["returning_session_count"] = int(hints.get("returning_session_count", 0)) + 1

    hints["profile_state"] = "returning" if bootstrapped else "new"
    hints["last_surface"] = source_surface or "terminal"
    hints["stack_hint"] = stack_hint
    hints["repo_type"] = "coding" if coding_work else "general"
    hints["language_preference"] = stack_hint
    hints["output_style"] = "concise"
    hints["patch_granularity"] = "surgical"
    hints["debug_preference"] = "reproduce_first" if debug_heavy else "balanced"
    hints["explanation_preference"] = "detailed" if explanation_dense else "concise"
    hints["testing_preference"] = "run_relevant_tests" if coding_work else "light_checks"
    hints["execution_bias"] = "plan_first" if plan_first else "execute_first"
    hints["stability_preference"] = "balanced"
    hints["last_prompt_excerpt"] = prompt[:255]
    profile.workload_pattern = "coding" if coding_work else "general"
    profile.pacing_context = "plan_first" if plan_first else "steady"
    profile.learned_hints_json = hints
    return {
        "user_state": hints["profile_state"],
        "stack_hint": stack_hint,
        "coding_work": coding_work,
        "execution_bias": hints["execution_bias"],
    }


def get_or_create_agent_profile(db: Session, user_id: int) -> AgentProfile:
    profile = db.scalar(select(AgentProfile).where(AgentProfile.user_id == user_id))
    if profile:
        return profile
    user = db.get(User, user_id)
    preferred_mode = user.preferred_mode if user else "smart"
    profile = AgentProfile(
        user_id=user_id,
        preferred_mode=preferred_mode,
        default_provider_family="ds",
        workload_pattern="general",
        escalation_sensitivity="balanced",
        qa_preference="adaptive",
        cost_guardrail_band="standard",
        stable_task_bias="enabled",
        pacing_context="steady",
        last_successful_provider="remote_balanced",
        recent_premium_trigger_count=0,
        recent_ds_success_rate=1.0,
        fallback_count=0,
        qa_trigger_count=0,
        fallback_count_7d=0,
        qa_trigger_rate_7d=0.0,
        stable_task_completion_rate_7d=1.0,
        ds_clean_success_count_7d=0,
        premium_escalation_count_7d=0,
        last_execution_profile="remote_balanced",
        learned_hints_json={
            "observed_high_risk_turns": 0,
            "stable_task_bias": "enabled",
            "fallback_count": 0,
            "qa_trigger_count": 0,
            "stable_task_count": 0,
            "ds_clean_success_count": 0,
            "ds_attempt_count": 0,
            "premium_events_7d": [],
            "fallback_events_7d": [],
            "qa_events_7d": [],
            "stable_task_events_7d": [],
            "task_events_7d": [],
            "ds_clean_success_events_7d": [],
            "ds_attempt_events_7d": [],
        },
    )
    db.add(profile)
    db.flush()
    return profile


def choose_initial_lane(profile: AgentProfile, requested_mode: str | None, prompt: str) -> tuple[str, str, bool]:
    text = prompt.lower()
    high_risk_terms = [
        "release",
        "production",
        "migration",
        "security",
        "legal",
        "incident",
        "billing",
        "auth",
        "payment",
        "data loss",
    ]
    verify_terms = ["verify", "recheck", "double-check", "double check", "correct", "fix again", "audit"]
    high_risk = any(term in text for term in high_risk_terms)
    medium_risk = any(term in text for term in ["architecture", "customer", "finance", "incident", "auth", "billing", "payment"])
    verify_language = any(term in text for term in verify_terms)
    coding_terms = [
        "code",
        "repo",
        "patch",
        "refactor",
        "test",
        "debug",
        "stack trace",
        "function",
        "class ",
        "python",
        "typescript",
        "javascript",
        "sql",
        "pytest",
        "terminal",
        "cli",
        "compile",
        "build",
        "commit",
        "diff",
        "bug",
    ]
    coding_work = any(term in text for term in coding_terms) or "```" in prompt
    mode = requested_mode or profile.preferred_mode or "smart"
    stable_user = (
        profile.stable_task_bias == "enabled"
        and profile.recent_ds_success_rate >= 0.8
        and profile.fallback_count_7d <= 2
        and profile.qa_trigger_rate_7d <= 0.35
    )
    if coding_work:
        if mode == "fast":
            return "fast", "fast", False
        if mode == "assured" or high_risk or verify_language:
            return "assured", "balanced", True
        return "smart", "balanced", False
    if mode == "assured" or high_risk:
        return "assured", "premium", True
    if mode == "fast":
        return "fast", "fast", False
    if profile.escalation_sensitivity == "conservative" and any(term in text for term in ["customer", "contract", "incident"]):
        return "assured", "premium", True
    if profile.default_provider_family == "premium" and medium_risk:
        return "assured", "premium", True
    if (profile.recent_ds_success_rate < 0.45 or profile.fallback_count_7d >= 3 or profile.premium_escalation_count_7d >= 3) and medium_risk:
        return "assured", "premium", True
    if verify_language and (profile.qa_trigger_rate_7d > 0.2 or profile.recent_ds_success_rate < 0.7):
        return "assured", "premium", True
    if stable_user:
        return "smart", "balanced", False
    return "smart", "balanced", profile.qa_preference == "strict" or profile.qa_trigger_rate_7d > 0.35 or verify_language


def update_profile_after_turn(
    profile: AgentProfile,
    task_id: str,
    visible_lane: str,
    quality_checked: bool,
    provider_family: str,
    execution_profile: str,
    premium_escalated: bool,
    fallback_used: bool,
    task_stable: bool,
    ds_succeeded_cleanly: bool,
) -> None:
    provider_family_label = (
        "premium" if provider_family == "premium_anthropic"
        else "ds_fast" if provider_family == "remote_fast"
        else "ds_balanced"
    )
    profile.last_task_id = task_id
    profile.preferred_mode = visible_lane
    profile.last_successful_provider = provider_family
    profile.last_execution_profile = execution_profile
    if provider_family_label == "premium":
        profile.default_provider_family = "premium" if profile.recent_premium_trigger_count >= 2 else profile.default_provider_family
    else:
        profile.default_provider_family = provider_family_label
    if premium_escalated:
        profile.recent_premium_trigger_count += 1
    if fallback_used:
        profile.fallback_count += 1
    if quality_checked:
        profile.qa_trigger_count += 1
    hints = dict(profile.learned_hints_json or {})
    hints["last_visible_lane"] = visible_lane
    hints["quality_checked_recently"] = quality_checked
    hints["stable_task_bias"] = profile.stable_task_bias
    hints["fallback_count"] = profile.fallback_count
    hints["qa_trigger_count"] = profile.qa_trigger_count
    hints["stable_task_count"] = int(hints.get("stable_task_count", 0)) + (1 if task_stable else 0)
    hints["ds_attempt_count"] = int(hints.get("ds_attempt_count", 0)) + (1 if provider_family_label != "premium" else 0)
    hints["ds_clean_success_count"] = int(hints.get("ds_clean_success_count", 0)) + (1 if ds_succeeded_cleanly else 0)
    _append_event(hints, "task_events_7d")
    if task_stable:
        _append_event(hints, "stable_task_events_7d")
    if premium_escalated:
        _append_event(hints, "premium_events_7d")
    if fallback_used:
        _append_event(hints, "fallback_events_7d")
    if quality_checked:
        _append_event(hints, "qa_events_7d")
    if provider_family_label != "premium":
        _append_event(hints, "ds_attempt_events_7d")
    if ds_succeeded_cleanly:
        _append_event(hints, "ds_clean_success_events_7d")
    ds_attempts = max(int(hints.get("ds_attempt_count", 0)), 1)
    ds_successes = int(hints.get("ds_clean_success_count", 0))
    profile.recent_ds_success_rate = round(ds_successes / ds_attempts, 3)
    ds_attempt_events = _trim_window(list(hints.get("ds_attempt_events_7d", [])))
    ds_clean_events = _trim_window(list(hints.get("ds_clean_success_events_7d", [])))
    task_events = _trim_window(list(hints.get("task_events_7d", [])))
    stable_task_events = _trim_window(list(hints.get("stable_task_events_7d", [])))
    premium_events = _trim_window(list(hints.get("premium_events_7d", [])))
    fallback_events = _trim_window(list(hints.get("fallback_events_7d", [])))
    qa_events = _trim_window(list(hints.get("qa_events_7d", [])))
    hints["ds_attempt_events_7d"] = ds_attempt_events
    hints["ds_clean_success_events_7d"] = ds_clean_events
    hints["task_events_7d"] = task_events
    hints["stable_task_events_7d"] = stable_task_events
    hints["premium_events_7d"] = premium_events
    hints["fallback_events_7d"] = fallback_events
    hints["qa_events_7d"] = qa_events
    profile.ds_clean_success_count_7d = len(ds_clean_events)
    profile.premium_escalation_count_7d = len(premium_events)
    profile.fallback_count_7d = len(fallback_events)
    profile.qa_trigger_rate_7d = round(len(qa_events) / max(len(task_events), 1), 3)
    profile.stable_task_completion_rate_7d = round(len(stable_task_events) / max(len(task_events), 1), 3)
    profile.recent_premium_trigger_count = profile.premium_escalation_count_7d
    profile.recent_ds_success_rate = round(len(ds_clean_events) / max(len(ds_attempt_events), 1), 3)
    if profile.recent_ds_success_rate >= 0.85 and profile.fallback_count_7d <= 1:
        profile.stable_task_bias = "enabled"
    elif profile.fallback_count_7d >= 3:
        profile.stable_task_bias = "guarded"
    profile.learned_hints_json = hints
